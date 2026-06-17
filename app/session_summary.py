from __future__ import annotations

import datetime
import math
from collections import defaultdict
from typing import Any, Callable, Iterable

from .distance import update_ca_distance

MAX_SUMMARY_DT_SECONDS = 5.0
MAX_GPS_DISTANCE_KPH = 250.0
GPS_DISTANCE_JUMP_FLOOR_KM = 0.05
GPS_DISTANCE_JUMP_MARGIN_KM = 0.05
GPS_UNTIMED_SEGMENT_LIMIT_KM = 1.0
ELEVATION_DEADBAND_M = 5.0
_ELEVATION_ANCHOR_KEY = "_elevation_anchor_m"
_RAW_GPS_ELEVATION_ANCHOR_KEY = "_raw_gps_elevation_anchor_m"


def parse_raw_values(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        parts = raw.strip().split()
        if len(parts) < 14:
            return None
        return [float(x) for x in parts[:14]]
    except Exception:
        return None


def parse_timestamp(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    clean = ts.rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(clean, fmt)
        except Exception:
            continue
    try:
        return datetime.datetime.fromisoformat(clean)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _non_negative(value: Any) -> float:
    number = _safe_float(value)
    return max(0.0, number or 0.0)


def _bounded_dt(last_ts: datetime.datetime | None, current_ts: datetime.datetime | None) -> float:
    if last_ts is None or current_ts is None:
        return 0.0
    dt = (current_ts - last_ts).total_seconds()
    if dt < 0 or dt > MAX_SUMMARY_DT_SECONDS:
        return 0.0
    return dt


def _valid_gps(lat: Any, lon: Any) -> tuple[float, float] | None:
    lat_f = _safe_float(lat)
    lon_f = _safe_float(lon)
    if lat_f is None or lon_f is None:
        return None
    if lat_f == 0 or lon_f == 0:
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def _sample_altitude(sample: dict[str, Any]) -> float | None:
    terrain_alt = _safe_float(sample.get("terrain_alt_m"))
    if terrain_alt is not None:
        return terrain_alt
    return _safe_float(sample.get("gps_alt"))


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def gps_segment_plausible(
    previous_sample: dict[str, Any] | None,
    current_sample: dict[str, Any],
    distance_km: float,
) -> bool:
    if previous_sample is None:
        return True

    previous_ts = parse_timestamp(previous_sample.get("timestamp"))
    current_ts = parse_timestamp(current_sample.get("timestamp"))
    if previous_ts is None or current_ts is None:
        return distance_km <= GPS_UNTIMED_SEGMENT_LIMIT_KM

    dt = (current_ts - previous_ts).total_seconds()
    if dt < 0:
        return False

    max_distance = (MAX_GPS_DISTANCE_KPH * dt / 3600.0) + GPS_DISTANCE_JUMP_MARGIN_KM
    return distance_km <= max(GPS_DISTANCE_JUMP_FLOOR_KM, max_distance)


def _sample_time_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[bool, datetime.datetime, int]:
    index, sample = item
    timestamp = parse_timestamp(sample.get("timestamp"))
    return timestamp is None, timestamp or datetime.datetime.max, index


def chronological_samples(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [sample for _, sample in sorted(enumerate(samples), key=_sample_time_sort_key)]


def filter_plausible_gps_samples(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    last_gps = None
    last_sample = None
    for sample in chronological_samples(samples):
        gps = _valid_gps(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is None:
            continue
        if last_gps is not None:
            distance_km = _haversine_km(last_gps, gps)
            if not gps_segment_plausible(last_sample, sample, distance_km):
                continue
        filtered.append(sample)
        last_gps = gps
        last_sample = sample
    return filtered


def _empty_metrics() -> dict[str, Any]:
    return {
        "sample_count": 0,
        "speed_sum": 0.0,
        "speed_max": 0.0,
        "speed_count": 0,
        "power_sum": 0.0,
        "power_max": float("-inf"),
        "power_min": float("inf"),
        "human_power_sum": 0.0,
        "human_power_max": 0.0,
        "human_power_count": 0,
        "solar_power_sum": 0.0,
        "solar_power_max": 0.0,
        "solar_power_count": 0,
        "solar_enabled": True,
        "positive_Wh": 0.0,
        "regen_Wh": 0.0,
        "human_Wh": 0.0,
        "solar_Wh": 0.0,
        "temp_sum": 0.0,
        "temp_max": 0.0,
        "temp_count": 0,
        "distance": 0.0,
        "Ah": 0.0,
        "ca_Ah_raw": 0.0,
        "duration": 0.0,
        "ca_reset_count": 0,
        "gps_points": 0,
        "gps_distance_km": 0.0,
        "gps_distance_rejected_count": 0,
        "gps_uphill_m": 0.0,
        "gps_downhill_m": 0.0,
        "gps_alt_min": None,
        "gps_alt_max": None,
        "raw_gps_uphill_m": 0.0,
        "raw_gps_downhill_m": 0.0,
        "raw_gps_alt_min": None,
        "raw_gps_alt_max": None,
        "gps_speed_sum": 0.0,
        "gps_speed_max": 0.0,
        "gps_speed_count": 0,
        "gps_fix_count": 0,
        "gps_fix_samples": 0,
        "gps_sats_sum": 0.0,
        "gps_sats_count": 0,
        "gps_hdop_sum": 0.0,
        "gps_hdop_count": 0,
        "solar_samples": 0,
    }


def _finalize_metrics(m: dict[str, Any]) -> dict[str, Any]:
    if m["power_max"] == float("-inf"):
        m["power_max"] = 0.0
    if m["power_min"] == float("inf"):
        m["power_min"] = 0.0
    m.pop(_ELEVATION_ANCHOR_KEY, None)
    m.pop(_RAW_GPS_ELEVATION_ANCHOR_KEY, None)
    return m


def _raw_ah_value(values: list[float] | None) -> float | None:
    if not values:
        return None
    return _safe_float(values[0])


def _add_ca_raw_ah_delta(
    m: dict[str, Any],
    values: list[float] | None,
    previous_values: list[float] | None,
) -> None:
    raw_ah = _raw_ah_value(values)
    previous_raw_ah = _raw_ah_value(previous_values)
    if raw_ah is None or previous_raw_ah is None:
        return
    delta = raw_ah - previous_raw_ah
    if delta >= 0:
        m["ca_Ah_raw"] += delta


def _add_elevation_sample(
    m: dict[str, Any],
    alt: Any,
    *,
    uphill_key: str = "gps_uphill_m",
    downhill_key: str = "gps_downhill_m",
    anchor_key: str = _ELEVATION_ANCHOR_KEY,
) -> None:
    alt_f = _safe_float(alt)
    if alt_f is None:
        return

    anchor = m.get(anchor_key)
    if anchor is None:
        m[anchor_key] = alt_f
        return

    diff = alt_f - float(anchor)
    if diff >= ELEVATION_DEADBAND_M:
        m[uphill_key] += diff
        m[anchor_key] = alt_f
    elif diff <= -ELEVATION_DEADBAND_M:
        m[downhill_key] += abs(diff)
        m[anchor_key] = alt_f


def _add_altitude_metrics(m: dict[str, Any], sample: dict[str, Any]) -> None:
    alt = _sample_altitude(sample)
    if alt is not None:
        m["gps_alt_min"] = alt if m["gps_alt_min"] is None else min(m["gps_alt_min"], alt)
        m["gps_alt_max"] = alt if m["gps_alt_max"] is None else max(m["gps_alt_max"], alt)
        _add_elevation_sample(m, alt)

    raw_alt = _safe_float(sample.get("gps_alt"))
    if raw_alt is not None:
        m["raw_gps_alt_min"] = raw_alt if m["raw_gps_alt_min"] is None else min(m["raw_gps_alt_min"], raw_alt)
        m["raw_gps_alt_max"] = raw_alt if m["raw_gps_alt_max"] is None else max(m["raw_gps_alt_max"], raw_alt)
        _add_elevation_sample(
            m,
            raw_alt,
            uphill_key="raw_gps_uphill_m",
            downhill_key="raw_gps_downhill_m",
            anchor_key=_RAW_GPS_ELEVATION_ANCHOR_KEY,
        )


def _sample_user(sample: dict[str, Any]) -> str | None:
    user = sample.get("user") or sample.get("user_initials")
    if user is None:
        return None
    user = str(user).strip()
    return user or None


def _sample_solar_enabled(sample: dict[str, Any]) -> bool:
    value = sample.get("solar_enabled")
    if value is None:
        return True
    try:
        return bool(int(value))
    except Exception:
        return bool(value)


def _solar_power_for_sample(sample: dict[str, Any], solar_enabled: bool) -> tuple[float, bool]:
    if not solar_enabled:
        return 0.0, False
    solar_a = _non_negative(sample.get("solar_current_a"))
    solar_v = _non_negative(sample.get("solar_bus_v"))
    solar_power = _safe_float(sample.get("solar_power_w"))
    if solar_power is None:
        solar_power = solar_v * solar_a
    has_solar = (
        sample.get("solar_current_a") is not None
        or sample.get("solar_bus_v") is not None
        or sample.get("solar_power_w") is not None
    )
    return max(0.0, solar_power), has_solar


def _add_instant_metrics(m: dict[str, Any], sample: dict[str, Any], values: list[float] | None, solar_power: float, has_solar: bool) -> None:
    m["sample_count"] += 1
    if has_solar:
        m["solar_samples"] += 1
        m["solar_power_sum"] += solar_power
        m["solar_power_count"] += 1
        m["solar_power_max"] = max(m["solar_power_max"], solar_power)

    if values:
        v = values[1]
        a = values[2]
        speed = values[3]
        temp = values[5]
        human_a = values[13]
        power = v * a
        human_power = v * human_a
        if speed >= 1:
            m["speed_sum"] += speed
            m["speed_count"] += 1
            m["speed_max"] = max(m["speed_max"], speed)
            m["power_sum"] += power
            m["power_max"] = max(m["power_max"], power)
            m["power_min"] = min(m["power_min"], power)
            m["human_power_sum"] += human_power
            m["human_power_count"] += 1
            m["human_power_max"] = max(m["human_power_max"], human_power)
            m["temp_sum"] += temp
            m["temp_count"] += 1
            m["temp_max"] = max(m["temp_max"], temp)

    gps = _valid_gps(sample.get("gps_lat"), sample.get("gps_lon"))
    if gps is not None:
        m["gps_points"] += 1
        _add_altitude_metrics(m, sample)
        gps_speed = _safe_float(sample.get("gps_speed_kph"))
        if gps_speed is not None and gps_speed >= 0:
            m["gps_speed_sum"] += gps_speed
            m["gps_speed_count"] += 1
            m["gps_speed_max"] = max(m["gps_speed_max"], gps_speed)

    gps_fix = sample.get("gps_fix")
    if gps_fix is not None:
        m["gps_fix_samples"] += 1
        if bool(gps_fix):
            m["gps_fix_count"] += 1
    sats = _safe_float(sample.get("gps_sats"))
    if sats is not None:
        m["gps_sats_sum"] += sats
        m["gps_sats_count"] += 1
    hdop = _safe_float(sample.get("gps_hdop"))
    if hdop is not None:
        m["gps_hdop_sum"] += hdop
        m["gps_hdop_count"] += 1


def _add_interval_metrics(
    m: dict[str, Any],
    sample: dict[str, Any],
    values: list[float] | None,
    previous_sample: dict[str, Any] | None,
    previous_values: list[float] | None,
    dt: float,
    solar_power: float,
) -> None:
    m["duration"] += dt
    _add_ca_raw_ah_delta(m, values, previous_values)
    if dt <= 0:
        return

    if values:
        v = values[1]
        a = values[2]
        raw_distance = values[4]
        human_a = values[13]
        power = v * a
        m["Ah"] += a * dt / 3600
        if abs(power) > 2:
            if a > 0:
                m["positive_Wh"] += power * dt / 3600
            elif a < 0:
                m["regen_Wh"] += abs(power) * dt / 3600
        m["human_Wh"] += (v * human_a) * dt / 3600
        if previous_values:
            previous_distance = previous_values[4]
            if raw_distance < previous_distance - 0.1:
                m["ca_reset_count"] += 1
            else:
                m["distance"] += max(0.0, raw_distance - previous_distance)

    m["solar_Wh"] += solar_power * dt / 3600

    gps = _valid_gps(sample.get("gps_lat"), sample.get("gps_lon"))
    previous_gps = _valid_gps(previous_sample.get("gps_lat"), previous_sample.get("gps_lon")) if previous_sample else None
    if gps is not None and previous_gps is not None:
        gps_distance = _haversine_km(previous_gps, gps)
        if gps_segment_plausible(previous_sample, sample, gps_distance):
            m["gps_distance_km"] += gps_distance
        else:
            m["gps_distance_rejected_count"] += 1


def compute_session_metrics(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    m = _empty_metrics()
    last_ts = None
    first_ts = None
    final_ts = None
    last_gps = None
    last_gps_sample = None
    previous_values = None

    for sample in chronological_samples(samples):
        values = parse_raw_values(sample.get("raw"))
        m["sample_count"] += 1
        current_ts = parse_timestamp(sample.get("timestamp"))
        if current_ts is not None:
            first_ts = first_ts or current_ts
            final_ts = current_ts
        dt = _bounded_dt(last_ts, current_ts)
        last_ts = current_ts or last_ts

        solar_enabled = _sample_solar_enabled(sample)
        if not solar_enabled:
            m["solar_enabled"] = False

        solar_power, has_solar = _solar_power_for_sample(sample, solar_enabled)
        if has_solar:
            m["solar_samples"] += 1
            m["solar_power_sum"] += solar_power
            m["solar_power_count"] += 1
            m["solar_power_max"] = max(m["solar_power_max"], solar_power)
            m["solar_Wh"] += solar_power * dt / 3600

        if values:
            v = values[1]
            a = values[2]
            speed = values[3]
            raw_distance = values[4]
            temp = values[5]
            human_a = values[13]
            power = v * a
            human_power = v * human_a

            update_ca_distance(
                m,
                raw_distance,
                now=current_ts,
                distance_key="distance",
                reset_count_key="ca_reset_count",
            )
            _add_ca_raw_ah_delta(m, values, previous_values)

            if speed >= 1:
                m["speed_sum"] += speed
                m["speed_count"] += 1
                m["speed_max"] = max(m["speed_max"], speed)
                m["power_sum"] += power
                m["power_max"] = max(m["power_max"], power)
                m["power_min"] = min(m["power_min"], power)
                m["human_power_sum"] += human_power
                m["human_power_count"] += 1
                m["human_power_max"] = max(m["human_power_max"], human_power)
                m["temp_sum"] += temp
                m["temp_count"] += 1
                m["temp_max"] = max(m["temp_max"], temp)

            m["Ah"] += a * dt / 3600
            if abs(power) > 2:
                if a > 0:
                    m["positive_Wh"] += power * dt / 3600
                elif a < 0:
                    m["regen_Wh"] += abs(power) * dt / 3600
            m["human_Wh"] += human_power * dt / 3600

        gps = _valid_gps(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is not None:
            m["gps_points"] += 1
            if last_gps is not None:
                gps_distance = _haversine_km(last_gps, gps)
                if gps_segment_plausible(last_gps_sample, sample, gps_distance):
                    m["gps_distance_km"] += gps_distance
                    last_gps = gps
                    last_gps_sample = sample
                else:
                    m["gps_distance_rejected_count"] += 1
            else:
                last_gps = gps
                last_gps_sample = sample

            _add_altitude_metrics(m, sample)

            gps_speed = _safe_float(sample.get("gps_speed_kph"))
            if gps_speed is not None and gps_speed >= 0:
                m["gps_speed_sum"] += gps_speed
                m["gps_speed_count"] += 1
                m["gps_speed_max"] = max(m["gps_speed_max"], gps_speed)

            gps_fix = sample.get("gps_fix")
            if gps_fix is not None:
                m["gps_fix_samples"] += 1
                if bool(gps_fix):
                    m["gps_fix_count"] += 1
            sats = _safe_float(sample.get("gps_sats"))
            if sats is not None:
                m["gps_sats_sum"] += sats
                m["gps_sats_count"] += 1
            hdop = _safe_float(sample.get("gps_hdop"))
            if hdop is not None:
                m["gps_hdop_sum"] += hdop
                m["gps_hdop_count"] += 1

        previous_values = values or previous_values

    if first_ts is not None and final_ts is not None:
        m["duration"] = max(0.0, (final_ts - first_ts).total_seconds())
    return _finalize_metrics(m)


def compute_timeline_metrics_by_user(samples: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ordered = chronological_samples(samples)
    metrics_by_user: dict[str, dict[str, Any]] = {}
    previous_sample = None
    previous_values = None
    previous_ts = None

    for sample in ordered:
        user = _sample_user(sample)
        values = parse_raw_values(sample.get("raw"))
        current_ts = parse_timestamp(sample.get("timestamp"))
        dt = _bounded_dt(previous_ts, current_ts)
        solar_enabled = _sample_solar_enabled(sample)
        solar_power, has_solar = _solar_power_for_sample(sample, solar_enabled)

        if user:
            m = metrics_by_user.setdefault(user, _empty_metrics())
            if not solar_enabled:
                m["solar_enabled"] = False
            _add_instant_metrics(m, sample, values, solar_power, has_solar)
            _add_interval_metrics(m, sample, values, previous_sample, previous_values, dt, solar_power)

        previous_sample = sample
        previous_values = values
        previous_ts = current_ts or previous_ts

    return {user: _finalize_metrics(metrics) for user, metrics in metrics_by_user.items()}


def group_samples_by_user(samples: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        user = sample.get("user") or sample.get("user_initials")
        if user:
            grouped[str(user)].append(sample)
    return dict(grouped)


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / max(denominator, 1e-6)


MetricFunc = Callable[[dict[str, Any]], float]
MetricSpec = tuple[str, str, MetricFunc]


SUMMARY_GROUPS: list[tuple[str, list[MetricSpec]]] = [
    (
        "Duration & distance",
        [
            ("Duration", "min", lambda m: m["duration"] / 60),
            ("CA distance", "km", lambda m: m["distance"]),
            ("GPS distance", "km", lambda m: m["gps_distance_km"]),
            ("GPS/CA delta", "km", lambda m: m["gps_distance_km"] - m["distance"]),
        ],
    ),
    (
        "Speed",
        [
            ("Avg CA speed", "km/h", lambda m: safe_div(m["speed_sum"], m["speed_count"])),
            ("Max CA speed", "km/h", lambda m: m["speed_max"]),
            ("Avg GPS speed", "km/h", lambda m: safe_div(m["gps_speed_sum"], m["gps_speed_count"])),
            ("Max GPS speed", "km/h", lambda m: m["gps_speed_max"]),
            (
                "Avg GPS/CA speed delta",
                "km/h",
                lambda m: safe_div(m["gps_speed_sum"], m["gps_speed_count"])
                - safe_div(m["speed_sum"], m["speed_count"]),
            ),
            ("Max GPS/CA speed delta", "km/h", lambda m: m["gps_speed_max"] - m["speed_max"]),
        ],
    ),
    (
        "Power",
        [
            ("Avg Power", "W", lambda m: safe_div(m["power_sum"], m["speed_count"])),
            ("Max Power", "W", lambda m: m["power_max"]),
            ("Min Power", "W", lambda m: m["power_min"]),
            ("Avg Human Power", "W", lambda m: safe_div(m["human_power_sum"], m["human_power_count"])),
            ("Max Human Power", "W", lambda m: m["human_power_max"]),
            ("Avg Solar Power", "W", lambda m: safe_div(m["solar_power_sum"], m["solar_power_count"])),
            ("Max Solar Power", "W", lambda m: m["solar_power_max"]),
        ],
    ),
    (
        "Energy",
        [
            ("Battery Used", "Ah", lambda m: m["Ah"]),
            ("CA Ah raw", "Ah", lambda m: m["ca_Ah_raw"]),
            ("Positive Energy", "Wh", lambda m: m["positive_Wh"]),
            ("Regen Energy", "Wh", lambda m: m["regen_Wh"]),
            ("Human Energy", "Wh", lambda m: m["human_Wh"]),
            ("Solar Energy", "Wh", lambda m: m["solar_Wh"]),
            ("Net Energy", "Wh", lambda m: m["positive_Wh"] - m["regen_Wh"] - m["human_Wh"] - m["solar_Wh"]),
        ],
    ),
    (
        "Efficiency",
        [
            ("Total Wh/km", "Wh/km", lambda m: safe_div(m["positive_Wh"], m["distance"])),
            ("Net Wh/km", "Wh/km", lambda m: safe_div(m["positive_Wh"] - m["regen_Wh"] - m["human_Wh"] - m["solar_Wh"], m["distance"])),
            ("Solar Wh/km", "Wh/km", lambda m: safe_div(m["solar_Wh"], m["distance"])),
        ],
    ),
    (
        "Percentages",
        [
            ("Regen", "%", lambda m: 100 * safe_div(m["regen_Wh"], m["positive_Wh"] + m["regen_Wh"])),
            ("Human", "%", lambda m: 100 * safe_div(m["human_Wh"], m["positive_Wh"] + m["regen_Wh"])),
            ("Solar", "%", lambda m: 100 * safe_div(m["solar_Wh"], m["positive_Wh"] + m["regen_Wh"])),
        ],
    ),
    (
        "GPS",
        [
            ("GPS points", "", lambda m: m["gps_points"]),
            ("GPS fix coverage", "%", lambda m: 100 * safe_div(m["gps_fix_count"], m["gps_fix_samples"])),
            ("Uphill", "m", lambda m: m["gps_uphill_m"]),
            ("Raw GPS uphill", "m", lambda m: m["raw_gps_uphill_m"]),
            ("Downhill", "m", lambda m: m["gps_downhill_m"]),
            ("Raw GPS downhill", "m", lambda m: m["raw_gps_downhill_m"]),
            ("Min altitude", "m", lambda m: m["gps_alt_min"] or 0.0),
            ("Max altitude", "m", lambda m: m["gps_alt_max"] or 0.0),
            ("Raw GPS min altitude", "m", lambda m: m["raw_gps_alt_min"] or 0.0),
            ("Raw GPS max altitude", "m", lambda m: m["raw_gps_alt_max"] or 0.0),
            ("Avg GPS satellites", "", lambda m: safe_div(m["gps_sats_sum"], m["gps_sats_count"])),
            ("Avg GPS HDOP", "", lambda m: safe_div(m["gps_hdop_sum"], m["gps_hdop_count"])),
        ],
    ),
    (
        "Temperature",
        [
            ("Avg Temp", "deg C", lambda m: safe_div(m["temp_sum"], m["temp_count"])),
            ("Max Temp", "deg C", lambda m: m["temp_max"]),
        ],
    ),
    (
        "Human effort",
        [
            ("Calories Burned", "kcal", lambda m: m["human_Wh"] * 1.433),
        ],
    ),
]


def format_metric_value(value: float, unit: str) -> str:
    if value is None or not math.isfinite(float(value)):
        return "-"
    if unit == "":
        return f"{value:.0f}" if abs(value - round(value)) < 0.01 else f"{value:.2f}"
    if unit == "%":
        return f"{value:.1f}%"
    if unit in {"m", "kcal"}:
        return f"{value:.0f} {unit}"
    if unit == "deg C":
        return f"{value:.1f} C"
    return f"{value:.2f} {unit}"


def build_summary_table(metrics_by_user: dict[str, dict[str, Any]], users: list[str]) -> list[list[str]]:
    table = [["Metric"] + users]
    for category, metrics in SUMMARY_GROUPS:
        table.append([f"-- {category} --"] + [""] * len(users))
        for label, unit, func in metrics:
            row = [label]
            for user in users:
                row.append(format_metric_value(func(metrics_by_user[user]), unit))
            table.append(row)
    return table


def build_summary_sections(metrics_by_user: dict[str, dict[str, Any]], users: list[str]) -> list[dict[str, Any]]:
    sections = []
    for category, metrics in SUMMARY_GROUPS:
        rows = []
        for label, unit, func in metrics:
            rows.append(
                {
                    "label": label,
                    "values": [
                        {
                            "user": user,
                            "value": format_metric_value(func(metrics_by_user[user]), unit),
                        }
                        for user in users
                    ],
                }
            )
        sections.append({"category": category, "rows": rows})
    return sections
