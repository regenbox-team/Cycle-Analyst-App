import os
import shutil
import sqlite3
import unittest


class MonitorUploadSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "monitor_upload_tmp", self._testMethodName))
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.tmp_dir, "monitor.db")
        os.environ["MONITOR_DB"] = self.db_path
        os.environ["MONITOR_USER"] = "admin"
        os.environ["MONITOR_PASS"] = "secret"
        os.environ["MONITOR_TERRAIN_ELEVATION_ENABLED"] = "0"

        import monitor_server.app as monitor_app

        self.monitor_app = monitor_app
        self.monitor_app._init_db()

    def tearDown(self):
        for key in (
            "MONITOR_DB",
            "MONITOR_USER",
            "MONITOR_PASS",
            "MONITOR_TERRAIN_ELEVATION_ENABLED",
        ):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def sample(self, index):
        return {
            "timestamp": f"2026-05-07 10:00:0{index}",
            "raw": f"0 52 1 10 {index}.0 20 0 0 0 0 0 0 0 0 flags",
            "user": "JD",
            "user_id": "JD",
            "gps_lat": 48.0 + index * 0.001,
            "gps_lon": 2.0,
            "gps_alt": 100 + index,
            "solar_enabled": 1,
        }

    def test_chunked_upload_finalizes_session_summary(self):
        base_payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
            "total_chunks": 2,
            "total_rows": 3,
        }
        first_chunk = [self.sample(0), self.sample(1)]
        second_chunk = [self.sample(2)]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.monitor_app._insert_telemetry_samples(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
                first_chunk,
                1,
            )
            self.monitor_app._insert_telemetry_samples(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
                second_chunk,
                1,
            )
            all_samples = self.monitor_app._telemetry_samples_for_session(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
            )
            summary = self.monitor_app._insert_uploaded_session_summary(conn, base_payload, all_samples)
            self.monitor_app._upsert_upload_device(conn, base_payload, 1, "127.0.0.1")
            conn.commit()

        self.assertEqual(summary["rows_count"], 3)

        with sqlite3.connect(self.db_path) as conn:
            session = conn.execute(
                """
                SELECT rows_count, distance_km
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                ("bike", "2026-05-07_10-00-00", "supercycle_live"),
            ).fetchone()
            sample_count = conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]

        self.assertEqual(session[0], 3)
        self.assertAlmostEqual(session[1], 2.0)
        self.assertEqual(sample_count, 3)


if __name__ == "__main__":
    unittest.main()
