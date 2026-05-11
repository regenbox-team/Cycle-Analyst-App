import os
import shutil
import sqlite3
import unittest


class SuntripPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "suntrip_tmp", self._testMethodName))
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.tmp_dir, "monitor.db")
        os.environ["MONITOR_DB"] = self.db_path
        os.environ["MONITOR_USER"] = "admin"
        os.environ["MONITOR_PASS"] = "secret"
        os.environ["MONITOR_TERRAIN_ELEVATION_ENABLED"] = "0"
        os.environ["SUNTRIP_START_DATE"] = "2026-05-04"
        os.environ["SUNTRIP_DEVICE_ALIASES"] = "Supercycle-1,sc-vehicule-1"
        os.environ["SUNTRIP_PHOTO_LIMIT"] = "5000"

        import monitor_server.app as monitor_app

        self.monitor_app = monitor_app
        self.monitor_app._init_db()

    def tearDown(self):
        for key in (
            "MONITOR_DB",
            "MONITOR_USER",
            "MONITOR_PASS",
            "MONITOR_TERRAIN_ELEVATION_ENABLED",
            "SUNTRIP_START_DATE",
            "SUNTRIP_DEVICE_ALIASES",
            "SUNTRIP_PHOTO_LIMIT",
        ):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_suntrip_uses_start_date_and_device_alias_for_photo_points(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO photos (
                    device_id, session_id, mode, captured_at, relative_path, uploaded_at,
                    is_public, gps_lat, gps_lon, solar_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Supercycle-1",
                    "2026-05-03_10-00-00",
                    "supercycle_live",
                    "2026-05-03 10:00:00",
                    "photos/Supercycle-1/old.jpg",
                    "2026-05-03 10:01:00",
                    1,
                    40.0,
                    -3.0,
                    1,
                ),
            )
            conn.execute(
                """
                INSERT INTO photos (
                    device_id, session_id, mode, captured_at, relative_path, uploaded_at,
                    is_public, gps_lat, gps_lon, solar_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Supercycle-1",
                    "2026-05-04_09-00-00",
                    "supercycle_live",
                    "2026-05-04 09:05:00",
                    "photos/Supercycle-1/aliased.jpg",
                    "2026-05-04 09:06:00",
                    1,
                    None,
                    None,
                    1,
                ),
            )
            conn.execute(
                """
                INSERT INTO telemetry_samples (
                    device_id, session_id, mode, timestamp, gps_lat, gps_lon, gps_alt,
                    gps_speed_kph, gps_fix, gps_sats, solar_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sc-vehicule-1",
                    "2026-05-04_09-00-00",
                    "supercycle_live",
                    "2026-05-04 09:05:02",
                    27.1492,
                    -13.2033,
                    55.0,
                    31.5,
                    1,
                    8,
                    1,
                ),
            )
            conn.commit()

        response = self.monitor_app.app.test_client().get("/public/suntrip.json?device_id=sc-vehicule-1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["latest"]["session_id"], "2026-05-04_09-00-00")
        self.assertEqual(len(payload["points"]), 1)
        self.assertEqual(payload["points"][0]["id"], payload["latest"]["id"])
        self.assertAlmostEqual(payload["points"][0]["lat"], 27.1492)
        self.assertAlmostEqual(payload["points"][0]["lon"], -13.2033)


if __name__ == "__main__":
    unittest.main()
