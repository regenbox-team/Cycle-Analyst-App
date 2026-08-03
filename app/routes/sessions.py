from __future__ import annotations
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import render_template, jsonify, request, redirect

from app.config import BASE_DIR, get_db_file, SESSION_METRICS_DIR
from app import state
from app.reader import parse_line
from app.metrics import reset_session_state, restore_session_metrics
from app.photo_capture import configure_session_photo_capture, normalize_interval_km
from app.solar_range import initialize_solar_session, persist_estimate
from app.session_summary import build_summary_sections, build_summary_table, compute_session_metrics, compute_timeline_metrics_by_user
from app.user_profiles import active_profiles, get_profile


_upload_jobs: dict[str, dict] = {}
_upload_jobs_lock = threading.Lock()
_UPLOAD_JOB_RETENTION_SECONDS = 3600
UPLOAD_JOBS_DIR = os.path.join(BASE_DIR, "upload_jobs")


def _upload_job_path(job_id: str) -> str:
    safe_id = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in {"-", "_"})
    return os.path.join(UPLOAD_JOBS_DIR, f"{safe_id}.json")


def _write_upload_job(job: dict) -> None:
    try:
        os.makedirs(UPLOAD_JOBS_DIR, exist_ok=True)
        path = _upload_job_path(job["job_id"])
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(job, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        pass


def _read_upload_job(job_id: str) -> dict | None:
    try:
        with open(_upload_job_path(job_id), "r", encoding="utf-8") as f:
            job = json.load(f)
        return job if isinstance(job, dict) else None
    except Exception:
        return None


def _upload_request_payload() -> tuple[str, str | None]:
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session") or data.get("session_id") or request.form.get("session") or "").strip()
    mode = data.get("mode") or request.args.get("mode")
    return session_id, mode


def _trim_upload_jobs_locked() -> None:
    now = time.time()
    stale_ids = [
        job_id
        for job_id, job in _upload_jobs.items()
        if job.get("complete") and now - float(job.get("updated_at") or 0) > _UPLOAD_JOB_RETENTION_SECONDS
    ]
    for job_id in stale_ids:
        _upload_jobs.pop(job_id, None)
        try:
            os.remove(_upload_job_path(job_id))
        except Exception:
            pass

    try:
        for name in os.listdir(UPLOAD_JOBS_DIR):
            if not name.endswith(".json"):
                continue
            path = os.path.join(UPLOAD_JOBS_DIR, name)
            if now - os.path.getmtime(path) > _UPLOAD_JOB_RETENTION_SECONDS:
                os.remove(path)
    except Exception:
        pass


def _set_upload_job(job_id: str, **updates) -> dict:
    with _upload_jobs_lock:
        job = _upload_jobs.get(job_id)
        if not job:
            return {}
        job.update(updates)
        job["updated_at"] = time.time()
        _write_upload_job(job)
        return dict(job)


def _upload_job_progress(job_id: str):
    def progress(updates: dict) -> None:
        _set_upload_job(job_id, **updates)

    return progress


def _run_upload_job(job_id: str, session_id: str, mode: str | None) -> None:
    try:
        from app.monitor_client import upload_session_now as monitor_upload_session_now

        _set_upload_job(job_id, status="working", phase="checking")
        result = monitor_upload_session_now(session_id, mode=mode, progress=_upload_job_progress(job_id))
    except Exception as exc:
        result = {"status": "error", "error": str(exc), "session": session_id}

    status = result.get("status")
    _set_upload_job(
        job_id,
        status=status,
        phase="done",
        complete=True,
        ok=status in ("ok", "already_uploaded"),
        result=result,
    )


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
    if solar_enabled:
        voltage = state.latest_raw_values[1] if state.latest_raw_values else None
        initialize_solar_session(
            state.session_metrics,
            voltage,
            solar_voltage=state.solar_sensor.get("bus_v"),
        )
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
    if bool(state.session_metrics.get("solar_enabled", state.solar_roof_enabled)):
        voltage = state.latest_raw_values[1] if state.latest_raw_values else None
        persist_estimate(
            state.session_id,
            state.session_metrics,
            voltage,
            gps_state=getattr(state, "gps_state", None),
            solar_voltage=state.solar_sensor.get("bus_v"),
        )
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
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    session_to_delete = (data.get("session") or request.args.get("session") or "").strip()
    mode = data.get("mode") or request.args.get("mode")
    if not session_to_delete:
        return jsonify({"error": "No session specified"}), 400

    with sqlite3.connect(get_db_file(mode)) as conn:
        deleted_rows = conn.execute("DELETE FROM logs WHERE session = ?", (session_to_delete,)).rowcount
        conn.commit()

    import os
    json_path = os.path.join(SESSION_METRICS_DIR, f"{session_to_delete}_session_metrics.json")
    if os.path.exists(json_path):
        try:
            os.remove(json_path)
        except Exception:
            pass

    return jsonify({
        "status": f"Session {session_to_delete} supprimee.",
        "session": session_to_delete,
        "deleted_rows": deleted_rows,
    })


def select_session():
    mode = request.args.get('mode')
    with sqlite3.connect(get_db_file(mode)) as conn:
        sessions = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC").fetchall()
    return render_template("select_session.html", sessions=[s[0] for s in sessions], mode=mode)


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


def upload_session_now():
    session_id, mode = _upload_request_payload()
    try:
        from app.monitor_client import upload_session_now as monitor_upload_session_now
        result = monitor_upload_session_now(session_id, mode=mode)
    except Exception as exc:
        result = {"status": "error", "error": str(exc), "session": session_id}

    status = result.get("status")
    http_status = 200 if status in ("ok", "already_uploaded") else 400
    if status == "error":
        http_status = 502
    return jsonify(result), http_status


def upload_session_start():
    session_id, mode = _upload_request_payload()
    if not session_id:
        return jsonify({"status": "error", "error": "missing session"}), 400

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "session": session_id,
        "mode": mode,
        "complete": False,
        "ok": False,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _upload_jobs_lock:
        _trim_upload_jobs_locked()
        _upload_jobs[job_id] = job
        _write_upload_job(job)

    thread = threading.Thread(target=_run_upload_job, args=(job_id, session_id, mode), daemon=True)
    thread.start()
    return jsonify(job), 202


def upload_session_status(job_id: str):
    with _upload_jobs_lock:
        job = _upload_jobs.get(job_id)
        if job is None:
            job = _read_upload_job(job_id)
            if job is not None:
                _upload_jobs[job_id] = job
        payload = dict(job) if job else None
    if payload is None:
        return jsonify({"status": "not_found", "error": "upload job not found"}), 404
    return jsonify(payload)


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
        motor_current_col = "motor_sensor_current_a" if "motor_sensor_current_a" in cols else "NULL AS motor_sensor_current_a"
        motor_voltage_col = "motor_sensor_bus_v" if "motor_sensor_bus_v" in cols else "NULL AS motor_sensor_bus_v"
        motor_corrected_col = "motor_corrected_current_a" if "motor_corrected_current_a" in cols else "NULL AS motor_corrected_current_a"
        motor_valid_col = "motor_sensor_valid" if "motor_sensor_valid" in cols else "0 AS motor_sensor_valid"
        rows = conn.execute(
            f"""
            SELECT user, timestamp, raw,
                   gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                   solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c,
                   {solar_enabled_col}, {user_id_col}, {user_initials_col}, {user_snapshot_col},
                   {motor_current_col}, {motor_voltage_col}, {motor_corrected_col}, {motor_valid_col}
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
            "motor_sensor_current_a": row[20],
            "motor_sensor_bus_v": row[21],
            "motor_corrected_current_a": row[22],
            "motor_sensor_valid": row[23],
        }
        for row in rows
    ]

    metrics_by_user = compute_timeline_metrics_by_user(samples)
    metrics_by_user["Total"] = compute_session_metrics(samples)
    session_users = [user for user in metrics_by_user.keys() if user != "Total"]
    all_users = ["Total"] if len(session_users) <= 1 else ["Total"] + session_users
    table = build_summary_table(metrics_by_user, all_users)
    sections = build_summary_sections(metrics_by_user, all_users)

    return render_template(
        "summary.html",
        session_id=session_id,
        table=table,
        sections=sections,
        users=all_users,
        mode=request.args.get("mode"),
    )


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
    bp.add_url_rule("/api/upload_session_now", methods=["POST"], view_func=upload_session_now)
    bp.add_url_rule("/api/upload_session_start", methods=["POST"], view_func=upload_session_start)
    bp.add_url_rule("/api/upload_session_status/<job_id>", view_func=upload_session_status)
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
    app.add_url_rule("/api/upload_session_now", methods=["POST"], view_func=upload_session_now)
    app.add_url_rule("/api/upload_session_start", methods=["POST"], view_func=upload_session_start)
    app.add_url_rule("/api/upload_session_status/<job_id>", view_func=upload_session_status)
    app.add_url_rule("/summary", view_func=summary)
