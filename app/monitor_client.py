from __future__ import annotations
import base64
import gzip
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from collections.abc import Callable
from typing import Any

from .config import BASE_DIR, DB_FILE, SESSION_METRICS_DIR, get_db_file
from .gps import get_status
from .modes import is_test_mode
from . import state
from .user_profiles import load_profiles, profile_snapshot


DEFAULT_UPLOAD_CHUNK_SIZE = 1000
DEFAULT_UPLOAD_CHUNK_MAX_BYTES = 256 * 1024
DEFAULT_UPLOAD_GZIP_MIN_BYTES = 1024
ProgressCallback = Callable[[dict[str, Any]], None]


def _notify_progress(progress: ProgressCallback | None, **updates: Any) -> None:
    if progress is None:
        return
    try:
        progress(updates)
    except Exception:
        pass


def _device_id() -> str:
    return os.getenv("MONITOR_DEVICE_ID") or socket.gethostname()


def _monitor_url() -> str | None:
    return os.getenv("MONITOR_URL")


def _auto_upload_sessions_enabled() -> bool:
    return os.getenv("MONITOR_AUTO_UPLOAD_SESSIONS", "0").strip().lower() in {"1", "true", "yes", "on"}


def _auth_header() -> dict[str, str]:
    user = os.getenv("MONITOR_USER", "")
    password = os.getenv("MONITOR_PASS", "")
    raw = f"{user}:{password}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _encode_json_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 5,
    gzip_payload: bool = False,
) -> Any:
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_header())
    data = None
    if payload is not None:
        data = _encode_json_payload(payload)
        if gzip_payload and len(data) >= _upload_gzip_min_bytes():
            compressed = gzip.compress(data)
            if len(compressed) < len(data):
                headers["Content-Encoding"] = "gzip"
                headers["X-Uncompressed-Length"] = str(len(data))
                data = compressed
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def _request_upload_json(
    method: str,
    url: str,
    payload: dict[str, Any],
    timeout: int,
    gzip_payload: bool,
) -> tuple[Any, bool]:
    try:
        return _request_json(method, url, payload, timeout=timeout, gzip_payload=gzip_payload), gzip_payload
    except urllib.error.HTTPError as exc:
        if gzip_payload and exc.code in {400, 415}:
            return _request_json(method, url, payload, timeout=timeout, gzip_payload=False), False
        raise


def _upload_chunk_size() -> int:
    try:
        return max(1, int(os.getenv("MONITOR_UPLOAD_CHUNK_SIZE", str(DEFAULT_UPLOAD_CHUNK_SIZE))))
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_CHUNK_SIZE


def _upload_chunk_max_bytes() -> int:
    try:
        return max(16 * 1024, int(os.getenv("MONITOR_UPLOAD_CHUNK_MAX_BYTES", str(DEFAULT_UPLOAD_CHUNK_MAX_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_CHUNK_MAX_BYTES


def _upload_gzip_enabled() -> bool:
    return os.getenv("MONITOR_UPLOAD_GZIP", "1").strip().lower() in {"1", "true", "yes", "on"}


def _upload_gzip_min_bytes() -> int:
    try:
        return max(0, int(os.getenv("MONITOR_UPLOAD_GZIP_MIN_BYTES", str(DEFAULT_UPLOAD_GZIP_MIN_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_GZIP_MIN_BYTES


def _chunk_payload(
    payload: dict[str, Any],
    samples: list[dict[str, Any]],
    upload_id: str,
    chunk_index: int,
    total_chunks: int,
    total_rows: int,
) -> dict[str, Any]:
    chunk = dict(payload)
    chunk["telemetry_samples"] = samples
    chunk["upload_id"] = upload_id
    chunk["chunk_index"] = chunk_index
    chunk["total_chunks"] = total_chunks
    chunk["total_rows"] = total_rows
    chunk["final"] = chunk_index == total_chunks - 1
    chunk["replace"] = chunk_index == 0
    return chunk


def _chunk_payload_size(
    payload: dict[str, Any],
    samples: list[dict[str, Any]],
    total_rows: int | None = None,
) -> int:
    probe = _chunk_payload(payload, samples, "probe", 0, 1, len(samples) if total_rows is None else total_rows)
    return len(_encode_json_payload(probe))


def _split_upload_samples(
    payload: dict[str, Any],
    samples: list[dict[str, Any]],
    chunk_size: int,
    max_bytes: int,
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    base_size = _chunk_payload_size(payload, [], total_rows=len(samples))
    metadata_slack = min(128, max(0, max_bytes // 20))
    effective_max_bytes = max(1, max_bytes - metadata_slack)
    current_size = base_size
    for sample in samples:
        sample_size = len(_encode_json_payload(sample))
        candidate_size = current_size + sample_size + (1 if current else 0)
        if current and (len(current) >= chunk_size or candidate_size > effective_max_bytes):
            chunks.append(current)
            current = []
            current_size = base_size
            candidate_size = current_size + sample_size
        current.append(sample)
        current_size = candidate_size
        if len(current) == 1 and current_size > max_bytes:
            chunks.append(current)
            current = []
            current_size = base_size
    if current:
        chunks.append(current)
    return chunks


def _list_db_files() -> list[str]:
    dbs = []
    try:
        for name in os.listdir(BASE_DIR):
            if name.startswith("ride_data") and name.endswith(".db"):
                dbs.append(os.path.join(BASE_DIR, name))
    except Exception:
        pass
    if DB_FILE not in dbs and os.path.exists(DB_FILE):
        dbs.append(DB_FILE)
    return dbs


def _mode_from_db_path(path: str) -> str:
    base = os.path.basename(path)
    if base == "ride_data.db":
        return "default"
    if base.startswith("ride_data_") and base.endswith(".db"):
        return base[len("ride_data_"):-len(".db")]
    return "default"


def _metrics_for_session(session_id: str) -> dict[str, Any] | None:
    metrics_path = os.path.join(SESSION_METRICS_DIR, f"{session_id}_session_metrics.json")
    if not os.path.exists(metrics_path):
        return None
    try:
        with open(metrics_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _fetch_sessions(db_path: str) -> list[str]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 1000")
            rows = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session").fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def _fetch_session_rows(db_path: str, session_id: str) -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA busy_timeout = 1000")
            cols = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}

            def col(name: str) -> str:
                return name if name in cols else f"NULL AS {name}"

            def bool_col(name: str) -> str:
                return name if name in cols else f"1 AS {name}"

            rows = conn.execute(
                f"""
                SELECT timestamp, session, raw, user,
                       {col("user_id")}, {col("user_initials")}, {col("user_snapshot_json")},
                       {col("gps_lat")}, {col("gps_lon")}, {col("gps_alt")},
                       {col("gps_speed_kph")}, {col("gps_track_deg")}, {col("gps_fix")},
                       {col("gps_sats")}, {col("gps_hdop")},
                       {col("solar_current_a")}, {col("solar_bus_v")}, {col("solar_shunt_v")},
                       {col("solar_power_w")}, {col("solar_temperature_c")},
                       {bool_col("solar_enabled")},
                       {col("motor_sensor_current_a")}, {col("motor_sensor_bus_v")},
                       {col("motor_corrected_current_a")}, {col("motor_sensor_valid")}
                FROM logs
                WHERE session = ?
                ORDER BY timestamp, id
                """,
                (session_id,),
            ).fetchall()
        return [
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
                "gps_speed_kph": r[10],
                "gps_track_deg": r[11],
                "gps_fix": r[12],
                "gps_sats": r[13],
                "gps_hdop": r[14],
                "solar_current_a": r[15],
                "solar_bus_v": r[16],
                "solar_shunt_v": r[17],
                "solar_power_w": r[18],
                "solar_temperature_c": r[19],
                "solar_enabled": r[20],
                "motor_sensor_current_a": r[21],
                "motor_sensor_bus_v": r[22],
                "motor_corrected_current_a": r[23],
                "motor_sensor_valid": r[24],
            }
            for r in rows
        ]
    except Exception:
        return []


def _known_sessions(url: str, device_id: str, mode: str) -> set[str] | None:
    try:
        resp = _request_json("GET", f"{url}/api/known_sessions?device_id={device_id}&mode={mode}")
        return set(resp.get("sessions", []))
    except Exception:
        return None


def _upload_session_whole(
    url: str,
    payload: dict[str, Any],
    timeout: int = 60,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    try:
        _notify_progress(progress, phase="uploading", chunk_index=0, total_chunks=1)
        resp, _ = _request_upload_json(
            "POST",
            f"{url}/api/upload_session",
            payload,
            timeout=timeout,
            gzip_payload=_upload_gzip_enabled(),
        )
        _notify_progress(progress, phase="uploading", chunk_index=1, total_chunks=1)
        return resp if isinstance(resp, dict) else {"status": "error", "error": "invalid monitor response"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _upload_session_chunked(
    url: str,
    payload: dict[str, Any],
    chunk_size: int | None = None,
    max_bytes: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    samples = payload.get("telemetry_samples") or []
    if not isinstance(samples, list):
        return {"status": "error", "error": "invalid telemetry_samples"}
    if not samples:
        return _upload_session_whole(url, payload, progress=progress)

    chunk_size = chunk_size or _upload_chunk_size()
    max_bytes = max_bytes or _upload_chunk_max_bytes()
    sample_chunks = _split_upload_samples(payload, samples, chunk_size, max_bytes)
    total_chunks = len(sample_chunks)
    upload_id = f"{payload.get('device_id')}:{payload.get('mode')}:{payload.get('session_id')}:{int(time.time())}"
    last_resp: dict[str, Any] = {}
    gzip_payload = _upload_gzip_enabled()
    _notify_progress(
        progress,
        phase="uploading",
        chunk_index=0,
        total_chunks=total_chunks,
        rows_count=len(samples),
    )
    for chunk_index, sample_chunk in enumerate(sample_chunks):
        chunk_payload = _chunk_payload(
            payload,
            sample_chunk,
            upload_id,
            chunk_index,
            total_chunks,
            len(samples),
        )
        try:
            resp, gzip_payload = _request_upload_json(
                "POST",
                f"{url}/api/upload_session_chunk",
                chunk_payload,
                timeout=60,
                gzip_payload=gzip_payload,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"status": "unsupported", "error": "monitor does not support chunked uploads"}
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        if not isinstance(resp, dict):
            return {"status": "error", "error": "invalid monitor response"}
        last_resp = resp
        _notify_progress(
            progress,
            phase="uploading",
            chunk_index=chunk_index + 1,
            total_chunks=total_chunks,
            rows_count=len(samples),
            rows_received=resp.get("rows_received"),
        )
        if resp.get("status") == "exists":
            return resp
        if resp.get("status") != "ok":
            return resp
    return last_resp or {"status": "ok", "chunks": total_chunks, "rows_count": len(samples)}


def _upload_session(url: str, payload: dict[str, Any]) -> bool:
    return _upload_session_with_status(url, payload).get("status") in ("ok", "exists")


def _upload_session_with_status(
    url: str,
    payload: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    samples = payload.get("telemetry_samples") or []
    if isinstance(samples, list) and (len(samples) > _upload_chunk_size() or len(_encode_json_payload(payload)) > _upload_chunk_max_bytes()):
        chunked = _upload_session_chunked(url, payload, progress=progress)
        if chunked.get("status") != "unsupported":
            return chunked
    return _upload_session_whole(url, payload, progress=progress)


def _build_session_payload(db_path: str, session_id: str, device_id: str | None = None) -> dict[str, Any] | None:
    rows = _fetch_session_rows(db_path, session_id)
    if not rows:
        return None
    metrics = _metrics_for_session(session_id) or {}
    solar_enabled = bool(metrics.get("solar_enabled", rows[0].get("solar_enabled", True)))
    return {
        "device_id": device_id or _device_id(),
        "session_id": session_id,
        "mode": _mode_from_db_path(db_path),
        "test_mode": 1 if is_test_mode() else 0,
        "solar_enabled": 1 if solar_enabled else 0,
        "telemetry_samples": rows,
        "metrics": metrics,
    }


def upload_session_now(
    session_id: str,
    mode: str | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    url = _monitor_url()
    if not url:
        return {"status": "missing_config", "error": "MONITOR_URL is not configured"}
    if not session_id:
        return {"status": "error", "error": "missing session"}
    if state.session_active and session_id == state.session_id:
        return {"status": "active_session", "error": "active session is not uploaded until it is ended"}

    db_path = get_db_file(mode)
    device_id = _device_id()
    resolved_mode = _mode_from_db_path(db_path)
    _notify_progress(progress, phase="checking", mode=resolved_mode, device_id=device_id)
    known = _known_sessions(url, device_id, resolved_mode)
    if known is not None and session_id in known:
        return {
            "status": "already_uploaded",
            "session": session_id,
            "mode": resolved_mode,
            "device_id": device_id,
        }

    _notify_progress(progress, phase="preparing", mode=resolved_mode, device_id=device_id)
    payload = _build_session_payload(db_path, session_id, device_id)
    if payload is None:
        return {"status": "not_found", "error": "session has no rows", "session": session_id}

    rows_count = len(payload["telemetry_samples"])
    raw_bytes = sum(len(str(row.get("raw") or "")) for row in payload["telemetry_samples"])
    _notify_progress(
        progress,
        phase="uploading",
        mode=resolved_mode,
        device_id=device_id,
        rows_count=rows_count,
        size_kb=round(raw_bytes / 1024, 2),
    )
    resp = _upload_session_with_status(url, payload, progress=progress)
    status = resp.get("status")
    if status == "exists":
        status = "already_uploaded"
    return {
        **resp,
        "status": status,
        "session": session_id,
        "mode": resolved_mode,
        "device_id": device_id,
        "rows_count": rows_count,
        "size_kb": round(raw_bytes / 1024, 2),
        "chunk_size": _upload_chunk_size(),
        "chunk_max_kb": round(_upload_chunk_max_bytes() / 1024, 2),
    }


def _sync_users(url: str, device_id: str) -> None:
    profiles = load_profiles()
    if not profiles:
        return
    server_profiles = [dict(profile) | {"active": True} for profile in profiles]
    try:
        _request_json("POST", f"{url}/api/users/sync", {"device_id": device_id, "users": server_profiles})
    except Exception:
        pass


def fetch_monitor_users() -> list[dict[str, Any]]:
    url = _monitor_url()
    if not url:
        return []
    try:
        resp = _request_json("GET", f"{url}/api/users")
        users = resp.get("users", [])
        return users if isinstance(users, list) else []
    except Exception:
        return []


def build_photo_upload_payload(
    *,
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    captured_at: str,
    distance_km: float,
    interval_km: float,
    gps_snapshot: dict[str, Any] | None = None,
    metrics_snapshot: dict[str, Any] | None = None,
    raw_values_snapshot: list[Any] | None = None,
    solar_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gps = gps_snapshot or get_status()
    gps_ok = (
        bool(gps.get("has_fix"))
        and not gps.get("stale")
        and gps.get("lat") is not None
        and gps.get("lon") is not None
    )
    raw_values = raw_values_snapshot if isinstance(raw_values_snapshot, list) else state.latest_raw_values
    raw_values = raw_values if isinstance(raw_values, list) else []
    voltage = _safe_float(raw_values[1] if len(raw_values) > 1 else None)
    speed_kph = _safe_float(raw_values[3] if len(raw_values) > 3 else None)
    generator_current_a = _safe_float(raw_values[13] if len(raw_values) > 13 else None)
    generator_power_w = None
    if voltage is not None and generator_current_a is not None:
        generator_power_w = max(0.0, voltage * generator_current_a)
    metrics = metrics_snapshot or state.session_metrics
    solar_enabled = bool(metrics.get("solar_enabled", state.solar_roof_enabled))
    solar = solar_snapshot or state.solar_sensor
    solar_power_w = _safe_float(solar.get("power_w")) if solar_enabled else 0.0
    if solar_power_w is None:
        solar_current_a = _safe_float(solar.get("current_a")) or 0.0
        solar_bus_v = _safe_float(solar.get("bus_v")) or 0.0
        solar_power_w = max(0.0, solar_current_a * solar_bus_v)
    payload = {
        "device_id": _device_id(),
        "session_id": state.session_id,
        "mode": _mode_from_db_path(get_db_file()),
        "test_mode": 1 if is_test_mode() else 0,
        "solar_enabled": 1 if solar_enabled else 0,
        "user_id": getattr(state, "current_user_id", None),
        "user_initials": getattr(state, "current_user", None),
        "user_snapshot": profile_snapshot(getattr(state, "current_user_profile", None)),
        "captured_at": captured_at,
        "distance_km": distance_km,
        "interval_km": interval_km,
        "filename": filename,
        "mime_type": mime_type,
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "gps_available": 1 if gps_ok else 0,
        "gps_lat": gps.get("lat") if gps_ok else None,
        "gps_lon": gps.get("lon") if gps_ok else None,
        "gps_alt": gps.get("alt") if gps_ok else None,
        "gps_speed_kph": gps.get("speed_kph") if gps_ok else None,
        "gps_track_deg": gps.get("track_deg") if gps_ok else None,
        "gps_fix": 1 if gps_ok else 0,
        "gps_sats": gps.get("sats") if gps_ok else None,
        "gps_hdop": gps.get("hdop") if gps_ok else None,
        "speed_kph": speed_kph,
        "session_distance_km": metrics.get("distance_km"),
        "gps_uphill_m": metrics.get("gps_uphill_m"),
        "solar_power_w": solar_power_w,
        "generator_power_w": generator_power_w,
        "solar_wh": metrics.get("solar_Wh") if solar_enabled else 0.0,
        "metrics": {
            "distance_km": metrics.get("distance_km"),
            "gps_uphill_m": metrics.get("gps_uphill_m"),
            "solar_enabled": solar_enabled,
            "positive_Wh": metrics.get("positive_Wh"),
            "regen_Wh": metrics.get("regen_Wh"),
            "human_Wh": metrics.get("human_Wh"),
            "solar_Wh": metrics.get("solar_Wh") if solar_enabled else 0.0,
            "speed_kph": speed_kph,
            "solar_power_w": solar_power_w,
            "generator_power_w": generator_power_w,
        },
    }
    return payload


def upload_photo_payload(payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    url = _monitor_url()
    if not url:
        raise RuntimeError("MONITOR_URL is not configured")

    resp = _request_json("POST", f"{url}/api/upload_photo", payload, timeout=timeout)
    if resp.get("status") != "ok":
        raise RuntimeError(resp.get("error") or "photo upload failed")
    return resp


def monitor_upload_photo(
    *,
    image_bytes: bytes,
    filename: str,
    mime_type: str,
    captured_at: str,
    distance_km: float,
    interval_km: float,
    gps_snapshot: dict[str, Any] | None = None,
    metrics_snapshot: dict[str, Any] | None = None,
    raw_values_snapshot: list[Any] | None = None,
    solar_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_photo_upload_payload(
        image_bytes=image_bytes,
        filename=filename,
        mime_type=mime_type,
        captured_at=captured_at,
        distance_km=distance_km,
        interval_km=interval_km,
        gps_snapshot=gps_snapshot,
        metrics_snapshot=metrics_snapshot,
        raw_values_snapshot=raw_values_snapshot,
        solar_snapshot=solar_snapshot,
    )
    return upload_photo_payload(payload)


def _safe_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _send_heartbeat(url: str, device_id: str) -> None:
    current_db = get_db_file()
    gps = get_status()
    gps_ok = (
        bool(gps.get("has_fix"))
        and not gps.get("stale")
        and gps.get("lat") is not None
        and gps.get("lon") is not None
    )
    payload = {
        "device_id": device_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": state.session_id,
        "session_active": 1 if state.session_active else 0,
        "mode": _mode_from_db_path(current_db),
        "test_mode": 1 if is_test_mode() else 0,
        "solar_enabled": 1 if bool(state.session_metrics.get("solar_enabled", state.solar_roof_enabled)) else 0,
        "user_id": getattr(state, "current_user_id", None),
        "user_initials": getattr(state, "current_user", None),
        "gps_available": 1 if gps_ok else 0,
        "gps_lat": gps.get("lat") if gps_ok else None,
        "gps_lon": gps.get("lon") if gps_ok else None,
        "gps_timestamp_utc": gps.get("timestamp_utc") if gps_ok else None,
    }
    try:
        _request_json("POST", f"{url}/api/heartbeat", payload)
    except Exception:
        pass


def _sync_once() -> None:
    url = _monitor_url()
    if not url:
        return
    device_id = _device_id()
    _send_heartbeat(url, device_id)
    _sync_users(url, device_id)
    try:
        from .photo_capture import flush_pending_photo_uploads

        flush_pending_photo_uploads()
    except Exception:
        pass

    current_session = state.session_id if state.session_active else None

    if not _auto_upload_sessions_enabled():
        return

    for db_path in _list_db_files():
        mode = _mode_from_db_path(db_path)
        known = _known_sessions(url, device_id, mode)
        sessions = _fetch_sessions(db_path)
        for sid in sessions:
            if not sid or sid == current_session:
                continue
            if known is not None and sid in known:
                continue
            payload = _build_session_payload(db_path, sid, device_id)
            if payload is None:
                continue
            if not _upload_session(url, payload):
                return


def _sync_loop() -> None:
    while True:
        try:
            _sync_once()
        except Exception:
            pass
        time.sleep(60)


def start_monitor_sync() -> None:
    if state.monitor_started:
        return
    if not _monitor_url():
        return
    thread = threading.Thread(target=_sync_loop, daemon=True)
    thread.start()
    state.monitor_started = True
