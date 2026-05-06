from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.session_summary import compute_session_metrics


def _samples_for_session(
    conn: sqlite3.Connection,
    device_id: str,
    session_id: str,
    mode: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT timestamp, raw, user,
               user_id, user_initials, user_snapshot_json,
               gps_lat, gps_lon, gps_alt, terrain_alt_m, terrain_alt_source, terrain_alt_updated_at,
               gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
               solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c, solar_enabled
        FROM telemetry_samples
        WHERE device_id = ? AND session_id = ? AND mode = ?
        ORDER BY id
        """,
        (device_id, session_id, mode),
    ).fetchall()
    return [dict(row) for row in rows]


def _patch_metrics_json(raw: str | None, distance_km: float) -> str | None:
    if not raw:
        return raw
    try:
        metrics = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(metrics, dict):
        return raw
    metrics["distance_km"] = distance_km
    photo_capture = metrics.get("photo_capture")
    if isinstance(photo_capture, dict):
        last_trigger = photo_capture.get("last_trigger_distance_km")
        try:
            if last_trigger is not None and float(last_trigger) > distance_km + 5:
                photo_capture["last_trigger_distance_km"] = min(distance_km, float(last_trigger))
        except Exception:
            pass
    return json.dumps(metrics, separators=(",", ":"))


def recompute(
    db_path: Path,
    *,
    session_id: str | None,
    dry_run: bool,
    update_metrics_json: bool,
) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT id, device_id, session_id, mode, distance_km, duration_sec, avg_speed_kph, metrics_json
            FROM sessions
        """
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY start_ts, id"
        sessions = conn.execute(query, params).fetchall()

        checked = 0
        updated = 0
        for session in sessions:
            samples = _samples_for_session(
                conn,
                session["device_id"],
                session["session_id"],
                session["mode"],
            )
            if not samples:
                continue
            metrics = compute_session_metrics(samples)
            distance = metrics.get("distance")
            if distance is None:
                continue
            distance = float(distance)
            checked += 1
            current = session["distance_km"]
            changed = current is None or abs(float(current) - distance) >= 0.001
            if not changed:
                continue

            avg_speed = session["avg_speed_kph"]
            duration = session["duration_sec"]
            if duration and float(duration) > 0:
                avg_speed = distance / (float(duration) / 3600.0)
            metrics_json = session["metrics_json"]
            if update_metrics_json:
                metrics_json = _patch_metrics_json(metrics_json, distance)

            print(
                f"{session['device_id']} {session['mode']} {session['session_id']}: "
                f"{current} -> {distance:.4f} km"
            )
            if not dry_run:
                conn.execute(
                    """
                    UPDATE sessions
                    SET distance_km = ?, avg_speed_kph = ?, metrics_json = ?
                    WHERE id = ?
                    """,
                    (distance, avg_speed, metrics_json, session["id"]),
                )
            updated += 1
        if not dry_run:
            conn.commit()
    print(f"checked={checked} updated={updated} dry_run={dry_run}")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute monitor session distances from telemetry_samples.")
    parser.add_argument("--db", required=True, type=Path, help="Path to monitor SQLite DB.")
    parser.add_argument("--session", help="Optional session_id to recompute.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only prints a dry run.")
    parser.add_argument(
        "--update-metrics-json",
        action="store_true",
        help="Also patch sessions.metrics_json distance_km when present.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        parser.error(f"DB not found: {args.db}")
    recompute(
        args.db,
        session_id=args.session,
        dry_run=not args.apply,
        update_metrics_json=args.update_metrics_json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
