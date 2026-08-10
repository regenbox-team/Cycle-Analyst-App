import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from app import monitor_client, state
from app.routes import sessions


class SessionDownloadTest(unittest.TestCase):
    def test_download_is_the_direct_upload_payload(self):
        session_id = "2026-08-10_12-00-00"
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "ride_data_supercycle_live.db")
            metrics_dir = os.path.join(tmp_dir, "session_metrics")
            os.makedirs(metrics_dir)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE logs (id INTEGER PRIMARY KEY, timestamp TEXT, session TEXT, raw TEXT, user TEXT)"
                )
                conn.executemany(
                    "INSERT INTO logs (timestamp, session, raw, user) VALUES (?, ?, ?, ?)",
                    [
                        ("2026-08-10 12:00:00", session_id, "0 52 1 10 0.0", "JD"),
                        ("2026-08-10 12:00:01", session_id, "0 52 1 10 0.1", "JD"),
                    ],
                )
            metrics = {"distance_km": 0.1, "solar_enabled": True}
            with open(os.path.join(metrics_dir, f"{session_id}_session_metrics.json"), "w", encoding="utf-8") as handle:
                json.dump(metrics, handle)

            app = Flask(__name__)
            app.register_blueprint(sessions.create_blueprint())
            previous_active = state.session_active
            previous_session = state.session_id
            state.session_active = False
            state.session_id = None
            try:
                with (
                    patch.object(sessions, "get_db_file", return_value=db_path),
                    patch.object(monitor_client, "SESSION_METRICS_DIR", metrics_dir),
                    patch.object(monitor_client, "is_test_mode", return_value=False),
                    patch.dict(os.environ, {"MONITOR_DEVICE_ID": "bike-phone"}),
                ):
                    expected = monitor_client._build_session_payload(db_path, session_id, "bike-phone")
                    response = app.test_client().get(
                        f"/api/download_session?session={session_id}&mode=supercycle_live"
                    )
            finally:
                state.session_active = previous_active
                state.session_id = previous_session

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.get_json(), expected)


if __name__ == "__main__":
    unittest.main()
