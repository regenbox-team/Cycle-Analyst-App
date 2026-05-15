import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from app import state
from app.routes import core


class DummyResponse:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self):
        return self.payload


class SessionTrackRouteTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.original_session_id = state.session_id
        self.original_get_db_file = core.get_db_file
        self.original_jsonify = core.jsonify
        self.original_request = core.request
        state.session_id = "ride-a"
        core.get_db_file = lambda mode=None: self.db_path
        core.jsonify = lambda payload: DummyResponse(payload)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    session TEXT,
                    gps_lat REAL,
                    gps_lon REAL,
                    gps_fix INTEGER,
                    solar_current_a REAL,
                    solar_bus_v REAL,
                    solar_power_w REAL,
                    solar_enabled INTEGER
                )
                """
            )

    def tearDown(self):
        state.session_id = self.original_session_id
        core.get_db_file = self.original_get_db_file
        core.jsonify = self.original_jsonify
        core.request = self.original_request
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _insert_rows(self, rows):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO logs (timestamp, session, gps_lat, gps_lon, gps_fix)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _payload(self, query="/session_track?samples=500"):
        args = {}
        if "?" in query:
            for part in query.split("?", 1)[1].split("&"):
                if not part:
                    continue
                key, _, value = part.partition("=")
                args[key] = value
        core.request = SimpleNamespace(args=args)
        return core.session_track().get_json()

    def test_returns_valid_points_for_current_session(self):
        self._insert_rows([
            ("t1", "ride-a", 48.0, 2.0, 1),
            ("t2", "ride-a", 48.1, 2.1, 1),
            ("t3", "ride-a", 48.2, 2.2, 0),
            ("t4", "ride-a", 48.1, 2.1, 1),
            ("t5", "ride-a", 91.0, 2.3, 1),
            ("t6", "other", 49.0, 3.0, 1),
        ])

        payload = self._payload()

        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            payload["points"],
            [
                {"timestamp": "t1", "lat": 48.0, "lon": 2.0},
                {"timestamp": "t2", "lat": 48.1, "lon": 2.1},
            ],
        )

    def test_samples_long_tracks(self):
        self._insert_rows(
            [
                (f"t{i}", "ride-a", 48.0 + i * 0.0001, 2.0 + i * 0.0001, 1)
                for i in range(150)
            ]
        )

        payload = self._payload("/session_track?samples=100")

        self.assertEqual(payload["sample_count"], 100)
        self.assertEqual(payload["count"], 100)

    def test_solar_session_profile_returns_time_of_day_power(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO logs (
                    timestamp, session, solar_current_a, solar_bus_v, solar_power_w, solar_enabled
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("2026-05-14 09:02:00", "ride-a", 2.0, 50.0, None, 1),
                    ("2026-05-14 09:04:00", "ride-a", None, None, 140.0, 1),
                    ("2026-05-14 12:00:00", "ride-a", None, None, 300.0, 1),
                    ("2026-05-14 12:01:00", "ride-a", None, None, 900.0, 0),
                    ("2026-05-14 13:00:00", "other", None, None, 500.0, 1),
                ],
            )

        core.request = SimpleNamespace(args={"bucket_minutes": "5", "samples": "50"})
        payload = core.solar_session_profile().get_json()

        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["bucket_minutes"], 5)
        self.assertEqual(payload["peak_w"], 150.0)
        self.assertEqual(payload["points"][0]["time"], "09:00")
        self.assertEqual(payload["points"][0]["power_w"], 120.0)
        self.assertEqual(payload["points"][1]["time"], "12:00")
        self.assertEqual(payload["points"][1]["power_w"], 150.0)


if __name__ == "__main__":
    unittest.main()
