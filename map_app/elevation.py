import os
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

import requests
from PIL import Image


VIEWFINDER_URL = "http://www.viewfinderpanoramas.org/DEM/TIF15/15-{chunk}.zip"
BLOCK_ROWS = 10_800
BLOCK_COLS = 14_400
RESOLUTION = 240.0
_DOWNLOAD_LOCK = threading.Lock()

Image.MAX_IMAGE_PIXELS = None


def _cache_directory() -> Path:
    configured = os.environ.get("WEATHER_TERRAIN_CACHE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / ".terrain_cache"


def _cell(lat: float, lon: float) -> tuple[str, int, int]:
    if not -90 <= lat <= 90:
        raise ValueError(f"Invalid latitude: {lat}")

    row = round((90.0 - lat) * RESOLUTION)
    col = round(((lon + 180.0) % 360.0) * RESOLUTION)
    block_row = min(3, row // BLOCK_ROWS)
    block_col = min(5, col // BLOCK_COLS)
    chunk = chr(ord("A") + block_row * 6 + block_col)
    return chunk, row % BLOCK_ROWS, col % BLOCK_COLS


def _ensure_chunk(chunk: str) -> Path:
    cache = _cache_directory()
    cache.mkdir(parents=True, exist_ok=True)
    tif_path = cache / f"15-{chunk}.tif"
    if tif_path.exists():
        return tif_path

    with _DOWNLOAD_LOCK:
        if tif_path.exists():
            return tif_path

        archive_path = cache / f"15-{chunk}.zip.part"
        url = VIEWFINDER_URL.format(chunk=chunk)
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with archive_path.open("wb") as output:
                for piece in response.iter_content(chunk_size=1024 * 1024):
                    if piece:
                        output.write(piece)

        try:
            with zipfile.ZipFile(archive_path) as archive:
                member = next(
                    name for name in archive.namelist()
                    if name.upper().endswith(f"15-{chunk}.TIF")
                )
                temporary_tif = cache / f"15-{chunk}.tif.part"
                with archive.open(member) as source, temporary_tif.open("wb") as output:
                    while True:
                        piece = source.read(1024 * 1024)
                        if not piece:
                            break
                        output.write(piece)
                temporary_tif.replace(tif_path)
        finally:
            archive_path.unlink(missing_ok=True)

    return tif_path


@lru_cache(maxsize=6)
def _open_chunk(chunk: str):
    return Image.open(_ensure_chunk(chunk))


def get_elevation(lat: float, lon: float) -> float:
    """Return Ruaumoko-compatible nearest-cell terrain elevation in metres."""
    chunk, row, col = _cell(lat, lon)
    value = _open_chunk(chunk).getpixel((col, row))
    if isinstance(value, tuple):
        value = value[0]
    value = int(value)
    if value > 32767:
        value -= 65536
    return float(value)
