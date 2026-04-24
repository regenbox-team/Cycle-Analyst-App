from __future__ import annotations
import sqlite3
import os
import time
import json
from .state import default_photo_capture_settings, session_metrics

MAX_METRICS_DT_SECONDS = 2.0


def _migrate_legacy_metrics(store: dict) -> None:
    if "human_Ah" not in store:
        store["human_Ah"] = 0
    if "solar_Ah" not in store:
        store["solar_Ah"] = 0
    if "human_Wh" not in store:
        store["human_Wh"] = store.get("solar_Wh", 0)
        store["solar_Wh"] = 0
    if "human_power_sum" not in store:
        store["human_power_sum"] = store.get("solar_power_sum", 0)
        store["human_power_count"] = store.get("solar_power_count", 0)
        store["human_power_max"] = store.get("solar_power_max", 0)
        store["solar_power_sum"] = 0
        store["solar_power_count"] = 0
        store["solar_power_max"] = 0
    if "human_pct_per_km_last" not in store:
        store["human_pct_per_km_last"] = list(store.get("solar_pct_per_km_last", []))
        store["solar_pct_per_km_last"] = []
    if "calories_burned" not in store:
        store["calories_burned"] = store.get("human_Wh", 0) * 1.433
    photo_capture = store.get("photo_capture")
    defaults = default_photo_capture_settings()
    if not isinstance(photo_capture, dict):
        store["photo_capture"] = defaults
    else:
        defaults.update(photo_capture)
        store["photo_capture"] = defaults


def reset_session_state():
    session_metrics.update({
        "speed_max": 0,
        "speed_sum": 0,
        "speed_count": 0,
        "power_sum": 0,
        "power_max": float('-inf'),
        "power_min": float('inf'),
        "human_power_max": 0,
        "human_power_sum": 0,
        "human_power_count": 0,
        "solar_power_max": 0,
        "solar_power_sum": 0,
        "solar_power_count": 0,
        "temp_sum": 0,
        "temp_max": 0,
        "temp_count": 0,
        "positive_Wh": 0,
        "regen_Wh": 0,
        "human_Ah": 0,
        "solar_Ah": 0,
        "human_Wh": 0,
        "solar_Wh": 0,
        "calories_burned": 0,
        "distance_km": 0,
        "distance_start": None,
        "last_km_checkpoints": [],
        "distance_total": 0.0,
        "distance_offset": 0.0,
        "last_raw_distance": None,
        "ca_reset_detected": False,
        "ca_reset_prompt": False,
        "ah_offset": 0.0,
        "Wh_per_km_last": [],
        "net_Wh_per_km_last": [],
        "human_pct_per_km_last": [],
        "solar_pct_per_km_last": [],
        "last_regen_checkpoint": 0,
        "regen_pct_per_km_last": [],
        "photo_capture": default_photo_capture_settings(),
    })

    # Ensure at least one checkpoint exists
    session_metrics["last_km_checkpoints"] = [session_metrics["positive_Wh"]]

    # The data generator distance cache is managed in reader module (if used)


def update_metrics(data, now=None, solar_sample=None):
    import time as _time
    v = data[1]
    a = data[2]
    speed = data[3]
    distance = data[4]
    temp = data[5]
    human_a = data[13]
    solar_a = max(0.0, getattr(solar_sample, "current_a", 0.0) or 0.0)
    solar_v = max(0.0, getattr(solar_sample, "bus_v", 0.0) or 0.0)

    if now is None:
        now = _time.time()

    session_metrics["ca_reset_prompt"] = 53 <= v <= 54.6 and distance > 1

    if not hasattr(update_metrics, "last_time"):
        update_metrics.last_time = None
    dt = _bounded_dt(update_metrics.last_time, now)
    update_metrics.last_time = now

    if speed >= 1:
        session_metrics["speed_sum"] += speed
        session_metrics["speed_count"] += 1
        session_metrics["speed_max"] = max(session_metrics["speed_max"], speed)

        power = v * a
        human_power = v * human_a
        solar_power = solar_v * solar_a
        session_metrics["power_sum"] += power
        session_metrics["power_max"] = max(session_metrics["power_max"], power)
        session_metrics["power_min"] = min(session_metrics["power_min"], power)

        session_metrics["human_power_sum"] += human_power
        session_metrics["human_power_count"] += 1
        session_metrics["human_power_max"] = max(session_metrics["human_power_max"], human_power)

        session_metrics["solar_power_sum"] += solar_power
        session_metrics["solar_power_count"] += 1
        session_metrics["solar_power_max"] = max(session_metrics["solar_power_max"], solar_power)

        session_metrics["temp_sum"] += temp
        session_metrics["temp_count"] += 1

    power = v * a
    if abs(power) > 2:
        if a > 0:
            session_metrics["positive_Wh"] += power * dt / 3600
        elif a < 0:
            session_metrics["regen_Wh"] += abs(power) * dt / 3600

    session_metrics["human_Ah"] += max(0.0, human_a) * dt / 3600
    session_metrics["solar_Ah"] += max(0.0, solar_a) * dt / 3600
    session_metrics["human_Wh"] += (v * human_a) * dt / 3600
    session_metrics["solar_Wh"] += (solar_v * solar_a) * dt / 3600
    session_metrics["calories_burned"] = session_metrics["human_Wh"] * 1.433

    # Distance tracking (with CA reset handling)
    if session_metrics.get("last_raw_distance") is not None and distance < session_metrics["last_raw_distance"] - 0.1:
        session_metrics["ca_reset_detected"] = True
        session_metrics["distance_offset"] = session_metrics.get("distance_km", 0)
        session_metrics["distance_start"] = distance
    if session_metrics.get("distance_start") is None:
        session_metrics["distance_start"] = distance
    session_metrics["last_raw_distance"] = distance

    adjusted_distance = distance - session_metrics["distance_start"] + session_metrics.get("distance_offset", 0)
    prev_distance = session_metrics["distance_km"]
    session_metrics["distance_km"] = max(adjusted_distance, 0)
    prev_km = int(prev_distance)
    curr_km = int(session_metrics["distance_km"])
    km_diff = curr_km - prev_km

    if km_diff > 0:
        last_checkpoint = session_metrics["last_km_checkpoints"][-1] if session_metrics["last_km_checkpoints"] else 0
        delta_total = session_metrics["positive_Wh"] - last_checkpoint if session_metrics["last_km_checkpoints"] else 0
        delta_per_km = delta_total / km_diff

        km_distance = max(session_metrics["distance_km"], 1e-6)
        avg_human = session_metrics["human_Wh"] / km_distance
        avg_solar = session_metrics["solar_Wh"] / km_distance

        regen_prev = session_metrics.get("last_regen_checkpoint", 0)
        regen_total = session_metrics["regen_Wh"] - regen_prev
        regen_per_km = regen_total / km_diff
        session_metrics["last_regen_checkpoint"] = session_metrics["regen_Wh"]

        for _ in range(km_diff):
            last_checkpoint += delta_per_km
            session_metrics["last_km_checkpoints"].append(last_checkpoint)
            session_metrics["Wh_per_km_last"].append(round(delta_per_km, 3))
            net_Wh = delta_per_km - avg_human - avg_solar - regen_per_km
            session_metrics["net_Wh_per_km_last"].append(round(net_Wh, 3))
            if delta_per_km > 0:
                percent_human = min(100, max(0, 100 * avg_human / delta_per_km))
                percent_solar = min(100, max(0, 100 * avg_solar / delta_per_km))
                percent_regen = min(100, max(0, 100 * regen_per_km / delta_per_km))
            else:
                percent_human = 0
                percent_solar = 0
                percent_regen = 0
            session_metrics.setdefault("human_pct_per_km_last", []).append(round(percent_human, 1))
            session_metrics.setdefault("solar_pct_per_km_last", []).append(round(percent_solar, 1))
            session_metrics.setdefault("regen_pct_per_km_last", []).append(round(percent_regen, 1))

        # Trim history
        session_metrics["last_km_checkpoints"] = session_metrics["last_km_checkpoints"][-60:]
        session_metrics["Wh_per_km_last"] = session_metrics["Wh_per_km_last"][-60:]
        session_metrics["net_Wh_per_km_last"] = session_metrics["net_Wh_per_km_last"][-60:]
        session_metrics["human_pct_per_km_last"] = session_metrics["human_pct_per_km_last"][-60:]
        session_metrics["solar_pct_per_km_last"] = session_metrics["solar_pct_per_km_last"][-60:]
        session_metrics["regen_pct_per_km_last"] = session_metrics["regen_pct_per_km_last"][-60:]


def update_solar_only_metrics(solar_sample, now=None):
    import time as _time

    if solar_sample is None:
        return

    solar_a = max(0.0, getattr(solar_sample, "current_a", 0.0) or 0.0)
    solar_v = max(0.0, getattr(solar_sample, "bus_v", 0.0) or 0.0)
    solar_power = solar_v * solar_a

    if now is None:
        now = _time.time()

    if not hasattr(update_solar_only_metrics, "last_time"):
        update_solar_only_metrics.last_time = None
    dt = _bounded_dt(update_solar_only_metrics.last_time, now)
    update_solar_only_metrics.last_time = now

    session_metrics["solar_power_sum"] += solar_power
    session_metrics["solar_power_count"] += 1
    session_metrics["solar_power_max"] = max(session_metrics["solar_power_max"], solar_power)
    session_metrics["solar_Ah"] += solar_a * dt / 3600
    session_metrics["solar_Wh"] += solar_power * dt / 3600


def _bounded_dt(last_time, now):
    if last_time is None:
        return 0.0
    dt = now - last_time
    if dt < 0 or dt > MAX_METRICS_DT_SECONDS:
        return 0.0
    return dt


def restore_session_metrics(session_id: str, db_file: str, parse_line_func):
    """Restore metrics from JSON if available, otherwise rebuild from DB."""
    from .state import session_metrics as _sm
    from .state import metrics_json_path
    json_path = metrics_json_path(session_id)
    if json_path and os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                loaded = json.load(f)
            _migrate_legacy_metrics(loaded)
            _sm.update(loaded)
            return
        except Exception:
            pass

    # fallback to DB
    try:
        with sqlite3.connect(db_file) as conn:
            rows = conn.execute(
                """
                SELECT raw, solar_current_a, solar_bus_v, solar_shunt_v
                FROM logs
                WHERE session = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
            for row in rows:
                parsed = parse_line_func(row[0])
                if parsed:
                    solar_sample = None
                    if row[1] is not None or row[2] is not None or row[3] is not None:
                        class _SolarSample:
                            pass
                        solar_sample = _SolarSample()
                        solar_sample.current_a = row[1] or 0.0
                        solar_sample.bus_v = row[2] or 0.0
                        solar_sample.shunt_v = row[3] or 0.0
                    update_metrics(parsed, solar_sample=solar_sample)
    except Exception:
        pass
