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
                duration_sec REAL,
                avg_speed_kph REAL,
                uphill_m REAL,
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


def _migrate_db() -> None:
    with _get_db() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        missing = {
            "duration_sec": "REAL",
            "avg_speed_kph": "REAL",
            "uphill_m": "REAL",
        }
        for name, col_type in missing.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {col_type}")
        conn.execute(
            """
            UPDATE sessions
            SET duration_sec = (julianday(end_ts) - julianday(start_ts)) * 86400
            WHERE duration_sec IS NULL AND start_ts IS NOT NULL AND end_ts IS NOT NULL
            """
        )
        conn.execute(
            """
            UPDATE sessions
            SET avg_speed_kph = distance_km / (duration_sec / 3600.0)
            WHERE avg_speed_kph IS NULL AND duration_sec IS NOT NULL AND duration_sec > 0
              AND distance_km IS NOT NULL
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
    _migrate_db()

    def _is_active(ts: str | None, window_sec: int = 120) -> bool:
        if not ts:
            return False
        try:
            last = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return datetime.utcnow() - last <= timedelta(seconds=window_sec)
        except Exception:
            return False

    def _format_ago(ts: str | None, now: datetime) -> str:
        if not ts:
            return ""
        try:
            last = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""
        delta = now - last
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        if total_seconds < 60:
            return "less than a minute"
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        if days == 0 and hours == 0 and minutes == 1:
            return "1 minute ago"
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts) + " ago"

    def _format_dt(ts: str | None) -> str:
        if not ts:
            return ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(ts, fmt)
                return parsed.strftime("%b %d, %Y %H:%M:%S")
            except Exception:
                continue
        return ts

    def _parse_ts(ts: str | None) -> datetime | None:
        if not ts:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(ts, fmt)
            except Exception:
                continue
        return None

    def _project_point(lat: float, lon: float, width: float = 1000, height: float = 500) -> tuple[float, float]:
        x = (lon + 180.0) / 360.0 * width
        y = (90.0 - lat) / 180.0 * height
        return x, y

    @app.route("/")
    @_require_auth
    def index():
        now_utc = datetime.utcnow()
        with _get_db() as conn:
            device_rows = conn.execute(
                "SELECT device_id, last_seen, last_session, mode, test_mode FROM devices ORDER BY last_seen DESC"
            ).fetchall()
            sessions = conn.execute(
                """
                SELECT device_id, session_id, mode, start_ts, end_ts, rows_count, distance_km, uploaded_at
                FROM sessions
                ORDER BY start_ts DESC
                LIMIT 50
                """
            ).fetchall()
            daily_all_rows = conn.execute(
                """
                SELECT date(start_ts) AS day, SUM(COALESCE(distance_km, 0)) AS distance_km
                FROM sessions
                WHERE start_ts IS NOT NULL
                GROUP BY day
                ORDER BY day
                """
            ).fetchall()
            daily_last_30_rows = conn.execute(
                """
                SELECT date(start_ts) AS day, SUM(COALESCE(distance_km, 0)) AS distance_km
                FROM sessions
                WHERE start_ts IS NOT NULL
                  AND date(start_ts) >= date('now', '-30 days')
                GROUP BY day
                ORDER BY day
                """
            ).fetchall()
            totals_30 = conn.execute(
                """
                SELECT
                    SUM(COALESCE(distance_km, 0)) AS total_distance_km,
                    SUM(COALESCE(uphill_m, 0)) AS total_uphill_m
                FROM sessions
                WHERE start_ts IS NOT NULL
                  AND date(start_ts) >= date('now', '-30 days')
                """
            ).fetchone()
            avg_session = conn.execute(
                """
                SELECT AVG(COALESCE(distance_km, 0)) AS avg_distance_km
                FROM sessions
                WHERE distance_km IS NOT NULL
                """
            ).fetchone()
            avg_speed_row = conn.execute(
                """
                SELECT
                    SUM(COALESCE(distance_km, 0)) AS total_distance_km,
                    SUM(COALESCE(duration_sec, 0)) AS total_duration_sec
                FROM sessions
                """
            ).fetchone()
            session_points = []
            for s in sessions:
                point = conn.execute(
                    """
                    SELECT gps_lat, gps_lon
                    FROM logs
                    WHERE device_id = ? AND session_id = ? AND mode = ?
                      AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
                      AND gps_lat != 0 AND gps_lon != 0
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (s["device_id"], s["session_id"], s["mode"]),
                ).fetchone()
                if not point:
                    continue
                lat = float(point["gps_lat"])
                lon = float(point["gps_lon"])
                x, y = _project_point(lat, lon)
                session_points.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "x": x,
                        "y": y,
                        "device_id": s["device_id"],
                        "session_id": s["session_id"],
                    }
                )
        devices = []
        for d in device_rows:
            devices.append(
                dict(d)
                | {
                    "active": _is_active(d["last_seen"]),
                    "last_seen_ago": _format_ago(d["last_seen"], now_utc),
                }
            )
        sessions = [
            dict(s)
            | {
                "start_ts_fmt": _format_dt(s["start_ts"]),
                "end_ts_fmt": _format_dt(s["end_ts"]),
                "uploaded_at_fmt": _format_dt(s["uploaded_at"]),
            }
            for s in sessions
        ]
        daily_all = [
            {"day": row["day"], "distance_km": float(row["distance_km"] or 0)}
            for row in daily_all_rows
        ]
        daily_last_30 = [
            {"day": row["day"], "distance_km": float(row["distance_km"] or 0)}
            for row in daily_last_30_rows
        ]
        total_distance_30 = float(totals_30["total_distance_km"] or 0) if totals_30 else 0.0
        total_uphill_30 = float(totals_30["total_uphill_m"] or 0) if totals_30 else 0.0
        avg_session_distance = float(avg_session["avg_distance_km"] or 0) if avg_session else 0.0
        avg_speed = 0.0
        if avg_speed_row and avg_speed_row["total_duration_sec"]:
            total_duration = float(avg_speed_row["total_duration_sec"] or 0)
            total_distance = float(avg_speed_row["total_distance_km"] or 0)
            if total_duration > 0:
                avg_speed = total_distance / (total_duration / 3600)
        return render_template(
            "index.html",
            devices=devices,
            sessions=sessions,
            session_points=session_points,
            daily_last_30=daily_last_30,
            daily_all=daily_all,
            total_distance_30=total_distance_30,
            total_uphill_30=total_uphill_30,
            avg_session_distance=avg_session_distance,
            avg_speed=avg_speed,
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
        server_seen = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
                    server_seen,
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
            if distance_km is None and isinstance(data.get("metrics"), dict):
                distance_km = data.get("metrics", {}).get("distance_km")
            duration_sec = None
            avg_speed_kph = None
            start_dt = _parse_ts(start_ts)
            end_dt = _parse_ts(end_ts)
            if start_dt and end_dt:
                duration_sec = max(0.0, (end_dt - start_dt).total_seconds())
            prev_alt = None
            total_uphill = 0.0
            for row in logs:
                alt = row.get("gps_alt")
                if alt is None:
                    continue
                try:
                    alt_val = float(alt)
                except Exception:
                    continue
                if prev_alt is not None and alt_val > prev_alt:
                    total_uphill += alt_val - prev_alt
                prev_alt = alt_val
            uphill_m = total_uphill
            if duration_sec and distance_km is not None and duration_sec > 0:
                avg_speed_kph = float(distance_km) / (duration_sec / 3600)
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
                    rows_count, distance_km, duration_sec, avg_speed_kph, uphill_m, metrics_json, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    session_id,
                    mode,
                    start_ts,
                    end_ts,
                    rows_count,
                    distance_km,
                    duration_sec,
                    avg_speed_kph,
                    uphill_m,
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

    @app.route("/api/export_session")
    @_require_auth
    def export_session():
        device_id = request.args.get("device_id")
        session_id = request.args.get("session_id")
        mode = request.args.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            session_row = conn.execute(
                """
                SELECT metrics_json, start_ts, end_ts, rows_count, distance_km, uploaded_at
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (device_id, session_id, mode),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT timestamp, session_id, raw, user,
                       gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop
                FROM logs
                WHERE device_id = ? AND session_id = ? AND mode = ?
                ORDER BY id
                """,
                (device_id, session_id, mode),
            ).fetchall()

        metrics = None
        if session_row and session_row["metrics_json"]:
            try:
                metrics = json.loads(session_row["metrics_json"])
            except Exception:
                metrics = None

        return jsonify(
            {
                "device_id": device_id,
                "session_id": session_id,
                "mode": mode,
                "meta": {
                    "start_ts": session_row["start_ts"] if session_row else None,
                    "end_ts": session_row["end_ts"] if session_row else None,
                    "rows_count": session_row["rows_count"] if session_row else len(rows),
                    "distance_km": session_row["distance_km"] if session_row else None,
                    "uploaded_at": session_row["uploaded_at"] if session_row else None,
                },
                "metrics": metrics,
                "logs": [
                    {
                        "timestamp": r[0],
                        "session": r[1],
                        "raw": r[2],
                        "user": r[3],
                        "gps_lat": r[4],
                        "gps_lon": r[5],
                        "gps_alt": r[6],
                        "gps_speed_kph": r[7],
                        "gps_track_deg": r[8],
                        "gps_fix": r[9],
                        "gps_sats": r[10],
                        "gps_hdop": r[11],
                    }
                    for r in rows
                ],
            }
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("MONITOR_PORT", "8080")))
