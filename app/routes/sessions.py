from __future__ import annotations
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import render_template, jsonify, request, redirect

from app.config import DB_FILE, SESSION_METRICS_DIR
from app import state
from app.reader import parse_line
from app.metrics import reset_session_state, restore_session_metrics


def start_page():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC LIMIT 5").fetchall()
    recent_sessions = [row[0] for row in rows]
    return render_template("start.html", session_active=state.session_active, recent_sessions=recent_sessions)


def start_session():
    selected_user = request.form.get("user", "JD").strip()
    state.current_user = selected_user if selected_user in ("JD", "LL") else "JD"

    state.session_id = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    state.save_session_id(state.session_id)
    state.save_current_user(state.current_user)
    state.session_start_time = datetime.now().timestamp()

    reset_session_state()

    state.session_active = True
    state.save_session_active(True)

    return redirect("/dashboard")


def resume_session():
    sid = request.form.get("session_id")
    if not sid:
        return "Missing session ID", 400

    state.save_session_id(sid)
    state.session_id = sid
    restore_session_metrics(state.session_id, DB_FILE, parse_line)
    state.session_start_time = datetime.now().timestamp()
    state.session_active = True
    state.save_session_active(True)
    return redirect("/dashboard")


def end_session():
    state.session_active = False
    state.save_session_active(False)
    return redirect(f"/summary?session={state.session_id}")


def delete_session():
    data = request.json
    session_to_delete = data.get("session")
    if not session_to_delete:
        return jsonify({"error": "No session specified"}), 400

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM logs WHERE session = ?", (session_to_delete,))
        conn.commit()

    import os
    json_path = os.path.join(SESSION_METRICS_DIR, f"{session_to_delete}_session_metrics.json")
    if os.path.exists(json_path):
        try:
            os.remove(json_path)
        except Exception:
            pass

    return jsonify({"status": f"Session {session_to_delete} deleted."})


def select_session():
    with sqlite3.connect(DB_FILE) as conn:
        sessions = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC").fetchall()
    return render_template("select_session.html", sessions=[s[0] for s in sessions])


def edit_session_page():
    return render_template("edit_session.html")


def session_rows():
    session_id = request.args.get("session")
    if not session_id:
        return jsonify({"error": "Missing session ID"}), 400

    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, raw FROM logs WHERE session = ? ORDER BY id LIMIT 200",
            (session_id,)
        ).fetchall()

    return jsonify([{"id": r[0], "timestamp": r[1], "raw": r[2]} for r in rows])


def delete_row():
    row_id = request.json.get("id")
    if row_id is None:
        return jsonify({"error": "Missing row ID"}), 400
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM logs WHERE id = ?", (row_id,))
        conn.commit()
    return jsonify({"status": "deleted", "id": row_id})


def summary():
    session_id = request.args.get("session")
    if not session_id:
        return "Missing session ID", 400

    import datetime
    from collections import defaultdict

    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT user, timestamp, raw FROM logs WHERE session = ? ORDER BY id",
            (session_id,)
        ).fetchall()

    def _parse_line(line):
        try:
            parts = line.strip().split()
            if len(parts) != 15:
                return None
            return [float(x) for x in parts[:14]] + [parts[14]]
        except Exception:
            return None

    user_data = defaultdict(list)
    timestamps = defaultdict(list)

    for user, ts, raw in rows:
        parsed = _parse_line(raw)
        if not parsed or not user:
            continue
        user_data[user].append(parsed)
        timestamps[user].append(ts)

    def compute_metrics(data, ts_list):
        m = {
            "speed_sum": 0, "speed_max": 0, "speed_count": 0,
            "power_sum": 0, "power_max": float('-inf'), "power_min": float('inf'),
            "solar_power_sum": 0, "solar_power_max": 0, "solar_power_count": 0,
            "positive_Wh": 0, "regen_Wh": 0, "solar_Wh": 0,
            "temp_sum": 0, "temp_max": 0, "temp_count": 0,
            "distance_start": None, "distance_end": None,
            "Ah": 0
        }

        last_ts = None
        for i, d in enumerate(data):
            try:
                ah = d[0]
                v = d[1]
                a = d[2]
                speed = d[3]
                dist = d[4]
                temp = d[5]
                solar_a = d[13]
                power = v * a
                solar_power = v * solar_a

                if m["distance_start"] is None:
                    m["distance_start"] = dist
                m["distance_end"] = dist

                if i < len(ts_list):
                    current_ts = datetime.datetime.fromisoformat(ts_list[i])
                    if last_ts:
                        dt = (current_ts - last_ts).total_seconds()
                    else:
                        dt = 0.1
                    last_ts = current_ts
                else:
                    dt = 0.1

                if speed >= 1:
                    m["speed_sum"] += speed
                    m["speed_count"] += 1
                    m["speed_max"] = max(m["speed_max"], speed)
                    m["power_sum"] += power
                    m["power_max"] = max(m["power_max"], power)
                    m["power_min"] = min(m["power_min"], power)
                    m["solar_power_sum"] += solar_power
                    m["solar_power_count"] += 1
                    m["solar_power_max"] = max(m["solar_power_max"], solar_power)
                    m["temp_sum"] += temp
                    m["temp_count"] += 1
                    m["temp_max"] = max(m["temp_max"], temp)

                m["Ah"] += a * dt / 3600
                if a > 0:
                    m["positive_Wh"] += power * dt / 3600
                elif a < 0:
                    m["regen_Wh"] += abs(power) * dt / 3600
                m["solar_Wh"] += solar_power * dt / 3600
            except Exception:
                pass

        if ts_list:
            try:
                t0 = datetime.datetime.fromisoformat(ts_list[0])
                t1 = datetime.datetime.fromisoformat(ts_list[-1])
                m["duration"] = (t1 - t0).total_seconds()
            except Exception:
                m["duration"] = 0
        else:
            m["duration"] = 0

        if m["distance_start"] is not None and m["distance_end"] is not None:
            m["distance"] = max(0.0, m["distance_end"] - m["distance_start"])
        else:
            m["distance"] = 0.0
        return m

    def compute_total_metrics(all_user_data, all_timestamps):
        all_points = sum(all_user_data.values(), [])
        all_ts = sum(all_timestamps.values(), [])
        m = compute_metrics(all_points, all_ts)
        distances = [p[4] for p in all_points if len(p) > 4]
        if distances:
            m["distance"] = max(distances) - min(distances)
        return m

    metrics_by_user = {user: compute_metrics(user_data[user], timestamps[user]) for user in user_data}
    metrics_by_user["Total"] = compute_total_metrics(user_data, timestamps)
    all_users = list(user_data.keys()) + ["Total"]

    def safe_div(n, d):
        return n / max(d, 1e-6)

    grouped_rows = [
        ("Duration & distance", [("Duration (min)", lambda m: m["duration"] / 60), ("Distance (km)", lambda m: m["distance"]) ]),
        ("Speed", [("Avg Speed (km/h)", lambda m: safe_div(m["speed_sum"], m["speed_count"])), ("Max Speed (km/h)", lambda m: m["speed_max"]) ]),
        ("Power", [("Avg Power (W)", lambda m: safe_div(m["power_sum"], m["speed_count"])), ("Max Power (W)", lambda m: m["power_max"]), ("Min Power (W)", lambda m: m["power_min"]) ]),
        ("Energy", [("Battery Used (Ah)", lambda m: m["Ah"]), ("Regen Energy (Wh)", lambda m: m["regen_Wh"]), ("Solar Energy (Wh)", lambda m: m["solar_Wh"]), ("Net Energy (Wh)", lambda m: m["positive_Wh"] - m["regen_Wh"] - m["solar_Wh"]) ]),
        ("Efficiency", [("Total Wh/km", lambda m: safe_div(m["positive_Wh"], m["distance"])), ("Net Wh/km", lambda m: safe_div(m["positive_Wh"] - m["regen_Wh"] - m["solar_Wh"], m["distance"])) ]),
        ("Percentages", [("Regen %", lambda m: 100 * safe_div(m["regen_Wh"], m["positive_Wh"] + m["regen_Wh"])), ("Solar %", lambda m: 100 * safe_div(m["solar_Wh"], m["positive_Wh"] + m["regen_Wh"])) ]),
        ("Temperature", [("Avg Temp (°C)", lambda m: safe_div(m["temp_sum"], m["temp_count"])), ("Max Temp (°C)", lambda m: m["temp_max"]) ]),
        ("Human effort", [("Calories Burned (kcal)", lambda m: m["positive_Wh"] * 0.086)])
    ]

    table = [["Metric"] + all_users]
    for category, metrics in grouped_rows:
        table.append([f"—— {category} ——"] + [""] * len(all_users))
        for label, func in metrics:
            row = [label]
            for u in all_users:
                value = func(metrics_by_user[u])
                unit = " min" if "Duration" in label else \
                       " km" if "Distance" in label else \
                       " km/h" if "Speed" in label else \
                       " W" if "Power" in label else \
                       " Ah" if "Battery" in label else \
                       " Wh/km" if "/km" in label else \
                       " Wh" if "Energy" in label or "Energy" in label else \
                       " %" if "%" in label else \
                       " °C" if "Temp" in label else \
                       " kcal" if "Calories" in label else ""
                row.append(f"{value:.2f}{unit}")
            table.append(row)

    return render_template("summary.html", session_id=session_id, table=table)


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("sessions", __name__)
    bp.add_url_rule("/start", view_func=start_page)
    bp.add_url_rule("/start_session", methods=["POST"], view_func=start_session)
    bp.add_url_rule("/resume_session", methods=["POST"], view_func=resume_session)
    bp.add_url_rule("/end_session", methods=["POST"], view_func=end_session)
    bp.add_url_rule("/delete_session", methods=["POST"], view_func=delete_session)
    bp.add_url_rule("/select_session", view_func=select_session)
    bp.add_url_rule("/edit_session", view_func=edit_session_page)
    bp.add_url_rule("/api/session_rows", view_func=session_rows)
    bp.add_url_rule("/api/delete_row", methods=["POST"], view_func=delete_row)
    bp.add_url_rule("/summary", view_func=summary)
    return bp


def register(app):
    app.add_url_rule("/start", view_func=start_page)
    app.add_url_rule("/start_session", methods=["POST"], view_func=start_session)
    app.add_url_rule("/resume_session", methods=["POST"], view_func=resume_session)
    app.add_url_rule("/end_session", methods=["POST"], view_func=end_session)
    app.add_url_rule("/delete_session", methods=["POST"], view_func=delete_session)
    app.add_url_rule("/select_session", view_func=select_session)
    app.add_url_rule("/edit_session", view_func=edit_session_page)
    app.add_url_rule("/api/session_rows", view_func=session_rows)
    app.add_url_rule("/api/delete_row", methods=["POST"], view_func=delete_row)
    app.add_url_rule("/summary", view_func=summary)
