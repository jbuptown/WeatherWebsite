"""
Balloon trajectory prediction using NOAA GFS 1.0/0.5-degree GRIB2 data.

Data source  : NOAA GFS Open Data on AWS
File         : gfs.t{HH}z.pgrb2*.{GRID}.f{FFF}
Resolution   : 1.0 or 0.5 degrees
Levels       : isobaric pressure levels, 1-1000 hPa
Interpolation: linear latitude, longitude, altitude and time
Integration  : fourth-order Runge-Kutta, dt = 60 seconds
"""

import math
import os
import hashlib
import pickle
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests
import eccodes
from datetime import datetime, timedelta

from .elevation import get_elevation

EARTH_RADIUS  = 6_371_009.0
GFS_S3_BASE = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com'
GFS_GRIDS = {
    'approx': {
        'label': '1.0°',
        'file_suffix': 'pgrb2.1p00',
    },
    'full': {
        'label': '0.5°',
        'file_suffix': 'pgrb2full.0p50',
    },
}
GFS_CACHE_VERSION = 3
GFS_DOWNLOAD_WORKERS = 6
GFS_HISTORY_DAYS = 8
ECCODES_PARSE_LOCK = Lock()


def _gfs_cache_dir() -> Path:
    configured = os.environ.get("WEATHER_GFS_CACHE")
    path = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parent.parent / ".gfs_cache"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path

# Изобарические уровни в pgrb2full.0p50, подтверждённые реальными данными
GFS_LEVELS_HPA = [
    1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100,
    125, 150, 175, 200, 225, 250, 275, 300, 325, 350,
    375, 400, 425, 450, 475, 500, 525, 550, 575, 600,
    625, 650, 675, 700, 725, 750, 775, 800, 825, 850,
    875, 900, 925, 950, 975, 1000,
]


# ─────────────────────── Стандартная атмосфера ──────────────────────────────

def altitude_to_pressure(h: float) -> float:
    """ICAO ISA: altitude (m) → pressure (hPa)."""
    h = max(0.0, h)
    if h <= 11_000:
        T = 288.15 - 0.0065 * h
        return 1013.25 * (T / 288.15) ** 5.2561
    if h <= 20_000:
        return 226.32 * math.exp(-0.0001577 * (h - 11_000))
    if h <= 32_000:
        T = 216.65 + 0.001 * (h - 20_000)
        return 54.749 * (216.65 / T) ** 34.1626
    T = 228.65 + 0.0028 * (h - 32_000)
    return 8.6802 * (228.65 / T) ** 13.2


def pressure_to_altitude(p: float) -> float:
    """Inverse ISA: pressure (hPa) → altitude (m) via bisection."""
    lo, hi = 0.0, 50_000.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if altitude_to_pressure(mid) > p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _atmosphere_density(alt_m: float) -> float:
    """Return atmospheric density from the NASA atmosphere model, kg/m^3."""
    alt_m = max(0.0, alt_m)
    if alt_m > 25_000:
        temp = -131.21 + 0.00299 * alt_m
        pressure = 2.488 * ((temp + 273.1) / 216.6) ** -11.388
    elif alt_m > 11_000:
        temp = -56.46
        pressure = 22.65 * math.exp(1.73 - 0.000157 * alt_m)
    else:
        temp = 15.04 - 0.00649 * alt_m
        pressure = 101.29 * ((temp + 273.1) / 288.08) ** 5.256
    return pressure / (0.2869 * (temp + 273.1))


def _descent_rate_at_alt(sea_level_rate: float, alt_m: float) -> float:
    """
    Scale descent speed at altitude using atmospheric density.

    From drag-force balance (constant mass, constant drag coefficient):
        F_drag = ½ · Cd · A · ρ(h) · v(h)² = m·g  ⟹  v(h) = v_ref · √(ρ₀/ρ(h))

    At burst altitude (~30 km, ρ ≈ 0.018 kg/m³):
        v ≈ 5 × √(1.225/0.018) ≈ 41 m/s  →  balloon falls very fast initially.
    """
    drag_coefficient = sea_level_rate * 1.1045
    return drag_coefficient / math.sqrt(_atmosphere_density(alt_m))


# Заранее вычисляем высоту каждого уровня давления GFS для интерполяции
_LEVEL_ALTS = {lev: pressure_to_altitude(lev) for lev in GFS_LEVELS_HPA}
# ─────────────────────── Поиск прогона GFS ──────────────────────────────────

def _gfs_run_dt(launch_dt: datetime) -> datetime:
    """
    Select the GFS run appropriate for the requested launch time.

    Historical launches use the latest cycle at or before launch time. Current
    and future launches use the most recently published cycle that covers them.
    GFS runs every 6 h; files appear ~5 h after run time.
    """
    now = datetime.utcnow()
    oldest = now.replace(hour=0, minute=0, second=0, microsecond=0)
    oldest -= timedelta(days=GFS_HISTORY_DAYS)
    if launch_dt < oldest:
        raise ValueError(
            f"Приложение поддерживает расчёт GFS примерно за "
            f"{GFS_HISTORY_DAYS + 1} суток. Выберите дату не ранее "
            f"{oldest.strftime('%Y-%m-%d')}"
        )

    if launch_dt <= now - timedelta(hours=5):
        run_hour = (launch_dt.hour // 6) * 6
        return launch_dt.replace(
            hour=run_hour, minute=0, second=0, microsecond=0
        )

    for lag in range(0, 25, 6):
        candidate = now - timedelta(hours=lag + 5)
        rh = (candidate.hour // 6) * 6
        run = candidate.replace(hour=rh, minute=0, second=0, microsecond=0)
        fxx = (launch_dt - run).total_seconds() / 3600
        if 0 <= fxx <= 384:
            return run
    # Запасной вариант
    fb = now - timedelta(hours=12)
    rh = (fb.hour // 6) * 6
    return fb.replace(hour=rh, minute=0, second=0, microsecond=0)


def _fxx_list(run_dt: datetime, launch_dt: datetime,
               flight_hours: float) -> list:
    """3-hourly forecast hours needed to cover the flight."""
    start_h = int((launch_dt - run_dt).total_seconds() / 3600)
    start_h = (start_h // 3) * 3
    end_h   = start_h + int(math.ceil(flight_hours)) + 3
    return list(range(start_h, min(end_h + 1, 385), 3))


# ─────────────────────── Загрузка и разбор GRIB2 ────────────────────────────

def _aws_object_url(run_dt: datetime, fxx: int, gfs_mode: str) -> str:
    grid = GFS_GRIDS[gfs_mode]
    dat = run_dt.strftime('%Y%m%d')
    rh = f'{run_dt.hour:02d}'
    filename = f'gfs.t{rh}z.{grid["file_suffix"]}.f{fxx:03d}'
    return f'{GFS_S3_BASE}/gfs.{dat}/{rh}/atmos/{filename}'


def _inventory_is_needed(line: str) -> bool:
    parts = line.strip().split(':')
    if len(parts) < 5:
        return False
    variable, level_name = parts[3], parts[4]
    if variable == 'HGT' and level_name == 'surface':
        return True
    if variable not in ('UGRD', 'VGRD', 'HGT') or not level_name.endswith(' mb'):
        return False
    try:
        level = float(level_name[:-3])
    except ValueError:
        return False
    return level.is_integer() and int(level) in GFS_LEVELS_HPA


def _selected_grib_ranges(index_text: str) -> list[tuple[int, int]]:
    records = []
    for line in index_text.splitlines():
        parts = line.split(':', 2)
        if len(parts) < 3:
            continue
        try:
            records.append((int(parts[1]), line))
        except ValueError:
            continue

    selected = []
    for idx, (start, line) in enumerate(records):
        if not _inventory_is_needed(line):
            continue
        if idx + 1 >= len(records):
            raise RuntimeError('Selected GRIB message has no following index offset')
        selected.append((idx, start, records[idx + 1][0] - 1))

    if not selected:
        raise RuntimeError('No required UGRD/VGRD/HGT messages in GFS index')

    ranges = []
    prev_idx, start, end = selected[0]
    for idx, next_start, next_end in selected[1:]:
        if idx == prev_idx + 1:
            end = next_end
        else:
            ranges.append((start, end))
            start, end = next_start, next_end
        prev_idx = idx
    ranges.append((start, end))
    return ranges


def _fetch_grib2(run_dt: datetime, fxx: int,
                 lat_min: float, lat_max: float,
                 lon_min: float, lon_max: float,
                 gfs_mode: str = 'approx') -> dict:
    """
    Download selected messages from one AWS GFS forecast slice and parse them.

    Returns:
        {
          'lats': [float, ...],           # N→S (decreasing)
          'lons': [float, ...],           # W→E (increasing)
          'u':    {hPa: ndarray(nj,ni)},
          'v':    {hPa: ndarray(nj,ni)},
        }
    """
    url = _aws_object_url(run_dt, fxx, gfs_mode)

    cache_identity = (
        f'{GFS_CACHE_VERSION}:{url}:'
        f'{lat_min:.2f}:{lat_max:.2f}:{lon_min:.2f}:{lon_max:.2f}'
    )
    cache_key = hashlib.sha256(cache_identity.encode('utf-8')).hexdigest()
    cache_path = _gfs_cache_dir() / f"{cache_key}.pickle"
    if cache_path.exists():
        try:
            with cache_path.open("rb") as cached:
                return pickle.load(cached)
        except (OSError, EOFError, pickle.PickleError):
            cache_path.unlink(missing_ok=True)

    session = requests.Session()
    index_response = session.get(url + '.idx', timeout=30)
    if index_response.status_code != 200:
        raise RuntimeError(
            f'AWS GFS index f{fxx:03d}: HTTP {index_response.status_code}'
        )
    ranges = _selected_grib_ranges(index_response.text)

    fd, tmp = tempfile.mkstemp(suffix='.grib2')
    try:
        with os.fdopen(fd, 'wb') as f:
            for start, end in ranges:
                response = session.get(
                    url,
                    headers={'Range': f'bytes={start}-{end}'},
                    timeout=90,
                )
                if response.status_code != 206 or response.content[:4] != b'GRIB':
                    raise RuntimeError(
                        f'AWS GFS f{fxx:03d} range {start}-{end}: '
                        f'HTTP {response.status_code}, body={response.content[:80]}'
                    )
                f.write(response.content)
        # Парсер определений ecCodes не поддерживает безопасную работу из
        # нескольких потоков в Windows. Загрузки могут идти одновременно,
        # но декодирование GRIB выполняется последовательно.
        with ECCODES_PARSE_LOCK:
            result = _parse_grib2(
                tmp,
                lat_min=lat_min,
                lat_max=lat_max,
                lon_center=(lon_min + lon_max) / 2,
                lon_margin=(lon_max - lon_min) / 2,
            )
        cache_fd, cache_tmp = tempfile.mkstemp(
            suffix=".pickle", dir=_gfs_cache_dir()
        )
        try:
            with os.fdopen(cache_fd, "wb") as cached:
                pickle.dump(result, cached, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(cache_tmp, cache_path)
        finally:
            if os.path.exists(cache_tmp):
                os.unlink(cache_tmp)
        return result
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _grid_subset_indices(ni: int, nj: int,
                         lat1: float, lat2: float,
                         lon1: float, lon2: float,
                         lat_min: float, lat_max: float,
                         lon_center: float, lon_margin: float) -> tuple:
    dlat = (lat2 - lat1) / (nj - 1) if nj > 1 else 0.0
    dlon = (lon2 - lon1) / (ni - 1) if ni > 1 else 0.0
    full_lats = [lat1 + idx * dlat for idx in range(nj)]
    full_lons = [lon1 + idx * dlon for idx in range(ni)]

    lat_indices = [
        idx for idx, value in enumerate(full_lats)
        if lat_min <= value <= lat_max
    ]
    lon_candidates = []
    for idx, value in enumerate(full_lons):
        unwrapped = lon_center + ((value - lon_center + 180.0) % 360.0) - 180.0
        if abs(unwrapped - lon_center) <= lon_margin:
            lon_candidates.append((unwrapped, idx))
    lon_candidates.sort()

    if len(lat_indices) < 2 or len(lon_candidates) < 2:
        raise RuntimeError('Requested region is outside the downloaded GFS grid')

    lons = [value for value, _ in lon_candidates]
    lon_indices = [idx for _, idx in lon_candidates]
    lats = [full_lats[idx] for idx in lat_indices]
    return lats, lons, lat_indices, lon_indices


def _parse_grib2(path: str,
                 lat_min: float = -90.0, lat_max: float = 90.0,
                 lon_center: float = 0.0, lon_margin: float = 180.0) -> dict:
    """
    Parse GRIB2 file, extract isobaricInhPa U/V wind grids.
    Returns dict with lats, lons, u{hPa→2D}, v{hPa→2D}.
    """
    result = {
        'lats': None, 'lons': None,
        'u': {}, 'v': {}, 'hgt': {},
        'surface_hgt': None,
    }
    lat_indices = None
    lon_indices = None

    with open(path, 'rb') as f:
        while True:
            msg = eccodes.codes_grib_new_from_file(f)
            if msg is None:
                break
            try:
                level_type = eccodes.codes_get(msg, 'typeOfLevel')
                name = eccodes.codes_get(msg, 'shortName')
                is_surface_hgt = (
                    level_type == 'surface'
                    and name in ('gh', 'z', 'orog')
                )
                if not is_surface_hgt and (
                    level_type != 'isobaricInhPa'
                    or name not in ('u', 'v', 'gh', 'z')
                ):
                    continue
                level = eccodes.codes_get(msg, 'level')
                if not is_surface_hgt and level not in GFS_LEVELS_HPA:
                    continue

                ni    = eccodes.codes_get(msg, 'Ni')
                nj    = eccodes.codes_get(msg, 'Nj')
                lat1  = eccodes.codes_get(msg, 'latitudeOfFirstGridPointInDegrees')
                lat2  = eccodes.codes_get(msg, 'latitudeOfLastGridPointInDegrees')
                lon1  = eccodes.codes_get(msg, 'longitudeOfFirstGridPointInDegrees')
                lon2  = eccodes.codes_get(msg, 'longitudeOfLastGridPointInDegrees')
                vals  = eccodes.codes_get_values(msg).reshape(nj, ni)
                target = 'hgt' if name in ('gh', 'z') else name
                if name == 'z':
                    try:
                        units = eccodes.codes_get(msg, 'units')
                    except Exception:
                        units = ''
                    if 'm**2' in units or 's**-2' in units:
                        vals = vals / 9.80665

                if result['lats'] is None:
                    lats, lons, lat_indices, lon_indices = _grid_subset_indices(
                        ni, nj, lat1, lat2, lon1, lon2,
                        lat_min, lat_max, lon_center, lon_margin,
                    )
                    result['lats'] = lats
                    result['lons'] = lons

                vals = vals[lat_indices, :][:, lon_indices]

                if is_surface_hgt:
                    result['surface_hgt'] = vals
                else:
                    result[target][level] = vals
            finally:
                eccodes.codes_release(msg)

    if result['lats'] is None:
        raise RuntimeError('No isobaric U/V messages found in GRIB2 file')
    return result


# ─────────────────────── Трилинейная интерполяция ветра ─────────────────────

def _bilinear_grid(grid, lats, lons, lat, lon) -> float:
    """Bilinear spatial interpolation on a 2-D lat/lon grid."""
    # Индекс широты: значения могут убывать с севера на юг
    if len(lats) < 2:
        j, jf = 0, 0.0
    elif lats[0] > lats[-1]:              # N→S
        j = next((i for i in range(len(lats)-1)
                  if lats[i] >= lat >= lats[i+1]),
                 0 if lat >= lats[0] else len(lats)-2)
        jf = (lat - lats[j]) / (lats[j+1] - lats[j]) if lats[j+1] != lats[j] else 0.0
    else:                                  # S→N
        j = next((i for i in range(len(lats)-1)
                  if lats[i] <= lat <= lats[i+1]),
                 0 if lat <= lats[0] else len(lats)-2)
        jf = (lat - lats[j]) / (lats[j+1] - lats[j]) if lats[j+1] != lats[j] else 0.0

    # Индекс долготы: значения всегда возрастают с запада на восток
    if len(lons) < 2:
        i, lf = 0, 0.0
    else:
        i = next((k for k in range(len(lons)-1)
                  if lons[k] <= lon <= lons[k+1]),
                 0 if lon <= lons[0] else len(lons)-2)
        lf = (lon - lons[i]) / (lons[i+1] - lons[i]) if lons[i+1] != lons[i] else 0.0

    jf = max(0.0, min(1.0, jf))
    lf = max(0.0, min(1.0, lf))

    v00 = float(grid[j  ][i  ])
    v01 = float(grid[j  ][i+1])
    v10 = float(grid[j+1][i  ])
    v11 = float(grid[j+1][i+1])
    return (v00*(1-jf)*(1-lf) + v01*(1-jf)*lf +
            v10*   jf *(1-lf) + v11*   jf * lf)


# ─────────────────────── Набор данных GFS за несколько часов ────────────────

class GFSDataset:
    """Caches GFS GRIB2 grids and provides 4D wind interpolation."""

    def __init__(self, run_dt: datetime, fxx_list: list, grids: dict):
        self.run_dt   = run_dt
        self.fxx_list = sorted(fxx_list)
        self.grids    = grids           # {fxx: parsed_grid}

    def _time_bounds(self, dt_utc: datetime) -> tuple:
        hours = (dt_utc - self.run_dt).total_seconds() / 3600.0
        fxx0 = max((f for f in self.fxx_list if f <= hours), default=self.fxx_list[0])
        hi = [f for f in self.fxx_list if f > hours]
        if not hi:
            return self.fxx_list[-1], self.fxx_list[-1], 0.0
        fxx1 = hi[0]
        tf = (hours - fxx0) / (fxx1 - fxx0) if fxx1 != fxx0 else 0.0
        return fxx0, fxx1, max(0.0, min(1.0, tf))

    def _interp_level(self, fxx0: int, fxx1: int, tf: float,
                      level: int, variable: str,
                      lat: float, lon: float) -> float:
        g0 = self.grids[fxx0]
        a0 = g0[variable].get(level)
        if a0 is None and variable == 'hgt':
            v0 = _LEVEL_ALTS[level]
        else:
            v0 = _bilinear_grid(a0, g0['lats'], g0['lons'], lat, lon)

        if fxx0 == fxx1:
            return v0

        g1 = self.grids[fxx1]
        a1 = g1[variable].get(level)
        if a1 is None and variable == 'hgt':
            v1 = _LEVEL_ALTS[level]
        else:
            v1 = _bilinear_grid(a1, g1['lats'], g1['lons'], lat, lon)
        return v0 * (1 - tf) + v1 * tf

    def get_uv(self, lat: float, lon: float,
               dt_utc: datetime, alt_m: float) -> tuple:
        """Interpolate time, latitude and longitude before altitude."""
        fxx0, fxx1, tf = self._time_bounds(dt_utc)
        g0 = self.grids[fxx0]
        g1 = self.grids[fxx1]
        levels = sorted(set(g0['u']) & set(g0['v']) & set(g1['u']) & set(g1['v']))
        if not levels:
            return 0.0, 0.0

        heights = [
            (self._interp_level(fxx0, fxx1, tf, level, 'hgt', lat, lon), level)
            for level in levels
        ]
        heights.sort()
        if len(heights) == 1:
            level = heights[0][1]
            return (
                self._interp_level(fxx0, fxx1, tf, level, 'u', lat, lon),
                self._interp_level(fxx0, fxx1, tf, level, 'v', lat, lon),
            )

        idx = 0
        for k, (height, _) in enumerate(heights):
            if height <= alt_m:
                idx = k
        if idx >= len(heights) - 1:
            idx = len(heights) - 2

        lower_alt, lower_level = heights[idx]
        upper_alt, upper_level = heights[idx + 1]
        if upper_alt != lower_alt:
            lower_weight = (upper_alt - alt_m) / (upper_alt - lower_alt)
        else:
            lower_weight = 0.5

        u_lower = self._interp_level(fxx0, fxx1, tf, lower_level, 'u', lat, lon)
        u_upper = self._interp_level(fxx0, fxx1, tf, upper_level, 'u', lat, lon)
        v_lower = self._interp_level(fxx0, fxx1, tf, lower_level, 'v', lat, lon)
        v_upper = self._interp_level(fxx0, fxx1, tf, upper_level, 'v', lat, lon)

        return (
            u_lower * lower_weight + u_upper * (1 - lower_weight),
            v_lower * lower_weight + v_upper * (1 - lower_weight),
        )

    def get_ground_altitude(self, lat: float, lon: float,
                            dt_utc: datetime) -> float:
        """Use Ruaumoko-compatible DEM, falling back to GFS surface height."""
        try:
            return max(0.0, get_elevation(lat, lon))
        except Exception:
            pass

        fxx0, fxx1, tf = self._time_bounds(dt_utc)
        g0 = self.grids[fxx0]
        h0 = g0.get('surface_hgt')
        if h0 is None:
            return 0.0
        value0 = _bilinear_grid(h0, g0['lats'], g0['lons'], lat, lon)

        if fxx0 == fxx1:
            return max(0.0, value0)
        g1 = self.grids[fxx1]
        h1 = g1.get('surface_hgt')
        if h1 is None:
            return max(0.0, value0)
        value1 = _bilinear_grid(h1, g1['lats'], g1['lons'], lat, lon)
        return max(0.0, value0 * (1 - tf) + value1 * tf)


def build_gfs_dataset(lat: float, lon: float,
                       launch_dt: datetime,
                       flight_hours: float = 10.0,
                       margin: float = 5.0,
                       gfs_mode: str = 'approx') -> GFSDataset:
    """Download all required GFS forecast hours for the trajectory."""
    run_dt  = _gfs_run_dt(launch_dt)
    fxxs    = _fxx_list(run_dt, launch_dt, flight_hours)
    # Постоянные границы в целых градусах позволяют соседним точкам запуска
    # использовать один дисковый кэш.
    lat_min = math.floor(lat - margin)
    lat_max = math.ceil(lat + margin)
    lon_min = math.floor(lon - margin)
    lon_max = math.ceil(lon + margin)

    grids = {}
    workers = min(GFS_DOWNLOAD_WORKERS, len(fxxs))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _fetch_grib2, run_dt, fxx,
                lat_min, lat_max, lon_min, lon_max, gfs_mode,
            ): fxx
            for fxx in fxxs
        }
        for future in as_completed(futures):
            grids[futures[future]] = future.result()
    return GFSDataset(run_dt, fxxs, grids)


# ─────────────────────── Шаг метода РК4 ─────────────────────────────────────

def _rk4_step(ds: GFSDataset,
              lat: float, lon: float, alt: float,
              t: datetime, dt: float, vertical_rate):
    """Advance latitude, longitude and altitude by one coupled RK4 step."""

    def derivative(la, lo, al, ti):
        u, v = ds.get_uv(la, lo, ti, al)
        radius = EARTH_RADIUS + al
        cos_lat = math.cos(math.radians(la)) or 1e-10
        return (
            v / radius * (180 / math.pi),
            u / (radius * cos_lat) * (180 / math.pi),
            vertical_rate(al),
        )

    h2 = timedelta(seconds=dt / 2)
    h1 = timedelta(seconds=dt)
    k1 = derivative(lat, lon, alt, t)
    k2 = derivative(
        lat + k1[0] * dt / 2,
        lon + k1[1] * dt / 2,
        alt + k1[2] * dt / 2,
        t + h2,
    )
    k3 = derivative(
        lat + k2[0] * dt / 2,
        lon + k2[1] * dt / 2,
        alt + k2[2] * dt / 2,
        t + h2,
    )
    k4 = derivative(
        lat + k3[0] * dt,
        lon + k3[1] * dt,
        alt + k3[2] * dt,
        t + h1,
    )

    return (
        lat + dt * (k1[0] + 2*k2[0] + 2*k3[0] + k4[0]) / 6,
        lon + dt * (k1[1] + 2*k2[1] + 2*k3[1] + k4[1]) / 6,
        alt + dt * (k1[2] + 2*k2[2] + 2*k3[2] + k4[2]) / 6,
    )


# ─────────────────────── Основная точка входа ────────────────────────────────

def calculate_trajectory(lat: float, lon: float, alt: float,
                          launch_dt_utc: datetime,
                          ascent_rate: float,
                          float_altitude: float,
                          burst_altitude: float,
                          descent_rate: float,
                          profile: str = 'standard',
                          max_float_seconds: float = 172_800.0,
                          gfs_mode: str = 'approx') -> tuple:
    """
    Balloon trajectory using NOAA GFS GRIB2 wind data.

    The model uses isobaric pressure levels, four-dimensional linear
    wind interpolation, density-adjusted descent speed and RK4 integration
    with a 60-second timestep.
    """
    # Из-за влияния плотности спуск примерно втрое быстрее оценки с постоянной
    # скоростью. Берём запас, чтобы прогноз всегда охватывал нужное число часов.
    ascent_h  = burst_altitude / ascent_rate / 3600
    descent_h = burst_altitude / descent_rate / 3600 / 2.5   # avg density factor
    float_h   = max_float_seconds / 3600
    flight_h  = (float_h + 10 if profile == 'float_profile'
                 else ascent_h + descent_h + 2)

    ds = build_gfs_dataset(
        lat, lon, launch_dt_utc,
        flight_hours=flight_h,
        gfs_mode=gfs_mode,
    )

    DT = 60;  MAX_STEPS = 14_400
    cur_lat, cur_lon, cur_alt = lat, lon, alt
    cur_time = launch_dt_utc
    phase    = 'ascent';  float_elapsed = 0.0
    trajectory = []

    def format_time(value: datetime) -> str:
        stamp = value.isoformat(timespec='microseconds')
        if '.' in stamp:
            stamp = stamp.rstrip('0').rstrip('.')
        return stamp + 'Z'

    def append_point(point_phase: str):
        trajectory.append({
            'lat': round(cur_lat, 8),
            'lon': round(cur_lon, 8),
            'alt': round(cur_alt, 3),
            'time': format_time(cur_time),
            'phase': point_phase,
        })

    def termination_lerp(start_state, end_state, terminator):
        """Locate a phase boundary between two integration states."""
        left, right = 0.0, 1.0
        result = end_state
        while right - left > 0.01:
            mid = (left + right) / 2
            result = (
                start_state[0] + (end_state[0] - start_state[0]) * mid,
                start_state[1] + (end_state[1] - start_state[1]) * mid,
                start_state[2] + (end_state[2] - start_state[2]) * mid,
                start_state[3] + (end_state[3] - start_state[3]) * mid,
            )
            if terminator(result):
                right = mid
            else:
                left = mid
        return result

    for _ in range(MAX_STEPS):
        append_point(phase)

        if phase == 'ascent':
            target_alt = (float_altitude if profile == 'float_profile'
                          else burst_altitude)
            start_state = (cur_lat, cur_lon, cur_alt, cur_time)
            next_lat, next_lon, next_alt = _rk4_step(
                ds, cur_lat, cur_lon, cur_alt, cur_time, DT,
                lambda _height: ascent_rate,
            )
            end_state = (
                next_lat, next_lon, next_alt,
                cur_time + timedelta(seconds=DT),
            )

            if next_alt >= target_alt:
                cur_lat, cur_lon, cur_alt, cur_time = termination_lerp(
                    start_state, end_state,
                    lambda state: state[2] >= target_alt,
                )
                append_point('ascent')
                phase = 'float' if profile == 'float_profile' else 'descent'
            else:
                cur_lat, cur_lon, cur_alt, cur_time = end_state

        elif phase == 'float':
            remaining = max_float_seconds - float_elapsed
            step_dt = min(DT, remaining)
            cur_lat, cur_lon, cur_alt = _rk4_step(
                ds, cur_lat, cur_lon, cur_alt, cur_time, step_dt,
                lambda _height: 0.0,
            )
            cur_time += timedelta(seconds=step_dt)
            float_elapsed += step_dt
            if remaining <= DT:
                phase = 'descent'

        elif phase == 'descent':
            start_state = (cur_lat, cur_lon, cur_alt, cur_time)
            vertical_rate = lambda height: -_descent_rate_at_alt(
                descent_rate, height)
            next_lat, next_lon, next_alt = _rk4_step(
                ds, cur_lat, cur_lon, cur_alt, cur_time, DT, vertical_rate)
            end_state = (
                next_lat, next_lon, next_alt,
                cur_time + timedelta(seconds=DT),
            )
            ground_alt = ds.get_ground_altitude(
                next_lat, next_lon, end_state[3])

            if next_alt <= ground_alt:
                cur_lat, cur_lon, cur_alt, cur_time = termination_lerp(
                    start_state, end_state,
                    lambda state: (
                        ds.get_ground_altitude(
                            state[0], state[1], state[3]
                        ) > state[2]
                        or state[2] <= 0.0
                    ),
                )
                append_point('landed')
                break

            cur_lat, cur_lon, cur_alt, cur_time = end_state

    first_grid = next(iter(ds.grids.values()), {})
    levels_count = len(first_grid.get('u', {}))
    grid_label = GFS_GRIDS[gfs_mode]['label']
    file_suffix = GFS_GRIDS[gfs_mode]['file_suffix']
    info = {
        'source':    (f'NOAA GFS AWS {grid_label} {file_suffix} — RK4, variable descent '
                      f'(run {ds.run_dt.strftime("%Y-%m-%d %HZ")})'),
        'points':    len(trajectory),
        'api_calls': len(ds.grids),
        'gfs_mode':  gfs_mode,
        'grid':      grid_label,
        'levels':    levels_count,
    }
    return trajectory, info
