#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a short stationary solar-sensor slice from a Cycle app or monitor SQLite DB."
    )
    parser.add_argument("--db", required=True, help="Path to ride_data_*.db or monitor.db")
    parser.add_argument("--session", required=True, help="Session id, e.g. 2026-05-09_10-43-40")
    parser.add_argument("--mode", default=None, help="Monitor mode filter, e.g. supercycle_live")
    parser.add_argument("--device", default=None, help="Monitor device_id filter")
    parser.add_argument("--minutes", type=float, default=3.0, help="Slice length to export (default: 3)")
    parser.add_argument("--speed-max", type=float, default=0.5, help="Stationary threshold in km/h (default: 0.5)")
    parser.add_argument("--start", default=None, help="Optional start timestamp substring/ISO prefix")
    parser.add_argument("--out", default=None, help="Output CSV path")
    return parser


def parse_line(line: str | None):
    try:
        if not line:
            return None
        parts = re.split(r"\s+", line.strip())
        if len(parts) != 15:
            return None
        return [float(x) for x in parts[:14]] + [parts[14]]
    except Exception:
        return None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def pick_table(conn: sqlite3.Connection) -> tuple[str, str]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "telemetry_samples" in tables:
        return "telemetry_samples", "session_id"
    if "logs" in tables:
        return "logs", "session"
    raise SystemExit("No telemetry_samples or logs table found.")


def fetch_rows(conn: sqlite3.Connection, table: str, session_col: str, args: argparse.Namespace) -> list[sqlite3.Row]:
    cols = table_columns(conn, table)
    where = [f"{session_col} = ?"]
    params: list[object] = [args.session]
    if args.mode and "mode" in cols:
        where.append("mode = ?")
        params.append(args.mode)
    if args.device and "device_id" in cols:
        where.append("device_id = ?")
        params.append(args.device)
    if args.start:
        where.append("timestamp >= ?")
        params.append(args.start)

    query = f"""
        SELECT *
        FROM {table}
        WHERE {" AND ".join(where)}
        ORDER BY id
    """
    return conn.execute(query, params).fetchall()


def timestamp_seconds(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).timestamp()
        except Exception:
            pass
    return None


def sample_from_row(row: sqlite3.Row) -> dict:
    raw = row["raw"] if "raw" in row.keys() else None
    parsed = parse_line(raw) if raw else None
    speed = parsed[3] if parsed else row["gps_speed_kph"] if "gps_speed_kph" in row.keys() else None
    return {
        "timestamp": row["timestamp"],
        "t_seconds": timestamp_seconds(row["timestamp"]),
        "speed_kph": speed,
        "raw": raw,
        "solar_current_a": row["solar_current_a"] if "solar_current_a" in row.keys() else None,
        "solar_bus_v": row["solar_bus_v"] if "solar_bus_v" in row.keys() else None,
        "solar_shunt_v": row["solar_shunt_v"] if "solar_shunt_v" in row.keys() else None,
        "solar_power_w": row["solar_power_w"] if "solar_power_w" in row.keys() else None,
        "solar_temperature_c": row["solar_temperature_c"] if "solar_temperature_c" in row.keys() else None,
        "gps_lat": row["gps_lat"] if "gps_lat" in row.keys() else None,
        "gps_lon": row["gps_lon"] if "gps_lon" in row.keys() else None,
    }


def stationary_slice(samples: list[dict], minutes: float, speed_max: float) -> list[dict]:
    target_seconds = max(1.0, minutes * 60.0)
    best: list[dict] = []
    current: list[dict] = []

    for sample in samples:
        try:
            speed = float(sample["speed_kph"])
        except Exception:
            speed = 0.0
        if speed <= speed_max and sample["solar_current_a"] is not None:
            current.append(sample)
        else:
            if duration(current) > duration(best):
                best = current
            current = []
        if duration(current) >= target_seconds:
            return trim_to_duration(current, target_seconds)

    if duration(current) > duration(best):
        best = current
    return trim_to_duration(best, target_seconds)


def duration(samples: list[dict]) -> float:
    if len(samples) < 2:
        return 0.0
    start = samples[0].get("t_seconds")
    end = samples[-1].get("t_seconds")
    if start is None or end is None:
        return float(len(samples) - 1)
    return max(0.0, end - start)


def trim_to_duration(samples: list[dict], seconds: float) -> list[dict]:
    if not samples:
        return []
    start = samples[0].get("t_seconds")
    if start is None:
        return samples[: int(seconds) + 1]
    kept = []
    for sample in samples:
        t_seconds = sample.get("t_seconds")
        if t_seconds is not None and t_seconds - start > seconds:
            break
        kept.append(sample)
    return kept


def write_csv(samples: list[dict], out_path: Path) -> None:
    fields = [
        "timestamp",
        "speed_kph",
        "solar_current_a",
        "solar_bus_v",
        "solar_shunt_v",
        "solar_power_w",
        "solar_temperature_c",
        "gps_lat",
        "gps_lon",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for sample in samples:
            writer.writerow({field: sample.get(field) for field in fields})


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    out_path = Path(args.out or f"solar_noise_{args.session}.csv")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        table, session_col = pick_table(conn)
        rows = fetch_rows(conn, table, session_col, args)

    samples = [sample_from_row(row) for row in rows]
    selected = stationary_slice(samples, args.minutes, args.speed_max)
    if not selected:
        raise SystemExit("No stationary solar samples found for that session.")

    write_csv(selected, out_path)
    print(
        f"Exported {len(selected)} samples over {duration(selected):.1f}s "
        f"from {table} to {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
