#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEVICE_ID = "sc-vehicule-2"
MODE = "supercycle_live"
MAX_GPX_POINTS = 2500
OVERNIGHT_SPLIT_SECONDS = 6 * 3600
ADJACENT_MERGE_SECONDS = 60


@dataclass
class LegacyRow:
    timestamp: datetime
    timestamp_text: str
    source_session: str
    raw: str | None
    user: str | None


@dataclass
class GpxPoint:
    timestamp: datetime
    lat: float
    lon: float
    alt: float | None


@dataclass
class GpxTrack:
    path: Path
    points: list[GpxPoint]

    @property
    def start(self) -> datetime:
        return self.points[0].timestamp

    @property
    def end(self) -> datetime:
        return self.points[-1].timestamp


@dataclass
class ImportSession:
    session_id: str
    legacy_rows: list[LegacyRow]
    gpx_tracks: list[GpxTrack]
    source_sessions: list[str]

    @property
    def start(self) -> datetime:
        candidates = [row.timestamp for row in self.legacy_rows]
        candidates.extend(track.start for track in self.gpx_tracks)
        return min(candidates)

    @property
    def end(self) -> datetime:
        candidates = [row.timestamp for row in self.legacy_rows]
        candidates.extend(track.end for track in self.gpx_tracks)
        return max(candidates)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def load_legacy_rows(path: Path) -> dict[str, list[LegacyRow]]:
    sessions: dict[str, list[LegacyRow]] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for timestamp, session, raw, user in conn.execute(
            "SELECT timestamp, session, raw, user FROM logs ORDER BY session, timestamp, id"
        ):
            if not timestamp or not session:
                continue
            sessions.setdefault(str(session), []).append(
                LegacyRow(
                    timestamp=parse_timestamp(str(timestamp)),
                    timestamp_text=str(timestamp),
                    source_session=str(session),
                    raw=raw,
                    user=user,
                )
            )
    return sessions


def split_overnight_sessions(sessions: dict[str, list[LegacyRow]]) -> list[ImportSession]:
    result: list[ImportSession] = []
    for source_session, rows in sessions.items():
        chunks: list[list[LegacyRow]] = [[]]
        for row in rows:
            if chunks[-1]:
                previous = chunks[-1][-1]
                gap = (row.timestamp - previous.timestamp).total_seconds()
                if gap > OVERNIGHT_SPLIT_SECONDS and row.timestamp.date() != previous.timestamp.date():
                    chunks.append([])
            chunks[-1].append(row)

        multiple = len(chunks) > 1
        for chunk in chunks:
            session_id = source_session
            if multiple:
                session_id = f"{source_session}__{chunk[0].timestamp:%Y-%m-%d}"
            result.append(
                ImportSession(
                    session_id=session_id,
                    legacy_rows=chunk,
                    gpx_tracks=[],
                    source_sessions=[source_session],
                )
            )

    # A few samples were written into the previous session immediately before a
    # new session began. Merge only cross-session chunks separated by <=1 min.
    ordered = sorted(result, key=lambda item: item.start)
    merged: list[ImportSession] = []
    for item in ordered:
        if merged:
            previous = merged[-1]
            gap = (item.start - previous.end).total_seconds()
            if (
                0 <= gap <= ADJACENT_MERGE_SECONDS
                and item.start.date() == previous.end.date()
                and item.source_sessions != previous.source_sessions
            ):
                previous.legacy_rows.extend(item.legacy_rows)
                previous.legacy_rows.sort(key=lambda row: row.timestamp)
                previous.source_sessions.extend(item.source_sessions)
                if len(item.legacy_rows) > len(previous.legacy_rows) / 2:
                    previous.session_id = item.session_id
                continue
        merged.append(item)
    return merged


def child_text(element: ET.Element, local_name: str) -> str | None:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == local_name and child.text:
            return child.text.strip()
    return None


def load_gpx_track(path: Path) -> GpxTrack | None:
    points: list[GpxPoint] = []
    root = ET.parse(path).getroot()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "trkpt":
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
        alt = None
        elevation = child_text(element, "ele")
        if elevation is not None:
            try:
                alt = float(elevation)
            except ValueError:
                pass
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            points.append(GpxPoint(timestamp=timestamp, lat=lat, lon=lon, alt=alt))
    points.sort(key=lambda point: point.timestamp)
    return GpxTrack(path=path, points=points) if points else None


def load_gpx_tracks(directory: Path) -> tuple[list[GpxTrack], list[tuple[str, str]]]:
    tracks = [
        track
        for path in sorted(directory.glob("*.gpx"))
        if (track := load_gpx_track(path)) is not None
    ]
    duplicates: list[tuple[str, str]] = []
    keep: list[GpxTrack] = []
    for track in sorted(tracks, key=lambda item: len(item.points), reverse=True):
        duplicate_of = next(
            (
                candidate
                for candidate in keep
                if abs((candidate.start - track.start).total_seconds()) <= 1
                and abs((candidate.end - track.end).total_seconds()) <= 1
                and candidate.points[0].lat == track.points[0].lat
                and candidate.points[0].lon == track.points[0].lon
                and candidate.points[-1].lat == track.points[-1].lat
                and candidate.points[-1].lon == track.points[-1].lon
            ),
            None,
        )
        if duplicate_of is not None:
            duplicates.append((track.path.name, duplicate_of.path.name))
        else:
            keep.append(track)
    return sorted(keep, key=lambda item: item.start), duplicates


def overlap_seconds(session: ImportSession, track: GpxTrack) -> float:
    return max(0.0, (min(session.end, track.end) - max(session.start, track.start)).total_seconds())


def associate_tracks(
    sessions: list[ImportSession],
    tracks: list[GpxTrack],
) -> list[ImportSession]:
    result = list(sessions)
    for track in tracks:
        matches = sorted(
            ((overlap_seconds(session, track), session) for session in sessions),
            key=lambda item: item[0],
            reverse=True,
        )
        if matches and matches[0][0] > 0:
            matches[0][1].gpx_tracks.append(track)
            continue
        result.append(
            ImportSession(
                session_id=f"gpx-{track.path.stem}-{track.start:%Y-%m-%d_%H-%M-%S}",
                legacy_rows=[],
                gpx_tracks=[track],
                source_sessions=[],
            )
        )
    return sorted(result, key=lambda item: item.start)


def sample_points(points: list[GpxPoint], maximum: int = MAX_GPX_POINTS) -> list[GpxPoint]:
    if len(points) <= maximum:
        return points
    step = (len(points) - 1) / (maximum - 1)
    indexes = sorted({round(index * step) for index in range(maximum)})
    return [points[index] for index in indexes]


def haversine_km(first: GpxPoint, second: GpxPoint) -> float:
    radius_km = 6371.0
    lat1 = math.radians(first.lat)
    lat2 = math.radians(second.lat)
    dlat = lat2 - lat1
    dlon = math.radians(second.lon - first.lon)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return radius_km * 2 * math.asin(math.sqrt(value))


def gpx_distance_km(session: ImportSession) -> float | None:
    if not session.gpx_tracks:
        return None
    return sum(
        haversine_km(first, second)
        for track in session.gpx_tracks
        for first, second in zip(track.points, track.points[1:])
    )


def session_samples(session: ImportSession) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = [
        {
            "timestamp": row.timestamp_text,
            "raw": row.raw,
            "user": row.user,
            "user_initials": row.user,
            "solar_enabled": 0,
        }
        for row in session.legacy_rows
    ]
    for track in session.gpx_tracks:
        for point in sample_points(track.points):
            samples.append(
                {
                    "timestamp": iso_timestamp(point.timestamp),
                    "gps_lat": point.lat,
                    "gps_lon": point.lon,
                    "gps_alt": point.alt,
                    "solar_enabled": 0,
                }
            )
    samples.sort(key=lambda sample: parse_timestamp(str(sample["timestamp"])))
    return samples


def print_report(
    sessions: list[ImportSession],
    duplicates: list[tuple[str, str]],
) -> None:
    print(f"Sessions to import: {len(sessions)}")
    print(f"Legacy rows: {sum(len(session.legacy_rows) for session in sessions)}")
    print(f"GPX tracks used: {sum(len(session.gpx_tracks) for session in sessions)}")
    for duplicate, kept in duplicates:
        print(f"Duplicate GPX ignored: {duplicate} (same track as {kept})")
    print()
    for session in sessions:
        tracks = ",".join(track.path.name for track in session.gpx_tracks) or "-"
        sources = ",".join(session.source_sessions) or "GPX only"
        print(
            f"{session.session_id:42} "
            f"{session.start.isoformat()}..{session.end.isoformat()} "
            f"legacy={len(session.legacy_rows):6} gpx={tracks:16} source={sources}"
        )


def apply_import(target_db: Path, sessions: list[ImportSession]) -> None:
    os.environ["MONITOR_DB"] = str(target_db)
    from monitor_server import app as monitor_app

    with monitor_app._get_db() as conn:
        collisions = []
        for session in sessions:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE device_id = ? AND session_id = ? AND mode = ?",
                (DEVICE_ID, session.session_id, MODE),
            ).fetchone()
            if row:
                collisions.append(session.session_id)
        if collisions:
            raise RuntimeError(f"target sessions already exist: {', '.join(collisions)}")

        conn.execute("BEGIN IMMEDIATE")
        try:
            for session in sessions:
                samples = session_samples(session)
                gps_distance = gpx_distance_km(session)
                payload = {
                    "device_id": DEVICE_ID,
                    "session_id": session.session_id,
                    "mode": MODE,
                    "solar_enabled": 0,
                    "metrics": {
                        "legacy_import": True,
                        "legacy_source_sessions": session.source_sessions,
                        "gpx_files": [track.path.name for track in session.gpx_tracks],
                        "distance_km": gps_distance,
                    },
                }
                monitor_app._insert_uploaded_session_summary(conn, payload, samples)
                if gps_distance is not None:
                    duration_sec = max(0.0, (session.end - session.start).total_seconds())
                    avg_speed = gps_distance / (duration_sec / 3600) if duration_sec > 0 else None
                    conn.execute(
                        """
                        UPDATE sessions
                        SET distance_km = ?, duration_sec = ?, avg_speed_kph = ?
                        WHERE device_id = ? AND session_id = ? AND mode = ?
                        """,
                        (
                            gps_distance,
                            duration_sec,
                            avg_speed,
                            DEVICE_ID,
                            session.session_id,
                            MODE,
                        ),
                    )
                monitor_app._insert_telemetry_samples(
                    conn,
                    DEVICE_ID,
                    session.session_id,
                    MODE,
                    samples,
                    0,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the 2025 legacy ride DB and matching GPX tracks.")
    parser.add_argument("--legacy-db", type=Path, required=True)
    parser.add_argument("--gpx-dir", type=Path, required=True)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    legacy = load_legacy_rows(args.legacy_db)
    sessions = split_overnight_sessions(legacy)
    tracks, duplicates = load_gpx_tracks(args.gpx_dir)
    sessions = associate_tracks(sessions, tracks)
    print_report(sessions, duplicates)

    if args.apply:
        if args.target_db is None:
            parser.error("--target-db is required with --apply")
        apply_import(args.target_db, sessions)
        print(f"Imported {len(sessions)} sessions into {args.target_db}")
    else:
        print("\nDry run only; use --apply with --target-db to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
