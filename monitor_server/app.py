from __future__ import annotations
import base64
import gzip
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
from xml.sax.saxutils import escape

from flask import Flask, jsonify, request, render_template, Response, send_file, send_from_directory, url_for
from jinja2 import ChoiceLoader, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

from app.session_summary import (
    SUMMARY_GROUPS,
    build_summary_sections,
    chronological_samples,
    compute_session_metrics,
    compute_timeline_metrics_by_user,
    filter_plausible_gps_samples,
    format_metric_value,
    gps_segment_plausible,
    parse_timestamp,
    parse_raw_values,
    _haversine_km,
)
from app.distance import update_ca_distance


TELEMETRY_TABLE = "telemetry_samples"
TERRAIN_CACHE_TABLE = "terrain_elevation_cache"
TERRAIN_API_URL = "https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json"
TERRAIN_RESOURCE = os.getenv("MONITOR_TERRAIN_RESOURCE", "ign_rge_alti_wld")
TERRAIN_CACHE_DECIMALS = int(os.getenv("MONITOR_TERRAIN_CACHE_DECIMALS", "5"))
TERRAIN_BATCH_SIZE = int(os.getenv("MONITOR_TERRAIN_BATCH_SIZE", "5000"))
TERRAIN_TIMEOUT_SEC = float(os.getenv("MONITOR_TERRAIN_TIMEOUT_SEC", "8"))
TERRAIN_FALLBACK_DATASET = os.getenv("MONITOR_TERRAIN_FALLBACK_DATASET", "srtm30m")
TERRAIN_FALLBACK_API_URL = os.getenv(
    "MONITOR_TERRAIN_FALLBACK_API_URL",
    f"https://api.opentopodata.org/v1/{TERRAIN_FALLBACK_DATASET}",
)
TERRAIN_FALLBACK_BATCH_SIZE = int(os.getenv("MONITOR_TERRAIN_FALLBACK_BATCH_SIZE", "100"))
TERRAIN_FALLBACK_THROTTLE_SEC = float(os.getenv("MONITOR_TERRAIN_FALLBACK_THROTTLE_SEC", "1.0"))
TERRAIN_BACKFILL_LIMIT_POINTS = int(os.getenv("MONITOR_TERRAIN_BACKFILL_LIMIT_POINTS", "500"))
SUNTRIP_DEFAULT_START_DATE = "2026-05-04"
SUNTRIP_DEFAULT_DEVICE_ALIASES = ("Supercycle-1", "sc-vehicule-1")
SUNTRIP_DEFAULT_PHOTO_LIMIT = 5000
SUNTRIP_ANALYSIS_START_DATE = "2026-05-04"
SUNTRIP_ANALYSIS_END_DATE = "2026-06-04"
SUNTRIP_ANALYSIS_VEHICLES = (
    {
        "key": "supercycle_1",
        "label": "Supercycle 1",
        "aliases": ("Supercycle-1", "supercycle-1", "sc-vehicule-1", "sc-vehicle-1"),
    },
    {
        "key": "supercycle_2",
        "label": "Supercycle 2",
        "aliases": ("Supercycle-2", "supercycle-2", "sc-vehicule-2", "sc-vehicle-2"),
    },
)
DEFAULT_DB_TIMEOUT_SEC = 30.0
HEARTBEAT_ACTIVE_WINDOW_SEC = 120
DEFAULT_STARTUP_LOCK_TIMEOUT_SEC = 45.0
DEFAULT_UPLOAD_CHUNK_MAX_BYTES = 256 * 1024
DEFAULT_RESPONSE_GZIP_MIN_BYTES = 1024
SESSION_TRACE_CACHE_VERSION = 3
SESSION_TRACE_MAX_POINTS = 2500
SESSION_TRACE_BREAK_MIN_GAP_SEC = 120
SESSION_TRACE_BREAK_MIN_DISTANCE_KM = 1.0
SUNTRIP_ANALYSIS_TRACE_MAX_POINTS = 1400
SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_POINTS = 12000
SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_KM = 15.0
SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_MINUTES = 30.0
_SUNTRIP_ANALYSIS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
SUNTRIP_CORRECTION_MIN_DISTANCE_KM = 10.0
SUNTRIP_CORRECTION_MIN_GPS_POINTS = 100
SUNTRIP_CORRECTION_MAX_GPS_REJECTED_RATIO = 0.02
SUNTRIP_CORRECTION_MIN_RATIO = 0.75
SUNTRIP_CORRECTION_MAX_RATIO = 1.25
SUNTRIP_CORRECTION_INLIER_REL_TOLERANCE = 0.03


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _db_path() -> str:
    path = os.getenv("MONITOR_DB", os.path.join(os.path.dirname(__file__), "monitor.db"))
    path = os.path.expanduser(path)
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    return path


def _db_timeout_sec() -> float:
    try:
        return max(0.1, float(os.getenv("MONITOR_DB_TIMEOUT_SEC", str(DEFAULT_DB_TIMEOUT_SEC))))
    except (TypeError, ValueError):
        return DEFAULT_DB_TIMEOUT_SEC


def _startup_lock_timeout_sec() -> float:
    try:
        return max(
            1.0,
            float(os.getenv("MONITOR_STARTUP_LOCK_TIMEOUT_SEC", str(DEFAULT_STARTUP_LOCK_TIMEOUT_SEC))),
        )
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_LOCK_TIMEOUT_SEC


def _upload_chunk_max_bytes() -> int:
    try:
        return max(16 * 1024, int(os.getenv("MONITOR_UPLOAD_CHUNK_MAX_BYTES", str(DEFAULT_UPLOAD_CHUNK_MAX_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_CHUNK_MAX_BYTES


def _response_gzip_min_bytes() -> int:
    try:
        return max(0, int(os.getenv("MONITOR_RESPONSE_GZIP_MIN_BYTES", str(DEFAULT_RESPONSE_GZIP_MIN_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_RESPONSE_GZIP_MIN_BYTES


def _response_gzip_enabled() -> bool:
    return os.getenv("MONITOR_RESPONSE_GZIP", "1").strip().lower() in {"1", "true", "yes", "on"}


def _monitor_backfill_on_startup() -> bool:
    return os.getenv("MONITOR_BACKFILL_ON_STARTUP", "0").strip().lower() in {"1", "true", "yes", "on"}


def _read_json_request(max_bytes: int | None = None) -> tuple[dict[str, Any], tuple[Any, int] | None]:
    encoding = (request.headers.get("Content-Encoding") or "identity").lower()
    if encoding not in {"identity", "gzip"}:
        return {}, (jsonify({"error": "unsupported content encoding"}), 415)
    if encoding == "identity" and max_bytes is not None and request.content_length and request.content_length > max_bytes:
        return {}, (jsonify({"error": "request body too large", "max_bytes": max_bytes}), 413)

    raw = request.get_data(cache=False) or b""
    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return {}, (jsonify({"error": "invalid gzip request body"}), 400)
    if max_bytes is not None and len(raw) > max_bytes:
        return {}, (jsonify({"error": "request body too large", "max_bytes": max_bytes}), 413)
    if not raw:
        return {}, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, (jsonify({"error": "invalid json request body"}), 400)
    if not isinstance(data, dict):
        return {}, (jsonify({"error": "invalid json request body"}), 400)
    return data, None


@contextmanager
def _startup_db_lock():
    lock_path = f"{_db_path()}.startup.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "posix":
            import fcntl

            deadline = time.monotonic() + _startup_lock_timeout_sec()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for monitor DB startup lock: {lock_path}")
                    time.sleep(0.2)
        yield
    finally:
        if locked and os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _get_db() -> sqlite3.Connection:
    timeout_sec = _db_timeout_sec()
    conn = sqlite3.connect(_db_path(), timeout=timeout_sec)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_sec * 1000)}")
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
                solar_enabled INTEGER DEFAULT 1,
                current_user_id TEXT,
                current_user_initials TEXT,
                last_gps_lat REAL,
                last_gps_lon REAL,
                last_gps_ts TEXT,
                gps_available INTEGER,
                map_color TEXT
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
                raw_gps_uphill_m REAL,
                solar_enabled INTEGER DEFAULT 1,
                user_ids_json TEXT,
                metrics_json TEXT,
                suntrip_stage INTEGER DEFAULT 0,
                trace_json TEXT,
                trace_version INTEGER,
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
                user_id TEXT,
                user_initials TEXT,
                user_snapshot_json TEXT,
                gps_lat REAL,
                gps_lon REAL,
                gps_alt REAL,
                terrain_alt_m REAL,
                terrain_alt_source TEXT,
                terrain_alt_updated_at TEXT,
                gps_speed_kph REAL,
                gps_track_deg REAL,
                gps_fix INTEGER,
                gps_sats INTEGER,
                gps_hdop REAL,
                solar_current_a REAL,
                solar_bus_v REAL,
                solar_shunt_v REAL,
                solar_power_w REAL,
                solar_temperature_c REAL,
                solar_enabled INTEGER DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS terrain_elevation_cache (
                cache_key TEXT PRIMARY KEY,
                lat REAL,
                lon REAL,
                terrain_alt_m REAL,
                source TEXT,
                fetched_at TEXT
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
                solar_enabled INTEGER DEFAULT 1,
                user_id TEXT,
                user_initials TEXT,
                user_snapshot_json TEXT,
                metrics_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                initials TEXT,
                first_name TEXT,
                last_name TEXT,
                date_of_birth TEXT,
                gender TEXT,
                active INTEGER DEFAULT 1,
                source_device_id TEXT,
                created_at TEXT,
                updated_at TEXT,
                synced_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                deleted_at TEXT,
                UNIQUE(device_id, session_id, mode)
            )
            """
        )
        conn.commit()


def _migrate_db() -> None:
    with _get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT,
                session_id TEXT,
                mode TEXT,
                deleted_at TEXT,
                UNIQUE(device_id, session_id, mode)
            )
            """
        )
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
        if "solar_enabled" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN solar_enabled INTEGER DEFAULT 1")
        if "current_user_id" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN current_user_id TEXT")
        if "current_user_initials" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN current_user_initials TEXT")
        if "map_color" not in device_columns:
            conn.execute("ALTER TABLE devices ADD COLUMN map_color TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                initials TEXT,
                first_name TEXT,
                last_name TEXT,
                date_of_birth TEXT,
                gender TEXT,
                active INTEGER DEFAULT 1,
                source_device_id TEXT,
                created_at TEXT,
                updated_at TEXT,
                synced_at TEXT
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        missing = {
            "duration_sec": "REAL",
            "avg_speed_kph": "REAL",
            "uphill_m": "REAL",
            "raw_gps_uphill_m": "REAL",
            "solar_enabled": "INTEGER DEFAULT 1",
            "user_ids_json": "TEXT",
            "suntrip_stage": "INTEGER DEFAULT 0",
            "trace_json": "TEXT",
            "trace_version": "INTEGER",
        }
        for name, col_type in missing.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {col_type}")
        conn.execute("UPDATE sessions SET solar_enabled = 1 WHERE solar_enabled IS NULL")
        conn.execute("UPDATE sessions SET suntrip_stage = 0 WHERE suntrip_stage IS NULL")

        telemetry_columns = _table_columns(conn, TELEMETRY_TABLE)
        telemetry_missing = {
            "terrain_alt_m": "REAL",
            "terrain_alt_source": "TEXT",
            "terrain_alt_updated_at": "TEXT",
            "solar_current_a": "REAL",
            "solar_bus_v": "REAL",
            "solar_shunt_v": "REAL",
            "solar_power_w": "REAL",
            "solar_temperature_c": "REAL",
            "solar_enabled": "INTEGER DEFAULT 1",
            "user_id": "TEXT",
            "user_initials": "TEXT",
            "user_snapshot_json": "TEXT",
        }
        for name, col_type in telemetry_missing.items():
            if name not in telemetry_columns:
                conn.execute(f"ALTER TABLE {TELEMETRY_TABLE} ADD COLUMN {name} {col_type}")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TERRAIN_CACHE_TABLE} (
                cache_key TEXT PRIMARY KEY,
                lat REAL,
                lon REAL,
                terrain_alt_m REAL,
                source TEXT,
                fetched_at TEXT
            )
            """
        )
        conn.execute(f"UPDATE {TELEMETRY_TABLE} SET solar_enabled = 1 WHERE solar_enabled IS NULL")
        try:
            from app.user_profiles import legacy_profiles_for_initials, profile_snapshot_json
            initials_rows = conn.execute(
                f"SELECT DISTINCT user FROM {TELEMETRY_TABLE} WHERE user IS NOT NULL AND TRIM(user) != ''"
            ).fetchall()
            legacy_initials = {str(row[0]).strip().upper() for row in initials_rows if row[0]}
            legacy_profiles = legacy_profiles_for_initials(legacy_initials)
            for profile in legacy_profiles:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, initials, first_name, last_name, date_of_birth, gender, active,
                        source_device_id, created_at, updated_at, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        initials = excluded.initials,
                        first_name = excluded.first_name,
                        last_name = excluded.last_name,
                        date_of_birth = excluded.date_of_birth,
                        gender = excluded.gender,
                        updated_at = excluded.updated_at
                    """,
                    (
                        profile["user_id"],
                        profile["initials"],
                        profile["first_name"],
                        profile["last_name"],
                        profile["date_of_birth"],
                        profile["gender"],
                        1 if profile.get("active", True) else 0,
                        None,
                        profile.get("created_at"),
                        profile.get("updated_at"),
                        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.execute(
                    f"""
                    UPDATE {TELEMETRY_TABLE}
                    SET user_id = COALESCE(user_id, ?),
                        user_initials = COALESCE(user_initials, ?),
                        user_snapshot_json = COALESCE(user_snapshot_json, ?)
                    WHERE UPPER(user) = ?
                      AND (user_id IS NULL OR user_initials IS NULL OR user_snapshot_json IS NULL)
                    """,
                    (
                        profile["user_id"],
                        profile["initials"],
                        profile_snapshot_json(profile),
                        profile["initials"],
                    ),
                )
            session_rows = conn.execute(
                """
                SELECT id, device_id, session_id, mode
                FROM sessions
                WHERE user_ids_json IS NULL
                """
            ).fetchall()
            for session in session_rows:
                user_rows = conn.execute(
                    f"""
                    SELECT DISTINCT user_id
                    FROM {TELEMETRY_TABLE}
                    WHERE device_id = ? AND session_id = ? AND mode = ? AND user_id IS NOT NULL
                    """,
                    (session["device_id"], session["session_id"], session["mode"]),
                ).fetchall()
                user_ids = sorted(str(row["user_id"]) for row in user_rows if row["user_id"])
                if user_ids:
                    conn.execute(
                        "UPDATE sessions SET user_ids_json = ? WHERE id = ?",
                        (json.dumps(user_ids), session["id"]),
                    )
        except Exception:
            pass

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
                "user_id",
                "user_initials",
                "user_snapshot_json",
                "gps_lat",
                "gps_lon",
                "gps_alt",
                "terrain_alt_m",
                "terrain_alt_source",
                "terrain_alt_updated_at",
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
                "solar_enabled",
            ]
            source_columns = [
                name if name in legacy_columns else ("1 AS solar_enabled" if name == "solar_enabled" else f"NULL AS {name}")
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
            "solar_enabled": "INTEGER DEFAULT 1",
            "user_id": "TEXT",
            "user_initials": "TEXT",
            "user_snapshot_json": "TEXT",
            "metrics_json": "TEXT",
        }
        for name, col_type in photo_missing.items():
            if name not in photo_columns:
                conn.execute(f"ALTER TABLE photos ADD COLUMN {name} {col_type}")
        conn.execute("UPDATE devices SET solar_enabled = 1 WHERE solar_enabled IS NULL")
        conn.execute("UPDATE photos SET solar_enabled = 1 WHERE solar_enabled IS NULL")
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
        if _monitor_backfill_on_startup():
            _backfill_session_distances(conn)
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


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _parse_upload_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except Exception:
            continue
    return None


def _csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _suntrip_start_filter() -> tuple[str, str] | None:
    raw = os.getenv("SUNTRIP_START_DATE", SUNTRIP_DEFAULT_START_DATE).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def _suntrip_photo_limit() -> int | None:
    raw = os.getenv("SUNTRIP_PHOTO_LIMIT", str(SUNTRIP_DEFAULT_PHOTO_LIMIT)).strip()
    if not raw:
        return SUNTRIP_DEFAULT_PHOTO_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return SUNTRIP_DEFAULT_PHOTO_LIMIT
    return limit if limit > 0 else None


def _suntrip_device_aliases(device_id: str | None) -> list[str]:
    device = (device_id or "").strip()
    configured = _csv_values(os.getenv("SUNTRIP_DEVICE_ALIASES"))
    aliases = configured or list(SUNTRIP_DEFAULT_DEVICE_ALIASES)
    if device and device in aliases:
        return aliases
    return [device] if device else []


def _terrain_enabled() -> bool:
    value = os.getenv("MONITOR_TERRAIN_ELEVATION_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _terrain_fallback_enabled() -> bool:
    value = os.getenv("MONITOR_TERRAIN_FALLBACK_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _valid_lat_lon(lat: Any, lon: Any) -> tuple[float, float] | None:
    lat_f = _safe_float(lat)
    lon_f = _safe_float(lon)
    if lat_f is None or lon_f is None:
        return None
    if lat_f == 0 or lon_f == 0:
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def _terrain_cache_key(lat: float, lon: float) -> str:
    return f"{lat:.{TERRAIN_CACHE_DECIMALS}f},{lon:.{TERRAIN_CACHE_DECIMALS}f}"


def _fetch_ign_terrain_altitudes(points: list[tuple[float, float]]) -> dict[str, dict[str, Any]]:
    if not points:
        return {}
    result: dict[str, dict[str, Any]] = {}
    delimiter = "|"
    batch_size = max(1, min(TERRAIN_BATCH_SIZE, 5000))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Cycle-Analyst-Monitor/1.0",
    }
    for offset in range(0, len(points), batch_size):
        batch = points[offset : offset + batch_size]
        payload = {
            "lon": delimiter.join(f"{lon:.8f}" for lat, lon in batch),
            "lat": delimiter.join(f"{lat:.8f}" for lat, lon in batch),
            "resource": TERRAIN_RESOURCE,
            "delimiter": delimiter,
            "indent": "false",
            "measures": "false",
            "zonly": "false",
        }
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(TERRAIN_API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TERRAIN_TIMEOUT_SEC) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            continue
        elevations = data.get("elevations") if isinstance(data, dict) else None
        if not isinstance(elevations, list):
            continue
        for point, elevation in zip(batch, elevations):
            if not isinstance(elevation, dict):
                continue
            alt = _safe_float(elevation.get("z"))
            if alt is None or alt <= -99998:
                continue
            lat, lon = point
            result[_terrain_cache_key(lat, lon)] = {
                "terrain_alt_m": alt,
                "source": TERRAIN_RESOURCE,
            }
    return result


def _fetch_opentopodata_altitudes(points: list[tuple[float, float]]) -> dict[str, dict[str, Any]]:
    if not points:
        return {}
    result: dict[str, dict[str, Any]] = {}
    delimiter = "|"
    batch_size = max(1, min(TERRAIN_FALLBACK_BATCH_SIZE, 100))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Cycle-Analyst-Monitor/1.0",
    }
    for offset in range(0, len(points), batch_size):
        if offset > 0 and TERRAIN_FALLBACK_THROTTLE_SEC > 0:
            time.sleep(TERRAIN_FALLBACK_THROTTLE_SEC)
        batch = points[offset : offset + batch_size]
        payload = {
            "locations": delimiter.join(f"{lat:.8f},{lon:.8f}" for lat, lon in batch),
            "interpolation": "bilinear",
        }
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(TERRAIN_FALLBACK_API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TERRAIN_TIMEOUT_SEC) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("status") != "OK":
            continue
        elevations = data.get("results")
        if not isinstance(elevations, list):
            continue
        for point, elevation in zip(batch, elevations):
            if not isinstance(elevation, dict):
                continue
            alt = _safe_float(elevation.get("elevation"))
            if alt is None:
                continue
            dataset = str(elevation.get("dataset") or TERRAIN_FALLBACK_DATASET)
            lat, lon = point
            result[_terrain_cache_key(lat, lon)] = {
                "terrain_alt_m": alt,
                "source": f"opentopodata:{dataset}",
            }
    return result


def _fetch_terrain_altitudes(
    points: list[tuple[float, float]],
    *,
    allow_fallback: bool = True,
) -> dict[str, dict[str, Any]]:
    result = _fetch_ign_terrain_altitudes(points)
    if not allow_fallback or not _terrain_fallback_enabled():
        return result
    missing = [point for point in points if _terrain_cache_key(*point) not in result]
    if not missing:
        return result
    fallback = _fetch_opentopodata_altitudes(missing)
    for key, value in fallback.items():
        result.setdefault(key, value)
    return result


def _enrich_samples_with_terrain_altitude(
    conn: sqlite3.Connection,
    samples: list[dict[str, Any]],
    *,
    allow_fallback: bool = True,
) -> int:
    if not samples or not _terrain_enabled():
        return 0

    keyed_points: dict[str, tuple[float, float]] = {}
    for sample in samples:
        if _safe_float(sample.get("terrain_alt_m")) is not None:
            continue
        gps = _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is None:
            continue
        lat, lon = gps
        keyed_points.setdefault(_terrain_cache_key(lat, lon), (lat, lon))

    if not keyed_points:
        return 0

    elevations: dict[str, dict[str, Any]] = {}
    keys = list(keyed_points)
    for offset in range(0, len(keys), 900):
        chunk = keys[offset : offset + 900]
        cached_rows = conn.execute(
            f"""
            SELECT cache_key, terrain_alt_m, source
            FROM {TERRAIN_CACHE_TABLE}
            WHERE cache_key IN ({", ".join("?" for _ in chunk)})
            """,
            tuple(chunk),
        ).fetchall()
        elevations.update(
            {
                row["cache_key"]: {
                    "terrain_alt_m": row["terrain_alt_m"],
                    "source": row["source"],
                }
                for row in cached_rows
                if _safe_float(row["terrain_alt_m"]) is not None
            }
        )

    missing = [point for key, point in keyed_points.items() if key not in elevations]
    fetched = _fetch_terrain_altitudes(missing, allow_fallback=allow_fallback)
    if fetched:
        fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany(
            f"""
            INSERT INTO {TERRAIN_CACHE_TABLE} (cache_key, lat, lon, terrain_alt_m, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                terrain_alt_m = excluded.terrain_alt_m,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    key,
                    keyed_points[key][0],
                    keyed_points[key][1],
                    value["terrain_alt_m"],
                    value["source"],
                    fetched_at,
                )
                for key, value in fetched.items()
                if key in keyed_points
            ],
        )
        elevations.update(fetched)

    updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    enriched = 0
    for sample in samples:
        if _safe_float(sample.get("terrain_alt_m")) is not None:
            continue
        gps = _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is None:
            continue
        value = elevations.get(_terrain_cache_key(*gps))
        if not value:
            continue
        sample["terrain_alt_m"] = value["terrain_alt_m"]
        sample["terrain_alt_source"] = value["source"]
        sample["terrain_alt_updated_at"] = updated_at
        enriched += 1
    return enriched


def _compute_session_distance_km(samples: list[dict[str, Any]]) -> float | None:
    if not samples:
        return None
    if not any(_parse_distance_km(sample.get("raw")) is not None for sample in samples):
        return None
    try:
        metrics = compute_session_metrics(samples)
        distance = metrics.get("distance")
        if distance is not None:
            return float(distance)
    except Exception:
        pass
    return None


def _compute_session_uphill_m(samples: list[dict[str, Any]]) -> float | None:
    if not samples:
        return None
    if not any(
        _safe_float(sample.get("terrain_alt_m")) is not None
        or _safe_float(sample.get("gps_alt")) is not None
        for sample in samples
    ):
        return None
    try:
        metrics = compute_session_metrics(samples)
        uphill = metrics.get("gps_uphill_m")
        if uphill is not None:
            return float(uphill)
    except Exception:
        pass
    return None


def _compute_raw_gps_uphill_m(samples: list[dict[str, Any]]) -> float | None:
    if not samples:
        return None
    if not any(_safe_float(sample.get("gps_alt")) is not None for sample in samples):
        return None
    try:
        metrics = compute_session_metrics(samples)
        uphill = metrics.get("raw_gps_uphill_m")
        if uphill is not None:
            return float(uphill)
    except Exception:
        pass
    return None


def _telemetry_samples_for_session(
    conn: sqlite3.Connection,
    device_id: str,
    session_id: str,
    mode: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT timestamp, raw, user, user_id, user_initials, user_snapshot_json,
               solar_enabled, gps_lat, gps_lon, gps_alt,
               terrain_alt_m, terrain_alt_source, terrain_alt_updated_at,
               gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
               solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c
        FROM {TELEMETRY_TABLE}
        WHERE device_id = ? AND session_id = ? AND mode = ?
        ORDER BY timestamp IS NULL, timestamp, id
        """,
        (device_id, session_id, mode),
    ).fetchall()
    return [
        {
            "timestamp": row["timestamp"],
            "raw": row["raw"],
            "user": row["user"],
            "user_id": row["user_id"],
            "user_initials": row["user_initials"],
            "user_snapshot_json": row["user_snapshot_json"],
            "solar_enabled": row["solar_enabled"],
            "gps_lat": row["gps_lat"],
            "gps_lon": row["gps_lon"],
            "gps_alt": row["gps_alt"],
            "terrain_alt_m": row["terrain_alt_m"],
            "terrain_alt_source": row["terrain_alt_source"],
            "terrain_alt_updated_at": row["terrain_alt_updated_at"],
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
        for row in rows
    ]


def _sanitize_upload_samples(data: dict[str, Any]) -> list[dict[str, Any]]:
    samples = data.get("telemetry_samples") or data.get("logs") or []
    return [dict(row) for row in samples if isinstance(row, dict)]


def _point_segment_distance_sq(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return (px - x1) ** 2 + (py - y1) ** 2
    ratio = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    x = x1 + ratio * dx
    y = y1 + ratio * dy
    return (px - x) ** 2 + (py - y) ** 2


def _douglas_peucker(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    tolerance_sq = tolerance * tolerance
    while stack:
        start_index, end_index = stack.pop()
        farthest_index = -1
        farthest_distance = tolerance_sq
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance_sq(points[index], points[start_index], points[end_index])
            if distance > farthest_distance:
                farthest_distance = distance
                farthest_index = index
        if farthest_index >= 0:
            keep.add(farthest_index)
            stack.append((start_index, farthest_index))
            stack.append((farthest_index, end_index))
    return [point for index, point in enumerate(points) if index in keep]


def _gps_trace_segments(samples: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    segments: list[list[dict[str, Any]]] = []
    previous_sample = None
    previous_gps = None
    for sample in chronological_samples(samples):
        gps = _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is None:
            continue
        should_break = False
        if previous_sample is not None and previous_gps is not None:
            distance_km = _haversine_km(previous_gps, gps)
            previous_ts = parse_timestamp(previous_sample.get("timestamp"))
            current_ts = parse_timestamp(sample.get("timestamp"))
            gap_sec = (
                (current_ts - previous_ts).total_seconds()
                if previous_ts is not None and current_ts is not None
                else None
            )
            should_break = not gps_segment_plausible(
                previous_sample,
                sample,
                distance_km,
            ) or (
                gap_sec is not None
                and gap_sec >= SESSION_TRACE_BREAK_MIN_GAP_SEC
                and distance_km >= SESSION_TRACE_BREAK_MIN_DISTANCE_KM
            )
        if not segments or should_break:
            segments.append([])
        if not segments[-1] or gps != _valid_lat_lon(
            segments[-1][-1].get("gps_lat"),
            segments[-1][-1].get("gps_lon"),
        ):
            segments[-1].append(sample)
        previous_sample = sample
        previous_gps = gps
    return [segment for segment in segments if segment]


def _simplified_session_trace(
    samples: list[dict[str, Any]],
) -> list[list[float] | None]:
    segments = [
        [
            _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
            for sample in segment
        ]
        for segment in _gps_trace_segments(samples)
    ]
    point_segments: list[list[tuple[float, float]]] = [
        [point for point in segment if point is not None]
        for segment in segments
    ]
    point_segments = [segment for segment in point_segments if segment]
    if not point_segments:
        return []

    # Increase the tolerance until the cached geometry is small enough for a fast
    # overview map. Every disconnected segment keeps its own endpoints.
    point_budget = max(
        1,
        SESSION_TRACE_MAX_POINTS - max(0, len(point_segments) - 1),
    )
    tolerance = 0.000002
    simplified_segments = point_segments
    while (
        sum(len(segment) for segment in simplified_segments) > point_budget
        and tolerance <= 0.02
    ):
        simplified_segments = [
            _douglas_peucker(segment, tolerance)
            for segment in point_segments
        ]
        tolerance *= 1.8
    total_points = sum(len(segment) for segment in simplified_segments)
    if total_points > point_budget:
        scale = point_budget / total_points
        sampled_segments = []
        for segment in simplified_segments:
            target = max(2, round(len(segment) * scale)) if len(segment) > 1 else 1
            if target >= len(segment):
                sampled_segments.append(segment)
                continue
            step = (len(segment) - 1) / (target - 1)
            sampled_segments.append(
                [segment[round(index * step)] for index in range(target)]
            )
        simplified_segments = sampled_segments

    result: list[list[float] | None] = []
    for segment in simplified_segments:
        if result:
            result.append(None)
        result.extend(
            [[round(lat, 6), round(lon, 6)] for lat, lon in segment]
        )
    return result


def _store_session_trace(
    conn: sqlite3.Connection,
    device_id: str,
    session_id: str,
    mode: str,
    samples: list[dict[str, Any]],
) -> None:
    trace = _simplified_session_trace(samples)
    conn.execute(
        """
        UPDATE sessions SET trace_json = ?, trace_version = ?
        WHERE device_id = ? AND session_id = ? AND mode = ?
        """,
        (
            json.dumps(trace, separators=(",", ":")),
            SESSION_TRACE_CACHE_VERSION,
            device_id,
            session_id,
            mode,
        ),
    )


def _backfill_session_traces(conn: sqlite3.Connection) -> int:
    missing = conn.execute(
        """
        SELECT device_id, session_id, mode
        FROM sessions
        WHERE trace_json IS NULL OR trace_version IS NULL OR trace_version < ?
        """,
        (SESSION_TRACE_CACHE_VERSION,),
    ).fetchall()
    for session in missing:
        samples = conn.execute(
            f"""
            SELECT gps_lat, gps_lon, timestamp, gps_speed_kph, gps_fix, gps_hdop
            FROM {TELEMETRY_TABLE}
            WHERE device_id = ? AND session_id = ? AND mode = ?
              AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
            ORDER BY id
            """,
            (session["device_id"], session["session_id"], session["mode"]),
        ).fetchall()
        _store_session_trace(
            conn,
            session["device_id"],
            session["session_id"],
            session["mode"],
            [dict(row) for row in samples],
        )
    return len(missing)


def _default_device_color(device_id: str) -> str:
    normalized = (device_id or "").strip().lower()
    if normalized in {"sc-vehicule-1", "sc-vehicle-1", "supercycle-1"}:
        return "#ef4444"
    palette = ("#2563eb", "#16a34a", "#f59e0b", "#9333ea", "#0891b2", "#db2777", "#ea580c")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return palette[int.from_bytes(digest[:2], "big") % len(palette)]


def _payload_solar_enabled(data: dict[str, Any]) -> int:
    payload_metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    return 1 if data.get("solar_enabled", payload_metrics.get("solar_enabled", True)) else 0


def _insert_telemetry_samples(
    conn: sqlite3.Connection,
    device_id: str,
    session_id: str,
    mode: str,
    samples: list[dict[str, Any]],
    solar_enabled: int,
) -> None:
    if not samples:
        return
    conn.executemany(
        f"""
        INSERT INTO {TELEMETRY_TABLE} (
            device_id, session_id, mode, timestamp, raw, user,
            user_id, user_initials, user_snapshot_json,
            gps_lat, gps_lon, gps_alt, terrain_alt_m, terrain_alt_source, terrain_alt_updated_at,
            gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
            solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c, solar_enabled
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                device_id,
                session_id,
                mode,
                row.get("timestamp"),
                row.get("raw"),
                row.get("user"),
                row.get("user_id"),
                row.get("user_initials") or row.get("user"),
                row.get("user_snapshot_json"),
                row.get("gps_lat"),
                row.get("gps_lon"),
                row.get("gps_alt"),
                row.get("terrain_alt_m"),
                row.get("terrain_alt_source"),
                row.get("terrain_alt_updated_at"),
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
                1 if row.get("solar_enabled", solar_enabled) else 0,
            )
            for row in samples
        ],
    )


def _insert_uploaded_session_summary(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    device_id = data.get("device_id")
    session_id = data.get("session_id")
    mode = data.get("mode", "default")
    payload_metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    solar_enabled = _payload_solar_enabled(data)
    rows_count = len(samples)
    start_ts = samples[0].get("timestamp") if samples else None
    end_ts = samples[-1].get("timestamp") if samples else None
    distance_km = _compute_session_distance_km(samples)
    if distance_km is None:
        distance_km = payload_metrics.get("distance_km")
    if distance_km is None:
        distance_km = _parse_distance_km(samples[-1].get("raw") if samples else None)
    duration_sec = None
    avg_speed_kph = None
    start_dt = _parse_upload_ts(start_ts)
    end_dt = _parse_upload_ts(end_ts)
    if start_dt and end_dt:
        duration_sec = max(0.0, (end_dt - start_dt).total_seconds())
    uphill_m = _compute_session_uphill_m(samples) or 0.0
    raw_gps_uphill_m = _compute_raw_gps_uphill_m(samples) or 0.0
    if duration_sec and distance_km is not None and duration_sec > 0:
        avg_speed_kph = float(distance_km) / (duration_sec / 3600)
    user_ids = sorted({str(row.get("user_id")) for row in samples if row.get("user_id")})
    user_ids_json = json.dumps(user_ids) if user_ids else None
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
            rows_count, distance_km, duration_sec, avg_speed_kph, uphill_m, raw_gps_uphill_m,
            solar_enabled, user_ids_json, metrics_json, uploaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            raw_gps_uphill_m,
            solar_enabled,
            user_ids_json,
            metrics_json,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    _store_session_trace(conn, device_id, session_id, mode, samples)
    return {"rows_count": rows_count, "distance_km": distance_km}


def _upsert_upload_device(conn: sqlite3.Connection, data: dict[str, Any], solar_enabled: int, remote_addr: str | None) -> None:
    conn.execute(
        """
        INSERT INTO devices (device_id, last_seen, last_ip, last_session, mode, test_mode, solar_enabled, current_user_id, current_user_initials)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            last_seen = excluded.last_seen,
            last_ip = excluded.last_ip,
            last_session = excluded.last_session,
            mode = excluded.mode,
            test_mode = excluded.test_mode,
            solar_enabled = excluded.solar_enabled,
            current_user_id = excluded.current_user_id,
            current_user_initials = excluded.current_user_initials
        """,
        (
            data.get("device_id"),
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            remote_addr,
            data.get("session_id"),
            data.get("mode", "default"),
            int(data.get("test_mode") or 0),
            solar_enabled,
            data.get("user_id"),
            data.get("user_initials"),
        ),
    )


def _record_heartbeat(conn: sqlite3.Connection, data: dict[str, Any], remote_addr: str | None = None) -> dict[str, Any]:
    device_id = data.get("device_id")
    if not device_id:
        raise ValueError("missing device_id")

    server_seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    gps_available = 1 if data.get("gps_available") else 0
    gps_lat = data.get("gps_lat") if gps_available else None
    gps_lon = data.get("gps_lon") if gps_available else None
    gps_ts = data.get("gps_timestamp_utc") if gps_available else None
    solar_enabled = 1 if data.get("solar_enabled", 1) else 0
    conn.execute(
        """
        INSERT INTO devices (
            device_id, last_seen, last_ip, last_session, session_active, mode, test_mode,
            solar_enabled, current_user_id, current_user_initials, last_gps_lat, last_gps_lon, last_gps_ts, gps_available
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            last_seen = excluded.last_seen,
            last_ip = excluded.last_ip,
            last_session = excluded.last_session,
            session_active = excluded.session_active,
            mode = excluded.mode,
            test_mode = excluded.test_mode,
            solar_enabled = excluded.solar_enabled,
            current_user_id = excluded.current_user_id,
            current_user_initials = excluded.current_user_initials,
            last_gps_lat = excluded.last_gps_lat,
            last_gps_lon = excluded.last_gps_lon,
            last_gps_ts = excluded.last_gps_ts,
            gps_available = excluded.gps_available
        """,
        (
            device_id,
            server_seen,
            remote_addr,
            data.get("session_id"),
            int(data.get("session_active") or 0),
            data.get("mode"),
            int(data.get("test_mode") or 0),
            solar_enabled,
            data.get("user_id"),
            data.get("user_initials"),
            gps_lat,
            gps_lon,
            gps_ts,
            gps_available,
        ),
    )
    return {
        "status": "ok",
        "device_id": device_id,
        "last_seen": server_seen,
        "active_window_sec": HEARTBEAT_ACTIVE_WINDOW_SEC,
    }


def _backfill_session_distances(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            """
            SELECT id, device_id, session_id, mode, distance_km, duration_sec, avg_speed_kph, uphill_m, raw_gps_uphill_m
            FROM sessions
            """
        ).fetchall()
        for row in rows:
            samples = _telemetry_samples_for_session(
                conn,
                row["device_id"],
                row["session_id"],
                row["mode"],
            )
            distance_km = _compute_session_distance_km(samples)
            uphill_m = _compute_session_uphill_m(samples)
            raw_gps_uphill_m = _compute_raw_gps_uphill_m(samples)
            if distance_km is None and uphill_m is None and raw_gps_uphill_m is None:
                continue
            current = row["distance_km"]
            current_uphill = row["uphill_m"]
            current_raw_gps_uphill = row["raw_gps_uphill_m"]
            distance_unchanged = (
                distance_km is None
                or (current is not None and abs(float(current) - distance_km) < 0.001)
            )
            uphill_unchanged = (
                uphill_m is None
                or (current_uphill is not None and abs(float(current_uphill) - uphill_m) < 0.001)
            )
            raw_gps_uphill_unchanged = (
                raw_gps_uphill_m is None
                or (current_raw_gps_uphill is not None and abs(float(current_raw_gps_uphill) - raw_gps_uphill_m) < 0.001)
            )
            if distance_unchanged and uphill_unchanged and raw_gps_uphill_unchanged:
                continue
            if distance_km is None:
                distance_km = current
            if uphill_m is None:
                uphill_m = current_uphill
            if raw_gps_uphill_m is None:
                raw_gps_uphill_m = current_raw_gps_uphill
            avg_speed_kph = None
            if distance_km is not None and row["duration_sec"] and row["duration_sec"] > 0:
                avg_speed_kph = distance_km / (float(row["duration_sec"]) / 3600)
            else:
                avg_speed_kph = row["avg_speed_kph"]
            conn.execute(
                """
                UPDATE sessions
                SET distance_km = ?, avg_speed_kph = ?, uphill_m = ?, raw_gps_uphill_m = ?
                WHERE id = ?
                """,
                (distance_km, avg_speed_kph, uphill_m, raw_gps_uphill_m, row["id"]),
            )
    except Exception:
        pass


def _missing_terrain_cache_key(sample: dict[str, Any]) -> str | None:
    if _safe_float(sample.get("terrain_alt_m")) is not None:
        return None
    gps = _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
    if gps is None:
        return None
    return _terrain_cache_key(*gps)


def _backfill_terrain_altitudes(
    conn: sqlite3.Connection,
    limit_sessions: int | None = None,
    limit_points: int | None = None,
) -> dict[str, int | bool]:
    rows = conn.execute(
        """
        SELECT id, device_id, session_id, mode
        FROM sessions
        ORDER BY start_ts DESC, id DESC
        """
    ).fetchall()
    if limit_sessions and limit_sessions > 0:
        rows = rows[:limit_sessions]
    if limit_points is None:
        limit_points = TERRAIN_BACKFILL_LIMIT_POINTS
    remaining_points = limit_points if limit_points and limit_points > 0 else None

    stats = {
        "sessions_checked": 0,
        "sessions_updated": 0,
        "samples_updated": 0,
        "points_requested": 0,
        "points_limit": int(limit_points or 0),
        "limited": False,
        "cache_entries": 0,
    }
    for row in rows:
        if remaining_points is not None and remaining_points <= 0:
            stats["limited"] = True
            break

        stats["sessions_checked"] += 1
        sample_rows = conn.execute(
            f"""
            SELECT id, timestamp, raw, solar_enabled, gps_lat, gps_lon, gps_alt,
                   terrain_alt_m, terrain_alt_source, terrain_alt_updated_at
            FROM {TELEMETRY_TABLE}
            WHERE device_id = ? AND session_id = ? AND mode = ?
            ORDER BY id
            """,
            (row["device_id"], row["session_id"], row["mode"]),
        ).fetchall()
        samples = [dict(sample_row) for sample_row in sample_rows]
        before = {
            sample["id"]: _safe_float(sample.get("terrain_alt_m"))
            for sample in samples
            if sample.get("id") is not None
        }
        samples_to_enrich = samples
        if remaining_points is not None:
            missing_keys = list(dict.fromkeys(
                key for sample in samples if (key := _missing_terrain_cache_key(sample))
            ))
            if len(missing_keys) > remaining_points:
                stats["limited"] = True
                allowed_keys = set(missing_keys[:remaining_points])
                samples_to_enrich = [
                    sample
                    for sample in samples
                    if _missing_terrain_cache_key(sample) in allowed_keys
                ]
            else:
                allowed_keys = set(missing_keys)
            stats["points_requested"] += len(allowed_keys)
            remaining_points -= len(allowed_keys)

        enriched = _enrich_samples_with_terrain_altitude(conn, samples_to_enrich)
        if not enriched:
            continue
        for sample in samples:
            sample_id = sample.get("id")
            terrain_alt = _safe_float(sample.get("terrain_alt_m"))
            if sample_id is None or terrain_alt is None or before.get(sample_id) is not None:
                continue
            conn.execute(
                f"""
                UPDATE {TELEMETRY_TABLE}
                SET terrain_alt_m = ?,
                    terrain_alt_source = ?,
                    terrain_alt_updated_at = ?
                WHERE id = ?
                """,
                (
                    terrain_alt,
                    sample.get("terrain_alt_source"),
                    sample.get("terrain_alt_updated_at"),
                    sample_id,
                ),
            )
            stats["samples_updated"] += 1
        uphill_m = _compute_session_uphill_m(samples)
        raw_gps_uphill_m = _compute_raw_gps_uphill_m(samples)
        if uphill_m is not None:
            conn.execute(
                "UPDATE sessions SET uphill_m = ?, raw_gps_uphill_m = ? WHERE id = ?",
                (uphill_m, raw_gps_uphill_m, row["id"]),
            )
        stats["sessions_updated"] += 1
    cache_count = conn.execute(f"SELECT COUNT(*) FROM {TERRAIN_CACHE_TABLE}").fetchone()
    stats["cache_entries"] = int(cache_count[0] or 0)
    return stats


def _photo_extension(filename: str | None, mime_type: str | None) -> str:
    filename = (filename or "").lower()
    mime_type = (mime_type or "").lower()
    if filename.endswith(".png") or mime_type == "image/png":
        return ".png"
    return ".jpg"


def _remove_empty_parent_dirs(path: str, stop_dir: str) -> None:
    current = os.path.dirname(path)
    stop_dir = os.path.abspath(stop_dir)
    while current and os.path.abspath(current).startswith(stop_dir) and os.path.abspath(current) != stop_dir:
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def _delete_media_files(relative_paths: list[str]) -> int:
    media_dir = os.path.abspath(_media_dir())
    removed = 0
    for relative_path in relative_paths:
        if not relative_path:
            continue
        absolute_path = os.path.abspath(os.path.join(media_dir, relative_path))
        if not absolute_path.startswith(media_dir + os.sep):
            continue
        try:
            os.remove(absolute_path)
            removed += 1
            _remove_empty_parent_dirs(absolute_path, media_dir)
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return removed


def _delete_session_data(device_id: str, session_id: str, mode: str) -> dict[str, Any]:
    with _get_db() as conn:
        photo_rows = conn.execute(
            """
            SELECT relative_path
            FROM photos
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        ).fetchall()
        photo_paths = [row["relative_path"] for row in photo_rows if row["relative_path"]]

        session_cursor = conn.execute(
            """
            DELETE FROM sessions
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        )
        samples_cursor = conn.execute(
            f"""
            DELETE FROM {TELEMETRY_TABLE}
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        )
        photos_cursor = conn.execute(
            """
            DELETE FROM photos
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        )
        conn.execute(
            """
            UPDATE devices
            SET last_session = NULL
            WHERE device_id = ? AND last_session = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        )
        conn.execute(
            """
            INSERT INTO deleted_sessions (device_id, session_id, mode, deleted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id, session_id, mode) DO UPDATE SET
                deleted_at = excluded.deleted_at
            """,
            (
                device_id,
                session_id,
                mode,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()

    deleted_sessions = max(session_cursor.rowcount, 0)
    deleted_samples = max(samples_cursor.rowcount, 0)
    deleted_photos = max(photos_cursor.rowcount, 0)
    deleted_files = _delete_media_files(photo_paths)
    return {
        "status": "deleted",
        "device_id": device_id,
        "session_id": session_id,
        "mode": mode,
        "deleted_sessions": deleted_sessions,
        "deleted_samples": deleted_samples,
        "deleted_photos": deleted_photos,
        "deleted_files": deleted_files,
    }


def _delete_sessions_data(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    totals = {
        "deleted_sessions": 0,
        "deleted_samples": 0,
        "deleted_photos": 0,
        "deleted_files": 0,
    }
    for session in sessions:
        device_id = str(session.get("device_id") or "").strip()
        session_id = str(session.get("session_id") or "").strip()
        mode = str(session.get("mode") or "default").strip() or "default"
        if not device_id or not session_id:
            results.append(
                {
                    "status": "skipped",
                    "error": "missing device_id or session_id",
                    "device_id": device_id,
                    "session_id": session_id,
                    "mode": mode,
                }
            )
            continue

        result = _delete_session_data(device_id, session_id, mode)
        if result["deleted_sessions"] == 0 and result["deleted_samples"] == 0 and result["deleted_photos"] == 0:
            result["status"] = "not_found"
        results.append(result)
        for key in totals:
            totals[key] += int(result.get(key) or 0)

    deleted_count = sum(1 for result in results if result.get("status") == "deleted")
    return {
        "status": "deleted",
        "requested": len(sessions),
        "deleted_count": deleted_count,
        "results": results,
        **totals,
    }


def _normalize_device_id(value: str | None) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _suntrip_vehicle_for_device(device_id: str | None) -> dict[str, Any] | None:
    normalized = _normalize_device_id(device_id)
    for vehicle in SUNTRIP_ANALYSIS_VEHICLES:
        aliases = [_normalize_device_id(str(alias)) for alias in vehicle["aliases"]]
        if normalized in aliases:
            return vehicle
    return None


def _session_day(start_ts: str | None, session_id: str | None) -> datetime.date | None:
    parsed = _parse_upload_ts(start_ts)
    if parsed:
        return parsed.date()
    if session_id:
        try:
            return datetime.strptime(str(session_id)[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _parse_date_param(value: str | None, fallback: str) -> datetime.date:
    raw = value or fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return datetime.strptime(fallback, "%Y-%m-%d").date()


def _session_payload_from_request(data: dict[str, Any]) -> dict[str, str]:
    return {
        "device_id": str(data.get("device_id") or "").strip(),
        "session_id": str(data.get("session_id") or "").strip(),
        "mode": str(data.get("mode") or "default").strip() or "default",
    }


def _session_key(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    normalized = _session_payload_from_request(payload)
    if not normalized["device_id"] or not normalized["session_id"]:
        return None
    return normalized["device_id"], normalized["session_id"], normalized["mode"]


def _photo_counts_for_sessions(
    conn: sqlite3.Connection,
    session_keys: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for device_id, session_id, mode in session_keys:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM photos
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            (device_id, session_id, mode),
        ).fetchone()
        counts[(device_id, session_id, mode)] = int(row["count"] or 0) if row else 0
    return counts


def _safe_media_path(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    media_dir = os.path.abspath(_media_dir())
    absolute_path = os.path.abspath(os.path.join(media_dir, relative_path))
    if not absolute_path.startswith(media_dir + os.sep):
        return None
    return absolute_path


def _photo_video_rows(conn: sqlite3.Connection, sessions: list[dict[str, Any]]) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    seen = set()
    for raw in sessions:
        key = _session_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.extend(
            conn.execute(
                """
                SELECT device_id, session_id, mode, captured_at, distance_km, interval_km,
                       filename, mime_type, relative_path, uploaded_at, gps_lat, gps_lon,
                       gps_alt, gps_speed_kph, speed_kph, session_distance_km, gps_uphill_m,
                       solar_power_w, generator_power_w, solar_wh, metrics_json
                FROM photos
                WHERE device_id = ? AND session_id = ? AND mode = ?
                ORDER BY captured_at, id
                """,
                key,
            ).fetchall()
        )
    return rows


def _solar_profile_for_sessions(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]],
    *,
    max_points_per_session: int = 1440,
) -> dict[str, Any]:
    max_points = max(24, min(1440, int(max_points_per_session or 1440)))
    bucket_minutes = max(1, math.ceil(1440 / max_points))
    profiles = []
    seen = set()
    requested = 0
    total_raw_samples = 0
    total_profile_points = 0

    for raw in sessions:
        key = _session_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        requested += 1
        device_id, session_id, mode = key
        session_row = conn.execute(
            """
            SELECT start_ts, end_ts, rows_count, distance_km
            FROM sessions
            WHERE device_id = ? AND session_id = ? AND mode = ?
            """,
            key,
        ).fetchone()
        if not session_row:
            profiles.append(
                {
                    "device_id": device_id,
                    "session_id": session_id,
                    "mode": mode,
                    "label": f"{device_id} / {session_id}",
                    "status": "not_found",
                    "points": [],
                    "raw_sample_count": 0,
                }
            )
            continue

        gps_row = conn.execute(
            f"""
            SELECT AVG(gps_lat) AS avg_lat, AVG(gps_lon) AS avg_lon
            FROM {TELEMETRY_TABLE}
            WHERE device_id = ? AND session_id = ? AND mode = ?
              AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
              AND gps_lat != 0 AND gps_lon != 0
            """,
            key,
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT timestamp, solar_power_w, solar_current_a, solar_bus_v, solar_enabled
            FROM {TELEMETRY_TABLE}
            WHERE device_id = ? AND session_id = ? AND mode = ?
              AND timestamp IS NOT NULL
              AND COALESCE(solar_enabled, 1) = 1
              AND (
                solar_power_w IS NOT NULL
                OR (solar_current_a IS NOT NULL AND solar_bus_v IS NOT NULL)
              )
            ORDER BY id
            """,
            key,
        ).fetchall()

        buckets: dict[int, dict[str, float]] = {}
        raw_sample_count = 0
        for row in rows:
            ts = _parse_upload_ts(row["timestamp"])
            if not ts:
                continue
            power = _safe_float(row["solar_power_w"])
            if power is None:
                current = _safe_float(row["solar_current_a"])
                voltage = _safe_float(row["solar_bus_v"])
                if current is None or voltage is None:
                    continue
                power = current * voltage
            power = max(0.0, power)
            minute_of_day = ts.hour * 60 + ts.minute + ts.second / 60.0
            bucket = min(1439, int(minute_of_day // bucket_minutes) * bucket_minutes)
            item = buckets.setdefault(bucket, {"sum": 0.0, "count": 0.0})
            item["sum"] += power
            item["count"] += 1
            raw_sample_count += 1

        points = [
            {
                "hour": round((bucket + bucket_minutes / 2) / 60.0, 4),
                "w": round(values["sum"] / max(1.0, values["count"]), 3),
            }
            for bucket, values in sorted(buckets.items())
            if values["count"] > 0
        ]
        total_raw_samples += raw_sample_count
        total_profile_points += len(points)
        label_date = session_row["start_ts"] or session_id
        start_dt = _parse_upload_ts(session_row["start_ts"]) if session_row["start_ts"] else None
        avg_lat = _safe_float(gps_row["avg_lat"]) if gps_row else None
        avg_lon = _safe_float(gps_row["avg_lon"]) if gps_row else None
        profiles.append(
            {
                "device_id": device_id,
                "session_id": session_id,
                "mode": mode,
                "label": f"{device_id} / {session_id}",
                "status": "ok",
                "start_ts": session_row["start_ts"],
                "end_ts": session_row["end_ts"],
                "rows_count": session_row["rows_count"],
                "distance_km": session_row["distance_km"],
                "day_label": str(label_date)[:10],
                "date": start_dt.date().isoformat() if start_dt else str(label_date)[:10],
                "day_of_year": start_dt.timetuple().tm_yday if start_dt else None,
                "avg_lat": avg_lat,
                "avg_lon": avg_lon,
                "raw_sample_count": raw_sample_count,
                "point_count": len(points),
                "points": points,
            }
        )

    ok_profiles = [profile for profile in profiles if profile.get("status") == "ok" and profile.get("points")]
    return {
        "status": "ok",
        "requested": requested,
        "session_count": len(ok_profiles),
        "raw_sample_count": total_raw_samples,
        "profile_point_count": total_profile_points,
        "bucket_minutes": bucket_minutes,
        "reference": {
            "default_panel_max_w": _float_env("APP_SOLAR_PANEL_MAX_W", 690.0),
            "default_lat": _float_env("APP_SOLAR_LAT", 48.8566),
            "default_lon": _float_env("APP_SOLAR_LON", 2.3522),
        },
        "profiles": profiles,
    }


def _format_photo_video_metadata(row: sqlite3.Row) -> list[str]:
    metrics = {}
    if row["metrics_json"]:
        try:
            parsed = json.loads(row["metrics_json"])
            if isinstance(parsed, dict):
                metrics = parsed
        except Exception:
            metrics = {}
    distance = _safe_float(row["distance_km"])
    session_distance = _safe_float(row["session_distance_km"]) or _safe_float(metrics.get("distance_km"))
    gps_lat = _safe_float(row["gps_lat"])
    gps_lon = _safe_float(row["gps_lon"])
    speed = _safe_float(row["speed_kph"]) or _safe_float(row["gps_speed_kph"]) or _safe_float(metrics.get("speed_kph"))
    solar_power = _safe_float(row["solar_power_w"]) or _safe_float(metrics.get("solar_power_w"))
    generator_power = _safe_float(row["generator_power_w"]) or _safe_float(metrics.get("generator_power_w"))
    solar_wh = _safe_float(row["solar_wh"]) or _safe_float(metrics.get("solar_Wh")) or _safe_float(metrics.get("solar_wh"))
    line1 = f"{row['device_id']} | {row['session_id']} | {row['mode']} | {row['captured_at'] or ''}"
    line2_parts = []
    if distance is not None:
        line2_parts.append(f"photo {distance:.2f} km")
    if session_distance is not None:
        line2_parts.append(f"session {session_distance:.2f} km")
    if speed is not None:
        line2_parts.append(f"{speed:.1f} km/h")
    if gps_lat is not None and gps_lon is not None:
        line2_parts.append(f"GPS {gps_lat:.5f}, {gps_lon:.5f}")
    line3_parts = []
    if solar_power is not None:
        line3_parts.append(f"solar {solar_power:.0f} W")
    if generator_power is not None:
        line3_parts.append(f"generator {generator_power:.0f} W")
    if solar_wh is not None:
        line3_parts.append(f"solar {solar_wh:.0f} Wh")
    return [line for line in (line1, " | ".join(line2_parts), " | ".join(line3_parts)) if line]


def _load_video_font(size: int):
    from PIL import ImageFont

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_photo_video_frame(row: sqlite3.Row, source_path: str, output_path: str) -> None:
    from PIL import Image, ImageDraw, ImageOps

    width = 1280
    height = 900
    meta_height = 150
    photo_height = height - meta_height
    background = Image.new("RGB", (width, height), "#111111")
    with Image.open(source_path) as raw_image:
        image = ImageOps.exif_transpose(raw_image).convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        image.thumbnail((width, photo_height), resample)
        x = (width - image.width) // 2
        y = (photo_height - image.height) // 2
        background.paste(image, (x, y))
    draw = ImageDraw.Draw(background)
    draw.rectangle((0, photo_height, width, height), fill="#f7f3ec")
    title_font = _load_video_font(28)
    body_font = _load_video_font(22)
    y = photo_height + 22
    for index, line in enumerate(_format_photo_video_metadata(row)[:3]):
        font = title_font if index == 0 else body_font
        draw.text((34, y), line, fill="#1a1a1a", font=font)
        y += 38 if index == 0 else 30
    background.save(output_path, "JPEG", quality=92, optimize=True)


def _build_photo_video(rows: list[sqlite3.Row], *, fps: int = 24) -> tuple[str, int, int]:
    try:
        import PIL  # noqa: F401
    except Exception as exc:
        raise RuntimeError("Pillow is required to render photo metadata. Run: pip install -r requirements.txt") from exc

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to create the MP4 video. Install it on Ubuntu with: sudo apt install ffmpeg")

    fps = max(1, min(60, int(fps or 24)))
    work_dir = tempfile.mkdtemp(prefix="monitor-photo-video-")
    output_path = os.path.join(work_dir, "monitor_photos.mp4")
    frame_count = 0
    missing_count = 0
    try:
        for row in rows:
            source_path = _safe_media_path(row["relative_path"])
            if not source_path or not os.path.exists(source_path):
                missing_count += 1
                continue
            frame_path = os.path.join(work_dir, f"frame_{frame_count:06d}.jpg")
            _render_photo_video_frame(row, source_path, frame_path)
            frame_count += 1
        if frame_count == 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError("No photo files were found on disk for the selected sessions.")

        command = [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            os.path.join(work_dir, "frame_%06d.jpg"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            output_path,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            detail = (result.stderr or result.stdout or "ffmpeg failed").strip().splitlines()[-1]
            raise RuntimeError(f"ffmpeg failed: {detail}")
        return output_path, frame_count, missing_count
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _set_suntrip_stage_sessions(sessions: list[dict[str, Any]], suntrip_stage: bool) -> dict[str, Any]:
    results = []
    updated_count = 0
    with _get_db() as conn:
        for raw in sessions:
            payload = _session_payload_from_request(raw)
            if not payload["device_id"] or not payload["session_id"]:
                results.append({**payload, "status": "skipped", "error": "missing device_id or session_id"})
                continue
            cursor = conn.execute(
                """
                UPDATE sessions
                SET suntrip_stage = ?
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (
                    1 if suntrip_stage else 0,
                    payload["device_id"],
                    payload["session_id"],
                    payload["mode"],
                ),
            )
            if cursor.rowcount:
                updated_count += 1
                results.append({**payload, "status": "updated", "suntrip_stage": bool(suntrip_stage)})
            else:
                results.append({**payload, "status": "not_found", "suntrip_stage": bool(suntrip_stage)})
        conn.commit()
    return {
        "status": "ok",
        "requested": len(sessions),
        "updated_count": updated_count,
        "suntrip_stage": bool(suntrip_stage),
        "results": results,
    }


def _aggregate_session_metrics(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = compute_session_metrics([])
    if not metrics_list:
        return aggregate

    sum_keys = {
        "sample_count",
        "speed_sum",
        "speed_count",
        "power_sum",
        "human_power_sum",
        "human_power_count",
        "solar_power_sum",
        "solar_power_count",
        "positive_Wh",
        "regen_Wh",
        "human_Wh",
        "solar_Wh",
        "temp_sum",
        "temp_count",
        "distance",
        "Ah",
        "ca_Ah_raw",
        "duration",
        "ca_reset_count",
        "gps_points",
        "gps_distance_km",
        "gps_uphill_m",
        "gps_downhill_m",
        "raw_gps_uphill_m",
        "raw_gps_downhill_m",
        "gps_speed_sum",
        "gps_speed_count",
        "gps_fix_count",
        "gps_fix_samples",
        "gps_sats_sum",
        "gps_sats_count",
        "gps_hdop_sum",
        "gps_hdop_count",
        "solar_samples",
    }
    max_keys = {
        "speed_max",
        "power_max",
        "human_power_max",
        "solar_power_max",
        "temp_max",
        "gps_speed_max",
        "gps_alt_max",
        "raw_gps_alt_max",
    }
    min_keys = {"power_min", "gps_alt_min", "raw_gps_alt_min"}

    for key in sum_keys:
        aggregate[key] = sum(float(metrics.get(key) or 0) for metrics in metrics_list)
    for key in max_keys:
        values = [metrics.get(key) for metrics in metrics_list if metrics.get(key) is not None]
        aggregate[key] = max(values) if values else aggregate.get(key)
    for key in min_keys:
        values = [metrics.get(key) for metrics in metrics_list if metrics.get(key) is not None]
        aggregate[key] = min(values) if values else aggregate.get(key)
    aggregate["solar_enabled"] = all(bool(metrics.get("solar_enabled", True)) for metrics in metrics_list)
    return aggregate


def _ca_gps_correction_sample(metrics: dict[str, Any]) -> dict[str, Any] | None:
    ca_distance = _safe_float(metrics.get("distance"))
    gps_distance = _safe_float(metrics.get("gps_distance_km"))
    gps_points = int(_safe_float(metrics.get("gps_points")) or 0)
    rejected = int(_safe_float(metrics.get("gps_distance_rejected_count")) or 0)
    ca_resets = int(_safe_float(metrics.get("ca_reset_count")) or 0)
    if ca_distance is None or gps_distance is None:
        return None
    if ca_distance < SUNTRIP_CORRECTION_MIN_DISTANCE_KM or gps_distance < SUNTRIP_CORRECTION_MIN_DISTANCE_KM:
        return None
    if gps_points < SUNTRIP_CORRECTION_MIN_GPS_POINTS:
        return None
    rejected_ratio = rejected / max(1, gps_points + rejected)
    if rejected_ratio > SUNTRIP_CORRECTION_MAX_GPS_REJECTED_RATIO:
        return None
    if ca_resets:
        return None
    ratio = gps_distance / ca_distance
    if ratio < SUNTRIP_CORRECTION_MIN_RATIO or ratio > SUNTRIP_CORRECTION_MAX_RATIO:
        return None
    return {
        "ratio": ratio,
        "ca_distance_km": ca_distance,
        "gps_distance_km": gps_distance,
        "gps_points": gps_points,
        "gps_rejected_ratio": rejected_ratio,
    }


def _weighted_mean_ratio(samples: list[dict[str, Any]]) -> float | None:
    total_weight = sum(float(sample["ca_distance_km"]) for sample in samples)
    if total_weight <= 0:
        return None
    return sum(float(sample["ratio"]) * float(sample["ca_distance_km"]) for sample in samples) / total_weight


def _suntrip_vehicle_corrections(columns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_vehicle: dict[str, list[dict[str, Any]]] = {}
    for column in columns:
        sample = _ca_gps_correction_sample(column["metrics"])
        if not sample:
            continue
        sample["session_id"] = column.get("session_id")
        sample["vehicle_key"] = column.get("vehicle_key")
        by_vehicle.setdefault(column["vehicle_key"], []).append(sample)

    corrections: dict[str, dict[str, Any]] = {}
    for vehicle in SUNTRIP_ANALYSIS_VEHICLES:
        vehicle_key = vehicle["key"]
        samples = by_vehicle.get(vehicle_key, [])
        if not samples:
            corrections[vehicle_key] = {
                "vehicle_key": vehicle_key,
                "vehicle_label": vehicle["label"],
                "factor": 1.0,
                "available": False,
                "session_count": 0,
                "inlier_count": 0,
                "note": "No reliable CA/GPS calibration sessions",
            }
            continue
        ratios = sorted(float(sample["ratio"]) for sample in samples)
        median = ratios[len(ratios) // 2] if len(ratios) % 2 else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
        inliers = [
            sample
            for sample in samples
            if abs(float(sample["ratio"]) - median) / max(1e-9, median) <= SUNTRIP_CORRECTION_INLIER_REL_TOLERANCE
        ]
        if not inliers:
            inliers = samples
        factor = _weighted_mean_ratio(inliers) or median
        max_rel_spread = max(
            (abs(float(sample["ratio"]) - factor) / max(1e-9, factor) for sample in inliers),
            default=0.0,
        )
        corrections[vehicle_key] = {
            "vehicle_key": vehicle_key,
            "vehicle_label": vehicle["label"],
            "factor": factor,
            "available": True,
            "session_count": len(samples),
            "inlier_count": len(inliers),
            "max_rel_spread": max_rel_spread,
            "mean_ca_distance_km": sum(float(sample["ca_distance_km"]) for sample in inliers) / len(inliers),
            "note": "Reliable" if len(inliers) >= 2 and max_rel_spread <= 0.01 else "Limited calibration sample",
        }
    return corrections


def _apply_ca_gps_correction(metrics: dict[str, Any], factor: float | None) -> dict[str, Any]:
    corrected = dict(metrics)
    factor = _safe_float(factor)
    if factor is None or factor <= 0:
        return corrected
    for key in ("distance", "speed_sum", "speed_max"):
        value = _safe_float(corrected.get(key))
        if value is not None:
            corrected[key] = value * factor
    corrected["ca_gps_correction_factor"] = factor
    return corrected


def _db_state_key() -> tuple[str, int | None, int | None]:
    path = _db_path()
    try:
        stat = os.stat(path)
        return path, stat.st_mtime_ns, stat.st_size
    except OSError:
        return path, None, None


def _limited_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _downsample_points(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled


def _metric_options_from_specs() -> list[dict[str, str]]:
    return [
        {"key": "ca_speed_kph", "label": "CA speed", "unit": "km/h"},
        {"key": "gps_speed_kph", "label": "GPS speed", "unit": "km/h"},
        {"key": "battery_power_w", "label": "Battery power", "unit": "W"},
        {"key": "human_power_w", "label": "Human power", "unit": "W"},
        {"key": "solar_power_w", "label": "Solar power", "unit": "W"},
        {"key": "battery_current_a", "label": "Battery current", "unit": "A"},
        {"key": "human_current_a", "label": "Human current", "unit": "A"},
        {"key": "voltage_v", "label": "Battery voltage", "unit": "V"},
        {"key": "temperature_c", "label": "CA temperature", "unit": "C"},
        {"key": "altitude_m", "label": "Altitude", "unit": "m"},
        {"key": "gradient_percent", "label": "Gradient", "unit": "%"},
        {"key": "ca_distance_km", "label": "CA distance", "unit": "km"},
        {"key": "gps_distance_km", "label": "GPS distance", "unit": "km"},
        {"key": "elapsed_min", "label": "Elapsed time", "unit": "min"},
        {"key": "battery_used_ah", "label": "Battery used", "unit": "Ah"},
        {"key": "positive_wh", "label": "Positive energy", "unit": "Wh"},
        {"key": "regen_wh", "label": "Regen energy", "unit": "Wh"},
        {"key": "human_wh", "label": "Human energy", "unit": "Wh"},
        {"key": "solar_wh", "label": "Solar energy", "unit": "Wh"},
        {"key": "net_wh", "label": "Net energy", "unit": "Wh"},
    ]


def _trace_metric_option_map() -> dict[str, dict[str, str]]:
    return {option["key"]: option for option in _metric_options_from_specs()}


def _sample_metric_value(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    return _safe_float(value)


def _add_trace_gradient_metrics(points: list[dict[str, Any]]) -> None:
    window_km = 0.25
    min_km = 0.05
    for index, point in enumerate(points):
        metrics = point.get("metrics") or {}
        distance = _safe_float(metrics.get("gps_distance_km"))
        altitude = _safe_float(metrics.get("altitude_m"))
        if distance is None or altitude is None:
            continue
        reference: tuple[float, float] | None = None
        for previous in reversed(points[:index]):
            previous_metrics = previous.get("metrics") or {}
            previous_distance = _safe_float(previous_metrics.get("gps_distance_km"))
            previous_altitude = _safe_float(previous_metrics.get("altitude_m"))
            if previous_distance is None or previous_altitude is None:
                continue
            delta_km = distance - previous_distance
            if delta_km < min_km:
                continue
            reference = (previous_distance, previous_altitude)
            if delta_km >= window_km:
                break
        if reference is None:
            continue
        delta_km = distance - reference[0]
        if delta_km <= 0:
            continue
        gradient = ((altitude - reference[1]) / (delta_km * 1000.0)) * 100.0
        if math.isfinite(gradient):
            metrics["gradient_percent"] = round(max(-35.0, min(35.0, gradient)), 3)


def _session_trace_points(
    samples: list[dict[str, Any]],
    max_points: int,
    *,
    range_axis: str | None = None,
    range_min: float | None = None,
    range_max: float | None = None,
    overview_max_points: int | None = None,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]] | None]:
    first_ts = None
    previous_ts = None
    previous_values = None
    previous_gps_sample = None
    previous_gps = None
    distance_store: dict[str, Any] = {}
    ca_distance_km = 0.0
    gps_distance_km = 0.0
    battery_used_ah = 0.0
    positive_wh = 0.0
    regen_wh = 0.0
    human_wh = 0.0
    solar_wh = 0.0
    points: list[dict[str, Any]] = []

    for sample in samples:
        values = parse_raw_values(sample.get("raw"))
        current_ts = _parse_upload_ts(sample.get("timestamp"))
        if current_ts is not None and first_ts is None:
            first_ts = current_ts
        dt = 0.0
        if previous_ts is not None and current_ts is not None:
            candidate_dt = (current_ts - previous_ts).total_seconds()
            if 0 <= candidate_dt <= 5.0:
                dt = candidate_dt
        if current_ts is not None:
            previous_ts = current_ts

        voltage = values[1] if values else None
        battery_current = values[2] if values else None
        ca_speed = values[3] if values else None
        raw_distance = values[4] if values else None
        temperature = values[5] if values else None
        human_current = values[13] if values else None
        battery_power = voltage * battery_current if voltage is not None and battery_current is not None else None
        human_power = voltage * human_current if voltage is not None and human_current is not None else None

        solar_power = _safe_float(sample.get("solar_power_w"))
        if solar_power is None:
            solar_current = _safe_float(sample.get("solar_current_a"))
            solar_voltage = _safe_float(sample.get("solar_bus_v"))
            if solar_current is not None and solar_voltage is not None:
                solar_power = solar_current * solar_voltage
        solar_power = max(0.0, solar_power or 0.0)

        if raw_distance is not None:
            _, ca_distance_km, _ = update_ca_distance(
                distance_store,
                raw_distance,
                now=current_ts,
                distance_key="distance",
                reset_count_key="ca_reset_count",
            )

        if values and previous_values:
            previous_raw_ah = previous_values[0]
            raw_ah = values[0]
            if raw_ah >= previous_raw_ah:
                battery_used_ah += raw_ah - previous_raw_ah

        if dt > 0 and battery_power is not None:
            if abs(battery_power) > 2:
                if battery_current and battery_current > 0:
                    positive_wh += battery_power * dt / 3600
                elif battery_current and battery_current < 0:
                    regen_wh += abs(battery_power) * dt / 3600
            if human_power is not None:
                human_wh += human_power * dt / 3600
            solar_wh += solar_power * dt / 3600

        gps = _valid_lat_lon(sample.get("gps_lat"), sample.get("gps_lon"))
        if gps is None:
            previous_values = values or previous_values
            continue

        plausible = True
        if previous_gps is not None:
            segment_km = _haversine_km(previous_gps, gps)
            plausible = gps_segment_plausible(previous_gps_sample, sample, segment_km)
            if plausible:
                gps_distance_km += segment_km
        if not plausible:
            previous_values = values or previous_values
            continue
        previous_gps = gps
        previous_gps_sample = sample

        altitude = _safe_float(sample.get("terrain_alt_m"))
        if altitude is None:
            altitude = _safe_float(sample.get("gps_alt"))
        elapsed_sec = (current_ts - first_ts).total_seconds() if current_ts is not None and first_ts is not None else 0.0
        metrics = {
            "ca_speed_kph": ca_speed,
            "gps_speed_kph": _safe_float(sample.get("gps_speed_kph")),
            "battery_power_w": battery_power,
            "human_power_w": human_power,
            "solar_power_w": solar_power,
            "battery_current_a": battery_current,
            "human_current_a": human_current,
            "voltage_v": voltage,
            "temperature_c": temperature,
            "altitude_m": altitude,
            "ca_distance_km": ca_distance_km,
            "gps_distance_km": gps_distance_km,
            "elapsed_min": elapsed_sec / 60.0,
            "battery_used_ah": battery_used_ah,
            "positive_wh": positive_wh,
            "regen_wh": regen_wh,
            "human_wh": human_wh,
            "solar_wh": solar_wh,
            "net_wh": positive_wh - regen_wh - human_wh - solar_wh,
        }
        points.append(
            {
                "lat": round(gps[0], 7),
                "lon": round(gps[1], 7),
                "timestamp": sample.get("timestamp"),
                "x_distance": round(ca_distance_km, 5),
                "x_time": round(elapsed_sec / 60.0, 5),
                "metrics": {
                    key: round(value, 5)
                    for key, value in metrics.items()
                    if isinstance(value, (int, float)) and math.isfinite(float(value))
                },
            }
        )
        previous_values = values or previous_values

    _add_trace_gradient_metrics(points)

    selected_points = points
    overview_points = None
    if (
        range_axis in {"distance", "time"}
        and range_min is not None
        and range_max is not None
        and math.isfinite(range_min)
        and math.isfinite(range_max)
        and range_min < range_max
    ):
        x_key = "x_distance" if range_axis == "distance" else "x_time"
        selected_points = [
            point
            for point in points
            if range_min <= float(point.get(x_key, 0.0)) <= range_max
        ]
        if overview_max_points:
            overview_points = _downsample_points(points, overview_max_points)

    return _downsample_points(selected_points, max_points), len(points), overview_points


def _trace_session_payload(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    *,
    metric_key: str,
    max_points: int,
    range_axis: str | None = None,
    range_min: float | None = None,
    range_max: float | None = None,
    overview_max_points: int | None = None,
) -> dict[str, Any]:
    vehicle = _suntrip_vehicle_for_device(session["device_id"])
    samples = _telemetry_samples_for_session(
        conn,
        session["device_id"],
        session["session_id"],
        session["mode"],
    )
    points, raw_point_count, overview_points = _session_trace_points(
        samples,
        max_points,
        range_axis=range_axis,
        range_min=range_min,
        range_max=range_max,
        overview_max_points=overview_max_points,
    )
    payload = {
        "device_id": session["device_id"],
        "session_id": session["session_id"],
        "mode": session["mode"],
        "vehicle_key": vehicle["key"] if vehicle else "",
        "vehicle_label": vehicle["label"] if vehicle else session["device_id"],
        "day_key": (_session_day(session["start_ts"], session["session_id"]) or "").isoformat()
        if _session_day(session["start_ts"], session["session_id"])
        else "",
        "start_ts": session["start_ts"],
        "end_ts": session["end_ts"],
        "distance_km": session["distance_km"],
        "rows_count": session["rows_count"],
        "raw_point_count": raw_point_count,
        "point_count": len(points),
        "metric_values": [
            _sample_metric_value(point.get("metrics", {}), metric_key)
            for point in points
        ],
        "points": points,
    }
    if overview_points is not None:
        payload["overview_points"] = overview_points
        payload["overview_point_count"] = len(overview_points)
    return payload


def _comparison_sessions_for_trace(
    conn: sqlite3.Connection,
    selected: sqlite3.Row,
) -> list[sqlite3.Row]:
    selected_vehicle = _suntrip_vehicle_for_device(selected["device_id"])
    selected_day = _session_day(selected["start_ts"], selected["session_id"])
    if not selected_vehicle or not selected_day:
        return [selected]

    start_prefix = selected_day.isoformat()
    rows = conn.execute(
        """
        SELECT device_id, session_id, mode, solar_enabled, suntrip_stage,
               start_ts, end_ts, rows_count, distance_km, uploaded_at
        FROM sessions
        WHERE (date(start_ts) = ? OR substr(session_id, 1, 10) = ?)
        ORDER BY suntrip_stage DESC, COALESCE(start_ts, session_id), device_id
        """,
        (start_prefix, start_prefix),
    ).fetchall()
    by_vehicle: dict[str, sqlite3.Row] = {selected_vehicle["key"]: selected}
    selected_start = _parse_upload_ts(selected["start_ts"]) or datetime.min
    for row in rows:
        vehicle = _suntrip_vehicle_for_device(row["device_id"])
        if not vehicle or vehicle["key"] in by_vehicle:
            continue
        row_day = _session_day(row["start_ts"], row["session_id"])
        if row_day != selected_day:
            continue
        by_vehicle[vehicle["key"]] = row

    if len(by_vehicle) < len(SUNTRIP_ANALYSIS_VEHICLES):
        candidates = [
            row
            for row in rows
            if (vehicle := _suntrip_vehicle_for_device(row["device_id"]))
            and vehicle["key"] not in by_vehicle
            and _session_day(row["start_ts"], row["session_id"]) == selected_day
        ]
        candidates.sort(
            key=lambda row: abs(
                ((_parse_upload_ts(row["start_ts"]) or selected_start) - selected_start).total_seconds()
            )
        )
        for row in candidates:
            vehicle = _suntrip_vehicle_for_device(row["device_id"])
            if vehicle and vehicle["key"] not in by_vehicle:
                by_vehicle[vehicle["key"]] = row

    return [
        row
        for vehicle in SUNTRIP_ANALYSIS_VEHICLES
        if (row := by_vehicle.get(vehicle["key"]))
    ]


def _is_deleted_session(conn: sqlite3.Connection, device_id: str, session_id: str, mode: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM deleted_sessions
        WHERE device_id = ? AND session_id = ? AND mode = ?
        """,
        (device_id, session_id, mode),
    ).fetchone()
    return row is not None


def _clear_deleted_session_marker(conn: sqlite3.Connection, device_id: str, session_id: str, mode: str) -> None:
    conn.execute(
        """
        DELETE FROM deleted_sessions
        WHERE device_id = ? AND session_id = ? AND mode = ?
        """,
        (device_id, session_id, mode),
    )


def _compact_monitor_db() -> dict[str, Any]:
    path = _db_path()
    before_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    with sqlite3.connect(path, timeout=_db_timeout_sec(), isolation_level=None) as conn:
        before_page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
        before_freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        after_page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
        after_freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    after_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    return {
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "saved_bytes": max(0, before_bytes - after_bytes),
        "before_page_count": before_page_count,
        "after_page_count": after_page_count,
        "before_freelist_count": before_freelist_count,
        "after_freelist_count": after_freelist_count,
    }


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=os.path.join(REPO_ROOT, "static"),
        static_url_path="/static",
    )
    app.jinja_loader = ChoiceLoader(
        [
            app.jinja_loader,
            FileSystemLoader(os.path.join(REPO_ROOT, "templates")),
        ]
    )
    with _startup_db_lock():
        _init_db()
        _migrate_db()

    @app.after_request
    def _gzip_text_response(response):
        if not _response_gzip_enabled():
            return response
        if response.status_code < 200 or response.status_code in {204, 304}:
            return response
        if response.direct_passthrough or response.is_streamed:
            return response
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return response
        if response.headers.get("Content-Encoding"):
            return response
        if response.mimetype not in {
            "application/json",
            "application/javascript",
            "image/svg+xml",
            "text/css",
            "text/html",
            "text/javascript",
            "text/plain",
        }:
            return response
        body = response.get_data()
        if len(body) < _response_gzip_min_bytes():
            return response
        compressed = gzip.compress(body, compresslevel=5)
        if len(compressed) >= len(body):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        response.headers["Vary"] = "Accept-Encoding"
        return response

    def _is_active(ts: str | None, window_sec: int = HEARTBEAT_ACTIVE_WINDOW_SEC, future_grace_sec: int = 10) -> bool:
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

    def _session_month(start_ts: str | None, session_id: str | None) -> tuple[str, str]:
        parsed = _parse_ts(start_ts)
        if not parsed and session_id:
            try:
                parsed = datetime.strptime(session_id[:10], "%Y-%m-%d")
            except Exception:
                parsed = None
        if not parsed:
            return "unknown", "Date inconnue"
        month_names = (
            "janvier",
            "fevrier",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "aout",
            "septembre",
            "octobre",
            "novembre",
            "decembre",
        )
        return parsed.strftime("%Y-%m"), f"{month_names[parsed.month - 1]} {parsed.year}"

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
                SELECT d.device_id, d.last_seen, d.last_session, d.session_active, d.mode,
                       d.test_mode, d.map_color, d.solar_enabled, d.last_gps_lat,
                       d.last_gps_lon, d.last_gps_ts, d.gps_available,
                       (SELECT COUNT(*) FROM sessions s WHERE s.device_id = d.device_id) AS session_count
                FROM devices d
                ORDER BY d.last_seen DESC
                """
            ).fetchall()
            sessions = conn.execute(
                """
                SELECT device_id, session_id, mode, solar_enabled, suntrip_stage, start_ts, end_ts, rows_count, distance_km, uploaded_at
                FROM sessions
                ORDER BY start_ts DESC
                """
            ).fetchall()
            photo_counts = _photo_counts_for_sessions(
                conn,
                [(s["device_id"], s["session_id"], s["mode"]) for s in sessions],
            )
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
                    SUM(COALESCE(uphill_m, 0)) AS total_uphill_m,
                    SUM(COALESCE(raw_gps_uphill_m, uphill_m, 0)) AS raw_gps_total_uphill_m
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
            _backfill_session_traces(conn)
            trace_rows = conn.execute(
                """
                SELECT device_id, session_id, mode, start_ts, trace_json
                FROM sessions
                WHERE trace_json IS NOT NULL AND trace_json != '[]'
                ORDER BY start_ts DESC
                """
            ).fetchall()
            conn.commit()
        devices = []
        device_colors = {}
        for d in device_rows:
            gps_available = bool(d["gps_available"])
            map_color = d["map_color"] or _default_device_color(d["device_id"])
            device_colors[d["device_id"]] = map_color
            devices.append(
                dict(d)
                | {
                    "active": _is_active(d["last_seen"]),
                    "last_seen_ago": _format_ago(d["last_seen"], now_local),
                    "gps_available": gps_available,
                    "map_color": map_color,
                }
            )
        session_traces = []
        for row in trace_rows:
            try:
                points = json.loads(row["trace_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(points, list) or not points:
                continue
            session_traces.append(
                {
                    "device_id": row["device_id"],
                    "session_id": row["session_id"],
                    "mode": row["mode"],
                    "start_ts_fmt": _format_dt(row["start_ts"]),
                    "color": device_colors.get(row["device_id"], _default_device_color(row["device_id"])),
                    "points": points,
                    "summary_url": url_for(
                        "session_map",
                        device_id=row["device_id"],
                        session_id=row["session_id"],
                        mode=row["mode"],
                    ),
                }
            )
        def _session_view_payload(s):
            month_key, month_label = _session_month(s["start_ts"], s["session_id"])
            return dict(s) | {
                "start_ts_fmt": _format_dt(s["start_ts"]),
                "end_ts_fmt": _format_dt(s["end_ts"]),
                "uploaded_at_fmt": _format_dt(s["uploaded_at"]),
                "month_key": month_key,
                "month_label": month_label,
                "suntrip_stage": bool(s["suntrip_stage"]),
                "photo_count": photo_counts.get((s["device_id"], s["session_id"], s["mode"]), 0),
            }

        sessions = [_session_view_payload(s) for s in sessions]
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
        raw_gps_total_uphill_30 = float(totals_30["raw_gps_total_uphill_m"] or 0) if totals_30 else 0.0
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
            session_traces=session_traces,
            daily_last_30=daily_last_30,
            daily_all=daily_all,
            total_distance_30=total_distance_30,
            total_uphill_30=total_uphill_30,
            raw_gps_total_uphill_30=raw_gps_total_uphill_30,
            avg_session_distance=avg_session_distance,
            avg_speed=avg_speed,
            latest_photo=latest_photo_payload,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    @app.route("/api/devices/<path:device_id>/map_color", methods=["PATCH"])
    @_require_auth
    def update_device_map_color(device_id: str):
        data = request.get_json(silent=True) or {}
        color = str(data.get("color") or "").strip().lower()
        if len(color) != 7 or color[0] != "#" or any(ch not in "0123456789abcdef" for ch in color[1:]):
            return jsonify({"error": "invalid color; expected #rrggbb"}), 400
        with _get_db() as conn:
            cursor = conn.execute(
                "UPDATE devices SET map_color = ? WHERE device_id = ?",
                (color, device_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "device not found"}), 404
        return jsonify({"status": "ok", "device_id": device_id, "color": color})

    @app.route("/api/devices/<path:device_id>", methods=["DELETE"])
    @_require_auth
    def delete_device(device_id: str):
        with _get_db() as conn:
            session_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE device_id = ?",
                (device_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                "DELETE FROM devices WHERE device_id = ?",
                (device_id,),
            )
            conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "device not found"}), 404
        return jsonify(
            {
                "status": "deleted",
                "device_id": device_id,
                "sessions_preserved": int(session_count),
            }
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
                """
                SELECT session_id FROM sessions WHERE device_id = ? AND mode = ?
                """,
                (device_id, mode),
            ).fetchall()
            deleted_rows = conn.execute(
                """
                SELECT session_id FROM deleted_sessions WHERE device_id = ? AND mode = ?
                """,
                (device_id, mode),
            ).fetchall()
        return jsonify(
            {
                "sessions": [r[0] for r in rows],
                "deleted_sessions": [r[0] for r in deleted_rows],
            }
        )

    @app.route("/api/compact_db", methods=["POST"])
    @_require_auth
    def compact_db():
        stats = _compact_monitor_db()
        return jsonify({"status": "ok", **stats})

    @app.route("/api/users", methods=["GET"])
    @_require_auth
    def list_users():
        with _get_db() as conn:
            rows = conn.execute(
                """
                SELECT user_id, initials, first_name, last_name, date_of_birth, gender, active,
                       source_device_id, created_at, updated_at, synced_at
                FROM users
                ORDER BY initials, last_name, first_name
                """
            ).fetchall()
        return jsonify({"users": [dict(row) for row in rows]})

    @app.route("/api/users/sync", methods=["POST"])
    @_require_auth
    def sync_users():
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        users = data.get("users")
        if not isinstance(users, list):
            return jsonify({"error": "missing users"}), 400
        synced_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with _get_db() as conn:
            for user in users:
                if not isinstance(user, dict) or not user.get("user_id"):
                    continue
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, initials, first_name, last_name, date_of_birth, gender, active,
                        source_device_id, created_at, updated_at, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        initials = excluded.initials,
                        first_name = excluded.first_name,
                        last_name = excluded.last_name,
                        date_of_birth = excluded.date_of_birth,
                        gender = excluded.gender,
                        active = excluded.active,
                        source_device_id = COALESCE(users.source_device_id, excluded.source_device_id),
                        updated_at = excluded.updated_at,
                        synced_at = excluded.synced_at
                    """,
                    (
                        user.get("user_id"),
                        user.get("initials"),
                        user.get("first_name"),
                        user.get("last_name"),
                        user.get("date_of_birth"),
                        user.get("gender"),
                        1 if user.get("active", True) else 0,
                        device_id,
                        user.get("created_at"),
                        user.get("updated_at"),
                        synced_at,
                    ),
                )
            conn.commit()
        return jsonify({"status": "ok"})

    @app.route("/api/session", methods=["DELETE"])
    @_require_auth
    def delete_session():
        data = request.get_json(silent=True) or {}
        device_id = data.get("device_id") or request.args.get("device_id")
        session_id = data.get("session_id") or request.args.get("session_id")
        mode = data.get("mode") or request.args.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        result = _delete_session_data(device_id, session_id, mode)
        if result["deleted_sessions"] == 0 and result["deleted_samples"] == 0 and result["deleted_photos"] == 0:
            return jsonify({"error": "session not found"}), 404

        return jsonify(result)

    @app.route("/api/sessions", methods=["DELETE"])
    @_require_auth
    def delete_sessions():
        data = request.get_json(silent=True) or {}
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return jsonify({"error": "missing sessions"}), 400

        result = _delete_sessions_data(sessions)
        if result["deleted_count"] == 0:
            return jsonify({"error": "sessions not found", **result}), 404

        return jsonify(result)

    @app.route("/api/session/suntrip_stage", methods=["PATCH"])
    @_require_auth
    def update_session_suntrip_stage():
        data = request.get_json(silent=True) or {}
        payload = _session_payload_from_request(data)
        if not payload["device_id"] or not payload["session_id"]:
            return jsonify({"error": "missing device_id or session_id"}), 400
        result = _set_suntrip_stage_sessions([payload], bool(data.get("suntrip_stage")))
        if result["updated_count"] == 0:
            return jsonify({"error": "session not found", **result}), 404
        return jsonify(result["results"][0] | {"status": "ok"})

    @app.route("/api/sessions/suntrip_stage", methods=["PATCH"])
    @_require_auth
    def update_sessions_suntrip_stage():
        data = request.get_json(silent=True) or {}
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return jsonify({"error": "missing sessions"}), 400
        result = _set_suntrip_stage_sessions(sessions, bool(data.get("suntrip_stage")))
        if result["updated_count"] == 0:
            return jsonify({"error": "sessions not found", **result}), 404
        return jsonify(result)

    @app.route("/api/sessions/solar_profile", methods=["POST"])
    @_require_auth
    def sessions_solar_profile():
        data = request.get_json(silent=True) or {}
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return jsonify({"error": "missing sessions"}), 400
        max_points = _safe_int(data.get("max_points_per_session")) or 1440
        with _get_db() as conn:
            result = _solar_profile_for_sessions(
                conn,
                sessions,
                max_points_per_session=max_points,
            )
        return jsonify(result)

    @app.route("/api/photos/video", methods=["POST"])
    @_require_auth
    def export_photo_video():
        data = request.get_json(force=True) or {}
        sessions = data.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            return jsonify({"error": "missing sessions"}), 400
        fps = _safe_int(data.get("fps")) or 24
        with _get_db() as conn:
            rows = _photo_video_rows(conn, sessions)
        if not rows:
            return jsonify({"error": "no photos found for selected sessions"}), 404
        try:
            video_path, frame_count, missing_count = _build_photo_video(rows, fps=fps)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503

        first = _session_key(sessions[0]) or ("sessions", "photos", "default")
        download_name = f"photos_{first[1]}_{frame_count}frames.mp4".replace("/", "_")

        response = send_file(
            video_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=download_name,
        )
        response.call_on_close(lambda: shutil.rmtree(os.path.dirname(video_path), ignore_errors=True))
        response.headers["X-Photo-Frames"] = str(frame_count)
        response.headers["X-Missing-Photo-Files"] = str(missing_count)
        return response

    @app.route("/api/heartbeat", methods=["POST"])
    @_require_auth
    def heartbeat():
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        if not device_id:
            return jsonify({"error": "missing device_id"}), 400
        with _get_db() as conn:
            result = _record_heartbeat(conn, data, request.remote_addr)
            conn.commit()
        return jsonify(result)

    @app.route("/api/upload_session", methods=["POST"])
    @_require_auth
    def upload_session():
        data, json_error = _read_json_request()
        if json_error:
            return json_error
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
            _clear_deleted_session_marker(conn, device_id, session_id, mode)

            samples = _sanitize_upload_samples(data)
            _enrich_samples_with_terrain_altitude(conn, samples, allow_fallback=False)
            solar_enabled = _payload_solar_enabled(data)
            summary = _insert_uploaded_session_summary(conn, data, samples)
            _insert_telemetry_samples(conn, device_id, session_id, mode, samples, solar_enabled)
            _upsert_upload_device(conn, data, solar_enabled, request.remote_addr)
            conn.commit()

        return jsonify({"status": "ok", **summary})

    @app.route("/api/upload_session_chunk", methods=["POST"])
    @_require_auth
    def upload_session_chunk():
        data, json_error = _read_json_request(max_bytes=_upload_chunk_max_bytes())
        if json_error:
            return json_error
        device_id = data.get("device_id")
        session_id = data.get("session_id")
        mode = data.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400
        try:
            chunk_index = int(data.get("chunk_index", 0))
            total_chunks = int(data.get("total_chunks", 1))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid chunk index"}), 400
        if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks:
            return jsonify({"error": "invalid chunk range"}), 400

        samples = _sanitize_upload_samples(data)
        solar_enabled = _payload_solar_enabled(data)
        final = bool(data.get("final")) or chunk_index == total_chunks - 1

        with _get_db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE device_id = ? AND session_id = ? AND mode = ?",
                (device_id, session_id, mode),
            ).fetchone()
            if existing:
                return jsonify({"status": "exists"})
            _clear_deleted_session_marker(conn, device_id, session_id, mode)

            if chunk_index == 0 and data.get("replace", True):
                conn.execute(
                    f"DELETE FROM {TELEMETRY_TABLE} WHERE device_id = ? AND session_id = ? AND mode = ?",
                    (device_id, session_id, mode),
                )

            _enrich_samples_with_terrain_altitude(conn, samples, allow_fallback=False)
            _insert_telemetry_samples(conn, device_id, session_id, mode, samples, solar_enabled)
            rows_received = conn.execute(
                f"""
                SELECT COUNT(*) FROM {TELEMETRY_TABLE}
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (device_id, session_id, mode),
            ).fetchone()[0]

            summary = {}
            if final:
                all_samples = _telemetry_samples_for_session(conn, device_id, session_id, mode)
                summary = _insert_uploaded_session_summary(conn, data, all_samples)
                _upsert_upload_device(conn, data, solar_enabled, request.remote_addr)
            conn.commit()

        return jsonify(
            {
                "status": "ok",
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "rows_received": rows_received,
                "complete": final,
                **summary,
            }
        )

    @app.route("/api/backfill_terrain_altitudes", methods=["POST"])
    @_require_auth
    def backfill_terrain_altitudes():
        data = request.get_json(silent=True) or {}
        limit_sessions = _safe_int(data.get("limit_sessions"))
        limit_points = _safe_int(data.get("limit_points"))
        with _get_db() as conn:
            stats = _backfill_terrain_altitudes(conn, limit_sessions, limit_points)
            conn.commit()
        return jsonify({"status": "ok", **stats})

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
            solar_enabled = 1 if data.get("solar_enabled", payload_metrics.get("solar_enabled", True)) else 0

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
                    solar_enabled, user_id, user_initials, user_snapshot_json, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    solar_enabled,
                    data.get("user_id"),
                    data.get("user_initials"),
                    json.dumps(data.get("user_snapshot"), ensure_ascii=False) if isinstance(data.get("user_snapshot"), dict) else data.get("user_snapshot_json"),
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
                       user_id, user_initials, user_snapshot_json,
                       gps_lat, gps_lon, gps_alt, terrain_alt_m, terrain_alt_source, terrain_alt_updated_at,
                       gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                       solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c, solar_enabled
                FROM {TELEMETRY_TABLE}
                WHERE device_id = ? AND session_id = ? AND mode = ?
                ORDER BY id
                """,
                (device_id, session_id, mode),
            ).fetchall()
            photo_rows = conn.execute(
                """
                SELECT id, captured_at, distance_km, interval_km, filename, mime_type, relative_path,
                       uploaded_at, test_mode, is_public, gps_lat, gps_lon, gps_alt, gps_speed_kph,
                       gps_track_deg, gps_fix, gps_sats, gps_hdop, speed_kph, session_distance_km,
                       gps_uphill_m, solar_power_w, generator_power_w, solar_wh, solar_enabled,
                       user_id, user_initials, user_snapshot_json, metrics_json
                FROM photos
                WHERE device_id = ? AND session_id = ? AND mode = ?
                ORDER BY captured_at, id
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
                "user_id": r[4],
                "user_initials": r[5],
                "user_snapshot_json": r[6],
                "gps_lat": r[7],
                "gps_lon": r[8],
                "gps_alt": r[9],
                "terrain_alt_m": r[10],
                "terrain_alt_source": r[11],
                "terrain_alt_updated_at": r[12],
                "gps_speed_kph": r[13],
                "gps_track_deg": r[14],
                "gps_fix": r[15],
                "gps_sats": r[16],
                "gps_hdop": r[17],
                "solar_current_a": r[18],
                "solar_bus_v": r[19],
                "solar_shunt_v": r[20],
                "solar_power_w": r[21],
                "solar_temperature_c": r[22],
                "solar_enabled": r[23],
            }
            for r in rows
        ]

        photos = []
        for row in photo_rows:
            metrics_json = None
            if row["metrics_json"]:
                try:
                    metrics_json = json.loads(row["metrics_json"])
                except Exception:
                    metrics_json = row["metrics_json"]
            photos.append(
                {
                    "id": row["id"],
                    "captured_at": row["captured_at"],
                    "distance_km": row["distance_km"],
                    "interval_km": row["interval_km"],
                    "filename": row["filename"],
                    "mime_type": row["mime_type"],
                    "relative_path": str(row["relative_path"] or "").replace("\\", "/"),
                    "uploaded_at": row["uploaded_at"],
                    "test_mode": row["test_mode"],
                    "is_public": row["is_public"],
                    "gps_lat": row["gps_lat"],
                    "gps_lon": row["gps_lon"],
                    "gps_alt": row["gps_alt"],
                    "gps_speed_kph": row["gps_speed_kph"],
                    "gps_track_deg": row["gps_track_deg"],
                    "gps_fix": row["gps_fix"],
                    "gps_sats": row["gps_sats"],
                    "gps_hdop": row["gps_hdop"],
                    "speed_kph": row["speed_kph"],
                    "session_distance_km": row["session_distance_km"],
                    "gps_uphill_m": row["gps_uphill_m"],
                    "solar_power_w": row["solar_power_w"],
                    "generator_power_w": row["generator_power_w"],
                    "solar_wh": row["solar_wh"],
                    "solar_enabled": row["solar_enabled"],
                    "user_id": row["user_id"],
                    "user_initials": row["user_initials"],
                    "user_snapshot_json": row["user_snapshot_json"],
                    "metrics": metrics_json,
                }
            )

        payload = {
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
            "photos": photos,
        }
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in f"{device_id}_{session_id}_{mode}")
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}.json"',
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    @app.route("/api/export_gpx")
    @_require_auth
    def export_gpx():
        device_id = request.args.get("device_id")
        session_id = request.args.get("session_id")
        mode = request.args.get("mode", "default")
        altitude_mode = (request.args.get("altitude") or "terrain").strip().lower()
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, gps_lat, gps_lon, gps_alt, terrain_alt_m
                FROM {TELEMETRY_TABLE}
                WHERE device_id = ? AND session_id = ? AND mode = ?
                  AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
                  AND gps_lat != 0 AND gps_lon != 0
                ORDER BY id
                """,
                (device_id, session_id, mode),
            ).fetchall()

        samples = [
            {
                "timestamp": row["timestamp"],
                "gps_lat": row["gps_lat"],
                "gps_lon": row["gps_lon"],
                "gps_alt": row["gps_alt"],
                "terrain_alt_m": row["terrain_alt_m"],
            }
            for row in rows
        ]
        point_segments = []
        for sample_segment in _gps_trace_segments(samples):
            points = []
            for sample in sample_segment:
                try:
                    lat = float(sample["gps_lat"])
                    lon = float(sample["gps_lon"])
                except Exception:
                    continue
                alt = None
                alt_value = sample["gps_alt"] if altitude_mode == "gps" else sample["terrain_alt_m"]
                if alt_value is None:
                    alt_value = sample["gps_alt"]
                if alt_value is not None:
                    try:
                        alt = float(alt_value)
                    except Exception:
                        alt = None
                points.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "alt": alt,
                        "time": _format_gpx_time(sample["timestamp"]),
                    }
                )
            if points:
                point_segments.append(points)

        session_label = escape(session_id)
        creator = "Cycle Monitor"
        gpx_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1">',
            f"<metadata><name>{session_label}</name></metadata>",
            "<trk>",
            f"<name>{session_label}</name>",
        ]
        for points in point_segments:
            gpx_lines.append("<trkseg>")
            for p in points:
                line = f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">'
                if p["alt"] is not None:
                    line += f"<ele>{p['alt']:.1f}</ele>"
                if p["time"]:
                    line += f"<time>{p['time']}</time>"
                line += "</trkpt>"
                gpx_lines.append(line)
            gpx_lines.append("</trkseg>")
        gpx_lines.extend(["</trk>", "</gpx>"])
        gpx_text = "\n".join(gpx_lines)
        filename = f"{session_id}.gpx"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/gpx+xml; charset=utf-8",
        }
        return Response(gpx_text, headers=headers)

    def _suntrip_photo_payload(row, gps_fallback: sqlite3.Row | None = None) -> dict[str, Any]:
        image_path = str(row["relative_path"] or "").replace("\\", "/")
        image_url = url_for("photo_file", filename=image_path)
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
        gps_lat = row["gps_lat"]
        gps_lon = row["gps_lon"]
        gps_alt = row["gps_alt"]
        gps_speed_kph = row["gps_speed_kph"]
        gps_track_deg = row["gps_track_deg"]
        gps_fix = row["gps_fix"]
        gps_sats = row["gps_sats"]
        gps_hdop = row["gps_hdop"]
        if gps_fallback is not None:
            if _safe_float(gps_lat) in (None, 0) or _safe_float(gps_lon) in (None, 0):
                gps_lat = gps_fallback["gps_lat"]
                gps_lon = gps_fallback["gps_lon"]
            gps_alt = gps_alt if gps_alt is not None else gps_fallback["gps_alt"]
            gps_speed_kph = gps_speed_kph if gps_speed_kph is not None else gps_fallback["gps_speed_kph"]
            gps_track_deg = gps_track_deg if gps_track_deg is not None else gps_fallback["gps_track_deg"]
            gps_fix = gps_fix if gps_fix is not None else gps_fallback["gps_fix"]
            gps_sats = gps_sats if gps_sats is not None else gps_fallback["gps_sats"]
            gps_hdop = gps_hdop if gps_hdop is not None else gps_fallback["gps_hdop"]
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
                "lat": gps_lat,
                "lon": gps_lon,
                "alt": gps_alt,
                "speed_kph": gps_speed_kph,
                "track_deg": gps_track_deg,
                "fix": bool(gps_fix),
                "sats": gps_sats,
                "hdop": gps_hdop,
            },
            "metrics": {
                "speed_kph": speed_kph,
                "distance_km": distance_km,
                "gps_uphill_m": metric_value("gps_uphill_m", "gps_uphill_m"),
                "solar_enabled": bool(row["solar_enabled"]),
                "positive_Wh": stored_metrics.get("positive_Wh"),
                "regen_Wh": stored_metrics.get("regen_Wh"),
                "human_Wh": stored_metrics.get("human_Wh"),
                "solar_power_w": metric_value("solar_power_w", "solar_power_w") if row["solar_enabled"] else 0,
                "generator_power_w": metric_value("generator_power_w", "generator_power_w"),
                "solar_wh": metric_value("solar_wh", "solar_wh", "solar_Wh") if row["solar_enabled"] else 0,
            },
        }

    def _suntrip_photo_needs_gps(row: sqlite3.Row) -> bool:
        lat = _safe_float(row["gps_lat"])
        lon = _safe_float(row["gps_lon"])
        return lat is None or lon is None or lat == 0 or lon == 0

    def _suntrip_fallback_gps(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row | None:
        aliases = _suntrip_device_aliases(row["device_id"])
        if not aliases:
            return None
        placeholders = ",".join("?" for _ in aliases)
        params: list[Any] = [row["session_id"], row["mode"], *aliases]
        order = "id ASC"
        if row["captured_at"]:
            order = """
                CASE
                    WHEN timestamp IS NULL THEN 1
                    WHEN strftime('%s', timestamp) IS NULL THEN 1
                    ELSE 0
                END,
                ABS(strftime('%s', timestamp) - strftime('%s', ?)),
                id ASC
            """
            params.append(row["captured_at"])
        return conn.execute(
            f"""
            SELECT gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop
            FROM {TELEMETRY_TABLE}
            WHERE session_id = ? AND mode = ? AND device_id IN ({placeholders})
              AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
              AND gps_lat != 0 AND gps_lon != 0
            ORDER BY {order}
            LIMIT 1
            """,
            params,
        ).fetchone()

    def _suntrip_payload(device_id: str | None = None) -> dict[str, Any]:
        query = """
            SELECT id, device_id, session_id, mode, captured_at, distance_km, interval_km,
                   relative_path, uploaded_at, gps_lat, gps_lon, gps_alt, gps_speed_kph,
                   gps_track_deg, gps_fix, gps_sats, gps_hdop, speed_kph, session_distance_km,
                   gps_uphill_m, solar_power_w, generator_power_w, solar_wh, solar_enabled, metrics_json
            FROM photos
            WHERE is_public = 1
        """
        params: list[Any] = []
        start_filter = _suntrip_start_filter()
        if start_filter:
            start_ts, start_day = start_filter
            query += " AND (captured_at >= ? OR session_id >= ?)"
            params.extend([start_ts, start_day])
        requested_device = (device_id or "").strip()
        if requested_device:
            aliases = _suntrip_device_aliases(device_id)
            placeholders = ",".join("?" for _ in aliases)
            query += f" AND device_id IN ({placeholders})"
            params.extend(aliases)
        query += " ORDER BY captured_at DESC, id DESC"
        limit = _suntrip_photo_limit()
        if limit:
            query += " LIMIT ?"
            params.append(limit)

        with _get_db() as conn:
            rows = conn.execute(query, params).fetchall()
            photos = [
                _suntrip_photo_payload(
                    row,
                    _suntrip_fallback_gps(conn, row) if _suntrip_photo_needs_gps(row) else None,
                )
                for row in rows
            ]

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
                    "device_id": photo["device_id"],
                    "session_id": photo["session_id"],
                    "uploaded_at": photo["uploaded_at"],
                    "captured_at": photo["captured_at"],
                    "image_url": photo["image_url"],
                    "metrics": photo["metrics"],
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

    @app.route("/suntrip_analysis")
    @_require_auth
    def suntrip_analysis():
        start_date = _parse_date_param(request.args.get("start"), SUNTRIP_ANALYSIS_START_DATE)
        end_date = _parse_date_param(request.args.get("end"), SUNTRIP_ANALYSIS_END_DATE)
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        cache_key = ("suntrip_analysis", _db_state_key(), start_date.isoformat(), end_date.isoformat())
        payload = _SUNTRIP_ANALYSIS_CACHE.get(cache_key)
        if payload is None:
            vehicle_order = {vehicle["key"]: index for index, vehicle in enumerate(SUNTRIP_ANALYSIS_VEHICLES)}

            with _get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT device_id, session_id, mode, solar_enabled, suntrip_stage,
                           start_ts, end_ts, rows_count, distance_km, uploaded_at
                    FROM sessions
                    ORDER BY COALESCE(start_ts, session_id), device_id
                    """
                ).fetchall()

                candidates = []
                for row in rows:
                    vehicle = _suntrip_vehicle_for_device(row["device_id"])
                    day = _session_day(row["start_ts"], row["session_id"])
                    if not vehicle or not day or day < start_date or day > end_date:
                        continue
                    candidate = dict(row) | {
                        "day": day,
                        "day_key": day.isoformat(),
                        "day_label": day.strftime("%d/%m/%Y"),
                        "vehicle_key": vehicle["key"],
                        "vehicle_label": vehicle["label"],
                        "vehicle_order": vehicle_order.get(vehicle["key"], 99),
                        "start_ts_fmt": _format_dt(row["start_ts"]),
                        "end_ts_fmt": _format_dt(row["end_ts"]),
                        "suntrip_stage": bool(row["suntrip_stage"]),
                        "map_url": url_for(
                            "session_map",
                            device_id=row["device_id"],
                            session_id=row["session_id"],
                            mode=row["mode"],
                        ),
                    }
                    candidates.append(candidate)

                candidates.sort(
                    key=lambda item: (
                        item["day"],
                        item["vehicle_order"],
                        item["start_ts"] or "",
                        item["session_id"] or "",
                    )
                )

                columns = []
                for candidate in candidates:
                    if not candidate["suntrip_stage"]:
                        continue
                    samples = _telemetry_samples_for_session(
                        conn,
                        candidate["device_id"],
                        candidate["session_id"],
                        candidate["mode"],
                    )
                    metrics = compute_session_metrics(samples)
                    columns.append(candidate | {"metrics": metrics, "sample_count": len(samples)})

            ca_gps_corrections = _suntrip_vehicle_corrections(columns)
            for column in columns:
                correction = ca_gps_corrections.get(column["vehicle_key"], {})
                factor = correction.get("factor") if correction.get("available") else 1.0
                column["ca_gps_correction"] = correction
                column["corrected_metrics"] = _apply_ca_gps_correction(column["metrics"], factor)

            day_groups = []
            for column in columns:
                if not day_groups or day_groups[-1]["day_key"] != column["day_key"]:
                    day_groups.append({"day_key": column["day_key"], "day_label": column["day_label"], "columns": []})
                day_groups[-1]["columns"].append(column)

            total_columns = []
            for vehicle in SUNTRIP_ANALYSIS_VEHICLES:
                vehicle_columns = [column for column in columns if column["vehicle_key"] == vehicle["key"]]
                if not vehicle_columns:
                    continue
                total_columns.append(
                    {
                        "vehicle_key": vehicle["key"],
                        "vehicle_label": vehicle["label"],
                        "session_count": len(vehicle_columns),
                        "metrics": _aggregate_session_metrics([column["metrics"] for column in vehicle_columns]),
                        "corrected_metrics": _aggregate_session_metrics(
                            [column["corrected_metrics"] for column in vehicle_columns]
                        ),
                        "ca_gps_correction": ca_gps_corrections.get(vehicle["key"]),
                    }
                )

            metric_groups = []
            chart_metric_groups = []
            for category, specs in SUMMARY_GROUPS:
                rows = []
                chart_rows = []
                for label, unit, func in specs:
                    metric_key = f"metric_{len(chart_metric_groups)}_{len(chart_rows)}"
                    rows.append(
                        {
                            "key": metric_key,
                            "label": label,
                            "unit": unit,
                            "values": [format_metric_value(func(column["metrics"]), unit) for column in columns],
                            "corrected_values": [
                                format_metric_value(func(column["corrected_metrics"]), unit)
                                for column in columns
                            ],
                            "total_values": [
                                format_metric_value(func(total_column["metrics"]), unit)
                                for total_column in total_columns
                            ],
                            "corrected_total_values": [
                                format_metric_value(func(total_column["corrected_metrics"]), unit)
                                for total_column in total_columns
                            ],
                        }
                    )
                    series = []
                    for vehicle in SUNTRIP_ANALYSIS_VEHICLES:
                        values = []
                        for day in day_groups:
                            day_vehicle_columns = [
                                column
                                for column in day["columns"]
                                if column["vehicle_key"] == vehicle["key"]
                            ]
                            if not day_vehicle_columns:
                                values.append(None)
                                continue
                            metrics = _aggregate_session_metrics(
                                [column["metrics"] for column in day_vehicle_columns]
                            )
                            values.append(func(metrics))
                        series.append(
                            {
                                "vehicle_key": vehicle["key"],
                                "vehicle_label": vehicle["label"],
                                "values": values,
                            }
                        )
                    chart_rows.append(
                        {
                            "key": metric_key,
                            "category": category,
                            "label": label,
                            "unit": unit,
                            "days": [day["day_label"] for day in day_groups],
                            "series": series,
                        }
                    )
                metric_groups.append({"category": category, "rows": rows})
                chart_metric_groups.append({"category": category, "rows": chart_rows})

            trace_sessions = [
                {
                    "device_id": candidate["device_id"],
                    "session_id": candidate["session_id"],
                    "mode": candidate["mode"],
                    "day_key": candidate["day_key"],
                    "day_label": candidate["day_label"],
                    "vehicle_key": candidate["vehicle_key"],
                    "vehicle_label": candidate["vehicle_label"],
                    "start_ts_fmt": candidate["start_ts_fmt"],
                    "distance_km": candidate["distance_km"],
                    "suntrip_stage": candidate["suntrip_stage"],
                }
                for candidate in candidates
            ]
            payload = {
                "vehicles": SUNTRIP_ANALYSIS_VEHICLES,
                "candidates": candidates,
                "columns": columns,
                "total_columns": total_columns,
                "day_groups": day_groups,
                "metric_groups": metric_groups,
                "chart_metric_groups": chart_metric_groups,
                "trace_sessions": trace_sessions,
                "trace_metric_options": _metric_options_from_specs(),
                "trace_standard_max_points": SUNTRIP_ANALYSIS_TRACE_MAX_POINTS,
                "trace_detailed_max_points": SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_POINTS,
                "trace_detailed_max_km": SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_KM,
                "trace_detailed_max_minutes": SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_MINUTES,
                "ca_gps_corrections": list(ca_gps_corrections.values()),
                "stage_count": len(columns),
            }
            if len(_SUNTRIP_ANALYSIS_CACHE) > 8:
                _SUNTRIP_ANALYSIS_CACHE.clear()
            _SUNTRIP_ANALYSIS_CACHE[cache_key] = payload

        return render_template(
            "suntrip_analysis.html",
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            **payload,
        )

    @app.route("/api/suntrip_analysis/session_trace")
    @_require_auth
    def suntrip_analysis_session_trace():
        device_id = request.args.get("device_id")
        session_id = request.args.get("session_id")
        mode = request.args.get("mode", "default")
        metric_key = request.args.get("metric") or "ca_speed_kph"
        compare = request.args.get("compare", "1").strip().lower() not in {"0", "false", "no", "off"}
        requested_detailed = request.args.get("detailed", "0").strip().lower() in {"1", "true", "yes", "on"}
        requested_max_points = request.args.get("max_points")
        range_axis = request.args.get("range_axis")
        if range_axis not in {"distance", "time"}:
            range_axis = None
        range_min = _safe_float(request.args.get("range_min"))
        range_max = _safe_float(request.args.get("range_max"))
        if range_min is not None and range_max is not None and range_min > range_max:
            range_min, range_max = range_max, range_min
        requested_range_valid = (
            range_axis is not None
            and range_min is not None
            and range_max is not None
            and range_min < range_max
        )
        range_applied = False
        if requested_detailed and requested_range_valid:
            max_range_span = (
                SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_MINUTES
                if range_axis == "time"
                else SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_KM
            )
            if range_max - range_min > max_range_span:
                center = (range_min + range_max) / 2
                range_min = center - max_range_span / 2
                range_max = center + max_range_span / 2
            range_applied = True
        metric_options = _trace_metric_option_map()
        if metric_key not in metric_options:
            return jsonify({"error": "unknown metric"}), 400
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            selected = conn.execute(
                """
                SELECT device_id, session_id, mode, solar_enabled, suntrip_stage,
                       start_ts, end_ts, rows_count, distance_km, uploaded_at
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (device_id, session_id, mode),
            ).fetchone()
            if not selected:
                return jsonify({"error": "session not found"}), 404
            sessions = _comparison_sessions_for_trace(conn, selected) if compare else [selected]
            detailed = requested_detailed
            max_allowed_points = (
                SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_POINTS
                if detailed
                else SUNTRIP_ANALYSIS_TRACE_MAX_POINTS
            )
            max_points = _limited_int(
                requested_max_points,
                max_allowed_points,
                100,
                max_allowed_points,
            )
            series = [
                _trace_session_payload(
                    conn,
                    session,
                    metric_key=metric_key,
                    max_points=max_points,
                    range_axis=range_axis if detailed and range_applied else None,
                    range_min=range_min if detailed and range_applied else None,
                    range_max=range_max if detailed and range_applied else None,
                    overview_max_points=SUNTRIP_ANALYSIS_TRACE_MAX_POINTS if range_applied else None,
                )
                for session in sessions
            ]

        return jsonify(
            {
                "status": "ok",
                "metric": metric_options[metric_key],
                "compare": compare,
                "detailed": detailed,
                "detailed_allowed": True,
                "range_applied": bool(detailed and range_applied),
                "range_axis": range_axis if detailed and range_applied else None,
                "range_min": range_min if detailed and range_applied else None,
                "range_max": range_max if detailed and range_applied else None,
                "detailed_max_km": SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_KM,
                "detailed_max_minutes": SUNTRIP_ANALYSIS_TRACE_DETAILED_MAX_MINUTES,
                "max_points": max_points,
                "series": series,
            }
        )

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
                       user_id, user_initials, user_snapshot_json,
                       gps_lat, gps_lon, gps_alt, terrain_alt_m, terrain_alt_source, terrain_alt_updated_at,
                       gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                       solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c, solar_enabled
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
                "user_id": row["user_id"],
                "user_initials": row["user_initials"],
                "user_snapshot_json": row["user_snapshot_json"],
                "gps_lat": row["gps_lat"],
                "gps_lon": row["gps_lon"],
                "gps_alt": row["gps_alt"],
                "terrain_alt_m": row["terrain_alt_m"],
                "terrain_alt_source": row["terrain_alt_source"],
                "terrain_alt_updated_at": row["terrain_alt_updated_at"],
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
                "solar_enabled": row["solar_enabled"],
            }
            samples.append(sample)
        for sample in filter_plausible_gps_samples(samples):
            try:
                lat = float(sample["gps_lat"])
                lon = float(sample["gps_lon"])
            except Exception:
                continue
            if lat == 0 or lon == 0:
                continue
            point = {"lat": lat, "lon": lon}
            alt_value = sample["terrain_alt_m"] if sample["terrain_alt_m"] is not None else sample["gps_alt"]
            if alt_value is not None:
                try:
                    point["alt"] = float(alt_value)
                except Exception:
                    point["alt"] = None
            if sample["gps_alt"] is not None:
                try:
                    point["gps_alt"] = float(sample["gps_alt"])
                except Exception:
                    point["gps_alt"] = None
            if sample["terrain_alt_m"] is not None:
                try:
                    point["terrain_alt_m"] = float(sample["terrain_alt_m"])
                except Exception:
                    point["terrain_alt_m"] = None
            time_str = _format_gpx_time(sample["timestamp"])
            if time_str:
                point["time"] = time_str
            points.append(point)
        metrics_by_user = compute_timeline_metrics_by_user(samples)
        metrics_by_user["Total"] = compute_session_metrics(samples)
        display_distance_km = session_row["distance_km"] if session_row else None
        if display_distance_km is None:
            display_distance_km = metrics_by_user["Total"].get("distance")
        session_users = [user for user in metrics_by_user.keys() if user != "Total"]
        all_users = ["Total"] if len(session_users) <= 1 else ["Total"] + session_users

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
            distance_km=display_distance_km,
            rows_count=session_row["rows_count"] if session_row else None,
            sections=build_summary_sections(metrics_by_user, all_users),
            users=all_users,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("MONITOR_PORT", "8080")))
