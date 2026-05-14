from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import (
    SOLAR_BATTERY_CURVE_FILE,
    SOLAR_BATTERY_STATE_FILE,
    SOLAR_LOCATION_LAT,
    SOLAR_LOCATION_LON,
    SOLAR_PANEL_MAX_W,
    SOLAR_PROFILE_FILE,
    VEHICLE_CONFIGS,
)


DEFAULT_DISCHARGE_CURVE = [
    (43.820, 10.0),
    (46.610, 40.0),
    (47.345, 45.0),
    (47.625, 50.0),
    (48.620, 55.0),
    (50.360, 70.0),
    (51.535, 80.0),
    (52.430, 90.0),
    (53.740, 100.0),
]


def _vehicle_config() -> dict:
    try:
        from . import modes
        mode = getattr(modes, "vehicle_mode", None)
    except Exception:
        mode = None
    return VEHICLE_CONFIGS.get(mode, {})


def battery_nominal_voltage() -> float:
    try:
        return float(os.getenv("APP_BATTERY_NOMINAL_VOLTAGE") or _vehicle_config().get("battery_nominal_voltage") or 48.1)
    except Exception:
        return 48.1


def battery_capacity_wh(capacity_ah: float | int | None = None) -> float:
    if capacity_ah is None:
        capacity_ah = _vehicle_config().get("battery_capacity_ah", 64)
    try:
        return max(0.0, float(capacity_ah) * battery_nominal_voltage())
    except Exception:
        return 0.0


def select_estimation_voltage(ca_voltage: float | int | None, solar_voltage: float | int | None = None) -> tuple[float | None, str]:
    try:
        cv = float(ca_voltage)
        if cv > 0:
            return cv, "cycle_analyst"
    except Exception:
        pass
    try:
        sv = float(solar_voltage)
        if sv > 0:
            return sv, "solar_sensor"
    except Exception:
        pass
    return None, "none"


def _normalize_curve_point(point) -> tuple[float, float] | None:
    if isinstance(point, dict):
        voltage = point.get("voltage")
        soc = point.get("soc")
    elif isinstance(point, (list, tuple)) and len(point) >= 2:
        voltage, soc = point[0], point[1]
    else:
        return None
    try:
        return float(voltage), float(soc)
    except Exception:
        return None


def load_discharge_curve() -> list[tuple[float, float]]:
    path = (SOLAR_BATTERY_CURVE_FILE or "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            points = [_normalize_curve_point(point) for point in raw]
            curve = sorted(point for point in points if point is not None)
            if len(curve) >= 2:
                return curve
        except Exception:
            pass
    return list(DEFAULT_DISCHARGE_CURVE)


def soc_from_voltage(voltage: float | int | None, curve: list[tuple[float, float]] | None = None) -> float | None:
    try:
        v = float(voltage)
    except Exception:
        return None
    curve = sorted(curve or load_discharge_curve())
    if not curve:
        return None
    if v <= curve[0][0]:
        return curve[0][1]
    if v >= curve[-1][0]:
        return curve[-1][1]
    for (v0, soc0), (v1, soc1) in zip(curve, curve[1:]):
        if v0 <= v <= v1:
            ratio = (v - v0) / max(1e-9, v1 - v0)
            return soc0 + (soc1 - soc0) * ratio
    return None


def load_state() -> dict:
    try:
        with open(SOLAR_BATTERY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SOLAR_BATTERY_STATE_FILE), exist_ok=True)
        with open(SOLAR_BATTERY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _now_paris() -> datetime:
    return datetime.now(ZoneInfo("Europe/Paris"))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _solar_profile_path() -> str:
    return os.path.expanduser(SOLAR_PROFILE_FILE)


def _normalize_profile_point(point) -> dict | None:
    if not isinstance(point, dict):
        return None
    hour = point.get("hour")
    power = point.get("power_w", point.get("w"))
    try:
        hour = float(hour)
        power = max(0.0, float(power))
    except Exception:
        return None
    if not math.isfinite(hour) or not math.isfinite(power):
        return None
    if hour < 0 or hour > 24:
        return None
    total_minutes = max(0, min(1440, int(round(hour * 60))))
    time_label = "24:00" if total_minutes >= 1440 else f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    return {
        "hour": round(hour, 4),
        "time": time_label,
        "power_w": round(power, 3),
    }


def load_imported_solar_profile() -> dict | None:
    path = _solar_profile_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    points = [_normalize_profile_point(point) for point in raw.get("points", []) if isinstance(point, dict)]
    points = sorted((point for point in points if point is not None), key=lambda point: point["hour"])
    if len(points) < 2:
        return None
    return {
        "type": raw.get("type") or "cycle_analyst_solar_profile",
        "version": int(raw.get("version") or 1),
        "name": raw.get("name") or "Imported solar profile",
        "created_at": raw.get("created_at"),
        "source": raw.get("source") or "settings_import",
        "panel_max_w": raw.get("panel_max_w"),
        "reference": raw.get("reference") if isinstance(raw.get("reference"), dict) else {},
        "points": points,
    }


def imported_solar_profile_status() -> dict:
    profile = load_imported_solar_profile()
    path = _solar_profile_path()
    if not profile:
        return {"enabled": False, "path": path}
    peak = max(point["power_w"] for point in profile["points"])
    return {
        "enabled": True,
        "path": path,
        "name": profile["name"],
        "created_at": profile.get("created_at"),
        "point_count": len(profile["points"]),
        "peak_w": peak,
        "panel_max_w": profile.get("panel_max_w"),
    }


def save_imported_solar_profile(profile: dict) -> dict:
    if not isinstance(profile, dict):
        raise ValueError("profile must be a JSON object")
    points = [_normalize_profile_point(point) for point in profile.get("points", []) if isinstance(point, dict)]
    points = sorted((point for point in points if point is not None), key=lambda point: point["hour"])
    if len(points) < 2:
        raise ValueError("profile must contain at least two valid points")
    payload = {
        "type": profile.get("type") or "cycle_analyst_solar_profile",
        "version": int(profile.get("version") or 1),
        "name": profile.get("name") or "Imported solar profile",
        "created_at": profile.get("created_at") or _now_paris().isoformat(),
        "source": profile.get("source") or "settings_import",
        "panel_max_w": profile.get("panel_max_w"),
        "reference": profile.get("reference") if isinstance(profile.get("reference"), dict) else {},
        "points": points,
    }
    path = _solar_profile_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    return imported_solar_profile_status()


def delete_imported_solar_profile() -> bool:
    path = _solar_profile_path()
    if not path or not os.path.exists(path):
        return False
    os.remove(path)
    return True


def _imported_solar_power_at_hour(profile: dict, hour: float) -> float:
    points = profile.get("points") or []
    if not points:
        return 0.0
    hour = _clamp(float(hour), 0.0, 24.0)
    if hour <= points[0]["hour"]:
        return float(points[0]["power_w"])
    if hour >= points[-1]["hour"]:
        return float(points[-1]["power_w"])
    for previous, next_point in zip(points, points[1:]):
        if hour <= next_point["hour"]:
            span = max(1e-9, float(next_point["hour"]) - float(previous["hour"]))
            ratio = (hour - float(previous["hour"])) / span
            return float(previous["power_w"]) + (float(next_point["power_w"]) - float(previous["power_w"])) * ratio
    return 0.0


def initialize_solar_session(
    session_metrics: dict,
    voltage: float | int | None,
    capacity_ah: float | int | None = None,
    *,
    solar_voltage: float | int | None = None,
) -> dict:
    capacity_wh = battery_capacity_wh(capacity_ah)
    voltage, voltage_source = select_estimation_voltage(voltage, solar_voltage)
    stored = load_state()
    voltage_soc = soc_from_voltage(voltage)
    voltage_wh = capacity_wh * voltage_soc / 100.0 if voltage_soc is not None else None
    stored_wh = stored.get("remaining_wh")
    try:
        stored_wh = float(stored_wh)
    except Exception:
        stored_wh = None

    source = "capacity"
    confidence = 0.35
    if stored_wh is not None and capacity_wh > 0:
        start_wh = _clamp(stored_wh, 0.0, capacity_wh)
        source = "previous_state"
        confidence = float(stored.get("confidence") or 0.65)
        if voltage_wh is not None:
            start_wh = (start_wh * 0.7) + (voltage_wh * 0.3)
            source = "previous_state_voltage_corrected"
            confidence = max(confidence, 0.7)
    elif voltage_wh is not None:
        start_wh = voltage_wh
        source = "voltage_curve"
        confidence = 0.55
    else:
        start_wh = capacity_wh

    start_wh = _clamp(start_wh, 0.0, capacity_wh) if capacity_wh > 0 else 0.0
    session_metrics["solar_battery_start_wh"] = start_wh
    session_metrics["solar_battery_capacity_wh"] = capacity_wh
    session_metrics["solar_battery_start_soc"] = (100.0 * start_wh / capacity_wh) if capacity_wh > 0 else 0.0
    session_metrics["solar_battery_estimate_source"] = source
    session_metrics["solar_battery_voltage_source"] = voltage_source
    session_metrics["solar_battery_confidence"] = _clamp(confidence, 0.0, 1.0)
    return build_estimate(session_metrics, voltage, capacity_ah)


def net_session_battery_use_wh(session_metrics: dict) -> float:
    return (
        float(session_metrics.get("positive_Wh") or 0.0)
        - float(session_metrics.get("regen_Wh") or 0.0)
        - float(session_metrics.get("human_Wh") or 0.0)
        - float(session_metrics.get("solar_Wh") or 0.0)
    )


def _solar_declination_rad(day_of_year: int) -> float:
    return math.radians(23.44) * math.sin(math.radians((360.0 / 365.0) * (day_of_year - 81)))


def _solar_time_hours(when: datetime, longitude: float) -> float:
    day = when.timetuple().tm_yday
    b = math.radians((360.0 / 365.0) * (day - 81))
    equation_minutes = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
    tz_hours = (when.utcoffset() or timedelta()).total_seconds() / 3600.0
    local_standard_meridian = 15.0 * tz_hours
    correction_minutes = 4.0 * (longitude - local_standard_meridian) + equation_minutes
    return when.hour + when.minute / 60.0 + when.second / 3600.0 + correction_minutes / 60.0


def theoretical_solar_power_w(
    when: datetime | None = None,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    panel_max_w: float | None = None,
) -> float:
    when = when or _now_paris()
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("Europe/Paris"))
    imported_profile = load_imported_solar_profile()
    if imported_profile:
        hour = when.hour + when.minute / 60.0 + when.second / 3600.0
        return _imported_solar_power_at_hour(imported_profile, hour)
    latitude = SOLAR_LOCATION_LAT if latitude is None else latitude
    longitude = SOLAR_LOCATION_LON if longitude is None else longitude
    panel_max_w = SOLAR_PANEL_MAX_W if panel_max_w is None else panel_max_w

    lat_rad = math.radians(latitude)
    decl = _solar_declination_rad(when.timetuple().tm_yday)
    hour_angle = math.radians(15.0 * (_solar_time_hours(when, longitude) - 12.0))
    sin_elevation = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle)
    return max(0.0, float(panel_max_w) * max(0.0, sin_elevation))


def potential_solar_wh_remaining_today(
    when: datetime | None = None,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    panel_max_w: float | None = None,
    step_minutes: int = 5,
) -> float:
    when = when or _now_paris()
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("Europe/Paris"))
    step_minutes = max(1, int(step_minutes))
    end = when.replace(hour=23, minute=59, second=59, microsecond=0)
    total = 0.0
    cursor = when
    step = timedelta(minutes=step_minutes)
    while cursor < end:
        next_cursor = min(cursor + step, end)
        midpoint = cursor + (next_cursor - cursor) / 2
        hours = (next_cursor - cursor).total_seconds() / 3600.0
        total += theoretical_solar_power_w(
            midpoint,
            latitude=latitude,
            longitude=longitude,
            panel_max_w=panel_max_w,
        ) * hours
        cursor = next_cursor
    return max(0.0, total)


def solar_power_profile_today(
    when: datetime | None = None,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    panel_max_w: float | None = None,
    step_minutes: int = 60,
) -> dict:
    when = when or _now_paris()
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("Europe/Paris"))
    imported_profile = load_imported_solar_profile()
    if imported_profile:
        step_minutes = max(15, min(120, int(step_minutes)))
        day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        points = []
        for minute in range(0, 1441, step_minutes):
            hour = minute / 60.0
            points.append({
                "hour": round(hour, 3),
                "time": "24:00" if hour >= 24.0 else f"{minute // 60:02d}:{minute % 60:02d}",
                "power_w": round(_imported_solar_power_at_hour(imported_profile, hour), 1),
            })
        now_hour = (when - day_start).total_seconds() / 3600.0
        return {
            "points": points,
            "now_hour": round(_clamp(now_hour, 0.0, 24.0), 3),
            "date": day_start.date().isoformat(),
            "panel_max_w": imported_profile.get("panel_max_w"),
            "imported_profile": True,
            "name": imported_profile.get("name"),
        }
    latitude = SOLAR_LOCATION_LAT if latitude is None else latitude
    longitude = SOLAR_LOCATION_LON if longitude is None else longitude
    panel_max_w = SOLAR_PANEL_MAX_W if panel_max_w is None else panel_max_w
    step_minutes = max(15, min(120, int(step_minutes)))

    day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(hours=24)
    points = []
    cursor = day_start
    step = timedelta(minutes=step_minutes)
    while cursor <= day_end:
        hour = (cursor - day_start).total_seconds() / 3600.0
        label = "24:00" if hour >= 24.0 else cursor.strftime("%H:%M")
        points.append({
            "hour": round(hour, 3),
            "time": label,
            "power_w": round(theoretical_solar_power_w(
                cursor,
                latitude=latitude,
                longitude=longitude,
                panel_max_w=panel_max_w,
            ), 1),
        })
        cursor += step

    now_hour = (when - day_start).total_seconds() / 3600.0
    return {
        "points": points,
        "now_hour": round(_clamp(now_hour, 0.0, 24.0), 3),
        "date": day_start.date().isoformat(),
        "panel_max_w": float(panel_max_w),
    }


def _gps_or_config_location(gps_state: dict | None) -> tuple[float, float, str]:
    gps_state = gps_state or {}
    if gps_state.get("has_fix") and gps_state.get("lat") is not None and gps_state.get("lon") is not None:
        try:
            return float(gps_state["lat"]), float(gps_state["lon"]), "gps"
        except Exception:
            pass
    return SOLAR_LOCATION_LAT, SOLAR_LOCATION_LON, "config"


def build_estimate(
    session_metrics: dict,
    voltage: float | int | None,
    capacity_ah: float | int | None = None,
    *,
    solar_voltage: float | int | None = None,
    gps_state: dict | None = None,
    when: datetime | None = None,
) -> dict:
    voltage, voltage_source = select_estimation_voltage(voltage, solar_voltage)
    capacity_wh = float(session_metrics.get("solar_battery_capacity_wh") or battery_capacity_wh(capacity_ah))
    start_wh = session_metrics.get("solar_battery_start_wh")
    if start_wh is None:
        voltage_soc = soc_from_voltage(voltage)
        start_wh = capacity_wh * voltage_soc / 100.0 if voltage_soc is not None else capacity_wh
    try:
        start_wh = float(start_wh)
    except Exception:
        start_wh = capacity_wh

    used_wh = net_session_battery_use_wh(session_metrics)
    remaining_wh = _clamp(start_wh - used_wh, 0.0, capacity_wh) if capacity_wh > 0 else 0.0
    percent = 100.0 * remaining_wh / capacity_wh if capacity_wh > 0 else 0.0
    voltage_soc = soc_from_voltage(voltage)
    lat, lon, location_source = _gps_or_config_location(gps_state)
    when = when or _now_paris()
    potential_wh = potential_solar_wh_remaining_today(when, latitude=lat, longitude=lon)
    power_now = theoretical_solar_power_w(when, latitude=lat, longitude=lon)
    power_profile = solar_power_profile_today(when, latitude=lat, longitude=lon)

    return {
        "enabled": True,
        "capacity_wh": capacity_wh,
        "start_wh": start_wh,
        "used_wh": used_wh,
        "remaining_wh": remaining_wh,
        "percent": percent,
        "voltage_used": voltage,
        "voltage_source": voltage_source,
        "voltage_soc_percent": voltage_soc,
        "confidence": float(session_metrics.get("solar_battery_confidence") or 0.5),
        "source": session_metrics.get("solar_battery_estimate_source") or "session",
        "potential_power_now_w": power_now,
        "potential_remaining_today_wh": potential_wh,
        "solar_power_profile_today": power_profile,
        "location_source": location_source,
        "latitude": lat,
        "longitude": lon,
    }


def persist_estimate(
    session_id: str | None,
    session_metrics: dict,
    voltage: float | int | None,
    capacity_ah: float | int | None = None,
    gps_state: dict | None = None,
    *,
    solar_voltage: float | int | None = None,
) -> dict:
    estimate = build_estimate(session_metrics, voltage, capacity_ah, solar_voltage=solar_voltage, gps_state=gps_state)
    payload = {
        "session_id": session_id,
        "updated_at": _now_paris().isoformat(),
        "remaining_wh": estimate["remaining_wh"],
        "capacity_wh": estimate["capacity_wh"],
        "soc_percent": estimate["percent"],
        "voltage": estimate["voltage_used"],
        "voltage_source": estimate["voltage_source"],
        "voltage_soc_percent": estimate["voltage_soc_percent"],
        "production_solaire_session_wh": float(session_metrics.get("solar_Wh") or 0.0),
        "consommation_nette_session_wh": estimate["used_wh"],
        "confidence": estimate["confidence"],
    }
    save_state(payload)
    return payload
