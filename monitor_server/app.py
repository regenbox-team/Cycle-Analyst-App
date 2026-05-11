from __future__ import annotations
import base64
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
from xml.sax.saxutils import escape

from flask import Flask, jsonify, request, render_template, Response, send_from_directory, url_for
from jinja2 import ChoiceLoader, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT in sys.path:
    sys.path.remove(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)

from app.session_summary import (
    build_summary_sections,
    compute_session_metrics,
    compute_timeline_metrics_by_user,
)


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
DEFAULT_DB_TIMEOUT_SEC = 30.0
HEARTBEAT_ACTIVE_WINDOW_SEC = 120
DEFAULT_STARTUP_LOCK_TIMEOUT_SEC = 45.0


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
                raw_gps_uphill_m REAL,
                solar_enabled INTEGER DEFAULT 1,
                user_ids_json TEXT,
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
        }
        for name, col_type in missing.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {col_type}")
        conn.execute("UPDATE sessions SET solar_enabled = 1 WHERE solar_enabled IS NULL")

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
        ORDER BY id
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
                SELECT device_id, last_seen, last_session, session_active, mode, test_mode,
                       solar_enabled, last_gps_lat, last_gps_lon, last_gps_ts, gps_available
                FROM devices
                ORDER BY last_seen DESC
                """
            ).fetchall()
            sessions = conn.execute(
                """
                SELECT device_id, session_id, mode, solar_enabled, start_ts, end_ts, rows_count, distance_km, uploaded_at
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
                        "start_ts": s["start_ts"],
                        "start_ts_fmt": _format_dt(s["start_ts"]),
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
        def _session_view_payload(s):
            month_key, month_label = _session_month(s["start_ts"], s["session_id"])
            return dict(s) | {
                "start_ts_fmt": _format_dt(s["start_ts"]),
                "end_ts_fmt": _format_dt(s["end_ts"]),
                "uploaded_at_fmt": _format_dt(s["uploaded_at"]),
                "month_key": month_key,
                "month_label": month_label,
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
            session_points=session_points,
            daily_last_30=daily_last_30,
            daily_all=daily_all,
            total_distance_30=total_distance_30,
            total_uphill_30=total_uphill_30,
            raw_gps_total_uphill_30=raw_gps_total_uphill_30,
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
                """
                SELECT session_id FROM sessions WHERE device_id = ? AND mode = ?
                UNION
                SELECT session_id FROM deleted_sessions WHERE device_id = ? AND mode = ?
                """,
                (device_id, mode, device_id, mode),
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
        data = request.get_json(force=True) or {}
        device_id = data.get("device_id")
        session_id = data.get("session_id")
        mode = data.get("mode", "default")
        if not device_id or not session_id:
            return jsonify({"error": "missing device_id or session_id"}), 400

        with _get_db() as conn:
            if _is_deleted_session(conn, device_id, session_id, mode):
                return jsonify({"status": "exists", "deleted": True})
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE device_id = ? AND session_id = ? AND mode = ?",
                (device_id, session_id, mode),
            ).fetchone()
            if existing:
                return jsonify({"status": "exists"})

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
        data = request.get_json(force=True) or {}
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
            if _is_deleted_session(conn, device_id, session_id, mode):
                return jsonify({"status": "exists", "deleted": True})
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE device_id = ? AND session_id = ? AND mode = ?",
                (device_id, session_id, mode),
            ).fetchone()
            if existing:
                return jsonify({"status": "exists"})

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

        points = []
        for row in rows:
            try:
                lat = float(row["gps_lat"])
                lon = float(row["gps_lon"])
            except Exception:
                continue
            alt = None
            alt_value = row["gps_alt"] if altitude_mode == "gps" else row["terrain_alt_m"]
            if alt_value is None:
                alt_value = row["gps_alt"]
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
            try:
                lat = float(row["gps_lat"])
                lon = float(row["gps_lon"])
            except Exception:
                continue
            if lat == 0 or lon == 0:
                continue
            point = {"lat": lat, "lon": lon}
            alt_value = row["terrain_alt_m"] if row["terrain_alt_m"] is not None else row["gps_alt"]
            if alt_value is not None:
                try:
                    point["alt"] = float(alt_value)
                except Exception:
                    point["alt"] = None
            if row["gps_alt"] is not None:
                try:
                    point["gps_alt"] = float(row["gps_alt"])
                except Exception:
                    point["gps_alt"] = None
            if row["terrain_alt_m"] is not None:
                try:
                    point["terrain_alt_m"] = float(row["terrain_alt_m"])
                except Exception:
                    point["terrain_alt_m"] = None
            time_str = _format_gpx_time(row["timestamp"])
            if time_str:
                point["time"] = time_str
            points.append(point)
        metrics_by_user = compute_timeline_metrics_by_user(samples)
        metrics_by_user["Total"] = compute_session_metrics(samples)
        display_distance_km = metrics_by_user["Total"].get("distance")
        if display_distance_km is None and session_row:
            display_distance_km = session_row["distance_km"]
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
