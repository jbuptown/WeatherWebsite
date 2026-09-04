from datetime import datetime

from .trajectory import calculate_trajectory


def predict_trajectory_task(params):

    launch_dt = datetime.strptime(
        f"{params['launch_date']} {params['launch_time']}", '%Y-%m-%d %H:%M'
    )

    trajectory, info = calculate_trajectory(
        params['latitude'],
        params['longitude'],
        params['altitude'],
        launch_dt,
        params['ascent_rate'],
        params['float_altitude'],
        params['burst_altitude'],
        params['descent_rate'],
        params['profile'],
        params['max_float_seconds'],
        params['gfs_mode'],
    )

    return {'trajectory': trajectory, 'info': info}