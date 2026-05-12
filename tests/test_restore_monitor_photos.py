import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing


SCRIPT_PATH = os.path.abspath(os.path.join("scripts", "restore_monitor_photos.py"))
SPEC = importlib.util.spec_from_file_location("restore_monitor_photos", SCRIPT_PATH)
restore_monitor_photos = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore_monitor_photos)


def init_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                captured_at TEXT,
                distance_km REAL,
                interval_km REAL,
                filename TEXT,
                mime_type TEXT,
                relative_path TEXT,
                uploaded_at TEXT,
                test_mode INTEGER DEFAULT 0,
                is_public INTEGER DEFAULT 1,
                gps_lat REAL,
                gps_lon REAL,
                gps_alt REAL,
                gps_speed_kph REAL,
                gps_track_deg REAL,
                gps_fix INTEGER,
                gps_sats INTEGER,
                gps_hdop REAL,
                speed_kph REAL,
                session_distance_km REAL,
                gps_uphill_m REAL,
                solar_power_w REAL,
                generator_power_w REAL,
                solar_wh REAL,
                solar_enabled INTEGER DEFAULT 1,
                user_id TEXT,
                user_initials TEXT,
                user_snapshot_json TEXT,
                metrics_json TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class RestoreMonitorPhotosTest(unittest.TestCase):
    def test_export_import_restores_only_existing_session_photos(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_db = os.path.join(tmp_dir, "old_monitor.db")
            target_db = os.path.join(tmp_dir, "new_monitor.db")
            manifest = os.path.join(tmp_dir, "photos.json")
            init_db(source_db)
            init_db(target_db)

            with closing(sqlite3.connect(source_db)) as conn:
                conn.executemany(
                    """
                    INSERT INTO photos (
                        device_id, session_id, mode, captured_at, relative_path, uploaded_at, gps_lat, gps_lon
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "sc-vehicule-1",
                            "2026-05-09_10-43-40",
                            "supercycle_live",
                            "2026-05-09 10:50:00",
                            "photos/sc-vehicule-1/2026-05-09_10-43-40/2026-05-09_10-50-00.jpg",
                            "2026-05-09 10:50:01",
                            48.0,
                            2.0,
                        ),
                        (
                            "sc-vehicule-2",
                            "2026-05-09_23-49-41",
                            "supercycle_live",
                            "2026-05-09 23:55:00",
                            "photos/sc-vehicule-2/2026-05-09_23-49-41/2026-05-09_23-55-00.jpg",
                            "2026-05-09 23:55:01",
                            49.0,
                            3.0,
                        ),
                    ],
                )
                conn.commit()

            result = restore_monitor_photos.export_manifest(
                source_db,
                manifest,
                [
                    ("sc-vehicule-1", "2026-05-09_10-43-40", "supercycle_live"),
                    ("sc-vehicule-2", "2026-05-09_23-49-41", "supercycle_live"),
                ],
            )
            self.assertEqual(len(result["photos"]), 2)

            with closing(sqlite3.connect(target_db)) as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (device_id, session_id, mode)
                    VALUES (?, ?, ?)
                    """,
                    ("sc-vehicule-1", "2026-05-09_10-43-40", "supercycle_live"),
                )
                conn.execute(
                    """
                    INSERT INTO photos (device_id, session_id, mode, captured_at, relative_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "sc-vehicule-1",
                        "2026-05-09_10-43-40",
                        "supercycle_live",
                        "old",
                        "photos/stale.jpg",
                    ),
                )
                conn.commit()

            dry_run = restore_monitor_photos.import_manifest(target_db, manifest, dry_run=True)
            self.assertEqual(dry_run["photos_to_insert"], 1)
            self.assertEqual(dry_run["skipped_missing_session"], 1)

            imported = restore_monitor_photos.import_manifest(target_db, manifest)
            self.assertEqual(imported["photos_inserted"], 1)
            self.assertEqual(imported["skipped_missing_session"], 1)

            with closing(sqlite3.connect(target_db)) as conn:
                rows = conn.execute(
                    """
                    SELECT device_id, session_id, mode, captured_at, relative_path, gps_lat, gps_lon
                    FROM photos
                    ORDER BY captured_at
                    """
                ).fetchall()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "2026-05-09 10:50:00")
            self.assertIn("2026-05-09_10-43-40", rows[0][4])
            self.assertEqual(rows[0][5], 48.0)
            self.assertEqual(rows[0][6], 2.0)

            with open(manifest, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload["schema"], "cycle-monitor-photo-restore-v1")


if __name__ == "__main__":
    unittest.main()
