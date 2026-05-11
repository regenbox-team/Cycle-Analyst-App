from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from flask import render_template, jsonify, redirect, request, send_file

from app.config import get_db_file, VEHICLE_CONFIGS
from app.modes import vehicle_mode
from app import state
from app.metrics import update_metrics  # re-export compatibility
from app.photo_capture import latest_local_photo_path
from app.reader import parse_line
from app.solar_range import build_estimate


POWER_HISTORY_DEFAULT_SECONDS = 180
POWER_HISTORY_MAX_SECONDS = 1800
POWER_HISTORY_DEFAULT_SAMPLES = 180
POWER_HISTORY_MAX_SAMPLES = 180
SESSION_TRACK_DEFAULT_SAMPLES = 5000
SESSION_TRACK_MAX_SAMPLES = 10000


def _get_metrics_payload():
    sm = state.session_metrics
    solar_enabled = bool(sm.get("solar_enabled", state.solar_roof_enabled))
    total_Wh = sm["positive_Wh"] + sm["regen_Wh"]
    solar_Wh = sm["solar_Wh"] if solar_enabled else 0.0
    solar_Ah = sm.get("solar_Ah", 0.0) if solar_enabled else 0.0
    solar_power_sum = sm.get("solar_power_sum", 0.0) if solar_enabled else 0.0
    solar_power_count = sm.get("solar_power_count", 0) if solar_enabled else 0
    solar_power_max = sm.get("solar_power_max", 0.0) if solar_enabled else 0.0
    solar_sensor = state.solar_sensor if solar_enabled else {}
    solar_power_live = (
        solar_sensor.get("current_a", 0.0) * solar_sensor.get("bus_v", 0.0)
        if solar_enabled else 0.0
    )
    net_Wh = sm["positive_Wh"] - sm["regen_Wh"] - sm["human_Wh"] - solar_Wh
    distance = max(sm["distance_km"], 0.001)
    photo_cfg = sm.get("photo_capture")
    if not isinstance(photo_cfg, dict):
        photo_cfg = {}
    latest_local_url = None
    if latest_local_photo_path():
        latest_local_url = "/photo_capture/latest"

    base_ah = (state.latest_raw_values[0] if state.latest_raw_values else 0)
    ah_used_gross = base_ah + sm.get("ah_offset", 0.0)
    ah_recovered = sm.get("human_Ah", 0.0) + solar_Ah
    ah_used = max(0.0, ah_used_gross - ah_recovered)
    voltage = state.latest_raw_values[1] if state.latest_raw_values else 0
    capacity_ah = VEHICLE_CONFIGS.get(vehicle_mode, {}).get("battery_capacity_ah", 64)
    standard_Wh_remaining = max(0.0, (capacity_ah - ah_used) * voltage)
    solar_battery = None
    Wh_remaining = standard_Wh_remaining
    if solar_enabled:
        solar_battery = build_estimate(
            sm,
            voltage,
            capacity_ah,
            solar_voltage=solar_sensor.get("bus_v", 0.0),
            gps_state=getattr(state, "gps_state", None),
        )
        Wh_remaining = solar_battery["remaining_wh"]

    net_list = sm.get("net_Wh_per_km_last", [])
    net_last_km = net_list[-1] if net_list else 0
    net_10km_avg = sum(net_list[-10:]) / max(1, len(net_list[-10:]))
    session_avg_net = net_Wh / distance if distance > 0 else 0

    autonomy = {"range_session_avg": Wh_remaining / max(0.1, session_avg_net)}
    if len(net_list) >= 1:
        autonomy["range_last_km"] = Wh_remaining / max(0.1, net_last_km)
    if len(net_list) >= 10:
        autonomy["range_10km_avg"] = Wh_remaining / max(0.1, net_10km_avg)
    if solar_battery:
        solar_today_wh = Wh_remaining + solar_battery["potential_remaining_today_wh"]
        autonomy["solar_today_session_avg"] = solar_today_wh / max(0.1, session_avg_net)
        if len(net_list) >= 1:
            autonomy["solar_today_last_km"] = solar_today_wh / max(0.1, net_last_km)
        if len(net_list) >= 10:
            autonomy["solar_today_10km_avg"] = solar_today_wh / max(0.1, net_10km_avg)

    return {
        "raw_CA_values": state.latest_raw_values,
        "session_id": state.session_id,
        "user": state.current_user,
        "battery_capacity_ah": capacity_ah,
        "solar_enabled": solar_enabled,
        "ca_reset_detected": sm.get("ca_reset_detected", False),
        "ca_reset_prompt": bool(sm.get("ca_reset_prompt", False)) and not solar_enabled,
        "photo_capture": {
            "enabled": bool(photo_cfg.get("enabled")),
            "interval_km": photo_cfg.get("interval_km"),
            "last_trigger_distance_km": photo_cfg.get("last_trigger_distance_km"),
            "capture_count": int(photo_cfg.get("capture_count") or 0),
            "last_captured_at": photo_cfg.get("last_captured_at"),
            "last_uploaded_at": photo_cfg.get("last_uploaded_at"),
            "latest_local_url": latest_local_url,
            "latest_public_url": photo_cfg.get("latest_public_url"),
            "pending_upload_count": int(photo_cfg.get("pending_upload_count") or 0),
            "last_error": photo_cfg.get("last_error"),
        },
        "solar_sensor": state.solar_sensor if solar_enabled else {"enabled": False},
        "battery_ah_used_gross": ah_used_gross,
        "battery_ah_used_net": ah_used,
        "solar_battery": solar_battery,
        "calculated_CA_values": {
            "speed_avg": sm["speed_sum"] / max(1, sm["speed_count"]),
            "speed_max": sm["speed_max"],
            "power_live": state.latest_raw_values[1] * state.latest_raw_values[2] if state.latest_raw_values else 0,
            "power_avg": sm["power_sum"] / max(1, sm["speed_count"]),
            "power_max": sm["power_max"] if sm["power_max"] != float('-inf') else 0,
            "power_min": sm["power_min"] if sm["power_min"] != float('inf') else 0,
            "Wh_pos": sm["positive_Wh"],
            "Wh_regen": sm["regen_Wh"],
            "%_regen": sm["regen_Wh"] / max(1e-6, total_Wh),
            "solar_enabled": solar_enabled,
            "solar_current_live": solar_sensor.get("current_a", 0.0) if solar_enabled else 0.0,
            "solar_voltage_live": solar_sensor.get("bus_v", 0.0) if solar_enabled else 0.0,
            "solar_power_live": solar_power_live,
            "solar_Wh": solar_Wh,
            "solar_power_max": solar_power_max,
            "solar_power_avg": solar_power_sum / max(1, solar_power_count),
            "%_solar": solar_Wh / max(1e-6, total_Wh),
            "human_current_live": state.latest_raw_values[13] if state.latest_raw_values else 0,
            "human_power_live": state.latest_raw_values[1] * state.latest_raw_values[13] if state.latest_raw_values else 0,
            "human_Wh": sm["human_Wh"],
            "human_Ah": sm["human_Ah"],
            "human_power_max": sm["human_power_max"],
            "human_power_avg": sm["human_power_sum"] / max(1, sm["human_power_count"]),
            "human_calories_burned": sm.get("calories_burned", 0),
            "calories_burned": sm.get("calories_burned", 0),
            "%_human": sm["human_Wh"] / max(1e-6, total_Wh),
            "solar_Ah": solar_Ah,
            "net_Wh": net_Wh,
            "distance_km": sm["distance_km"],
            "net_Wh_per_km": session_avg_net,
            "live_Wh_per_km": (
                (state.latest_raw_values[1] * state.latest_raw_values[2]) / max(0.1, state.latest_raw_values[3])
                if state.latest_raw_values and state.latest_raw_values[3] >= 1 else 0
            ),
            "live_net_Wh_per_km": (
                ((state.latest_raw_values[1] * state.latest_raw_values[2]) - (state.latest_raw_values[1] * state.latest_raw_values[13]) - solar_power_live)
                / max(0.1, state.latest_raw_values[3])
                if state.latest_raw_values and state.latest_raw_values[3] >= 1 else 0
            ),
            "regen_power_live": abs(state.latest_raw_values[1] * state.latest_raw_values[2]) if state.latest_raw_values and state.latest_raw_values[2] < 0 else 0,
            "Wh_per_km_last": sm.get("Wh_per_km_last", []),
            "net_Wh_per_km_last": sm.get("net_Wh_per_km_last", []),
            "human_pct_per_km_last": sm.get("human_pct_per_km_last", []),
            "solar_pct_per_km_last": sm.get("solar_pct_per_km_last", []) if solar_enabled else [],
            "regen_pct_per_km_last": sm.get("regen_pct_per_km_last", []),
            "temp_avg": sm["temp_sum"] / max(1, sm["temp_count"]),
            "temp_max": sm["temp_max"],
            "autonomy": autonomy,
            "ah_offset": sm.get("ah_offset", 0.0),
            "battery_ah_used_gross": ah_used_gross,
            "battery_ah_used_net": ah_used,
            "battery_ah_recovered": ah_recovered,
            "battery_Wh_remaining": Wh_remaining,
            "battery_Wh_remaining_ca": standard_Wh_remaining,
            "battery_percent_remaining": (
                solar_battery["percent"] if solar_battery else max(0.0, min(100.0, 100.0 * (1 - (ah_used / max(1e-6, capacity_ah)))))
            ),
            "solar_battery": solar_battery
        }
    }


def metrics():
    return jsonify(_get_metrics_payload())


def _bounded_int_arg(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _sample_evenly(rows, sample_count: int):
    if sample_count <= 0 or len(rows) <= sample_count:
        return rows

    step = len(rows) / sample_count
    sampled = []
    for index in range(sample_count):
        bucket_end = max(1, int(round((index + 1) * step)))
        sampled.append(rows[min(bucket_end - 1, len(rows) - 1)])
    return sampled


def power_history():
    if not state.session_id:
        return jsonify({"points": []})

    window_seconds = _bounded_int_arg(
        "window_seconds",
        POWER_HISTORY_DEFAULT_SECONDS,
        60,
        POWER_HISTORY_MAX_SECONDS,
    )
    sample_count = _bounded_int_arg(
        "samples",
        POWER_HISTORY_DEFAULT_SAMPLES,
        30,
        POWER_HISTORY_MAX_SAMPLES,
    )
    cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).isoformat()
    raw_row_limit = min(3600, max(sample_count, window_seconds * 2))

    try:
        with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
            solar_enabled_col = "solar_enabled" if "solar_enabled" in cols else "1 AS solar_enabled"
            rows = conn.execute(
                f"""
                SELECT timestamp, raw, solar_current_a, solar_bus_v, {solar_enabled_col}
                FROM logs
                WHERE session = ? AND raw IS NOT NULL AND timestamp >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (state.session_id, cutoff, raw_row_limit),
            ).fetchall()
            rows.reverse()
            if not rows:
                rows = conn.execute(
                    f"""
                    SELECT timestamp, raw, solar_current_a, solar_bus_v, {solar_enabled_col}
                    FROM logs
                    WHERE session = ? AND raw IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (state.session_id, sample_count),
                ).fetchall()
                rows.reverse()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    rows = _sample_evenly(rows, sample_count)
    points = []
    for timestamp, raw, solar_current_a, solar_bus_v, solar_enabled in rows:
        parsed = parse_line(raw)
        if not parsed:
            continue
        voltage = parsed[1]
        motor_power = voltage * parsed[2]
        human_power = max(0.0, voltage * parsed[13])
        solar_power = max(0.0, (solar_current_a or 0.0) * (solar_bus_v or 0.0)) if solar_enabled else 0.0
        points.append({
            "timestamp": timestamp,
            "motor_power": round(motor_power, 3),
            "human_power": round(human_power, 3),
            "solar_power": round(solar_power, 3),
        })

    smoothed = _smooth_power_points(points, window=5)
    return jsonify({
        "points": smoothed,
        "window_seconds": window_seconds,
        "sample_count": sample_count,
        "server_time": datetime.utcnow().isoformat(),
    })


def session_track():
    if not state.session_id:
        return jsonify({"points": [], "count": 0, "sample_count": 0})

    sample_count = _bounded_int_arg(
        "samples",
        SESSION_TRACK_DEFAULT_SAMPLES,
        100,
        SESSION_TRACK_MAX_SAMPLES,
    )

    try:
        with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
            if "gps_lat" not in cols or "gps_lon" not in cols:
                return jsonify({"points": [], "count": 0, "sample_count": sample_count})

            gps_fix_filter = "AND (gps_fix IS NULL OR gps_fix = 1)" if "gps_fix" in cols else ""
            rows = conn.execute(
                f"""
                SELECT timestamp, gps_lat, gps_lon
                FROM logs
                WHERE session = ?
                  AND gps_lat IS NOT NULL
                  AND gps_lon IS NOT NULL
                  {gps_fix_filter}
                ORDER BY id ASC
                """,
                (state.session_id,),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    rows = _sample_evenly(rows, sample_count)
    points = []
    last_lat = None
    last_lon = None
    for timestamp, lat, lon in rows:
        try:
            lat_value = float(lat)
            lon_value = float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat_value <= 90 and -180 <= lon_value <= 180):
            continue
        if last_lat == lat_value and last_lon == lon_value:
            continue
        points.append({
            "timestamp": timestamp,
            "lat": round(lat_value, 7),
            "lon": round(lon_value, 7),
        })
        last_lat = lat_value
        last_lon = lon_value

    return jsonify({
        "points": points,
        "count": len(points),
        "sample_count": sample_count,
    })


def _smooth_power_points(points, window: int = 5):
    if not points:
        return []

    out = []
    motor_values = []
    human_values = []
    solar_values = []

    for point in points:
        motor_values.append(point["motor_power"])
        human_values.append(point["human_power"])
        solar_values.append(point["solar_power"])
        if len(motor_values) > window:
            motor_values.pop(0)
            human_values.pop(0)
            solar_values.pop(0)
        out.append({
            "timestamp": point["timestamp"],
            "motor_power": round(sum(motor_values) / len(motor_values), 3),
            "human_power": round(sum(human_values) / len(human_values), 3),
            "solar_power": round(sum(solar_values) / len(solar_values), 3),
        })
    return out


def logs():
    try:
        with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 10").fetchall()
        logs = [{"id": row[0], "timestamp": row[1], "session": row[2], "raw": row[3], "user": row[4]} for row in rows]
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def list_sessions():
    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        rows = conn.execute(
            """
            SELECT session, SUM(LENGTH(raw)) as size_bytes
            FROM logs
            GROUP BY session
            ORDER BY session DESC
            """
        ).fetchall()
    sessions = [{"session": r[0], "size_kb": round((r[1] or 0) / 1024, 2)} for r in rows]
    return jsonify(sessions)


def root():
    return redirect("/dashboard" if state.session_active else "/start")


def dashboard():
    if not state.session_active:
        return redirect("/start")
    return render_template("index.html")


def latest_photo():
    path = latest_local_photo_path()
    if not path:
        return jsonify({"error": "no local photo available"}), 404
    return send_file(path, mimetype="image/jpeg")


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("core", __name__)
    bp.add_url_rule("/metrics", view_func=metrics)
    bp.add_url_rule("/power_history", view_func=power_history)
    bp.add_url_rule("/session_track", view_func=session_track)
    bp.add_url_rule("/logs", view_func=logs)
    bp.add_url_rule("/sessions", view_func=list_sessions)
    bp.add_url_rule("/", view_func=root)
    bp.add_url_rule("/dashboard", view_func=dashboard)
    bp.add_url_rule("/photo_capture/latest", view_func=latest_photo)
    return bp


def register(app):
    app.add_url_rule("/metrics", view_func=metrics)
    app.add_url_rule("/power_history", view_func=power_history)
    app.add_url_rule("/session_track", view_func=session_track)
    app.add_url_rule("/logs", view_func=logs)
    app.add_url_rule("/sessions", view_func=list_sessions)
    app.add_url_rule("/", view_func=root)
    app.add_url_rule("/dashboard", view_func=dashboard)
    app.add_url_rule("/photo_capture/latest", view_func=latest_photo)
