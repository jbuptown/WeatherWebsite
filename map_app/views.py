import json
from datetime import datetime

from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from .models import MapPoint
from .trajectory import calculate_trajectory

import django_rq

from .tasks import predict_trajectory_task


def _parse_float(value):
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace(",", ".")
    return float(value)

def _clean_predict_params(data):
    """Проверяет входные данные и возвращает словарь примитивов для фоновой задачи."""
    params = {
        'latitude':       _parse_float(data['latitude']),
        'longitude':      _parse_float(data['longitude']),
        'altitude':       _parse_float(data.get('altitude',       100.0)),
        'ascent_rate':    _parse_float(data.get('ascent_rate',    3.0)),
        'float_altitude': _parse_float(data.get('float_altitude', 5000.0)),
        'burst_altitude': _parse_float(data.get('burst_altitude', 30000.0)),
        'descent_rate':   _parse_float(data.get('descent_rate',   5.0)),
        'profile':        str(data.get('profile', 'standard')),
    }

    if 'max_float_seconds' in data:
        params['max_float_seconds'] = _parse_float(data['max_float_seconds'])
    else:
        params['max_float_seconds'] = _parse_float(data.get('max_float_hours', 48.0)) * 3600

    gfs_mode = str(data.get('gfs_mode', 'approx'))
    if gfs_mode == 'fast':
        gfs_mode = 'approx'
    if gfs_mode not in ('approx', 'full', 'icon'):
        raise ValueError('gfs_mode must be approx, full or icon')
    params['gfs_mode'] = gfs_mode

    if params['ascent_rate'] <= 0 or params['descent_rate'] <= 0:
        raise ValueError('Ascent and descent rates must be greater than zero')
    if params['max_float_seconds'] <= 0:
        raise ValueError('Float duration must be greater than zero')

    params['launch_date'] = data.get('launch_date', datetime.utcnow().strftime('%Y-%m-%d'))
    params['launch_time'] = data.get('launch_time', '00:00')

    datetime.strptime(f"{params['launch_date']} {params['launch_time']}", '%Y-%m-%d %H:%M')

    return params

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

@csrf_exempt
@require_http_methods(["POST"])
def predict_trajectory(request):
    """
    Calculate balloon trajectory using NOAA GFS data from AWS Open Data.

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
        if gfs_mode not in ('approx', 'full', 'icon'):
            raise ValueError('gfs_mode must be approx, full or icon')

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

@csrf_exempt
@require_http_methods(["POST"])
def predict_start(request):
    """Ставит расчёт траектории в очередь. Отвечает сразу, не дожидаясь результата."""
    try:
        params = _clean_predict_params(json.loads(request.body))
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return JsonResponse({'error': f'Invalid parameters: {e}'}, status=400)

    queue = django_rq.get_queue('default')
    job = queue.enqueue(
        predict_trajectory_task,
        params,
        job_timeout=900,
        result_ttl=3600,
        failure_ttl=3600,
    )

    return JsonResponse({'task_id': job.id, 'status': 'queued'}, status=202)

@require_http_methods(["GET"])
def predict_status(request, job_id):
    """Возвращает состояние задачи, а когда она готова — результат расчёта."""
    queue = django_rq.get_queue('default')
    job = queue.fetch_job(job_id)

    if job is None:
        return JsonResponse({'error': 'Task not found or expired'}, status=404)

    payload = {'task_id': job.id}

    if job.is_finished:
        payload['status'] = 'finished'
        payload.update(job.return_value())      # trajectory + info
    elif job.is_failed:
        payload['status'] = 'failed'
        payload['error'] = (job.exc_info or 'unknown error').strip().splitlines()[-1]
    elif job.is_started:
        payload['status'] = 'running'
    else:
        payload['status'] = 'queued'

    return JsonResponse(payload)