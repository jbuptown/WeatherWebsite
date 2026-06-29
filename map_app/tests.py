import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase

from .views import _parse_float
from .trajectory import (
    _aws_object_url,
    _gfs_run_dt,
    _grid_subset_indices,
    _selected_grib_ranges,
)


class LocaleNumberTests(SimpleTestCase):
    def test_parse_float_accepts_dot_and_comma(self):
        self.assertEqual(_parse_float("0.1"), 0.1)
        self.assertEqual(_parse_float("0,1"), 0.1)

    @patch("map_app.views.calculate_trajectory")
    def test_prediction_uses_entered_coordinates_without_map_marker(self, calculate):
        calculate.return_value = (
            [{"lat": 55.75, "lon": 37.62, "alt": 100, "phase": "ascent"}],
            {"source": "test"},
        )

        response = self.client.post(
            "/api/predict/",
            data=json.dumps(
                {
                    "latitude": "55,75",
                    "longitude": "37.62",
                    "altitude": "100",
                    "launch_date": "2026-06-13",
                    "launch_time": "06:00",
                    "ascent_rate": "0,1",
                    "descent_rate": "5.0",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        args = calculate.call_args.args
        self.assertEqual(args[0], 55.75)
        self.assertEqual(args[1], 37.62)
        self.assertEqual(args[4], 0.1)


class HistoricalGfsTests(SimpleTestCase):
    @patch("map_app.trajectory.datetime")
    def test_historical_launch_uses_cycle_before_launch(self, mocked_datetime):
        mocked_datetime.utcnow.return_value = datetime(2026, 6, 13, 12)
        launch = datetime(2026, 6, 12, 14, 30)

        self.assertEqual(_gfs_run_dt(launch), datetime(2026, 6, 12, 12))

    @patch("map_app.trajectory.datetime")
    def test_dates_outside_gfs_history_are_rejected(self, mocked_datetime):
        mocked_datetime.utcnow.return_value = datetime(2026, 6, 13, 12)
        launch = datetime(2026, 6, 4, 11)

        with self.assertRaisesRegex(ValueError, "Выберите дату"):
            _gfs_run_dt(launch)


class AwsGfsTests(SimpleTestCase):
    def test_object_url_uses_selected_grid(self):
        run = datetime(2026, 6, 29, 6)

        self.assertEqual(
            _aws_object_url(run, 12, "approx"),
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            "gfs.20260629/06/atmos/gfs.t06z.pgrb2.1p00.f012",
        )
        self.assertTrue(
            _aws_object_url(run, 12, "full").endswith(
                "/gfs.t06z.pgrb2full.0p50.f012"
            )
        )

    def test_index_ranges_include_only_required_messages(self):
        index_text = "\n".join([
            "1:0:d=2026062900:TMP:1000 mb:anl:",
            "2:100:d=2026062900:HGT:1000 mb:anl:",
            "3:200:d=2026062900:UGRD:1000 mb:anl:",
            "4:300:d=2026062900:VGRD:1000 mb:anl:",
            "5:400:d=2026062900:HGT:0.01 mb:anl:",
            "6:500:d=2026062900:HGT:surface:anl:",
            "7:600:d=2026062900:TMP:surface:anl:",
        ])

        self.assertEqual(
            _selected_grib_ranges(index_text),
            [(100, 399), (500, 599)],
        )

    def test_global_grid_is_subset_and_longitude_is_unwrapped(self):
        lats, lons, lat_indices, lon_indices = _grid_subset_indices(
            ni=360,
            nj=181,
            lat1=90,
            lat2=-90,
            lon1=0,
            lon2=359,
            lat_min=54,
            lat_max=56,
            lon_center=-75,
            lon_margin=2,
        )

        self.assertEqual(lats, [56, 55, 54])
        self.assertEqual(lons, [-77, -76, -75, -74, -73])
        self.assertEqual(len(lat_indices), 3)
        self.assertEqual(len(lon_indices), 5)
