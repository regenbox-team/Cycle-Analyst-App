import sqlite3
import unittest

from monitor_server.weather_analysis import enrich_project_weather, ensure_weather_schema


class WeatherAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE sessions (
                device_id TEXT, session_id TEXT, mode TEXT, travel_project_id INTEGER
            );
            CREATE TABLE telemetry_samples (
                device_id TEXT, session_id TEXT, mode TEXT, timestamp TEXT,
                gps_lat REAL, gps_lon REAL
            );
            INSERT INTO sessions VALUES ('sc1', 'ride-1', 'default', 7);
            INSERT INTO telemetry_samples VALUES
              ('sc1', 'ride-1', 'default', '2026-05-04T10:10:00', 27.159, -13.207),
              ('sc1', 'ride-1', 'default', '2026-05-04T10:12:00', 27.161, -13.204);
            """
        )
        ensure_weather_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_enrichment_fetches_only_travelled_grid_and_is_idempotent(self):
        calls = []

        def fake_fetch(points, date):
            calls.append((points, date))
            return [
                {
                    "hourly": {
                        "time": [f"{date}T{hour:02d}:00" for hour in range(24)],
                        "wind_speed_10m": [8.0] * 24,
                        "wind_direction_10m": [180.0] * 24,
                        "wind_gusts_10m": [12.0] * 24,
                    }
                }
                for _ in points
            ]

        first = enrich_project_weather(self.conn, 7, fetcher=fake_fetch)
        second = enrich_project_weather(self.conn, 7, fetcher=fake_fetch)

        self.assertEqual(first["zones"], 1)
        self.assertEqual(first["rows_written"], 24)
        self.assertEqual(second["requested_zones"], 0)
        self.assertEqual(len(calls), 1)
        count = self.conn.execute("SELECT COUNT(*) FROM weather_samples").fetchone()[0]
        self.assertEqual(count, 24)


if __name__ == "__main__":
    unittest.main()
