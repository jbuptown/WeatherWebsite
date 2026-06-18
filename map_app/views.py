import json
from datetime import datetime

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import ensure_csrf_cookie

from .models import MapPoint
from .trajectory import calculate_trajectory


def _parse_float(value):
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    return float(value)


@ensure_csrf_cookie
def index(request):
    points = MapPoint.objects.all()
    points_data = [
        {
            'id': p.id,
            'name': p.name,
            'description': p.description,
            'latitude': p.latitude,
            'longitude': p.longitude,
        }
        for p in points
    ]
    return render(request, 'map_app/index.html', {'points_json': json.dumps(points_data)})


@require_http_methods(["POST"])
def add_point(request):
    try:
        data = json.loads(request.body)
        point = MapPoint.objects.create(
            name=data.get('name', 'Без названия'),
            description=data.get('description', ''),
            latitude=_parse_float(data['latitude']),
            longitude=_parse_float(data['longitude']),
        )
        return JsonResponse({
            'id': point.id,
            'name': point.name,
            'description': point.description,
            'latitude': point.latitude,
            'longitude': point.longitude,
        })
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(["DELETE"])
def delete_point(request, point_id):
    from django.shortcuts import get_object_or_404
    point = get_object_or_404(MapPoint, id=point_id)
    point.delete()
    return JsonResponse({'status': 'deleted'})


def get_points(request):
    points = MapPoint.objects.all()
    data = [
        {
            'id': p.id,
            'name': p.name,
            'description': p.description,
            'latitude': p.latitude,
            'longitude': p.longitude,
        }
        for p in points
    ]
    return JsonResponse(data, safe=False)


@require_http_methods(["POST"])
def predict_trajectory(request):
    """
    Calculate balloon trajectory using local NOAA GFS wind data.

    Expected JSON body:
    {
        "latitude":       55.75,
        "longitude":      37.62,
        "altitude":       100,        // launch altitude (m)
        "launch_date":    "2026-04-14",
        "launch_time":    "06:00",     // UTC
        "ascent_rate":    3.0,         // m/s
        "float_altitude": 5000,        // m (float target for float profile)
        "burst_altitude": 30000,       // m
        "descent_rate":   5.0,         // m/s
        "profile":        "standard",  // or "float_profile"
        "max_float_seconds": 172800,   // seconds
        "gfs_mode":       "approx"     // "approx" or "full"
    }
    """
    try:
        data = json.loads(request.body)

        lat           = _parse_float(data['latitude'])
        lon           = _parse_float(data['longitude'])
        alt           = _parse_float(data.get('altitude',       100.0))
        ascent_rate   = _parse_float(data.get('ascent_rate',    3.0))
        float_alt     = _parse_float(data.get('float_altitude', 5000.0))
        burst_alt     = _parse_float(data.get('burst_altitude', 30000.0))
        descent_rate     = _parse_float(data.get('descent_rate',     5.0))
        profile          = str(data.get('profile', 'standard'))
        if 'max_float_seconds' in data:
            max_float_seconds = _parse_float(data.get('max_float_seconds'))
        else:
            max_float_seconds = _parse_float(
                data.get('max_float_hours', 48.0)
            ) * 3600
        gfs_mode         = str(data.get('gfs_mode', 'approx'))
        if gfs_mode == 'fast':
            gfs_mode = 'approx'
        if ascent_rate <= 0 or descent_rate <= 0:
            raise ValueError('Ascent and descent rates must be greater than zero')
        if max_float_seconds <= 0:
            raise ValueError('Float duration must be greater than zero')
        if gfs_mode not in ('approx', 'full'):
            raise ValueError('gfs_mode must be approx or full')

        date_str = data.get('launch_date', datetime.utcnow().strftime('%Y-%m-%d'))
        time_str = data.get('launch_time', '00:00')
        launch_dt = datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')

        trajectory, info = calculate_trajectory(
            lat, lon, alt, launch_dt,
            ascent_rate, float_alt, burst_alt,
            descent_rate, profile, max_float_seconds, gfs_mode,
        )

        return JsonResponse({'trajectory': trajectory, 'info': info})

    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return JsonResponse({'error': f'Invalid parameters: {e}'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
