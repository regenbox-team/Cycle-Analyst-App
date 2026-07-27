#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.import_legacy_monitor_data import (  # noqa: E402
    GpxPoint,
    GpxTrack,
    haversine_km,
    iso_timestamp,
    load_gpx_tracks,
)


DEFAULT_DEVICE_ID = "sc-vehicule-2"
DEFAULT_MODE = "supercycle_live"
DEFAULT_TIMEZONE = "Europe/Paris"


@dataclass
class TrackFragment:
    source_path: Path
    points: list[GpxPoint]

    @property
    def start(self) -> datetime:
        return self.points[0].timestamp

    @property
    def end(self) -> datetime:
        return self.points[-1].timestamp


@dataclass
class DailySession:
    day: date
    fragments: list[TrackFragment]

    @property
    def session_id(self) -> str:
        return f"gpx-daily-{self.day.isoformat()}"

    @property
    def start(self) -> datetime:
        return min(fragment.start for fragment in self.fragments)

    @property
    def end(self) -> datetime:
        return max(fragment.end for fragment in self.fragments)

    @property
    def source_files(self) -> list[str]:
        return sorted({fragment.source_path.name for fragment in self.fragments})

    @property
    def source_point_count(self) -> int:
        return sum(len(fragment.points) for fragment in self.fragments)


def group_tracks_by_local_day(
    tracks: list[GpxTrack],
    timezone_name: str,
) -> list[DailySession]:
    timezone = ZoneInfo(timezone_name)
    fragments_by_day: dict[date, list[TrackFragment]] = defaultdict(list)
    for track in tracks:
        points_by_day: dict[date, list[GpxPoint]] = defaultdict(list)
        for point in track.points:
            points_by_day[point.timestamp.astimezone(timezone).date()].append(point)
        for day, points in points_by_day.items():
            fragments_by_day[day].append(
                TrackFragment(source_path=track.path, points=points)
            )

    return [
        DailySession(
            day=day,
            fragments=sorted(fragments, key=lambda fragment: fragment.start),
        )
        for day, fragments in sorted(fragments_by_day.items())
    ]


def fragment_distance_km(fragment: TrackFragment) -> float:
    return sum(
        haversine_km(first, second)
        for first, second in zip(fragment.points, fragment.points[1:])
    )


def session_distance_km(session: DailySession) -> float:
    # Do not invent straight-line connections between distinct GPX recordings.
    return sum(fragment_distance_km(fragment) for fragment in session.fragments)


def point_sample(point: GpxPoint) -> dict[str, Any]:
    return {
        "timestamp": iso_timestamp(point.timestamp),
        "gps_lat": point.lat,
        "gps_lon": point.lon,
        "gps_alt": point.alt,
        "solar_enabled": 0,
    }


def session_samples(session: DailySession) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float, float | None]] = set()
    for fragment in session.fragments:
        for point in fragment.points:
            timestamp = iso_timestamp(point.timestamp)
            key = (timestamp, point.lat, point.lon, point.alt)
            if key in seen:
                continue
            seen.add(key)
            samples.append(point_sample(point))
    samples.sort(key=lambda sample: str(sample["timestamp"]))
    return samples


def print_report(
    sessions: list[DailySession],
    duplicate_files: list[tuple[str, str]],
) -> None:
    print(f"Daily sessions: {len(sessions)}")
    source_files = {
        source_file
        for session in sessions
        for source_file in session.source_files
    }
    print(f"Source GPX files: {len(source_files)}")
    print(f"Source GPX points: {sum(session.source_point_count for session in sessions)}")
    print(f"Distance: {sum(session_distance_km(session) for session in sessions):.3f} km")
    for duplicate, kept in duplicate_files:
        print(f"Duplicate GPX ignored: {duplicate} (same track as {kept})")
    print()
    for session in sessions:
        print(
            f"{session.session_id:26} "
            f"{session.start.isoformat()}..{session.end.isoformat()} "
            f"points={session.source_point_count:6} "
            f"distance={session_distance_km(session):8.3f} km "
            f"files={','.join(session.source_files)}"
        )


def _fragment_uphill(
    monitor_app: Any,
    session: DailySession,
    helper_name: str,
) -> float:
    helper = getattr(monitor_app, helper_name)
    total = 0.0
    for fragment in session.fragments:
        total += helper([point_sample(point) for point in fragment.points]) or 0.0
    return total


def ensure_device(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    mode: str,
) -> None:
    conn.execute(
        """
        INSERT INTO devices (
            device_id, last_seen, last_ip, last_session, mode,
            test_mode, session_active, gps_available, solar_enabled
        )
        VALUES (?, NULL, NULL, NULL, ?, 0, 0, 0, 0)
        ON CONFLICT(device_id) DO NOTHING
        """,
        (device_id, mode),
    )


def apply_import(
    target_db: Path,
    sessions: list[DailySession],
    *,
    device_id: str,
    mode: str,
    timezone_name: str,
) -> None:
    os.environ["MONITOR_DB"] = str(target_db.resolve())
    from monitor_server import app as monitor_app

    with monitor_app._get_db() as conn:
        opened_db = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        if opened_db != target_db.resolve():
            raise RuntimeError(f"wrong database opened: {opened_db}")

        collisions = [
            session.session_id
            for session in sessions
            if conn.execute(
                """
                SELECT 1 FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (device_id, session.session_id, mode),
            ).fetchone()
        ]
        if collisions:
            raise RuntimeError(f"target sessions already exist: {', '.join(collisions)}")

        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_device(conn, device_id=device_id, mode=mode)
            for session in sessions:
                samples = session_samples(session)
                distance_km = session_distance_km(session)
                duration_sec = max(0.0, (session.end - session.start).total_seconds())
                avg_speed_kph = (
                    distance_km / (duration_sec / 3600.0)
                    if duration_sec > 0
                    else None
                )
                metrics = {
                    "gpx_only_import": True,
                    "daily_grouping": True,
                    "daily_grouping_timezone": timezone_name,
                    "gpx_files": session.source_files,
                    "source_gpx_points": session.source_point_count,
                    "stored_gpx_points": len(samples),
                    "distance_km": distance_km,
                }
                payload = {
                    "device_id": device_id,
                    "session_id": session.session_id,
                    "mode": mode,
                    "solar_enabled": 0,
                    "metrics": metrics,
                }
                monitor_app._insert_uploaded_session_summary(conn, payload, samples)
                monitor_app._insert_telemetry_samples(
                    conn,
                    device_id,
                    session.session_id,
                    mode,
                    samples,
                    0,
                )
                uphill_m = _fragment_uphill(
                    monitor_app, session, "_compute_session_uphill_m"
                )
                raw_gps_uphill_m = _fragment_uphill(
                    monitor_app, session, "_compute_raw_gps_uphill_m"
                )
                conn.execute(
                    """
                    UPDATE sessions
                    SET distance_km = ?, duration_sec = ?, avg_speed_kph = ?,
                        uphill_m = ?, raw_gps_uphill_m = ?, metrics_json = ?
                    WHERE device_id = ? AND session_id = ? AND mode = ?
                    """,
                    (
                        distance_km,
                        duration_sec,
                        avg_speed_kph,
                        uphill_m,
                        raw_gps_uphill_m,
                        json.dumps(metrics, separators=(",", ":"), ensure_ascii=False),
                        device_id,
                        session.session_id,
                        mode,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import GPX-only data as one session per local calendar day."
    )
    parser.add_argument("--gpx-dir", type=Path, required=True)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID)
    parser.add_argument("--mode", default=DEFAULT_MODE)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    tracks, duplicates = load_gpx_tracks(args.gpx_dir)
    if not tracks:
        parser.error("no valid GPX tracks found")
    sessions = group_tracks_by_local_day(tracks, args.timezone)
    print_report(sessions, duplicates)

    if args.apply:
        if args.target_db is None:
            parser.error("--target-db is required with --apply")
        apply_import(
            args.target_db,
            sessions,
            device_id=args.device_id,
            mode=args.mode,
            timezone_name=args.timezone,
        )
        print(f"Imported {len(sessions)} daily sessions into {args.target_db}")
    else:
        print("\nDry run only; use --apply with --target-db to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
