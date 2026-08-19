import json
import os
import shutil
import sqlite3
import unittest


class MonitorUserTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "monitor_user_timeline_tmp", self._testMethodName))
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
        self.headers = {"Authorization": "Basic YWRtaW46c2VjcmV0"}

    def tearDown(self):
        for key in ("MONITOR_DB", "MONITOR_USER", "MONITOR_PASS", "MONITOR_TERRAIN_ELEVATION_ENABLED"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _prepare_session(self):
        samples = []
        for index in range(6):
            samples.append({
                "timestamp": f"2026-08-12 10:00:0{index}",
                "raw": f"0 52 1 18 {index}.0 20 0 0 0 0 0 0 0 0 flags",
                "user": "AA" if index < 4 else "BB",
                "user_id": "alice" if index < 4 else "bob",
                "user_initials": "AA" if index < 4 else "BB",
                "gps_lat": 48.0 + index * 0.0001,
                "gps_lon": 2.0,
            })
        with self.monitor_app._get_db() as conn:
            for user_id, initials in (("alice", "AA"), ("bob", "BB")):
                conn.execute(
                    "INSERT INTO users (user_id, initials, active) VALUES (?, ?, 1)",
                    (user_id, initials),
                )
            self.monitor_app._insert_telemetry_samples(conn, "bike", "ride", "default", samples, 1)
            stored = self.monitor_app._telemetry_samples_for_session(conn, "bike", "ride", "default")
            self.monitor_app._insert_uploaded_session_summary(
                conn,
                {"device_id": "bike", "session_id": "ride", "mode": "default"},
                stored,
            )
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM telemetry_samples ORDER BY timestamp, id"
                ).fetchall()
            ]
            conn.commit()
        return ids

    def test_moves_user_change_and_refreshes_session_summary(self):
        ids = self._prepare_session()
        response = self.monitor_app.app.test_client().post(
            "/api/session/user_timeline",
            headers=self.headers,
            json={
                "device_id": "bike",
                "session_id": "ride",
                "mode": "default",
                "segments": [
                    {"start_sample_id": ids[0], "identity_key": "user:alice"},
                    {"start_sample_id": ids[2], "identity_key": "user:bob"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as conn:
            assignments = conn.execute(
                "SELECT user_id, user_initials FROM telemetry_samples ORDER BY timestamp, id"
            ).fetchall()
            session = conn.execute(
                "SELECT user_ids_json, metrics_json FROM sessions WHERE session_id = 'ride'"
            ).fetchone()
        self.assertEqual(assignments[:2], [("alice", "AA"), ("alice", "AA")])
        self.assertEqual(assignments[2:], [("bob", "BB")] * 4)
        self.assertEqual(json.loads(session[0]), ["alice", "bob"])
        self.assertEqual(json.loads(session[1])["sample_count"], 6)

    def test_rejects_marker_from_another_session(self):
        ids = self._prepare_session()
        response = self.monitor_app.app.test_client().post(
            "/api/session/user_timeline",
            headers=self.headers,
            json={
                "device_id": "bike",
                "session_id": "ride",
                "segments": [{"start_sample_id": ids[-1] + 100, "identity_key": "user:alice"}],
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_stop_ranges_only_include_continuous_zero_speed_over_thirty_seconds(self):
        def sample(second, speed, *, timestamp=None):
            values = [0.0] * 14
            values[3] = speed
            return {
                "timestamp": timestamp or f"2026-08-12 10:00:{second:02d}",
                "raw": " ".join(str(value) for value in values),
            }

        samples = [sample(second, 0) for second in range(32)]
        samples.append(sample(32, 12))
        samples.extend(sample(33 + second, 0) for second in range(12))

        stops = self.monitor_app._session_stop_ranges(samples)

        self.assertEqual(
            stops,
            [{"start_index": 0, "end_index": 31, "duration_sec": 31.0}],
        )

    def test_stop_ranges_do_not_bridge_a_logging_gap(self):
        samples = []
        for timestamp in (
            "2026-08-12 10:00:00",
            "2026-08-12 10:00:01",
            "2026-08-12 10:01:00",
            "2026-08-12 10:01:01",
        ):
            values = [0.0] * 14
            samples.append({"timestamp": timestamp, "raw": " ".join(str(value) for value in values)})

        self.assertEqual(self.monitor_app._session_stop_ranges(samples), [])


if __name__ == "__main__":
    unittest.main()
