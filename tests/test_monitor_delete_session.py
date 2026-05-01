import importlib.util
import os
import shutil
import sqlite3
import sys
import types
import unittest


class MonitorDeleteSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "monitor_delete_tmp"))
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.tmp_dir, "monitor.db")
        self.media_dir = os.path.join(self.tmp_dir, "media")
        os.environ["MONITOR_DB"] = self.db_path
        os.environ["MONITOR_MEDIA_DIR"] = self.media_dir
        os.environ["MONITOR_USER"] = "admin"
        os.environ["MONITOR_PASS"] = "secret"

        if "flask" not in sys.modules and importlib.util.find_spec("flask") is None:
            class DummyApp:
                def __init__(self, *args, **kwargs):
                    pass

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

        from monitor_server.app import _delete_session_data, _delete_sessions_data, _init_db, _is_deleted_session

        self.delete_session_data = _delete_session_data
        self.delete_sessions_data = _delete_sessions_data
        self.is_deleted_session = _is_deleted_session
        _init_db()

    def tearDown(self):
        for key in ("MONITOR_DB", "MONITOR_MEDIA_DIR", "MONITOR_USER", "MONITOR_PASS"):
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


if __name__ == "__main__":
    unittest.main()
