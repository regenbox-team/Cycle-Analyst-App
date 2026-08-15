from __future__ import annotations

import json
import math
import os
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable


WEATHER_SOURCE = "open-meteo"
WEATHER_MODEL = os.getenv("MONITOR_WEATHER_MODEL", "best_match")
WEATHER_API_URL = os.getenv(
    "MONITOR_WEATHER_API_URL",
    "https://archive-api.open-meteo.com/v1/archive",
)
WEATHER_GRID_DECIMALS = int(os.getenv("MONITOR_WEATHER_GRID_DECIMALS", "1"))
WEATHER_BATCH_SIZE = max(1, min(50, int(os.getenv("MONITOR_WEATHER_BATCH_SIZE", "25"))))
WEATHER_TIMEOUT_SEC = float(os.getenv("MONITOR_WEATHER_TIMEOUT_SEC", "30"))

WINDOW_SECONDS = 120
WINDOW_MIN_VALID_SECONDS = 105
MAX_SAMPLE_DT_SECONDS = 5
MAX_SPEED_SD_KPH = 2.5
MAX_ALTITUDE_RMSE_M = 6.0
CALM_MAX_WIND_KPH = 10.0
CALM_MAX_HEADWIND_KPH = 5.0
CALM_MAX_GUST_KPH = 20.0
SPEED_TARGETS = (25.0, 27.5, 30.0, 32.5, 35.0)
SLOPE_TARGETS = tuple(float(value) for value in range(-10, 12, 2))


def ensure_weather_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_samples (
            source TEXT NOT NULL,
            model TEXT NOT NULL,
            grid_lat REAL NOT NULL,
            grid_lon REAL NOT NULL,
            observed_hour TEXT NOT NULL,
            wind_speed_kph REAL,
            wind_direction_deg REAL,
            wind_gust_kph REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source, model, grid_lat, grid_lon, observed_hour)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_weather_samples_lookup
        ON weather_samples(grid_lat, grid_lon, observed_hour)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_efficiency_profiles (
            project_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            dataset_signature TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, device_id, dataset_signature, parameters_json)
        )
        """
    )


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).rstrip("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _grid_value(value: float) -> float:
    return round(float(value), WEATHER_GRID_DECIMALS)


def _hour_key(value: datetime) -> str:
    return value.replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")


def _weather_requirements(conn, project_id: int, device_id: str | None = None) -> dict[str, set[tuple[float, float]]]:
    query = """
        SELECT t.timestamp, t.gps_lat, t.gps_lon
        FROM telemetry_samples t
        JOIN sessions s
          ON s.device_id = t.device_id AND s.session_id = t.session_id AND s.mode = t.mode
        WHERE s.travel_project_id = ?
          AND t.gps_lat IS NOT NULL AND t.gps_lon IS NOT NULL
    """
    params: list[Any] = [project_id]
    if device_id:
        query += " AND s.device_id = ?"
        params.append(device_id)
    requirements: dict[str, set[tuple[float, float]]] = defaultdict(set)
    for row in conn.execute(query, params):
        timestamp = _parse_ts(row["timestamp"])
        if timestamp is None:
            continue
        lat = float(row["gps_lat"])
        lon = float(row["gps_lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        requirements[timestamp.date().isoformat()].add((_grid_value(lat), _grid_value(lon)))
    return requirements


def _missing_weather_points(conn, date: str, points: set[tuple[float, float]]) -> list[tuple[float, float]]:
    missing = []
    for lat, lon in sorted(points):
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM weather_samples
            WHERE source = ? AND model = ? AND grid_lat = ? AND grid_lon = ?
              AND observed_hour >= ? AND observed_hour < ?
            """,
            (
                WEATHER_SOURCE,
                WEATHER_MODEL,
                lat,
                lon,
                f"{date}T00:00",
                (datetime.fromisoformat(date) + timedelta(days=1)).isoformat(timespec="minutes"),
            ),
        ).fetchone()
        if not row or int(row["count"]) < 24:
            missing.append((lat, lon))
    return missing


def fetch_open_meteo_day(points: list[tuple[float, float]], date: str) -> list[dict[str, Any]]:
    if not points:
        return []
    params = urllib.parse.urlencode(
        {
            "latitude": ",".join(str(point[0]) for point in points),
            "longitude": ",".join(str(point[1]) for point in points),
            "start_date": date,
            "end_date": date,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "wind_speed_unit": "kmh",
            "timezone": "GMT",
        }
    )
    request = urllib.request.Request(
        f"{WEATHER_API_URL}?{params}",
        headers={"Accept": "application/json", "User-Agent": "Cycle-Analyst-Monitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=WEATHER_TIMEOUT_SEC) as response:
        payload = json.loads(response.read().decode("utf-8"))
    payloads = payload if isinstance(payload, list) else [payload]
    return [item for item in payloads if isinstance(item, dict)]


def enrich_project_weather(
    conn,
    project_id: int,
    device_id: str | None = None,
    *,
    fetcher: Callable[[list[tuple[float, float]], str], list[dict[str, Any]]] = fetch_open_meteo_day,
) -> dict[str, Any]:
    ensure_weather_schema(conn)
    requirements = _weather_requirements(conn, project_id, device_id)
    inserted = 0
    requested_points = 0
    failures: list[str] = []
    fetched_at = datetime.utcnow().isoformat(timespec="seconds")
    for date, points in sorted(requirements.items()):
        missing = _missing_weather_points(conn, date, points)
        requested_points += len(missing)
        for offset in range(0, len(missing), WEATHER_BATCH_SIZE):
            batch = missing[offset : offset + WEATHER_BATCH_SIZE]
            try:
                payloads = fetcher(batch, date)
            except Exception as exc:
                failures.append(f"{date}: {exc}")
                continue
            for (lat, lon), payload in zip(batch, payloads):
                hourly = payload.get("hourly") or {}
                times = hourly.get("time") or []
                speeds = hourly.get("wind_speed_10m") or []
                directions = hourly.get("wind_direction_10m") or []
                gusts = hourly.get("wind_gusts_10m") or []
                for observed, speed, direction, gust in zip(times, speeds, directions, gusts):
                    conn.execute(
                        """
                        INSERT INTO weather_samples (
                            source, model, grid_lat, grid_lon, observed_hour,
                            wind_speed_kph, wind_direction_deg, wind_gust_kph, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source, model, grid_lat, grid_lon, observed_hour) DO UPDATE SET
                            wind_speed_kph = excluded.wind_speed_kph,
                            wind_direction_deg = excluded.wind_direction_deg,
                            wind_gust_kph = excluded.wind_gust_kph,
                            fetched_at = excluded.fetched_at
                        """,
                        (
                            WEATHER_SOURCE,
                            WEATHER_MODEL,
                            lat,
                            lon,
                            observed,
                            speed,
                            direction,
                            gust,
                            fetched_at,
                        ),
                    )
                    inserted += 1
    conn.commit()
    return {
        "dates": len(requirements),
        "zones": sum(len(points) for points in requirements.values()),
        "requested_zones": requested_points,
        "rows_written": inserted,
        "failures": failures,
        "source": WEATHER_SOURCE,
        "model": WEATHER_MODEL,
    }


def _weather_at(conn, timestamp: datetime, lat: float, lon: float) -> dict[str, float] | None:
    grid_lat, grid_lon = _grid_value(lat), _grid_value(lon)
    hour = timestamp.replace(minute=0, second=0, microsecond=0)
    rows = conn.execute(
        """
        SELECT observed_hour, wind_speed_kph, wind_direction_deg, wind_gust_kph
        FROM weather_samples
        WHERE source = ? AND model = ? AND grid_lat = ? AND grid_lon = ?
          AND observed_hour IN (?, ?)
        ORDER BY observed_hour
        """,
        (
            WEATHER_SOURCE,
            WEATHER_MODEL,
            grid_lat,
            grid_lon,
            _hour_key(hour),
            _hour_key(hour + timedelta(hours=1)),
        ),
    ).fetchall()
    if not rows:
        return None
    by_hour = {str(row["observed_hour"]): row for row in rows}
    first = by_hour.get(_hour_key(hour))
    second = by_hour.get(_hour_key(hour + timedelta(hours=1))) or first
    if first is None:
        return None
    fraction = (timestamp - hour).total_seconds() / 3600.0
    speed1 = float(first["wind_speed_kph"] or 0.0)
    speed2 = float(second["wind_speed_kph"] or speed1)
    direction1 = math.radians(float(first["wind_direction_deg"] or 0.0))
    direction2 = math.radians(float(second["wind_direction_deg"] or 0.0))
    east = math.sin(direction1) * speed1 + fraction * (
        math.sin(direction2) * speed2 - math.sin(direction1) * speed1
    )
    north = math.cos(direction1) * speed1 + fraction * (
        math.cos(direction2) * speed2 - math.cos(direction1) * speed1
    )
    gust1 = float(first["wind_gust_kph"] or 0.0)
    gust2 = float(second["wind_gust_kph"] or gust1)
    return {
        "speed": math.hypot(east, north),
        "direction": math.degrees(math.atan2(east, north)) % 360.0,
        "gust": gust1 + fraction * (gust2 - gust1),
    }


def _parse_raw(raw: Any) -> list[float] | None:
    try:
        values = [float(value) for value in str(raw).split()[:14]]
        return values if len(values) >= 14 else None
    except (TypeError, ValueError):
        return None


def _circular_mean(values: list[float]) -> float | None:
    if not values:
        return None
    east = sum(math.sin(math.radians(value)) for value in values)
    north = sum(math.cos(math.radians(value)) for value in values)
    return math.degrees(math.atan2(east, north)) % 360.0


def _window_profile(bucket: list[dict[str, Any]]) -> dict[str, Any] | None:
    distance = positive = regen = human = duration = cumulative = 0.0
    speeds: list[float] = []
    altitudes: list[tuple[float, float]] = []
    positions: list[tuple[float, float]] = []
    headings: list[float] = []
    for previous, current in zip(bucket, bucket[1:]):
        t0 = _parse_ts(previous.get("timestamp"))
        t1 = _parse_ts(current.get("timestamp"))
        gps_speed = previous.get("gps_speed_kph")
        values = _parse_raw(previous.get("raw"))
        if t0 is None or t1 is None or values is None or gps_speed is None:
            continue
        if not previous.get("gps_fix"):
            continue
        hdop = previous.get("gps_hdop")
        if hdop is not None and float(hdop) > 3.0:
            continue
        dt = (t1 - t0).total_seconds()
        if not 0 < dt <= MAX_SAMPLE_DT_SECONDS:
            continue
        speed = float(gps_speed)
        if not 0 <= speed <= 80:
            continue
        delta = speed * dt / 3600.0
        distance += delta
        cumulative += delta
        power = values[1] * values[2]
        human_power = values[1] * values[13]
        positive += max(0.0, power) * dt / 3600.0
        regen += max(0.0, -power) * dt / 3600.0
        human += max(0.0, human_power) * dt / 3600.0
        duration += dt
        if speed > 5:
            speeds.append(speed)
            if previous.get("gps_track_deg") is not None:
                headings.append(float(previous["gps_track_deg"]) % 360.0)
        altitude = previous.get("terrain_alt_m")
        if altitude is None:
            altitude = previous.get("gps_alt")
        if altitude is not None:
            altitudes.append((cumulative, float(altitude)))
        if previous.get("gps_lat") is not None and previous.get("gps_lon") is not None:
            positions.append((float(previous["gps_lat"]), float(previous["gps_lon"])))
    if distance < 0.3 or duration < WINDOW_MIN_VALID_SECONDS or not speeds or len(altitudes) < 10 or not positions:
        return None
    mean_distance = statistics.mean(point[0] for point in altitudes)
    mean_altitude = statistics.mean(point[1] for point in altitudes)
    denominator = sum((point[0] - mean_distance) ** 2 for point in altitudes)
    if denominator <= 0:
        return None
    coefficient = sum(
        (point[0] - mean_distance) * (point[1] - mean_altitude) for point in altitudes
    ) / denominator
    intercept = mean_altitude - coefficient * mean_distance
    altitude_rmse = math.sqrt(
        sum((altitude - (intercept + coefficient * distance_km)) ** 2 for distance_km, altitude in altitudes)
        / len(altitudes)
    )
    return {
        "timestamp": _parse_ts(bucket[len(bucket) // 2].get("timestamp")),
        "lat": statistics.median(point[0] for point in positions),
        "lon": statistics.median(point[1] for point in positions),
        "heading": _circular_mean(headings),
        "speed": statistics.mean(speeds),
        "speed_sd": statistics.pstdev(speeds),
        "distance": distance,
        "duration_min": duration / 60.0,
        "gradient": coefficient / 10.0,
        "altitude_rmse": altitude_rmse,
        "efficiency": (positive - regen - human) / distance,
    }


def _weighted_stats(windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    distance = sum(window["distance"] for window in windows)
    if distance <= 0:
        return None
    mean = sum(window["efficiency"] * window["distance"] for window in windows) / distance
    deviation = math.sqrt(
        sum(window["distance"] * (window["efficiency"] - mean) ** 2 for window in windows) / distance
    )
    avg_speed = sum(window["speed"] * window["distance"] for window in windows) / distance
    return {
        "mean_wh_km": round(mean, 3),
        "stddev_wh_km": round(deviation, 3),
        "avg_speed_kph": round(avg_speed, 3),
        "duration_min": round(sum(window["duration_min"] for window in windows), 1),
        "distance_km": round(distance, 3),
        "sequence_count": len(windows),
        "session_count": len({window["session_id"] for window in windows}),
    }


def build_efficiency_profile(conn, project_id: int, device_id: str) -> dict[str, Any]:
    ensure_weather_schema(conn)
    rows = conn.execute(
        """
        SELECT t.timestamp, t.raw, t.gps_lat, t.gps_lon, t.gps_alt, t.terrain_alt_m,
               t.gps_speed_kph, t.gps_track_deg, t.gps_fix, t.gps_hdop, t.session_id
        FROM telemetry_samples t
        JOIN sessions s
          ON s.device_id = t.device_id AND s.session_id = t.session_id AND s.mode = t.mode
        WHERE s.travel_project_id = ? AND s.device_id = ?
        ORDER BY t.session_id, t.timestamp, t.id
        """,
        (project_id, device_id),
    ).fetchall()
    windows: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    bucket_start: datetime | None = None
    current_session = None
    for row in rows:
        sample = dict(row)
        timestamp = _parse_ts(sample.get("timestamp"))
        if timestamp is None:
            continue
        if current_session != sample["session_id"]:
            bucket = []
            bucket_start = timestamp
            current_session = sample["session_id"]
        if bucket_start is not None and (timestamp - bucket_start).total_seconds() >= WINDOW_SECONDS and bucket:
            window = _window_profile(bucket)
            if window:
                window["session_id"] = current_session
                weather = _weather_at(conn, window["timestamp"], window["lat"], window["lon"])
                if weather and window["heading"] is not None:
                    angle = math.radians(weather["direction"] - window["heading"])
                    window["wind_speed"] = weather["speed"]
                    window["headwind"] = weather["speed"] * math.cos(angle)
                    window["crosswind"] = abs(weather["speed"] * math.sin(angle))
                    window["wind_gust"] = weather["gust"]
                    window["calm"] = (
                        weather["speed"] <= CALM_MAX_WIND_KPH
                        and abs(window["headwind"]) <= CALM_MAX_HEADWIND_KPH
                        and weather["gust"] <= CALM_MAX_GUST_KPH
                    )
                windows.append(window)
            bucket = []
            bucket_start = timestamp
        bucket.append(sample)

    stable = [
        window for window in windows
        if window["speed_sd"] <= MAX_SPEED_SD_KPH
        and window["altitude_rmse"] <= MAX_ALTITUDE_RMSE_M
        and -40 < window["efficiency"] < 130
    ]
    calm = [window for window in stable if window.get("calm")]
    speed_profile = []
    for target in SPEED_TARGETS:
        selected = [
            window for window in calm
            if target - 1.25 <= window["speed"] < target + 1.25 and abs(window["gradient"]) <= 0.5
        ]
        speed_profile.append({"target": target, "stats": _weighted_stats(selected)})
    slope_profile = []
    for target in SLOPE_TARGETS:
        selected = [
            window for window in calm
            if target - 1 <= window["gradient"] < target + 1 and 25 <= window["speed"] <= 35
        ]
        stats = _weighted_stats(selected)
        if stats:
            slope_profile.append({"target": target, "stats": stats})
    weather_windows = sum(1 for window in windows if "wind_speed" in window)
    return {
        "project_id": project_id,
        "device_id": device_id,
        "parameters": {
            "window_seconds": WINDOW_SECONDS,
            "max_speed_sd_kph": MAX_SPEED_SD_KPH,
            "calm_max_wind_kph": CALM_MAX_WIND_KPH,
            "calm_max_headwind_kph": CALM_MAX_HEADWIND_KPH,
            "calm_max_gust_kph": CALM_MAX_GUST_KPH,
        },
        "coverage": {
            "window_count": len(windows),
            "weather_window_count": weather_windows,
            "calm_window_count": len(calm),
            "weather_percent": round(100 * weather_windows / len(windows), 1) if windows else 0.0,
        },
        "speed": speed_profile,
        "slope": slope_profile,
    }
