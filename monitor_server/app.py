from __future__ import annotations
import base64
import json
import os
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request, render_template, Response


def _db_path() -> str:
    path = os.getenv("MONITOR_DB", os.path.join(os.path.dirname(__file__), "monitor.db"))
    path = os.path.expanduser(path)
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    return path


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                last_ip TEXT,
                last_session TEXT,
                mode TEXT,
                test_mode INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                start_ts TEXT,
                end_ts TEXT,
                rows_count INTEGER,
                distance_km REAL,
                metrics_json TEXT,
                uploaded_at TEXT,
                UNIQUE(device_id, session_id, mode)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                timestamp TEXT,
                raw TEXT,
                user TEXT,
                gps_lat REAL,
                gps_lon REAL,
                gps_alt REAL,
                gps_speed_kph REAL,
                gps_track_deg REAL,
                gps_fix INTEGER,
                gps_sats INTEGER,
                gps_hdop REAL
            )
            """
        )
        conn.commit()


def _auth_ok(auth_header: str | None) -> bool:
    user = os.getenv("MONITOR_USER", "")
    password = os.getenv("MONITOR_PASS", "")
    if not user or not password:
        return False
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    return raw == f"{user}:{password}"


def _require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _auth_ok(request.headers.get("Authorization")):
            return fn(*args, **kwargs)
        return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Monitor"'})
    return wrapper


def _parse_distance_km(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        parts = raw.strip().split()
        if len(parts) < 5:
            return None
        return float(parts[4])
    except Exception:
        return None


def create_app() -> Flask:
    app = Flask(__name__)
    _init_db()

    def _is_active(ts: str | None, window_sec: int = 120) -> bool:
        if not ts:
            return False
        try:
            last = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return datetime.utcnow() - last <= timedelta(seconds=window_sec)
        except Exception:
            return False

    @app.route("/")
    @_require_auth
    def index():
        with _get_db() as conn:
            devices = conn.execute(
                "SELECT device_id, last_seen, last_session, mode, test_mode FROM devices ORDER BY last_seen DESC"
            ).fetchall()
            sessions = conn.execute(
                """
                SELECT device_id, session_id, mode, start_ts, end_ts, rows_count, distance_km, uploaded_at
                FROM sessions
                ORDER BY uploaded_at DESC
                LIMIT 50
                """
            ).fetchall()
        devices = [
            dict(d) | {"active": _is_active(d["last_seen"])}
            for d in devices
        ]
        return render_template(
            "index.html",
            devices=devices,
            sessions=sessions,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    @app.route("/api/health")
    @_require_auth
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/known_sessions")
    @_require_auth
    def known_sessions():
        device_id = request.args.get("device_id")
        mode = request.args.get("mode", "default")
        if not device_id:
            return jsonify({"error": "missing device_id"}), 400
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE device_id = ? AND mode = ?",
                (device_id, mode),
            ).fetchall()
        return jsonify({"sessions": [r[0] for r in rows]})

    @app.route("/api/heartbeat", methods=["POST"])
    @_require_auth
    def heartbeat():
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        if not device_id:
            return jsonify({"error": "missing device_id"}), 400
        with _get_db() as conn:
            conn.execute(
                """
                INSERT INTO devices (device_id, last_seen, last_ip, last_session, mode, test_mode)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    last_ip = excluded.last_ip,
                    last_session = excluded.last_session,
                    mode = excluded.mode,
                    test_mode = excluded.test_mode
                """,
                (
                    device_id,
                    data.get("timestamp"),
                    request.remote_addr,
                    data.get("session_id"),
                    data.get("mode"),
                    int(data.get("test_mode") or 0),
                ),
            )
            conn.commit()
        return jsonify({"status": "ok"})

    @app.route("/api/upload_session", methods=["POST"])
    @_require_auth
    def upload_session():
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        session_id = data.get("session_id")
        mode = data.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE device_id = ? AND session_id = ? AND mode = ?",
                (device_id, session_id, mode),
            ).fetchone()
            if existing:
                return jsonify({"status": "exists"})

            logs = data.get("logs") or []
            rows_count = len(logs)
            start_ts = logs[0].get("timestamp") if logs else None
            end_ts = logs[-1].get("timestamp") if logs else None
            distance_km = _parse_distance_km(logs[-1].get("raw") if logs else None)
            metrics_json = None
            if data.get("metrics") is not None:
                try:
                    metrics_json = json.dumps(data.get("metrics"))
                except Exception:
                    metrics_json = None

            conn.execute(
                """
                INSERT INTO sessions (
                    device_id, session_id, mode, start_ts, end_ts,
                    rows_count, distance_km, metrics_json, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    session_id,
                    mode,
                    start_ts,
                    end_ts,
                    rows_count,
                    distance_km,
                    metrics_json,
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

            if logs:
                conn.executemany(
                    """
                    INSERT INTO logs (
                        device_id, session_id, mode, timestamp, raw, user,
                        gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            device_id,
                            session_id,
                            mode,
                            row.get("timestamp"),
                            row.get("raw"),
                            row.get("user"),
                            row.get("gps_lat"),
                            row.get("gps_lon"),
                            row.get("gps_alt"),
                            row.get("gps_speed_kph"),
                            row.get("gps_track_deg"),
                            row.get("gps_fix"),
                            row.get("gps_sats"),
                            row.get("gps_hdop"),
                        )
                        for row in logs
                    ],
                )
            conn.commit()

            conn.execute(
                """
                INSERT INTO devices (device_id, last_seen, last_ip, last_session, mode, test_mode)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    last_ip = excluded.last_ip,
                    last_session = excluded.last_session,
                    mode = excluded.mode,
                    test_mode = excluded.test_mode
                """,
                (
                    device_id,
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    request.remote_addr,
                    session_id,
                    mode,
                    int(data.get("test_mode") or 0),
                ),
            )
            conn.commit()

        return jsonify({"status": "ok"})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("MONITOR_PORT", "8080")))
