#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.import_legacy_monitor_data import (  # noqa: E402
    GpxPoint,
    GpxTrack,
    child_text,
    haversine_km,
    iso_timestamp,
    load_gpx_tracks,
    parse_timestamp,
)


DEFAULT_DEVICE_ID = "sc-vehicule-2"
DEFAULT_MODE = "supercycle_live"
DEFAULT_TIMEZONE = "Europe/Paris"
TRACK_DATE_PATTERN = re.compile(r"(?:^|_)(\d{6})(?:_|$)")
TRACE_BREAK_MIN_GAP_SEC = 120
TRACE_BREAK_MIN_DISTANCE_KM = 1.0
MAX_GPS_DISTANCE_KPH = 160.0
GPS_DISTANCE_JUMP_MARGIN_KM = 0.25
GPS_DISTANCE_JUMP_FLOOR_KM = 0.25


@dataclass
class TrackFragment:
    source_path: Path
    points: list[GpxPoint]
    source_track_name: str | None = None

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

    @property
    def source_track_names(self) -> list[str]:
        return sorted(
            {
                fragment.source_track_name
                for fragment in self.fragments
                if fragment.source_track_name
            }
        )


@dataclass
class NamedGpxTrack:
    path: Path
    name: str
    day: date
    points: list[GpxPoint]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _track_day(name: str) -> date | None:
    match = TRACK_DATE_PATTERN.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%m%d").date()
    except ValueError:
        return None


def load_named_gpx_tracks(directory: Path) -> list[NamedGpxTrack]:
    tracks: list[NamedGpxTrack] = []
    for path in sorted(directory.glob("*.gpx")):
        root = ET.parse(path).getroot()
        for track_element in root.iter():
            if _local_name(track_element.tag) != "trk":
                continue
            name = child_text(track_element, "name") or ""
            day = _track_day(name)
            if day is None:
                continue
            points: list[GpxPoint] = []
            for element in track_element.iter():
                if _local_name(element.tag) != "trkpt":
                    continue
                time_text = child_text(element, "time")
                if not time_text:
                    continue
                try:
                    lat = float(element.attrib["lat"])
                    lon = float(element.attrib["lon"])
                    timestamp = parse_timestamp(time_text)
                except (KeyError, TypeError, ValueError):
                    continue
                elevation = child_text(element, "ele")
                try:
                    alt = float(elevation) if elevation is not None else None
                except ValueError:
                    alt = None
                if (
                    math.isfinite(lat)
                    and math.isfinite(lon)
                    and -90 <= lat <= 90
                    and -180 <= lon <= 180
                ):
                    points.append(
                        GpxPoint(
                            timestamp=timestamp,
                            lat=lat,
                            lon=lon,
                            alt=alt,
                        )
                    )
            if points:
                tracks.append(
                    NamedGpxTrack(
                        path=path,
                        name=name,
                        day=day,
                        points=points,
                    )
                )
    return tracks


def _normalize_named_track_timestamps(
    track: NamedGpxTrack,
    timezone_name: str,
) -> list[GpxPoint]:
    local_timezone = ZoneInfo(timezone_name)
    original_days = {
        point.timestamp.astimezone(local_timezone).date()
        for point in track.points
    }
    if original_days == {track.day}:
        rebased_start = track.points[0].timestamp
    else:
        rebased_start = datetime.combine(
            track.day,
            time(hour=8),
            tzinfo=local_timezone,
        ).astimezone(timezone.utc)

    normalized = [
        GpxPoint(
            timestamp=rebased_start,
            lat=track.points[0].lat,
            lon=track.points[0].lon,
            alt=track.points[0].alt,
        )
    ]
    previous_original = track.points[0].timestamp
    previous_normalized = rebased_start
    for point in track.points[1:]:
        delta_seconds = (point.timestamp - previous_original).total_seconds()
        # VisuGPX can contain an isolated timestamp reversal. Retain XML point
        # order and make the smallest possible monotonic correction.
        delta_seconds = max(0.001, delta_seconds)
        previous_normalized += timedelta(seconds=delta_seconds)
        normalized.append(
            GpxPoint(
                timestamp=previous_normalized,
                lat=point.lat,
                lon=point.lon,
                alt=point.alt,
            )
        )
        previous_original = point.timestamp

    if any(
        point.timestamp.astimezone(local_timezone).date() != track.day
        for point in normalized
    ):
        raise ValueError(
            f"track {track.name!r} cannot fit inside its encoded day {track.day}"
        )
    return normalized


def split_disconnected_points(points: list[GpxPoint]) -> list[list[GpxPoint]]:
    segments: list[list[GpxPoint]] = []
    previous = None
    for point in points:
        should_break = False
        if previous is not None:
            distance_km = haversine_km(previous, point)
            gap_sec = (point.timestamp - previous.timestamp).total_seconds()
            plausible_distance_km = max(
                GPS_DISTANCE_JUMP_FLOOR_KM,
                (MAX_GPS_DISTANCE_KPH * max(0.0, gap_sec) / 3600.0)
                + GPS_DISTANCE_JUMP_MARGIN_KM,
            )
            should_break = (
                gap_sec < 0
                or distance_km > plausible_distance_km
                or (
                    gap_sec >= TRACE_BREAK_MIN_GAP_SEC
                    and distance_km >= TRACE_BREAK_MIN_DISTANCE_KM
                )
            )
        if not segments or should_break:
            segments.append([])
        segments[-1].append(point)
        previous = point
    return [segment for segment in segments if segment]


def group_named_tracks_by_encoded_day(
    tracks: list[NamedGpxTrack],
    timezone_name: str,
) -> list[DailySession]:
    fragments_by_day: dict[date, list[TrackFragment]] = defaultdict(list)
    for track in tracks:
        normalized_points = _normalize_named_track_timestamps(track, timezone_name)
        fragments_by_day[track.day].extend(
            [
                TrackFragment(
                    source_path=track.path,
                    source_track_name=track.name,
                    points=points,
                )
                for points in split_disconnected_points(normalized_points)
            ]
        )
    return [
        DailySession(
            day=day,
            fragments=sorted(fragments, key=lambda fragment: fragment.start),
        )
        for day, fragments in sorted(fragments_by_day.items())
    ]


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
            f"{' tracks=' + ','.join(session.source_track_names) if session.source_track_names else ''}"
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
    replace_device_gpx_only: bool = False,
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
        if collisions and not replace_device_gpx_only:
            raise RuntimeError(f"target sessions already exist: {', '.join(collisions)}")

        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_device(conn, device_id=device_id, mode=mode)
            replaced_sessions: list[tuple[str, str]] = []
            if replace_device_gpx_only:
                for row in conn.execute(
                    """
                    SELECT session_id, mode, metrics_json
                    FROM sessions
                    WHERE device_id = ?
                    """,
                    (device_id,),
                ).fetchall():
                    try:
                        metrics = json.loads(row["metrics_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        metrics = {}
                    if isinstance(metrics, dict) and metrics.get("gpx_only_import") is True:
                        replaced_sessions.append((row["session_id"], row["mode"]))
                for old_session_id, old_mode in replaced_sessions:
                    conn.execute(
                        """
                        DELETE FROM telemetry_samples
                        WHERE device_id = ? AND session_id = ? AND mode = ?
                        """,
                        (device_id, old_session_id, old_mode),
                    )
                    conn.execute(
                        """
                        DELETE FROM sessions
                        WHERE device_id = ? AND session_id = ? AND mode = ?
                        """,
                        (device_id, old_session_id, old_mode),
                    )
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
                    "gpx_track_names": session.source_track_names,
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
            if replaced_sessions:
                print(f"Replaced {len(replaced_sessions)} existing GPX-only sessions")
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
    parser.add_argument(
        "--day-from-track-name",
        action="store_true",
        help="Use YYMMDD encoded in each GPX <trk><name> and preserve track boundaries.",
    )
    parser.add_argument(
        "--replace-device-gpx-only",
        action="store_true",
        help="Replace all prior GPX-only sessions for this device in the same transaction.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.day_from_track_name:
        named_tracks = load_named_gpx_tracks(args.gpx_dir)
        if not named_tracks:
            parser.error("no GPX tracks with a YYMMDD date in <trk><name> found")
        sessions = group_named_tracks_by_encoded_day(named_tracks, args.timezone)
        duplicates: list[tuple[str, str]] = []
    else:
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
            replace_device_gpx_only=args.replace_device_gpx_only,
        )
        print(f"Imported {len(sessions)} daily sessions into {args.target_db}")
    else:
        print("\nDry run only; use --apply with --target-db to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
