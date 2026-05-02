import importlib.util
import os
import shutil
import sqlite3
import sys
import types
import unittest


class MonitorDeleteSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "monitor_delete_tmp", self._testMethodName))
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.tmp_dir, "monitor.db")
        self.media_dir = os.path.join(self.tmp_dir, "media")
        os.environ["MONITOR_DB"] = self.db_path
        os.environ["MONITOR_MEDIA_DIR"] = self.media_dir
        os.environ["MONITOR_USER"] = "admin"
        os.environ["MONITOR_PASS"] = "secret"
        os.environ["MONITOR_TERRAIN_ELEVATION_ENABLED"] = "1"

        try:
            flask_available = importlib.util.find_spec("flask") is not None
        except ValueError:
            flask_available = False

        if not flask_available:
            class DummyApp:
                def __init__(self, *args, **kwargs):
                    self.jinja_loader = None

                def route(self, *args, **kwargs):
                    return lambda fn: fn

                def run(self, *args, **kwargs):
                    pass

            flask_stub = types.ModuleType("flask")
            flask_stub.Flask = DummyApp
            flask_stub.Response = object
            flask_stub.jsonify = lambda *args, **kwargs: None
            flask_stub.render_template = lambda *args, **kwargs: None
            flask_stub.request = None
            flask_stub.send_from_directory = lambda *args, **kwargs: None
            flask_stub.url_for = lambda *args, **kwargs: None
            sys.modules["flask"] = flask_stub
        elif "flask" in sys.modules:
            original_flask = sys.modules["flask"]
            try:
                has_jinja_loader = hasattr(original_flask.Flask(__name__), "jinja_loader")
            except Exception:
                has_jinja_loader = False
            if not has_jinja_loader:

                class DummyApp:
                    def __init__(self, *args, **kwargs):
                        self.jinja_loader = None

                    def route(self, *args, **kwargs):
                        return lambda fn: fn

                    def run(self, *args, **kwargs):
                        pass

                original_flask.Flask = DummyApp

        from monitor_server.app import (
            _delete_session_data,
            _delete_sessions_data,
            _init_db,
            _is_deleted_session,
            _migrate_db,
        )

        self.delete_session_data = _delete_session_data
        self.delete_sessions_data = _delete_sessions_data
        self.is_deleted_session = _is_deleted_session
        self.migrate_db = _migrate_db
        _init_db()

    def tearDown(self):
        for key in ("MONITOR_DB", "MONITOR_MEDIA_DIR", "MONITOR_USER", "MONITOR_PASS", "MONITOR_TERRAIN_ELEVATION_ENABLED"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def seed_session(self, device_id="bike", session_id="2026-05-01_10-00-00", mode="default"):
        relative_path = os.path.join("photos", device_id, session_id, "frame.jpg")
        absolute_path = os.path.join(self.media_dir, relative_path)
        os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
        with open(absolute_path, "wb") as fh:
            fh.write(b"jpg")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO devices (device_id, last_session, mode)
                VALUES (?, ?, ?)
                """,
                (device_id, session_id, mode),
            )
            conn.execute(
                """
                INSERT INTO sessions (device_id, session_id, mode, rows_count)
                VALUES (?, ?, ?, ?)
                """,
                (device_id, session_id, mode, 1),
            )
            conn.execute(
                """
                INSERT INTO telemetry_samples (device_id, session_id, mode, raw)
                VALUES (?, ?, ?, ?)
                """,
                (device_id, session_id, mode, "raw"),
            )
            conn.execute(
                """
                INSERT INTO photos (device_id, session_id, mode, relative_path)
                VALUES (?, ?, ?, ?)
                """,
                (device_id, session_id, mode, relative_path),
            )
            conn.commit()
        return absolute_path

    def test_delete_session_removes_related_rows_and_media_file(self):
        absolute_path = self.seed_session()
        payload = self.delete_session_data(
            "bike",
            "2026-05-01_10-00-00",
            "default",
        )

        self.assertEqual(payload["deleted_sessions"], 1)
        self.assertEqual(payload["deleted_samples"], 1)
        self.assertEqual(payload["deleted_photos"], 1)
        self.assertEqual(payload["deleted_files"], 1)
        self.assertFalse(os.path.exists(absolute_path))

        with sqlite3.connect(self.db_path) as conn:
            for table in ("sessions", "telemetry_samples", "photos"):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0)
            device = conn.execute("SELECT last_session FROM devices WHERE device_id = ?", ("bike",)).fetchone()
            tombstone_count = conn.execute("SELECT COUNT(*) FROM deleted_sessions").fetchone()[0]
            self.assertTrue(self.is_deleted_session(conn, "bike", "2026-05-01_10-00-00", "default"))
        self.assertIsNone(device[0])
        self.assertEqual(tombstone_count, 1)

    def test_delete_sessions_removes_multiple_selected_sessions(self):
        first_path = self.seed_session(session_id="2026-05-01_10-00-00")
        second_path = self.seed_session(session_id="2026-05-01_11-00-00")

        payload = self.delete_sessions_data(
            [
                {"device_id": "bike", "session_id": "2026-05-01_10-00-00", "mode": "default"},
                {"device_id": "bike", "session_id": "2026-05-01_11-00-00", "mode": "default"},
            ]
        )

        self.assertEqual(payload["deleted_count"], 2)
        self.assertEqual(payload["deleted_sessions"], 2)
        self.assertEqual(payload["deleted_samples"], 2)
        self.assertEqual(payload["deleted_photos"], 2)
        self.assertEqual(payload["deleted_files"], 2)
        self.assertFalse(os.path.exists(first_path))
        self.assertFalse(os.path.exists(second_path))

        with sqlite3.connect(self.db_path) as conn:
            for table in ("sessions", "telemetry_samples", "photos"):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0)
            tombstone_count = conn.execute("SELECT COUNT(*) FROM deleted_sessions").fetchone()[0]
            self.assertTrue(self.is_deleted_session(conn, "bike", "2026-05-01_10-00-00", "default"))
            self.assertTrue(self.is_deleted_session(conn, "bike", "2026-05-01_11-00-00", "default"))
        self.assertEqual(tombstone_count, 2)

    def test_migration_recomputes_session_distance_from_ca_delta(self):
        raw_start = "0 52 1 10 124.75 20 0 0 0 0 0 0 0 0 flags"
        raw_end = "0 52 1 10 145.77 20 0 0 0 0 0 0 0 0 flags"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    device_id, session_id, mode, start_ts, end_ts,
                    rows_count, distance_km, duration_sec, avg_speed_kph, uphill_m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bike",
                    "2026-05-02_10-00-00",
                    "default",
                    "2026-05-02 10:00:00",
                    "2026-05-02 12:00:00",
                    2,
                    145.77,
                    7200,
                    72.885,
                    1035.0,
                ),
            )
            conn.execute(
                """
                INSERT INTO telemetry_samples (device_id, session_id, mode, timestamp, raw, gps_lat, gps_lon, gps_alt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("bike", "2026-05-02_10-00-00", "default", "2026-05-02 10:00:00", raw_start, 48.0, 2.0, 100.0),
            )
            conn.execute(
                """
                INSERT INTO telemetry_samples (device_id, session_id, mode, timestamp, raw, gps_lat, gps_lon, gps_alt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("bike", "2026-05-02_10-00-00", "default", "2026-05-02 12:00:00", raw_end, 48.1, 2.0, 106.0),
            )
            conn.commit()

        self.migrate_db()

        with sqlite3.connect(self.db_path) as conn:
            distance_km, avg_speed_kph, uphill_m, raw_gps_uphill_m = conn.execute(
                """
                SELECT distance_km, avg_speed_kph, uphill_m, raw_gps_uphill_m
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                ("bike", "2026-05-02_10-00-00", "default"),
            ).fetchone()
        self.assertAlmostEqual(distance_km, 21.02, places=2)
        self.assertAlmostEqual(avg_speed_kph, 10.51, places=2)
        self.assertAlmostEqual(uphill_m, 6.0, places=2)
        self.assertAlmostEqual(raw_gps_uphill_m, 6.0, places=2)

    def test_fetches_and_caches_terrain_altitude_for_samples(self):
        import monitor_server.app as monitor_app

        original_fetch = monitor_app._fetch_terrain_altitudes

        def fake_fetch(points, allow_fallback=True):
            return {
                monitor_app._terrain_cache_key(lat, lon): {
                    "terrain_alt_m": 100.0 + index * 6.0,
                    "source": "test_dem",
                }
                for index, (lat, lon) in enumerate(points)
            }

        monitor_app._fetch_terrain_altitudes = fake_fetch
        try:
            samples = [
                {
                    "timestamp": f"2026-05-02 10:00:0{index}",
                    "raw": f"0 52 1 10 {index}.0 20 0 0 0 0 0 0 0 0 flags",
                    "gps_lat": 48.0 + index * 0.001,
                    "gps_lon": 2.0,
                    "gps_alt": 500.0 - index * 100.0,
                }
                for index in range(3)
            ]
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                enriched = monitor_app._enrich_samples_with_terrain_altitude(conn, samples)
                uphill_m = monitor_app._compute_session_uphill_m(samples)
                raw_gps_uphill_m = monitor_app._compute_raw_gps_uphill_m(samples)
                cache_count = conn.execute("SELECT COUNT(*) FROM terrain_elevation_cache").fetchone()[0]
        finally:
            monitor_app._fetch_terrain_altitudes = original_fetch

        terrain_values = [sample["terrain_alt_m"] for sample in samples]
        source_values = [sample["terrain_alt_source"] for sample in samples]
        self.assertEqual(enriched, 3)
        self.assertEqual(terrain_values, [100.0, 106.0, 112.0])
        self.assertEqual(source_values, ["test_dem", "test_dem", "test_dem"])
        self.assertAlmostEqual(uphill_m, 12.0)
        self.assertAlmostEqual(raw_gps_uphill_m, 0.0)
        self.assertEqual(cache_count, 3)

    def test_falls_back_to_opentopodata_when_ign_has_no_coverage(self):
        import monitor_server.app as monitor_app

        original_ign = monitor_app._fetch_ign_terrain_altitudes
        original_fallback = monitor_app._fetch_opentopodata_altitudes

        def fake_ign(points):
            lat, lon = points[0]
            return {
                monitor_app._terrain_cache_key(lat, lon): {
                    "terrain_alt_m": 42.0,
                    "source": "ign_rge_alti_wld",
                }
            }

        def fake_fallback(points):
            return {
                monitor_app._terrain_cache_key(lat, lon): {
                    "terrain_alt_m": 120.0 + index,
                    "source": "opentopodata:srtm30m",
                }
                for index, (lat, lon) in enumerate(points)
            }

        monitor_app._fetch_ign_terrain_altitudes = fake_ign
        monitor_app._fetch_opentopodata_altitudes = fake_fallback
        try:
            points = [(48.0, 2.0), (31.63, -7.99)]
            values = monitor_app._fetch_terrain_altitudes(points)
        finally:
            monitor_app._fetch_ign_terrain_altitudes = original_ign
            monitor_app._fetch_opentopodata_altitudes = original_fallback

        self.assertEqual(values[monitor_app._terrain_cache_key(48.0, 2.0)]["source"], "ign_rge_alti_wld")
        self.assertEqual(values[monitor_app._terrain_cache_key(31.63, -7.99)]["source"], "opentopodata:srtm30m")
        self.assertAlmostEqual(values[monitor_app._terrain_cache_key(31.63, -7.99)]["terrain_alt_m"], 120.0)

    def test_backfill_limits_unique_points_per_request(self):
        import monitor_server.app as monitor_app

        original_fetch = monitor_app._fetch_terrain_altitudes

        def fake_fetch(points, allow_fallback=True):
            return {
                monitor_app._terrain_cache_key(lat, lon): {
                    "terrain_alt_m": 200.0 + index,
                    "source": "test_dem",
                }
                for index, (lat, lon) in enumerate(points)
            }

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (device_id, session_id, mode, start_ts)
                VALUES (?, ?, ?, ?)
                """,
                ("bike", "limited", "default", "2026-05-02 10:00:00"),
            )
            for index in range(5):
                conn.execute(
                    """
                    INSERT INTO telemetry_samples (device_id, session_id, mode, timestamp, gps_lat, gps_lon, gps_alt)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("bike", "limited", "default", f"2026-05-02 10:00:0{index}", 31.0 + index * 0.001, -8.0, 100 + index),
                )
            conn.commit()

        monitor_app._fetch_terrain_altitudes = fake_fetch
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                first = monitor_app._backfill_terrain_altitudes(conn, limit_points=2)
                conn.commit()
                second = monitor_app._backfill_terrain_altitudes(conn, limit_points=10)
                conn.commit()
                filled = conn.execute(
                    "SELECT COUNT(*) FROM telemetry_samples WHERE session_id = ? AND terrain_alt_m IS NOT NULL",
                    ("limited",),
                ).fetchone()[0]
        finally:
            monitor_app._fetch_terrain_altitudes = original_fetch

        self.assertTrue(first["limited"])
        self.assertEqual(first["points_requested"], 2)
        self.assertEqual(first["samples_updated"], 2)
        self.assertEqual(second["samples_updated"], 3)
        self.assertEqual(filled, 5)


if __name__ == "__main__":
    unittest.main()
