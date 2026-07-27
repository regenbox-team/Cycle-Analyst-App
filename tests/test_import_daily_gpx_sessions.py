from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.import_daily_gpx_sessions import ensure_device, group_tracks_by_local_day
from scripts.import_daily_gpx_sessions import (
    NamedGpxTrack,
    group_named_tracks_by_encoded_day,
    split_disconnected_points,
)
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

    def test_uses_encoded_track_day_and_rebases_visugpx_timestamps(self) -> None:
        tracks = [
            NamedGpxTrack(
                path=Path("visugpx.gpx"),
                name="J10_230726_K",
                day=datetime.strptime("230726", "%y%m%d").date(),
                points=[
                    point("2023-08-24T22:56:03"),
                    point("2023-08-25T06:47:08"),
                ],
            ),
            NamedGpxTrack(
                path=Path("visugpx.gpx"),
                name="J11_230727_K",
                day=datetime.strptime("230727", "%y%m%d").date(),
                points=[
                    point("2023-09-03T03:27:31"),
                    point("2023-09-03T13:06:41"),
                ],
            ),
        ]

        sessions = group_named_tracks_by_encoded_day(tracks, "Europe/Paris")

        self.assertEqual(
            [session.session_id for session in sessions],
            ["gpx-daily-2023-07-26", "gpx-daily-2023-07-27"],
        )
        for session in sessions:
            self.assertTrue(
                all(
                    sample.timestamp.astimezone(ZoneInfo("Europe/Paris")).date()
                    == session.day
                    for fragment in session.fragments
                    for sample in fragment.points
                )
            )

    def test_splits_a_long_missing_gps_connection(self) -> None:
        points = [
            GpxPoint(
                timestamp=datetime.fromisoformat("2023-07-20T12:00:00+00:00"),
                lat=47.58,
                lon=1.33,
                alt=100.0,
            ),
            GpxPoint(
                timestamp=datetime.fromisoformat("2023-07-20T12:00:10+00:00"),
                lat=47.581,
                lon=1.331,
                alt=101.0,
            ),
            GpxPoint(
                timestamp=datetime.fromisoformat("2023-07-20T13:55:00+00:00"),
                lat=47.42,
                lon=1.00,
                alt=102.0,
            ),
        ]

        segments = split_disconnected_points(points)

        self.assertEqual([len(segment) for segment in segments], [2, 1])
