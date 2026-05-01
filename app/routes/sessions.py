from __future__ import annotations
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import render_template, jsonify, request, redirect

from app.config import get_db_file, SESSION_METRICS_DIR
from app import state
from app.reader import parse_line
from app.metrics import reset_session_state, restore_session_metrics
from app.photo_capture import configure_session_photo_capture, normalize_interval_km
from app.session_summary import build_summary_sections, build_summary_table, compute_session_metrics, compute_timeline_metrics_by_user
from app.user_profiles import active_profiles, get_profile


def start_page():
    users = active_profiles()
    if not state.session_active and not users:
        return redirect("/users?setup=1")
    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        rows = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC LIMIT 5").fetchall()
    recent_sessions = [row[0] for row in rows]
    return render_template(
        "start.html",
        session_active=state.session_active,
        recent_sessions=recent_sessions,
        solar_roof_enabled=state.solar_roof_enabled,
        users=users,
        current_user_id=state.current_user_id,
    )


def start_session():
    selected_user_id = request.form.get("user_id", "").strip()
    selected_profile = get_profile(selected_user_id)
    if selected_profile is None:
        users = active_profiles()
        selected_profile = users[0] if users else get_profile("JD")
    if selected_profile is None:
        return redirect("/users?setup=1")
    state.current_user_profile = selected_profile
    state.current_user_id = selected_profile["user_id"]
    state.current_user = selected_profile["initials"]
    photo_enabled = request.form.get("photo_capture_enabled") == "on"
    photo_interval_km = normalize_interval_km(request.form.get("photo_capture_interval_km"), default=1.0)
    solar_enabled = request.form.get("solar_roof_enabled") == "on"
    state.solar_roof_enabled = solar_enabled
    state.save_solar_roof_enabled(solar_enabled)

    state.session_id = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    state.save_session_id(state.session_id)
    state.save_current_user_id(state.current_user_id)
    state.save_current_user(state.current_user)
    state.session_start_time = datetime.now().timestamp()

    reset_session_state()
    state.session_metrics["solar_enabled"] = solar_enabled
    state.session_metrics["user_id"] = state.current_user_id
    state.session_metrics["user_initials"] = state.current_user
    configure_session_photo_capture(photo_enabled, photo_interval_km)
    state.save_session_metrics_to_file()

    state.session_active = True
    state.save_session_active(True)

    return redirect("/dashboard")


def resume_session():
    sid = request.form.get("session_id")
    if not sid:
        return "Missing session ID", 400

    state.save_session_id(sid)
    state.session_id = sid
    restore_session_metrics(state.session_id, get_db_file(), parse_line)
    state.session_start_time = datetime.now().timestamp()
    state.session_active = True
    state.save_session_active(True)
    return redirect("/dashboard")


def end_session():
    state.session_active = False
    state.save_session_active(False)
    # Erase any uploaded GPX track at end of session
    try:
        import os
        from app.config import GPX_ROUTE_FILE
        if os.path.exists(GPX_ROUTE_FILE):
            os.remove(GPX_ROUTE_FILE)
    except Exception:
        pass
    return redirect(f"/summary?session={state.session_id}")


def delete_session():
    data = request.json
    session_to_delete = data.get("session")
    if not session_to_delete:
        return jsonify({"error": "No session specified"}), 400

    with sqlite3.connect(get_db_file()) as conn:
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
    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        sessions = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC").fetchall()
    return render_template("select_session.html", sessions=[s[0] for s in sessions])


def edit_session_page():
    return render_template("edit_session.html")


def session_rows():
    session_id = request.args.get("session")
    if not session_id:
        return jsonify({"error": "Missing session ID"}), 400

    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, raw FROM logs WHERE session = ? ORDER BY id LIMIT 200",
            (session_id,)
        ).fetchall()

    return jsonify([{"id": r[0], "timestamp": r[1], "raw": r[2]} for r in rows])


def delete_row():
    row_id = request.json.get("id")
    if row_id is None:
        return jsonify({"error": "Missing row ID"}), 400
    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        conn.execute("DELETE FROM logs WHERE id = ?", (row_id,))
        conn.commit()
    return jsonify({"status": "deleted", "id": row_id})


def summary():
    session_id = request.args.get("session")
    if not session_id:
        return "Missing session ID", 400

    with sqlite3.connect(get_db_file(request.args.get('mode'))) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
        solar_enabled_col = "solar_enabled" if "solar_enabled" in cols else "1 AS solar_enabled"
        user_id_col = "user_id" if "user_id" in cols else "NULL AS user_id"
        user_initials_col = "user_initials" if "user_initials" in cols else "user AS user_initials"
        user_snapshot_col = "user_snapshot_json" if "user_snapshot_json" in cols else "NULL AS user_snapshot_json"
        rows = conn.execute(
            f"""
            SELECT user, timestamp, raw,
                   gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                   solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c,
                   {solar_enabled_col}, {user_id_col}, {user_initials_col}, {user_snapshot_col}
            FROM logs
            WHERE session = ?
            ORDER BY id
            """,
            (session_id,)
        ).fetchall()

    samples = [
        {
            "user": row[0],
            "timestamp": row[1],
            "raw": row[2],
            "gps_lat": row[3],
            "gps_lon": row[4],
            "gps_alt": row[5],
            "gps_speed_kph": row[6],
            "gps_track_deg": row[7],
            "gps_fix": row[8],
            "gps_sats": row[9],
            "gps_hdop": row[10],
            "solar_current_a": row[11],
            "solar_bus_v": row[12],
            "solar_shunt_v": row[13],
            "solar_power_w": row[14],
            "solar_temperature_c": row[15],
            "solar_enabled": row[16],
            "user_id": row[17],
            "user_initials": row[18],
            "user_snapshot_json": row[19],
        }
        for row in rows
    ]

    metrics_by_user = compute_timeline_metrics_by_user(samples)
    metrics_by_user["Total"] = compute_session_metrics(samples)
    all_users = ["Total"] + [user for user in metrics_by_user.keys() if user != "Total"]
    table = build_summary_table(metrics_by_user, all_users)
    sections = build_summary_sections(metrics_by_user, all_users)

    return render_template("summary.html", session_id=session_id, table=table, sections=sections, users=all_users)


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
