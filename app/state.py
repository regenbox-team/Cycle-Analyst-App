from __future__ import annotations
import os, time, json
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import (
    SESSION_FILE, SESSION_STATE_FILE, SESSION_METRICS_DIR, USER_FILE, SOLAR_ROOF_FILE,
)

# Live state used across modules
session_id: str | None = None
session_start_time: float = time.time()
latest_raw_values = None
current_user: str = "JD"
current_user_id: str | None = None
current_user_profile: dict | None = None
session_active: bool = False
reader_started: bool = False
gps_reader_started: bool = False
monitor_started: bool = False
solar_roof_enabled: bool = True

# Ultra-light runtime diagnostics exposed to the dashboard. Values are updated
# in memory only, never persisted.
reader_diag = {
    "updated_at": 0.0,
    "loop_hz": 0.0,
    "loop_ms": 0.0,
    "work_ms": 0.0,
    "source_ms": 0.0,
    "solar_ms": 0.0,
    "motor_ms": 0.0,
    "metrics_ms": 0.0,
    "db_ms": 0.0,
    "raw_hz": 0.0,
    "raw_age_ms": None,
    "raw_updates": 0,
    "last_raw_monotonic": 0.0,
    "source": None,
    "error": None,
}

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
    "raw_current_a": 0.0,
    "raw_power_w": 0.0,
    "temperature_c": 0.0,
    "last_update": 0.0,
}

# Optional INA228 between the battery and the 60 V hub. Detailed raw values are
# live-only; the database stores only the compact fields needed for analysis.
motor_sensor = {
    "enabled": False,
    "source": None,
    "address": None,
    "manufacturer_id": None,
    "device_id": None,
    "current_a": 0.0,
    "bus_v": 0.0,
    "shunt_v": 0.0,
    "power_w": 0.0,
    "raw_current_a": 0.0,
    "raw_power_w": 0.0,
    "temperature_c": 0.0,
    "corrected_current_a": 0.0,
    "corrected_power_w": 0.0,
    "solar_correction_a": 0.0,
    "generator_correction_a": 0.0,
    "valid": False,
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
        "pending_upload_count": 0,
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
    "last_distance_update_time": None,
    "pending_distance_reset_raw": None,
    "pending_distance_reset_offset": None,
    "pending_distance_reset_time": None,
    "distance_glitch_count": 0,
    "distance_reset_pending_count": 0,
    "last_rejected_distance": None,
    "last_rejected_distance_time": None,
    "ca_reset_detected": False,
    "ca_reset_prompt": False,
    "ah_offset": 0.0,
    "Wh_per_km_last": [],
    "net_Wh_per_km_last": [],
    "human_pct_per_km_last": [],
    "solar_pct_per_km_last": [],
    "last_regen_checkpoint": 0,
    "regen_pct_per_km_last": [],
    "gps_uphill_m": 0.0,
    "last_gps_alt_m": None,
    "photo_capture": default_photo_capture_settings(),
    "solar_enabled": True,
    "user_id": None,
    "user_initials": "JD",
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


def save_current_user_id(user_id: str) -> None:
    from .user_profiles import save_current_user_id as _save_current_user_id
    _save_current_user_id(user_id)


def load_current_user_id() -> str | None:
    from .user_profiles import load_current_user_id as _load_current_user_id
    return _load_current_user_id()


def save_solar_roof_enabled(flag: bool) -> None:
    with open(SOLAR_ROOF_FILE, "w") as f:
        f.write("true" if flag else "false")


def load_solar_roof_enabled() -> bool:
    if os.path.exists(SOLAR_ROOF_FILE):
        try:
            with open(SOLAR_ROOF_FILE, "r") as f:
                return f.read().strip().lower() == "true"
        except Exception:
            return True
    return True


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
            payload = dict(session_metrics)
            # In split-service installs the web process may set a manual full-charge
            # origin while the recorder owns the in-memory metrics. Preserve that
            # command when the recorder performs its next atomic snapshot write.
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as existing_file:
                        existing_payload = json.load(existing_file)
                    if existing_payload.get("solar_battery_estimate_source") == "manual_full_charge":
                        for key in (
                            "solar_battery_start_wh",
                            "solar_battery_capacity_wh",
                            "solar_battery_start_soc",
                            "solar_battery_estimate_source",
                            "solar_battery_confidence",
                            "solar_battery_use_baseline_wh",
                        ):
                            if key in existing_payload:
                                payload[key] = existing_payload[key]
                                session_metrics[key] = existing_payload[key]
            except Exception:
                pass
            if os.getenv("APP_PRESERVE_PHOTO_STATE_FROM_FILE", "0").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as existing_file:
                            existing_payload = json.load(existing_file)
                        existing_photo = existing_payload.get("photo_capture")
                        if isinstance(existing_photo, dict):
                            payload["photo_capture"] = existing_photo
                except Exception:
                    pass
            payload["_runtime"] = {
                "saved_at": time.time(),
                "session_id": session_id,
                "session_active": session_active,
                "latest_raw_values": latest_raw_values,
                "gps_state": gps_state,
                "solar_sensor": solar_sensor,
                "motor_sensor": motor_sensor,
                "reader_diag": reader_diag,
                "current_user": current_user,
                "current_user_id": current_user_id,
            }
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(payload, f)
            os.replace(tmp_path, path)
    except Exception:
        pass


def load_session_metrics_from_file(for_session: str | None = None) -> bool:
    path = metrics_json_path(for_session)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return False

        runtime = payload.pop("_runtime", None)
        session_metrics.update(payload)

        if isinstance(runtime, dict):
            raw_values = runtime.get("latest_raw_values")
            if isinstance(raw_values, list):
                globals()["latest_raw_values"] = raw_values
            gps = runtime.get("gps_state")
            if isinstance(gps, dict):
                gps_state.update(gps)
            solar = runtime.get("solar_sensor")
            if isinstance(solar, dict):
                solar_sensor.update(solar)
            motor = runtime.get("motor_sensor")
            if isinstance(motor, dict):
                motor_sensor.update(motor)
            diag = runtime.get("reader_diag")
            if isinstance(diag, dict):
                reader_diag.update(diag)
            globals()["session_id"] = runtime.get("session_id") or globals()["session_id"]
            globals()["session_active"] = bool(runtime.get("session_active"))
            user = runtime.get("current_user")
            if isinstance(user, str) and user:
                globals()["current_user"] = user
            user_id = runtime.get("current_user_id")
            if isinstance(user_id, str) and user_id:
                globals()["current_user_id"] = user_id
        return True
    except Exception:
        return False
