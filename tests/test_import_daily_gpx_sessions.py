from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.import_daily_gpx_sessions import ensure_device, group_tracks_by_local_day
from scripts.import_legacy_monitor_data import GpxPoint, GpxTrack


def point(timestamp: str) -> GpxPoint:
    return GpxPoint(
        timestamp=datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc),
        lat=44.0,
        lon=3.0,
        alt=500.0,
    )


class DailyGpxGroupingTest(unittest.TestCase):
    def test_groups_multiple_files_into_one_local_day(self) -> None:
        tracks = [
            GpxTrack(Path("07-10_01.gpx"), [point("2024-07-10T08:00:00")]),
            GpxTrack(Path("07-10_02.gpx"), [point("2024-07-10T18:00:00")]),
        ]

        sessions = group_tracks_by_local_day(tracks, "Europe/Paris")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "gpx-daily-2024-07-10")
        self.assertEqual(
            sessions[0].source_files,
            ["07-10_01.gpx", "07-10_02.gpx"],
        )

    def test_splits_a_track_at_the_local_calendar_boundary(self) -> None:
        track = GpxTrack(
            Path("overnight.gpx"),
            [
                point("2024-07-01T21:59:00"),
                point("2024-07-01T22:01:00"),
            ],
        )

        sessions = group_tracks_by_local_day([track], "Europe/Paris")

        self.assertEqual(
            [session.session_id for session in sessions],
            ["gpx-daily-2024-07-01", "gpx-daily-2024-07-02"],
        )
        self.assertEqual(
            [session.source_point_count for session in sessions],
            [1, 1],
        )

    def test_creates_device_without_faking_a_heartbeat(self) -> None:
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE devices (
                    device_id TEXT PRIMARY KEY,
                    last_seen TEXT,
                    last_ip TEXT,
                    last_session TEXT,
                    mode TEXT,
                    test_mode INTEGER,
                    session_active INTEGER,
                    gps_available INTEGER,
                    solar_enabled INTEGER
                )
                """
            )

            ensure_device(
                conn,
                device_id="Supercyclette",
                mode="supercycle_live",
            )

            device = conn.execute(
                "SELECT * FROM devices WHERE device_id = ?",
                ("Supercyclette",),
            ).fetchone()
            self.assertIsNotNone(device)
            self.assertIsNone(device["last_seen"])
            self.assertIsNone(device["last_ip"])
            self.assertIsNone(device["last_session"])
            self.assertEqual(device["mode"], "supercycle_live")
            self.assertEqual(device["session_active"], 0)
            self.assertEqual(device["gps_available"], 0)
            self.assertEqual(device["solar_enabled"], 0)
