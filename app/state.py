from __future__ import annotations
import os, time, json
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import (
    SESSION_FILE, SESSION_STATE_FILE, SESSION_METRICS_DIR, USER_FILE,
)

# Live state used across modules
session_id: str | None = None
session_start_time: float = time.time()
latest_raw_values = None
current_user: str = "JD"
session_active: bool = False
reader_started: bool = False
gps_reader_started: bool = False
monitor_started: bool = False

# GPS state
gps_state = {
    "lat": None,
    "lon": None,
    "alt": None,
    "speed_kph": None,
    "track_deg": None,
    "fix_quality": 0,
    "sats": 0,
    "hdop": None,
    "timestamp_utc": None,
    "has_fix": False,
    "last_update": 0.0,
}

# Optional external solar sensor state.
solar_sensor = {
    "enabled": False,
    "source": None,
    "address": None,
    "manufacturer_id": None,
    "device_id": None,
    "current_a": 0.0,
    "bus_v": 0.0,
    "shunt_v": 0.0,
    "power_w": 0.0,
    "temperature_c": 0.0,
    "last_update": 0.0,
}


def default_photo_capture_settings() -> dict:
    return {
        "enabled": False,
        "interval_km": 1.0,
        "last_trigger_distance_km": 0.0,
        "capture_count": 0,
        "last_captured_at": None,
        "last_uploaded_at": None,
        "latest_local_path": None,
        "latest_public_url": None,
        "last_error": None,
    }


# Core session metrics store
session_metrics = {
    "speed_max": 0,
    "speed_sum": 0,
    "speed_count": 0,
    "power_sum": 0,
    "power_max": float('-inf'),
    "power_min": float('inf'),
    "human_power_max": 0,
    "human_power_sum": 0,
    "human_power_count": 0,
    "solar_power_max": 0,
    "solar_power_sum": 0,
    "solar_power_count": 0,
    "temp_sum": 0,
    "temp_max": 0,
    "temp_count": 0,
    "positive_Wh": 0,
    "regen_Wh": 0,
    "human_Ah": 0,
    "solar_Ah": 0,
    "human_Wh": 0,
    "solar_Wh": 0,
    "calories_burned": 0,
    "distance_km": 0,
    "distance_start": None,
    "last_km_checkpoints": [0],
    "distance_total": 0.0,
    "distance_offset": 0.0,
    "last_raw_distance": None,
    "ca_reset_detected": False,
    "ca_reset_prompt": False,
    "ah_offset": 0.0,
    "Wh_per_km_last": [],
    "net_Wh_per_km_last": [],
    "human_pct_per_km_last": [],
    "solar_pct_per_km_last": [],
    "last_regen_checkpoint": 0,
    "regen_pct_per_km_last": [],
    "photo_capture": default_photo_capture_settings(),
}


def save_session_id(sid: str) -> None:
    with open(SESSION_FILE, "w") as f:
        f.write(str(sid))


def load_session_id() -> str:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    sid = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    save_session_id(sid)
    return sid


def save_session_active(flag: bool) -> None:
    with open(SESSION_STATE_FILE, "w") as f:
        f.write("active" if flag else "inactive")


def load_session_active() -> bool:
    if os.path.exists(SESSION_STATE_FILE):
        try:
            with open(SESSION_STATE_FILE, "r") as f:
                return f.read().strip() == "active"
        except Exception:
            return False
    return False


def save_current_user(user: str) -> None:
    with open(USER_FILE, "w") as f:
        f.write(user)


def load_current_user() -> str:
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r") as f:
                return f.read().strip()
        except Exception:
            return "JD"
    return "JD"


def metrics_json_path(for_session: str | None = None) -> str | None:
    sid = for_session
    if not sid:
        sid = session_id
    if not sid:
        return None
    return os.path.join(SESSION_METRICS_DIR, f"{sid}_session_metrics.json")


def save_session_metrics_to_file() -> None:
    try:
        path = metrics_json_path()
        if path:
            with open(path, "w") as f:
                json.dump(session_metrics, f)
    except Exception:
        pass
