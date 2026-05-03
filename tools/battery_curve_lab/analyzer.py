from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
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
    distance_km: float
    adjusted_ah: float = 0.0

    @property
    def power_w(self) -> float:
        return self.voltage * self.current_a


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


def parse_raw_values(raw: str | None) -> tuple[float, float, float, float, float] | None:
    if not raw:
        return None
    try:
        parts = raw.strip().split()
        if len(parts) < 5:
            return None
        values = tuple(float(parts[i]) for i in range(5))
        if not all(math.isfinite(v) for v in values):
            return None
        return values
    except Exception:
        return None


def _duration_seconds(start: datetime | None, end: datetime | None) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def list_databases(root: Path) -> list[dict]:
    var_dir = root / "var"
    dbs = []
    if var_dir.exists():
        for path in sorted(var_dir.glob("*.db")):
            if path.stat().st_size <= 0:
                continue
            try:
                with sqlite3.connect(path) as conn:
                    has_logs = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'logs'"
                    ).fetchone()
                if not has_logs:
                    continue
            except Exception:
                continue
            dbs.append({
                "name": path.name,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
            })
    return dbs


def resolve_db(root: Path, value: str | None) -> Path:
    if not value:
        dbs = list_databases(root)
        if not dbs:
            raise FileNotFoundError("No .db file found in var/")
        return Path(dbs[0]["path"]).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def load_samples(db_path: Path, selected_sessions: set[str] | None = None) -> dict[str, list[Sample]]:
    by_session: dict[str, list[Sample]] = {}
    params: list[str] = []
    session_filter = ""
    if selected_sessions:
        params = sorted(selected_sessions)
        placeholders = ",".join("?" for _ in params)
        session_filter = f"AND session IN ({placeholders})"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT session, timestamp, raw
            FROM logs
            WHERE raw IS NOT NULL AND TRIM(raw) != ''
              {session_filter}
            ORDER BY session, id
            """,
            params,
        ).fetchall()

    for session, timestamp, raw in rows:
        session = str(session or "")
        if selected_sessions and session not in selected_sessions:
            continue
        parsed = parse_raw_values(raw)
        if parsed is None:
            continue
        ah, voltage, current_a, speed_kph, distance_km = parsed
        by_session.setdefault(session, []).append(
            Sample(
                session=session,
                timestamp=parse_timestamp(timestamp),
                raw_timestamp=str(timestamp or ""),
                ah=ah,
                voltage=voltage,
                current_a=current_a,
                speed_kph=speed_kph,
                distance_km=distance_km,
            )
        )
    return by_session


def summarize_sessions(db_path: Path) -> list[dict]:
    summaries = []
    by_session = load_samples(db_path)
    for session, samples in by_session.items():
        if not samples:
            continue
        start = samples[0].timestamp
        end = samples[-1].timestamp
        voltages = [s.voltage for s in samples]
        ahs = [s.ah for s in samples]
        distances = [s.distance_km for s in samples]
        powers = [s.power_w for s in samples]
        summaries.append({
            "session": session,
            "samples": len(samples),
            "start": samples[0].raw_timestamp,
            "end": samples[-1].raw_timestamp,
            "duration_min": round(_duration_seconds(start, end) / 60.0, 1),
            "voltage_min": round(min(voltages), 2),
            "voltage_max": round(max(voltages), 2),
            "ah_min": round(min(ahs), 2),
            "ah_max": round(max(ahs), 2),
            "distance_km": round(max(distances) - min(distances), 2),
            "power_min": round(min(powers), 1),
            "power_max": round(max(powers), 1),
        })
    return sorted(summaries, key=lambda item: item["session"], reverse=True)


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


def session_series(samples: list[Sample], max_points: int = 1600) -> dict:
    if not samples:
        return {"points": []}
    adjust_ah_resets(samples)
    step = max(1, math.ceil(len(samples) / max(1, max_points)))
    start_ts = samples[0].timestamp
    points = []
    for sample in samples[::step]:
        t = seconds_between(start_ts, sample.timestamp, len(points), 1.0)
        points.append({
            "time_min": round(t / 60.0, 3),
            "voltage": round(sample.voltage, 3),
            "ah": round(sample.adjusted_ah, 3),
            "power": round(sample.power_w, 3),
            "distance": round(max(0.0, sample.distance_km - samples[0].distance_km), 3),
            "speed": round(sample.speed_kph, 3),
            "current": round(sample.current_a, 3),
            "timestamp": sample.raw_timestamp,
        })
    return {"points": points, "total_samples": len(samples), "returned_samples": len(points)}


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
        curve.append({
            "voltage": round(statistics.median(point.voltage for point in bucket_points), 3),
            "soc": round(soc, 3),
            "points": len(bucket_points),
        })
    return curve


def point_to_dict(point: RestPoint) -> dict:
    data = asdict(point)
    data["timestamp"] = point.raw_timestamp
    return data


def python_curve_snippet(curve: list[dict[str, float]]) -> str:
    lines = ["DEFAULT_DISCHARGE_CURVE = ["]
    for point in curve:
        lines.append(f"    ({point['voltage']:.3f}, {point['soc']:.1f}),")
    lines.append("]")
    return "\n".join(lines)


def write_outputs(points: list[RestPoint], curve: list[dict[str, float]], output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "battery_curve_points.csv"
    json_path = output_dir / "battery_curve.json"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["session", "timestamp", "ah_used", "soc", "voltage", "voltage_std", "duration_s", "samples"],
        )
        writer.writeheader()
        for point in points:
            writer.writerow({
                "session": point.session,
                "timestamp": point.raw_timestamp,
                "ah_used": round(point.ah_used, 4),
                "soc": round(point.soc, 3),
                "voltage": round(point.voltage, 4),
                "voltage_std": round(point.voltage_std, 5),
                "duration_s": round(point.duration_s, 1),
                "samples": point.samples,
            })
    with json_path.open("w", encoding="utf-8") as f:
        json.dump([{"voltage": p["voltage"], "soc": p["soc"]} for p in curve], f, indent=2)
        f.write("\n")
    return {"csv": csv_path, "json": json_path}
