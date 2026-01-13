from __future__ import annotations
import base64
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.request
from typing import Any

from .config import BASE_DIR, DB_FILE, SESSION_METRICS_DIR, get_db_file
from .modes import is_test_mode
from . import state


def _device_id() -> str:
    return os.getenv("MONITOR_DEVICE_ID") or socket.gethostname()


def _monitor_url() -> str | None:
    return os.getenv("MONITOR_URL")


def _auth_header() -> dict[str, str]:
    user = os.getenv("MONITOR_USER", "")
    password = os.getenv("MONITOR_PASS", "")
    raw = f"{user}:{password}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 5) -> Any:
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_header())
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


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
            rows = conn.execute(
                """
                SELECT timestamp, session, raw, user,
                       gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop
                FROM logs
                WHERE session = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        return [
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
        ]
    except Exception:
        return []


def _known_sessions(url: str, device_id: str, mode: str) -> set[str] | None:
    try:
        resp = _request_json("GET", f"{url}/api/known_sessions?device_id={device_id}&mode={mode}")
        return set(resp.get("sessions", []))
    except Exception:
        return None


def _upload_session(url: str, payload: dict[str, Any]) -> bool:
    try:
        resp = _request_json("POST", f"{url}/api/upload_session", payload)
        return resp.get("status") in ("ok", "exists")
    except Exception:
        return False


def _send_heartbeat(url: str, device_id: str) -> None:
    current_db = get_db_file()
    payload = {
        "device_id": device_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": state.session_id,
        "session_active": 1 if state.session_active else 0,
        "mode": _mode_from_db_path(current_db),
        "test_mode": 1 if is_test_mode() else 0,
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

    current_session = state.session_id if state.session_active else None

    for db_path in _list_db_files():
        mode = _mode_from_db_path(db_path)
        known = _known_sessions(url, device_id, mode)
        sessions = _fetch_sessions(db_path)
        for sid in sessions:
            if not sid or sid == current_session:
                continue
            if known is not None and sid in known:
                continue
            rows = _fetch_session_rows(db_path, sid)
            if not rows:
                continue
            payload = {
                "device_id": device_id,
                "session_id": sid,
                "mode": mode,
                "test_mode": 1 if is_test_mode() else 0,
                "logs": rows,
                "metrics": _metrics_for_session(sid),
            }
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
