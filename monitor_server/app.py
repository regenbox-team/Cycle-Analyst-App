from __future__ import annotations
import base64
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
from xml.sax.saxutils import escape

from flask import Flask, jsonify, request, render_template, Response, send_from_directory, url_for

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

from app.session_summary import compute_session_metrics, format_metric_value, safe_div


TELEMETRY_TABLE = "telemetry_samples"


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


def _media_dir() -> str:
    path = os.getenv("MONITOR_MEDIA_DIR", os.path.join(os.path.dirname(__file__), "media"))
    path = os.path.expanduser(path)
    os.makedirs(path, exist_ok=True)
    return path


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}
    except Exception:
        return set()


def _init_db() -> None:
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                last_seen TEXT,
                last_ip TEXT,
                last_session TEXT,
                session_active INTEGER,
                mode TEXT,
                test_mode INTEGER,
                last_gps_lat REAL,
                last_gps_lon REAL,
                last_gps_ts TEXT,
                gps_available INTEGER
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
            CREATE TABLE IF NOT EXISTS telemetry_samples (
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
                gps_hdop REAL,
                solar_current_a REAL,
                solar_bus_v REAL,
                solar_shunt_v REAL,
                solar_power_w REAL,
                solar_temperature_c REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                captured_at TEXT,
                distance_km REAL,
                interval_km REAL,
                filename TEXT,
                mime_type TEXT,
                relative_path TEXT,
                uploaded_at TEXT,
                test_mode INTEGER DEFAULT 0,
                is_public INTEGER DEFAULT 1,
                gps_lat REAL,
                gps_lon REAL,
                gps_alt REAL,
                gps_speed_kph REAL,
                gps_track_deg REAL,
                gps_fix INTEGER,
                gps_sats INTEGER,
                gps_hdop REAL,
                speed_kph REAL,
                session_distance_km REAL,
                gps_uphill_m REAL,
                solar_power_w REAL,
                generator_power_w REAL,
                solar_wh REAL,
                metrics_json TEXT
            )
            """
        )
        conn.commit()


def _migrate_db() -> None:
    with _get_db() as conn:
        device_columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}
        if "session_active" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN session_active INTEGER")
        if "last_gps_lat" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN last_gps_lat REAL")
        if "last_gps_lon" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN last_gps_lon REAL")
        if "last_gps_ts" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN last_gps_ts TEXT")
        if "gps_available" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN gps_available INTEGER")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        missing = {
            "duration_sec": "REAL",
            "avg_speed_kph": "REAL",
            "uphill_m": "REAL",
        }
        for name, col_type in missing.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {col_type}")

        telemetry_columns = _table_columns(conn, TELEMETRY_TABLE)
        telemetry_missing = {
            "solar_current_a": "REAL",
            "solar_bus_v": "REAL",
            "solar_shunt_v": "REAL",
            "solar_power_w": "REAL",
            "solar_temperature_c": "REAL",
        }
        for name, col_type in telemetry_missing.items():
            if name not in telemetry_columns:
                conn.execute(f"ALTER TABLE {TELEMETRY_TABLE} ADD COLUMN {name} {col_type}")

        if _table_exists(conn, "logs"):
            legacy_columns = _table_columns(conn, "logs")
            sample_columns = [
                "id",
                "device_id",
                "session_id",
                "mode",
                "timestamp",
                "raw",
                "user",
                "gps_lat",
                "gps_lon",
                "gps_alt",
                "gps_speed_kph",
                "gps_track_deg",
                "gps_fix",
                "gps_sats",
                "gps_hdop",
                "solar_current_a",
                "solar_bus_v",
                "solar_shunt_v",
                "solar_power_w",
                "solar_temperature_c",
            ]
            source_columns = [
                name if name in legacy_columns else f"NULL AS {name}"
                for name in sample_columns
            ]
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {TELEMETRY_TABLE} ({", ".join(sample_columns)})
                SELECT {", ".join(source_columns)}
                FROM logs
                """
            )

        photo_columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()}
        photo_missing = {
            "test_mode": "INTEGER DEFAULT 0",
            "is_public": "INTEGER DEFAULT 1",
            "gps_lat": "REAL",
            "gps_lon": "REAL",
            "gps_alt": "REAL",
            "gps_speed_kph": "REAL",
            "gps_track_deg": "REAL",
            "gps_fix": "INTEGER",
            "gps_sats": "INTEGER",
            "gps_hdop": "REAL",
            "speed_kph": "REAL",
            "session_distance_km": "REAL",
            "gps_uphill_m": "REAL",
            "solar_power_w": "REAL",
            "generator_power_w": "REAL",
            "solar_wh": "REAL",
            "metrics_json": "TEXT",
        }
        for name, col_type in photo_missing.items():
            if name not in photo_columns:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {name} {col_type}")
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
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TELEMETRY_TABLE}_device_session_mode
            ON {TELEMETRY_TABLE}(device_id, session_id, mode, timestamp)
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


def _session_map_summary_tiles(metrics: dict[str, Any]) -> list[dict[str, str]]:
    total_Wh = metrics["positive_Wh"] + metrics["regen_Wh"]
    net_Wh = metrics["positive_Wh"] - metrics["regen_Wh"] - metrics["human_Wh"] - metrics["solar_Wh"]
    return [
        {"label": "CA distance", "value": format_metric_value(metrics["distance"], "km")},
        {"label": "GPS distance", "value": format_metric_value(metrics["gps_distance_km"], "km")},
        {"label": "GPS/CA delta", "value": format_metric_value(metrics["gps_distance_km"] - metrics["distance"], "km")},
        {"label": "Net efficiency", "value": format_metric_value(safe_div(net_Wh, metrics["distance"]), "Wh/km")},
        {"label": "Solar energy", "value": format_metric_value(metrics["solar_Wh"], "Wh")},
        {"label": "Solar per km", "value": format_metric_value(safe_div(metrics["solar_Wh"], metrics["distance"]), "Wh/km")},
        {"label": "Solar share", "value": format_metric_value(100 * safe_div(metrics["solar_Wh"], total_Wh), "%")},
        {"label": "Human energy", "value": format_metric_value(metrics["human_Wh"], "Wh")},
        {"label": "Regen energy", "value": format_metric_value(metrics["regen_Wh"], "Wh")},
        {"label": "GPS climb", "value": format_metric_value(metrics["gps_uphill_m"], "m")},
        {"label": "GPS descent", "value": format_metric_value(metrics["gps_downhill_m"], "m")},
        {"label": "GPS fix coverage", "value": format_metric_value(100 * safe_div(metrics["gps_fix_count"], metrics["gps_fix_samples"]), "%")},
        {"label": "Avg GPS satellites", "value": format_metric_value(safe_div(metrics["gps_sats_sum"], metrics["gps_sats_count"]), "")},
        {"label": "Avg GPS HDOP", "value": format_metric_value(safe_div(metrics["gps_hdop_sum"], metrics["gps_hdop_count"]), "")},
        {"label": "Avg solar power", "value": format_metric_value(safe_div(metrics["solar_power_sum"], metrics["solar_power_count"]), "W")},
        {"label": "Max solar power", "value": format_metric_value(metrics["solar_power_max"], "W")},
    ]


def _photo_extension(filename: str | None, mime_type: str | None) -> str:
    filename = (filename or "").lower()
    mime_type = (mime_type or "").lower()
    if filename.endswith(".png") or mime_type == "image/png":
        return ".png"
    return ".jpg"


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=os.path.join(REPO_ROOT, "static"),
        static_url_path="/static",
    )
    _init_db()
    _migrate_db()

    def _is_active(ts: str | None, window_sec: int = 120, future_grace_sec: int = 10) -> bool:
        if not ts:
            return False
        try:
            last = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            if last - now > timedelta(seconds=future_grace_sec):
                return False
            return now - last <= timedelta(seconds=window_sec)
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
            return "clock skew"
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

    def _format_gpx_time(ts: str | None) -> str | None:
        parsed = _parse_ts(ts)
        if not parsed:
            return None
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _safe_float(value) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _safe_int(value) -> int | None:
        try:
            if value is None:
                return None
            return int(value)
        except Exception:
            return None

    def _project_point(lat: float, lon: float, width: float = 1000, height: float = 500) -> tuple[float, float]:
        x = (lon + 180.0) / 360.0 * width
        y = (90.0 - lat) / 180.0 * height
        return x, y

    @app.route("/")
    @_require_auth
    def index():
        now_local = datetime.now()
        with _get_db() as conn:
            device_rows = conn.execute(
                """
                SELECT device_id, last_seen, last_session, session_active, mode, test_mode,
                       last_gps_lat, last_gps_lon, last_gps_ts, gps_available
                FROM devices
                ORDER BY last_seen DESC
                """
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
            latest_photo = conn.execute(
                """
                SELECT device_id, session_id, mode, captured_at, distance_km, interval_km, relative_path
                FROM photos
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            session_points = []
            for s in sessions:
                point = conn.execute(
                    f"""
                    SELECT gps_lat, gps_lon
                    FROM {TELEMETRY_TABLE}
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
        device_locations = []
        for d in device_rows:
            gps_available = bool(d["gps_available"])
            lat = d["last_gps_lat"]
            lon = d["last_gps_lon"]
            if gps_available and lat is not None and lon is not None and lat != 0 and lon != 0:
                device_locations.append(
                    {
                        "device_id": d["device_id"],
                        "lat": float(lat),
                        "lon": float(lon),
                    }
                )
            devices.append(
                dict(d)
                | {
                    "active": _is_active(d["last_seen"]),
                    "last_seen_ago": _format_ago(d["last_seen"], now_local),
                    "gps_available": gps_available,
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
        latest_photo_payload = None
        if latest_photo:
            latest_photo_payload = {
                "device_id": latest_photo["device_id"],
                "session_id": latest_photo["session_id"],
                "mode": latest_photo["mode"],
                "captured_at": latest_photo["captured_at"],
                "distance_km": latest_photo["distance_km"],
                "interval_km": latest_photo["interval_km"],
                "image_url": url_for("photo_file", filename=latest_photo["relative_path"]),
            }
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
            latest_photo=latest_photo_payload,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            device_locations=device_locations,
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
        server_seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        gps_available = 1 if data.get("gps_available") else 0
        gps_lat = data.get("gps_lat") if gps_available else None
        gps_lon = data.get("gps_lon") if gps_available else None
        gps_ts = data.get("gps_timestamp_utc") if gps_available else None
        with _get_db() as conn:
            conn.execute(
                """
                INSERT INTO devices (
                    device_id, last_seen, last_ip, last_session, session_active, mode, test_mode,
                    last_gps_lat, last_gps_lon, last_gps_ts, gps_available
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    last_ip = excluded.last_ip,
                    last_session = excluded.last_session,
                    session_active = excluded.session_active,
                    mode = excluded.mode,
                    test_mode = excluded.test_mode,
                    last_gps_lat = excluded.last_gps_lat,
                    last_gps_lon = excluded.last_gps_lon,
                    last_gps_ts = excluded.last_gps_ts,
                    gps_available = excluded.gps_available
                """,
                (
                    device_id,
                    server_seen,
                    request.remote_addr,
                    data.get("session_id"),
                    int(data.get("session_active") or 0),
                    data.get("mode"),
                    int(data.get("test_mode") or 0),
                    gps_lat,
                    gps_lon,
                    gps_ts,
                    gps_available,
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

            samples = data.get("telemetry_samples") or data.get("logs") or []
            rows_count = len(samples)
            start_ts = samples[0].get("timestamp") if samples else None
            end_ts = samples[-1].get("timestamp") if samples else None
            distance_km = _parse_distance_km(samples[-1].get("raw") if samples else None)
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
            for row in samples:
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
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

            if samples:
                conn.executemany(
                    f"""
                    INSERT INTO {TELEMETRY_TABLE} (
                        device_id, session_id, mode, timestamp, raw, user,
                        gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                        solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            row.get("solar_current_a"),
                            row.get("solar_bus_v"),
                            row.get("solar_shunt_v"),
                            row.get("solar_power_w"),
                            row.get("solar_temperature_c"),
                        )
                        for row in samples
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

    @app.route("/api/upload_photo", methods=["POST"])
    @_require_auth
    def upload_photo():
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        session_id = data.get("session_id")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        image_b64 = data.get("image_b64")
        if not image_b64:
            return jsonify({"error": "missing image_b64"}), 400

        try:
            raw_bytes = base64.b64decode(image_b64)
        except Exception:
            return jsonify({"error": "invalid image_b64"}), 400

        captured_at = data.get("captured_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ext = _photo_extension(data.get("filename"), data.get("mime_type"))
        safe_device = str(device_id).replace("/", "_")
        safe_session = str(session_id).replace("/", "_")
        file_name = f"{captured_at.replace(':', '-').replace(' ', '_')}{ext}"
        relative_dir = os.path.join("photos", safe_device, safe_session)
        absolute_dir = os.path.join(_media_dir(), relative_dir)
        os.makedirs(absolute_dir, exist_ok=True)
        relative_path = os.path.join(relative_dir, file_name)
        absolute_path = os.path.join(_media_dir(), relative_path)

        with open(absolute_path, "wb") as f:
            f.write(raw_bytes)

        uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _get_db() as conn:
            payload_metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
            payload_gps = data.get("gps") if isinstance(data.get("gps"), dict) else {}

            def payload_float(name: str, *metric_names: str) -> float | None:
                value = _safe_float(data.get(name))
                if value is not None:
                    return value
                for metric_name in metric_names:
                    value = _safe_float(payload_metrics.get(metric_name))
                    if value is not None:
                        return value
                return None

            def gps_float(name: str, *nested_names: str) -> float | None:
                value = _safe_float(data.get(name))
                if value is not None:
                    return value
                for nested_name in nested_names:
                    value = _safe_float(payload_gps.get(nested_name))
                    if value is not None:
                        return value
                return None

            def gps_int(name: str, *nested_names: str) -> int | None:
                value = _safe_int(data.get(name))
                if value is not None:
                    return value
                for nested_name in nested_names:
                    value = _safe_int(payload_gps.get(nested_name))
                    if value is not None:
                        return value
                return None

            metrics_json = None
            if data.get("metrics") is not None:
                try:
                    metrics_json = json.dumps(data.get("metrics"))
                except Exception:
                    metrics_json = None
            conn.execute(
                """
                INSERT INTO photos (
                    device_id, session_id, mode, captured_at, distance_km, interval_km,
                    filename, mime_type, relative_path, uploaded_at, test_mode, is_public,
                    gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                    speed_kph, session_distance_km, gps_uphill_m, solar_power_w, generator_power_w, solar_wh,
                    metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    session_id,
                    data.get("mode", "default"),
                    captured_at,
                    data.get("distance_km"),
                    data.get("interval_km"),
                    data.get("filename"),
                    data.get("mime_type") or "image/jpeg",
                    relative_path,
                    uploaded_at,
                    int(data.get("test_mode") or 0),
                    1,
                    gps_float("gps_lat", "lat"),
                    gps_float("gps_lon", "lon"),
                    gps_float("gps_alt", "alt"),
                    gps_float("gps_speed_kph", "speed_kph"),
                    gps_float("gps_track_deg", "track_deg"),
                    gps_int("gps_fix", "fix"),
                    gps_int("gps_sats", "sats"),
                    gps_float("gps_hdop", "hdop"),
                    payload_float("speed_kph", "speed_kph"),
                    payload_float("session_distance_km", "distance_km"),
                    payload_float("gps_uphill_m", "gps_uphill_m"),
                    payload_float("solar_power_w", "solar_power_w"),
                    payload_float("generator_power_w", "generator_power_w"),
                    payload_float("solar_wh", "solar_wh", "solar_Wh"),
                    metrics_json,
                ),
            )
            conn.commit()

        return jsonify(
            {
                "status": "ok",
                "image_url": url_for("photo_file", filename=relative_path, _external=True),
                "public_latest_url": url_for("public_latest_photo", _external=True),
                "public_latest_image_url": url_for("public_latest_photo_jpg", _external=True),
                "uploaded_at": uploaded_at,
            }
        )

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
                f"""
                SELECT timestamp, session_id, raw, user,
                       gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                       solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c
                FROM {TELEMETRY_TABLE}
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

        samples = [
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
                "solar_current_a": r[12],
                "solar_bus_v": r[13],
                "solar_shunt_v": r[14],
                "solar_power_w": r[15],
                "solar_temperature_c": r[16],
            }
            for r in rows
        ]

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
                "telemetry_samples": samples,
                "logs": samples,
            }
        )

    @app.route("/api/export_gpx")
    @_require_auth
    def export_gpx():
        device_id = request.args.get("device_id")
        session_id = request.args.get("session_id")
        mode = request.args.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, gps_lat, gps_lon, gps_alt
                FROM {TELEMETRY_TABLE}
                WHERE device_id = ? AND session_id = ? AND mode = ?
                  AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
                  AND gps_lat != 0 AND gps_lon != 0
                ORDER BY id
                """,
                (device_id, session_id, mode),
            ).fetchall()

        points = []
        for row in rows:
            try:
                lat = float(row["gps_lat"])
                lon = float(row["gps_lon"])
            except Exception:
                continue
            alt = None
            if row["gps_alt"] is not None:
                try:
                    alt = float(row["gps_alt"])
                except Exception:
                    alt = None
            points.append(
                {
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "time": _format_gpx_time(row["timestamp"]),
                }
            )

        session_label = escape(session_id)
        creator = "Cycle Monitor"
        gpx_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1">',
            f"<metadata><name>{session_label}</name></metadata>",
            "<trk>",
            f"<name>{session_label}</name>",
            "<trkseg>",
        ]
        for p in points:
            line = f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">'
            if p["alt"] is not None:
                line += f"<ele>{p['alt']:.1f}</ele>"
            if p["time"]:
                line += f"<time>{p['time']}</time>"
            line += "</trkpt>"
            gpx_lines.append(line)
        gpx_lines.extend(["</trkseg>", "</trk>", "</gpx>"])
        gpx_text = "\n".join(gpx_lines)
        filename = f"{session_id}.gpx"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/gpx+xml; charset=utf-8",
        }
        return Response(gpx_text, headers=headers)

    def _suntrip_photo_payload(row) -> dict[str, Any]:
        image_url = url_for("photo_file", filename=row["relative_path"])
        stored_metrics = {}
        if row["metrics_json"]:
            try:
                stored_metrics = json.loads(row["metrics_json"])
            except Exception:
                stored_metrics = {}

        def metric_value(column: str, *metric_names: str):
            value = row[column]
            if value is not None:
                return value
            for metric_name in metric_names:
                if metric_name in stored_metrics:
                    return stored_metrics.get(metric_name)
            return None

        distance_km = metric_value("session_distance_km", "distance_km")
        if distance_km is None:
            distance_km = row["distance_km"]
        speed_kph = metric_value("speed_kph", "speed_kph")
        if speed_kph is None:
            speed_kph = row["gps_speed_kph"]
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "session_id": row["session_id"],
            "mode": row["mode"],
            "captured_at": row["captured_at"],
            "uploaded_at": row["uploaded_at"],
            "distance_km": distance_km,
            "capture_distance_km": row["distance_km"],
            "interval_km": row["interval_km"],
            "image_url": image_url,
            "gps": {
                "lat": row["gps_lat"],
                "lon": row["gps_lon"],
                "alt": row["gps_alt"],
                "speed_kph": row["gps_speed_kph"],
                "track_deg": row["gps_track_deg"],
                "fix": bool(row["gps_fix"]),
                "sats": row["gps_sats"],
                "hdop": row["gps_hdop"],
            },
            "metrics": {
                "speed_kph": speed_kph,
                "distance_km": distance_km,
                "gps_uphill_m": metric_value("gps_uphill_m", "gps_uphill_m"),
                "solar_power_w": metric_value("solar_power_w", "solar_power_w"),
                "generator_power_w": metric_value("generator_power_w", "generator_power_w"),
                "solar_wh": metric_value("solar_wh", "solar_wh", "solar_Wh"),
            },
        }

    def _suntrip_payload(device_id: str | None = None) -> dict[str, Any]:
        query = """
            SELECT id, device_id, session_id, mode, captured_at, distance_km, interval_km,
                   relative_path, uploaded_at, gps_lat, gps_lon, gps_alt, gps_speed_kph,
                   gps_track_deg, gps_fix, gps_sats, gps_hdop, speed_kph, session_distance_km,
                   gps_uphill_m, solar_power_w, generator_power_w, solar_wh, metrics_json
            FROM photos
            WHERE is_public = 1
        """
        params: list[Any] = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY captured_at DESC, id DESC LIMIT 250"

        with _get_db() as conn:
            rows = conn.execute(query, params).fetchall()

        photos = [_suntrip_photo_payload(row) for row in rows]
        latest = photos[0] if photos else None
        points = []
        for photo in photos:
            gps = photo["gps"]
            lat = _safe_float(gps.get("lat"))
            lon = _safe_float(gps.get("lon"))
            if lat is None or lon is None or lat == 0 or lon == 0:
                continue
            points.append(
                {
                    "id": photo["id"],
                    "lat": lat,
                    "lon": lon,
                    "captured_at": photo["captured_at"],
                    "image_url": photo["image_url"],
                    "latest": bool(latest and photo["id"] == latest["id"]),
                }
            )

        seconds_since_latest = None
        if latest:
            latest_dt = _parse_ts(latest.get("uploaded_at"))
            if latest_dt:
                seconds_since_latest = max(0, int((datetime.now() - latest_dt).total_seconds()))

        return {
            "latest": latest,
            "points": points,
            "seconds_since_latest": seconds_since_latest,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.route("/suntrip")
    @app.route("/public/suntrip")
    def public_suntrip_page():
        return render_template("suntrip.html")

    @app.route("/public/suntrip.json")
    def public_suntrip_data():
        return jsonify(_suntrip_payload(request.args.get("device_id")))

    @app.route("/media/photos/<path:filename>")
    def photo_file(filename: str):
        return send_from_directory(_media_dir(), filename)

    @app.route("/public/latest_photo")
    def public_latest_photo():
        device_id = request.args.get("device_id")
        query = """
            SELECT device_id, session_id, mode, captured_at, distance_km, interval_km, relative_path
            FROM photos
            WHERE is_public = 1
        """
        params: list[Any] = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY captured_at DESC, id DESC LIMIT 1"

        with _get_db() as conn:
            row = conn.execute(query, params).fetchone()

        if not row:
            return jsonify({"error": "no photo found"}), 404

        return jsonify(
            {
                "device_id": row["device_id"],
                "session_id": row["session_id"],
                "mode": row["mode"],
                "captured_at": row["captured_at"],
                "distance_km": row["distance_km"],
                "interval_km": row["interval_km"],
                "image_url": url_for("photo_file", filename=row["relative_path"], _external=True),
            }
        )

    @app.route("/public/photos.json")
    def public_photo_feed():
        limit = max(1, min(100, int(request.args.get("limit", 20))))
        device_id = request.args.get("device_id")
        query = """
            SELECT device_id, session_id, mode, captured_at, distance_km, interval_km, relative_path
            FROM photos
            WHERE is_public = 1
        """
        params: list[Any] = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        params.append(limit)

        with _get_db() as conn:
            rows = conn.execute(query, params).fetchall()

        return jsonify(
            {
                "photos": [
                    {
                        "device_id": row["device_id"],
                        "session_id": row["session_id"],
                        "mode": row["mode"],
                        "captured_at": row["captured_at"],
                        "distance_km": row["distance_km"],
                        "interval_km": row["interval_km"],
                        "image_url": url_for("photo_file", filename=row["relative_path"], _external=True),
                    }
                    for row in rows
                ]
            }
        )

    @app.route("/public/latest_photo.jpg")
    def public_latest_photo_jpg():
        device_id = request.args.get("device_id")
        query = """
            SELECT relative_path
            FROM photos
            WHERE is_public = 1
        """
        params: list[Any] = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY captured_at DESC, id DESC LIMIT 1"

        with _get_db() as conn:
            row = conn.execute(query, params).fetchone()

        if not row:
            return Response("No photo found", 404)
        return send_from_directory(_media_dir(), row["relative_path"])

    @app.route("/session_map")
    @_require_auth
    def session_map():
        device_id = request.args.get("device_id")
        session_id = request.args.get("session_id")
        mode = request.args.get("mode", "default")
        if not device_id or not session_id:
            return Response("missing device_id or session_id", 400)

        with _get_db() as conn:
            session_row = conn.execute(
                """
                SELECT start_ts, end_ts, distance_km, rows_count
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (device_id, session_id, mode),
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT timestamp, raw, user,
                       gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                       solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c
                FROM {TELEMETRY_TABLE}
                WHERE device_id = ? AND session_id = ? AND mode = ?
                ORDER BY id
                """,
                (device_id, session_id, mode),
            ).fetchall()

        points = []
        samples = []
        for row in rows:
            sample = {
                "timestamp": row["timestamp"],
                "raw": row["raw"],
                "user": row["user"],
                "gps_lat": row["gps_lat"],
                "gps_lon": row["gps_lon"],
                "gps_alt": row["gps_alt"],
                "gps_speed_kph": row["gps_speed_kph"],
                "gps_track_deg": row["gps_track_deg"],
                "gps_fix": row["gps_fix"],
                "gps_sats": row["gps_sats"],
                "gps_hdop": row["gps_hdop"],
                "solar_current_a": row["solar_current_a"],
                "solar_bus_v": row["solar_bus_v"],
                "solar_shunt_v": row["solar_shunt_v"],
                "solar_power_w": row["solar_power_w"],
                "solar_temperature_c": row["solar_temperature_c"],
            }
            samples.append(sample)
            try:
                lat = float(row["gps_lat"])
                lon = float(row["gps_lon"])
            except Exception:
                continue
            if lat == 0 or lon == 0:
                continue
            point = {"lat": lat, "lon": lon}
            if row["gps_alt"] is not None:
                try:
                    point["alt"] = float(row["gps_alt"])
                except Exception:
                    point["alt"] = None
            time_str = _format_gpx_time(row["timestamp"])
            if time_str:
                point["time"] = time_str
            points.append(point)
        summary_metrics = compute_session_metrics(samples)

        return render_template(
            "session_map.html",
            device_id=device_id,
            session_id=session_id,
            mode=mode,
            points=points,
            gpx_url=url_for(
                "export_gpx",
                device_id=device_id,
                session_id=session_id,
                mode=mode,
            ),
            start_ts=_format_dt(session_row["start_ts"]) if session_row else "",
            end_ts=_format_dt(session_row["end_ts"]) if session_row else "",
            distance_km=session_row["distance_km"] if session_row else None,
            rows_count=session_row["rows_count"] if session_row else None,
            summary_tiles=_session_map_summary_tiles(summary_metrics),
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("MONITOR_PORT", "8080")))
