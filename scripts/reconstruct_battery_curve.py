from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Sample:
    session: str
    timestamp: datetime | None
    raw_timestamp: str
    ah: float
    voltage: float
    current_a: float
    speed_kph: float
    adjusted_ah: float = 0.0


@dataclass
class RestPoint:
    session: str
    timestamp: datetime | None
    raw_timestamp: str
    ah_used: float
    soc: float
    voltage: float
    voltage_std: float
    duration_s: float
    samples: int


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    clean = value.rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(clean)
    except ValueError:
        return None


def parse_raw_values(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    try:
        parts = raw.strip().split()
        if len(parts) < 5:
            return None
        ah = float(parts[0])
        voltage = float(parts[1])
        current_a = float(parts[2])
        speed_kph = float(parts[3])
        if not all(math.isfinite(v) for v in (ah, voltage, current_a, speed_kph)):
            return None
        return ah, voltage, current_a, speed_kph
    except Exception:
        return None


def load_samples(db_path: Path, selected_sessions: set[str] | None = None) -> dict[str, list[Sample]]:
    by_session: dict[str, list[Sample]] = {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT session, timestamp, raw
            FROM logs
            WHERE raw IS NOT NULL AND TRIM(raw) != ''
            ORDER BY session, id
            """
        ).fetchall()

    for session, timestamp, raw in rows:
        session = str(session or "")
        if selected_sessions and session not in selected_sessions:
            continue
        parsed = parse_raw_values(raw)
        if parsed is None:
            continue
        ah, voltage, current_a, speed_kph = parsed
        by_session.setdefault(session, []).append(
            Sample(
                session=session,
                timestamp=parse_timestamp(timestamp),
                raw_timestamp=str(timestamp or ""),
                ah=ah,
                voltage=voltage,
                current_a=current_a,
                speed_kph=speed_kph,
            )
        )
    return by_session


def adjust_ah_resets(samples: list[Sample]) -> None:
    if not samples:
        return
    offset = 0.0
    previous = samples[0].ah
    origin = samples[0].ah
    for sample in samples:
        if sample.ah < previous - 0.2:
            offset += previous
            origin = sample.ah
        sample.adjusted_ah = max(0.0, offset + sample.ah - origin)
        previous = sample.ah


def seconds_between(a: datetime | None, b: datetime | None, fallback_samples: int, fallback_hz: float) -> float:
    if a is not None and b is not None:
        delta = (b - a).total_seconds()
        if delta >= 0:
            return delta
    return fallback_samples / max(0.01, fallback_hz)


def stable_rest_points(
    samples: list[Sample],
    *,
    capacity_ah: float,
    max_speed_kph: float,
    max_abs_current_a: float,
    min_rest_seconds: float,
    tail_seconds: float,
    max_voltage_std: float,
    min_samples: int,
    fallback_hz: float,
) -> list[RestPoint]:
    adjust_ah_resets(samples)
    points: list[RestPoint] = []
    chunk: list[Sample] = []

    def flush() -> None:
        nonlocal chunk
        if len(chunk) < min_samples:
            chunk = []
            return
        duration = seconds_between(chunk[0].timestamp, chunk[-1].timestamp, len(chunk), fallback_hz)
        if duration < min_rest_seconds:
            chunk = []
            return

        tail: list[Sample] = []
        for sample in reversed(chunk):
            if not tail:
                tail.append(sample)
                continue
            span = seconds_between(sample.timestamp, tail[0].timestamp, len(tail), fallback_hz)
            if span > tail_seconds:
                break
            tail.append(sample)
        tail.reverse()
        if len(tail) < min_samples:
            tail = chunk

        voltages = [sample.voltage for sample in tail]
        voltage_std = statistics.pstdev(voltages) if len(voltages) > 1 else 0.0
        if voltage_std > max_voltage_std:
            chunk = []
            return

        voltage = statistics.median(voltages)
        ah_used = statistics.median(sample.adjusted_ah for sample in tail)
        soc = max(0.0, min(100.0, 100.0 * (1.0 - ah_used / max(0.001, capacity_ah))))
        points.append(
            RestPoint(
                session=tail[-1].session,
                timestamp=tail[-1].timestamp,
                raw_timestamp=tail[-1].raw_timestamp,
                ah_used=ah_used,
                soc=soc,
                voltage=voltage,
                voltage_std=voltage_std,
                duration_s=duration,
                samples=len(chunk),
            )
        )
        chunk = []

    for sample in samples:
        resting = sample.speed_kph <= max_speed_kph and abs(sample.current_a) <= max_abs_current_a
        if resting:
            chunk.append(sample)
        else:
            flush()
    flush()
    return points


def build_curve(points: list[RestPoint], *, bin_percent: float, min_points_per_bin: int) -> list[dict[str, float]]:
    bins: dict[float, list[RestPoint]] = {}
    for point in points:
        bucket = round(point.soc / bin_percent) * bin_percent
        bucket = max(0.0, min(100.0, bucket))
        bins.setdefault(bucket, []).append(point)

    curve = []
    for soc in sorted(bins):
        bucket_points = bins[soc]
        if len(bucket_points) < min_points_per_bin:
            continue
        curve.append(
            {
                "voltage": round(statistics.median(point.voltage for point in bucket_points), 3),
                "soc": round(soc, 3),
            }
        )

    by_soc: dict[float, dict[str, float]] = {}
    for point in curve:
        by_soc[point["soc"]] = point
    return [by_soc[soc] for soc in sorted(by_soc)]


def write_points_csv(points: list[RestPoint], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "session",
                "timestamp",
                "ah_used",
                "soc",
                "voltage",
                "voltage_std",
                "duration_s",
                "samples",
            ],
        )
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    "session": point.session,
                    "timestamp": point.raw_timestamp,
                    "ah_used": round(point.ah_used, 4),
                    "soc": round(point.soc, 3),
                    "voltage": round(point.voltage, 4),
                    "voltage_std": round(point.voltage_std, 5),
                    "duration_s": round(point.duration_s, 1),
                    "samples": point.samples,
                }
            )


def write_curve_json(curve: list[dict[str, float]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(curve, f, indent=2)
        f.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct a pack voltage/SOC curve from Cycle Analyst logs stored in a dashboard SQLite DB."
    )
    parser.add_argument("db", type=Path, help="Path to ride_data*.db")
    parser.add_argument("--capacity-ah", type=float, default=64.0, help="Pack capacity used to convert Ah consumed to SOC")
    parser.add_argument("--sessions", nargs="*", help="Optional session IDs to include")
    parser.add_argument("--output-json", type=Path, default=Path("battery_curve_from_db.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("battery_curve_points.csv"))
    parser.add_argument("--max-speed-kph", type=float, default=1.0)
    parser.add_argument("--max-abs-current-a", type=float, default=1.5)
    parser.add_argument("--min-rest-seconds", type=float, default=120.0)
    parser.add_argument("--tail-seconds", type=float, default=60.0)
    parser.add_argument("--max-voltage-std", type=float, default=0.05)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--fallback-hz", type=float, default=1.0)
    parser.add_argument("--bin-percent", type=float, default=5.0)
    parser.add_argument("--min-points-per-bin", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sessions = set(args.sessions) if args.sessions else None
    by_session = load_samples(args.db, sessions)

    all_points: list[RestPoint] = []
    for samples in by_session.values():
        all_points.extend(
            stable_rest_points(
                samples,
                capacity_ah=args.capacity_ah,
                max_speed_kph=args.max_speed_kph,
                max_abs_current_a=args.max_abs_current_a,
                min_rest_seconds=args.min_rest_seconds,
                tail_seconds=args.tail_seconds,
                max_voltage_std=args.max_voltage_std,
                min_samples=args.min_samples,
                fallback_hz=args.fallback_hz,
            )
        )

    curve = build_curve(all_points, bin_percent=args.bin_percent, min_points_per_bin=args.min_points_per_bin)
    write_points_csv(all_points, args.output_csv)
    write_curve_json(curve, args.output_json)

    session_count = len(by_session)
    print(f"Sessions read: {session_count}")
    print(f"Stable rest points: {len(all_points)}")
    print(f"Curve points: {len(curve)}")
    print(f"Wrote: {args.output_json}")
    print(f"Wrote: {args.output_csv}")
    if curve:
        print(f"SOC coverage: {curve[0]['soc']:.1f}% to {curve[-1]['soc']:.1f}%")
    else:
        print("No curve points produced. Try lowering --min-rest-seconds or raising --max-voltage-std.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
