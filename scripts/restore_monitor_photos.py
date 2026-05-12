from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


PHOTO_COLUMNS = [
    "device_id",
    "session_id",
    "mode",
    "captured_at",
    "distance_km",
    "interval_km",
    "filename",
    "mime_type",
    "relative_path",
    "uploaded_at",
    "test_mode",
    "is_public",
    "gps_lat",
    "gps_lon",
    "gps_alt",
    "gps_speed_kph",
    "gps_track_deg",
    "gps_fix",
    "gps_sats",
    "gps_hdop",
    "speed_kph",
    "session_distance_km",
    "gps_uphill_m",
    "solar_power_w",
    "generator_power_w",
    "solar_wh",
    "solar_enabled",
    "user_id",
    "user_initials",
    "user_snapshot_json",
    "metrics_json",
]


def _connect(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"DB not found: {path}")
    mode = "ro" if read_only else "rw"
    uri = f"file:{os.path.abspath(path)}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _session_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("device_id") or ""),
        str(row.get("session_id") or ""),
        str(row.get("mode") or "default"),
    )


def _parse_session(raw: str) -> tuple[str | None, str, str | None]:
    parts = raw.split("|")
    if len(parts) == 1:
        return None, parts[0], None
    if len(parts) == 2:
        return parts[0] or None, parts[1], None
    return parts[0] or None, parts[1], parts[2] or None


def _load_sessions(path: str | None, raw_sessions: list[str]) -> list[tuple[str | None, str, str | None]]:
    sessions = [_parse_session(raw.strip()) for raw in raw_sessions if raw.strip()]
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    sessions.append(_parse_session(line))
    return sessions


def export_manifest(
    source_db: str,
    output_path: str,
    sessions: list[tuple[str | None, str, str | None]],
) -> dict[str, Any]:
    if not sessions:
        raise SystemExit("No sessions supplied. Use --session or --sessions-file.")

    conn = _connect(source_db, read_only=True)
    try:
        available_columns = _table_columns(conn, "photos")
        selected_columns = [col for col in PHOTO_COLUMNS if col in available_columns]
        if not selected_columns:
            raise SystemExit("The source DB has no compatible photos table.")

        rows: list[dict[str, Any]] = []
        seen = set()
        for device_id, session_id, mode in sessions:
            where = ["session_id = ?"]
            params: list[Any] = [session_id]
            if device_id:
                where.append("device_id = ?")
                params.append(device_id)
            if mode:
                where.append("mode = ?")
                params.append(mode)

            query = f"""
                SELECT {", ".join(selected_columns)}
                FROM photos
                WHERE {" AND ".join(where)}
                ORDER BY device_id, session_id, mode, captured_at, relative_path
            """
            for row in conn.execute(query, params).fetchall():
                payload = {col: row[col] if col in row.keys() else None for col in PHOTO_COLUMNS}
                key = (
                    payload.get("device_id"),
                    payload.get("session_id"),
                    payload.get("mode"),
                    payload.get("captured_at"),
                    payload.get("relative_path"),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(payload)
    finally:
        conn.close()

    manifest = {
        "schema": "cycle-monitor-photo-restore-v1",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": os.path.abspath(source_db),
        "photo_columns": PHOTO_COLUMNS,
        "photos": rows,
        "sessions": sorted({"|".join(_session_key(row)) for row in rows}),
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest


def import_manifest(target_db: str, manifest_path: str, *, dry_run: bool = False, replace: bool = True) -> dict[str, Any]:
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("schema") != "cycle-monitor-photo-restore-v1":
        raise SystemExit("Unsupported manifest schema.")

    photos = manifest.get("photos")
    if not isinstance(photos, list):
        raise SystemExit("Manifest has no photos list.")

    conn = _connect(target_db)
    try:
        target_columns = _table_columns(conn, "photos")
        insert_columns = [col for col in PHOTO_COLUMNS if col in target_columns]
        if not insert_columns:
            raise SystemExit("The target DB has no compatible photos table.")

        session_keys = sorted({_session_key(row) for row in photos})
        existing_sessions = set()
        for key in session_keys:
            row = conn.execute(
                """
                SELECT 1
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                key,
            ).fetchone()
            if row:
                existing_sessions.add(key)

        rows_to_insert = [row for row in photos if _session_key(row) in existing_sessions]
        skipped = len(photos) - len(rows_to_insert)

        if dry_run:
            return {
                "status": "dry-run",
                "photos_in_manifest": len(photos),
                "photos_to_insert": len(rows_to_insert),
                "skipped_missing_session": skipped,
                "sessions_in_manifest": len(session_keys),
                "sessions_found": len(existing_sessions),
            }

        if replace:
            for key in existing_sessions:
                conn.execute(
                    """
                    DELETE FROM photos
                    WHERE device_id = ? AND session_id = ? AND mode = ?
                    """,
                    key,
                )

        placeholders = ", ".join("?" for _ in insert_columns)
        conn.executemany(
            f"""
            INSERT INTO photos ({", ".join(insert_columns)})
            VALUES ({placeholders})
            """,
            [[row.get(col) for col in insert_columns] for row in rows_to_insert],
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "imported",
        "photos_in_manifest": len(photos),
        "photos_inserted": len(rows_to_insert),
        "skipped_missing_session": skipped,
        "sessions_in_manifest": len(session_keys),
        "sessions_found": len(existing_sessions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export/import Cycle Monitor photo metadata without copying the full monitor DB."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export photo rows from an old monitor DB to JSON.")
    export_parser.add_argument("--source-db", required=True, help="Old monitor.db containing the photo metadata.")
    export_parser.add_argument("--out", required=True, help="Small JSON manifest to create.")
    export_parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="Session filter. Format: session_id, device|session_id, or device|session_id|mode.",
    )
    export_parser.add_argument(
        "--sessions-file",
        help="Text file with one session filter per line using the same format as --session.",
    )

    import_parser = subparsers.add_parser("import", help="Import photo rows into the current monitor DB.")
    import_parser.add_argument("--target-db", required=True, help="Current monitor.db on the VPS.")
    import_parser.add_argument("--manifest", required=True, help="JSON manifest produced by export.")
    import_parser.add_argument("--dry-run", action="store_true", help="Show what would be imported.")
    import_parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Do not delete existing photo rows for the restored sessions before inserting.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "export":
        sessions = _load_sessions(args.sessions_file, args.session)
        result = export_manifest(args.source_db, args.out, sessions)
        print(
            f"Exported {len(result['photos'])} photo rows for {len(result['sessions'])} sessions to {Path(args.out)}"
        )
    elif args.command == "import":
        result = import_manifest(
            args.target_db,
            args.manifest,
            dry_run=args.dry_run,
            replace=not args.keep_existing,
        )
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
