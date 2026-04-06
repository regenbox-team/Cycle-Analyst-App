from __future__ import annotations
import sqlite3
from flask import render_template, jsonify, redirect, request

from app.config import get_db_file, VEHICLE_CONFIGS
from app.modes import vehicle_mode
from app import state
from app.metrics import update_metrics  # re-export compatibility


def _get_metrics_payload():
    sm = state.session_metrics
    total_Wh = sm["positive_Wh"] + sm["regen_Wh"]
    net_Wh = sm["positive_Wh"] - sm["regen_Wh"] - sm["human_Wh"] - sm["solar_Wh"]
    distance = max(sm["distance_km"], 0.001)

    base_ah = (state.latest_raw_values[0] if state.latest_raw_values else 0)
    ah_used_gross = base_ah + sm.get("ah_offset", 0.0)
    ah_recovered = sm.get("human_Ah", 0.0) + sm.get("solar_Ah", 0.0)
    ah_used = max(0.0, ah_used_gross - ah_recovered)
    voltage = state.latest_raw_values[1] if state.latest_raw_values else 0
    capacity_ah = VEHICLE_CONFIGS.get(vehicle_mode, {}).get("battery_capacity_ah", 64)
    Wh_remaining = (capacity_ah - ah_used) * voltage

    net_list = sm.get("net_Wh_per_km_last", [])
    net_last_km = net_list[-1] if net_list else 0
    net_10km_avg = sum(net_list[-10:]) / max(1, len(net_list[-10:]))
    session_avg_net = net_Wh / distance if distance > 0 else 0

    autonomy = {"range_session_avg": Wh_remaining / max(0.1, session_avg_net)}
    if len(net_list) >= 1:
        autonomy["range_last_km"] = Wh_remaining / max(0.1, net_last_km)
    if len(net_list) >= 10:
        autonomy["range_10km_avg"] = Wh_remaining / max(0.1, net_10km_avg)

    return {
        "raw_CA_values": state.latest_raw_values,
        "session_id": state.session_id,
        "user": state.current_user,
        "battery_capacity_ah": capacity_ah,
        "ca_reset_detected": sm.get("ca_reset_detected", False),
        "ca_reset_prompt": sm.get("ca_reset_prompt", False),
        "solar_sensor": state.solar_sensor,
        "battery_ah_used_gross": ah_used_gross,
        "battery_ah_used_net": ah_used,
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
            "solar_current_live": state.solar_sensor.get("current_a", 0.0),
            "solar_voltage_live": state.solar_sensor.get("bus_v", 0.0),
            "solar_power_live": state.solar_sensor.get("current_a", 0.0) * state.solar_sensor.get("bus_v", 0.0),
            "solar_Wh": sm["solar_Wh"],
            "solar_power_max": sm["solar_power_max"],
            "solar_power_avg": sm["solar_power_sum"] / max(1, sm["solar_power_count"]),
            "%_solar": sm["solar_Wh"] / max(1e-6, total_Wh),
            "human_current_live": state.latest_raw_values[13] if state.latest_raw_values else 0,
            "human_power_live": state.latest_raw_values[1] * state.latest_raw_values[13] if state.latest_raw_values else 0,
            "human_Wh": sm["human_Wh"],
            "human_Ah": sm["human_Ah"],
            "human_power_max": sm["human_power_max"],
            "human_power_avg": sm["human_power_sum"] / max(1, sm["human_power_count"]),
            "human_calories_burned": sm.get("calories_burned", 0),
            "calories_burned": sm.get("calories_burned", 0),
            "%_human": sm["human_Wh"] / max(1e-6, total_Wh),
            "solar_Ah": sm["solar_Ah"],
            "net_Wh": net_Wh,
            "distance_km": sm["distance_km"],
            "net_Wh_per_km": session_avg_net,
            "live_Wh_per_km": (
                (state.latest_raw_values[1] * state.latest_raw_values[2]) / max(0.1, state.latest_raw_values[3])
                if state.latest_raw_values and state.latest_raw_values[3] >= 1 else 0
            ),
            "live_net_Wh_per_km": (
                ((state.latest_raw_values[1] * state.latest_raw_values[2]) - (state.latest_raw_values[1] * state.latest_raw_values[13]) - (state.solar_sensor.get("current_a", 0.0) * state.solar_sensor.get("bus_v", 0.0)))
                / max(0.1, state.latest_raw_values[3])
                if state.latest_raw_values and state.latest_raw_values[3] >= 1 else 0
            ),
            "regen_power_live": abs(state.latest_raw_values[1] * state.latest_raw_values[2]) if state.latest_raw_values and state.latest_raw_values[2] < 0 else 0,
            "Wh_per_km_last": sm.get("Wh_per_km_last", []),
            "net_Wh_per_km_last": sm.get("net_Wh_per_km_last", []),
            "human_pct_per_km_last": sm.get("human_pct_per_km_last", []),
            "solar_pct_per_km_last": sm.get("solar_pct_per_km_last", []),
            "regen_pct_per_km_last": sm.get("regen_pct_per_km_last", []),
            "temp_avg": sm["temp_sum"] / max(1, sm["temp_count"]),
            "temp_max": sm["temp_max"],
            "autonomy": autonomy,
            "ah_offset": sm.get("ah_offset", 0.0),
            "battery_ah_used_gross": ah_used_gross,
            "battery_ah_used_net": ah_used,
            "battery_ah_recovered": ah_recovered,
            "battery_Wh_remaining": Wh_remaining
        }
    }


def metrics():
    return jsonify(_get_metrics_payload())


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


def live_logs_page():
    return render_template("live_logs.html")


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("core", __name__)
    bp.add_url_rule("/metrics", view_func=metrics)
    bp.add_url_rule("/logs", view_func=logs)
    bp.add_url_rule("/sessions", view_func=list_sessions)
    bp.add_url_rule("/", view_func=root)
    bp.add_url_rule("/dashboard", view_func=dashboard)
    bp.add_url_rule("/live_logs", view_func=live_logs_page)
    return bp


def register(app):
    app.add_url_rule("/metrics", view_func=metrics)
    app.add_url_rule("/logs", view_func=logs)
    app.add_url_rule("/sessions", view_func=list_sessions)
    app.add_url_rule("/", view_func=root)
    app.add_url_rule("/dashboard", view_func=dashboard)
    app.add_url_rule("/live_logs", view_func=live_logs_page)
