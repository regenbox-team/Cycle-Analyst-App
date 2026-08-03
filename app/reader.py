from __future__ import annotations
import time
import random
import re
import os
import shlex, subprocess
import serial
import sqlite3
from datetime import datetime

from .config import BAUDRATE, get_db_file, VEHICLE_CONFIGS
from .modes import is_test_mode
from .metrics import update_metrics, update_solar_only_metrics
from .photo_capture import maybe_schedule_photo_capture
from . import state
from . import modes
from .solar_sensor import read_sensor_sample, read_solar_sample


def corrected_motor_current(sensor_current_a, solar_current_a, generator_current_a) -> float:
    """Return pure motor current using the vehicle's measured bus topology."""
    return float(sensor_current_a or 0.0) - (
        float(solar_current_a or 0.0) + float(generator_current_a or 0.0)
    )


def parse_line(line: str):
    try:
        if not line:
            return None
        parts = re.split(r'\s+', line.strip())
        if len(parts) != 15:
            return None
        return [float(x) for x in parts[:14]] + [parts[14]]
    except Exception:
        return None


def generate_fake_data():
    if not hasattr(generate_fake_data, "distance"):
        generate_fake_data.distance = state.session_metrics.get("distance_total", 0.0)
    if not hasattr(generate_fake_data, "ah"):
        generate_fake_data.ah = 0.0
    if not hasattr(generate_fake_data, "amps"):
        generate_fake_data.amps = 0.0

    dt = 0.1 / 3600
    speed = round(random.uniform(40, 45), 1)
    generate_fake_data.distance += speed * dt

    drift = random.uniform(-5, 5)
    generate_fake_data.amps += drift
    generate_fake_data.amps *= 0.98
    generate_fake_data.amps = max(-50, min(100, generate_fake_data.amps))

    amps = round(generate_fake_data.amps, 2)
    voltage = 50

    generate_fake_data.ah += max(0, amps) * dt
    capacity_ah = VEHICLE_CONFIGS.get(modes.vehicle_mode, {}).get("battery_capacity_ah", 64)
    generate_fake_data.ah = max(0, min(capacity_ah, generate_fake_data.ah))

    return [
        round(generate_fake_data.ah, 4),
        voltage,
        amps,
        speed,
        round(generate_fake_data.distance, 3),
        round(random.uniform(25, 65), 1),
        random.randint(0, 90),
        0, 0, 0.8, 0.5, 50,
        round(random.uniform(0, 10), 3),
        round(random.uniform(1.0, 1.2), 2),
        "2B"
    ]


def _update_live_gps_climb() -> None:
    gps = getattr(state, "gps_state", {}) or {}
    if not gps.get("has_fix"):
        return
    alt = gps.get("alt")
    if alt is None:
        return
    try:
        alt_m = float(alt)
    except Exception:
        return

    last_alt = state.session_metrics.get("last_gps_alt_m")
    state.session_metrics["last_gps_alt_m"] = alt_m
    if last_alt is None:
        return
    try:
        diff = alt_m - float(last_alt)
    except Exception:
        return
    if diff > 1.0:
        state.session_metrics["gps_uphill_m"] = float(state.session_metrics.get("gps_uphill_m") or 0.0) + diff


def read_serial():
    last_db_write_time = time.time()
    last_live_state_write_time = 0.0
    last_data_time = time.time()
    last_control_sync_time = 0.0
    last_session_active = state.session_active
    serial_port_opened = False
    ser = None
    proc = None
    last_port = None
    solar_sensor = None
    solar_failure_backoff_until = 0.0
    motor_sensor = None
    motor_failure_backoff_until = 0.0

    while True:
        time.sleep(0.1)
        data = None

        now_ts = time.time()
        if now_ts - last_control_sync_time >= 0.5:
            last_control_sync_time = now_ts
            try:
                from . import modes as _modes
                requested_mode = _modes.load_vehicle_mode()
                if requested_mode != _modes.vehicle_mode:
                    _modes.apply_vehicle_mode(requested_mode)
            except Exception:
                pass
            try:
                loaded_session_id = state.load_session_id()
                loaded_active = state.load_session_active()
                session_changed = loaded_session_id != state.session_id
                active_started = loaded_active and not last_session_active
                state.session_id = loaded_session_id
                state.session_active = loaded_active
                last_session_active = loaded_active
                if session_changed or active_started:
                    state.load_session_metrics_from_file(loaded_session_id)
                state.current_user = state.load_current_user()
                state.current_user_id = state.load_current_user_id()
                state.solar_roof_enabled = state.load_solar_roof_enabled()
                try:
                    from .user_profiles import get_profile
                    state.current_user_profile = get_profile(state.current_user_id or state.current_user)
                except Exception:
                    pass
            except Exception:
                pass

        # Clear stale values regardless of source errors
        if now_ts - last_data_time > 3:
            if state.latest_raw_values is not None:
                state.latest_raw_values = None

        solar_roof_enabled = bool(getattr(state, "solar_roof_enabled", True))
        solar_sample = None
        if solar_roof_enabled:
            solar_sample, solar_failure_backoff_until, solar_sensor = read_solar_sample(
                solar_sensor,
                solar_failure_backoff_until,
            )
        if solar_sample is not None:
            state.solar_sensor.update({
                "enabled": True,
                "source": solar_sample.source,
                "address": getattr(solar_sensor, "address", None),
                "manufacturer_id": getattr(solar_sensor, "manufacturer_id", None),
                "device_id": getattr(solar_sensor, "device_id", None),
                "current_a": solar_sample.current_a,
                "bus_v": solar_sample.bus_v,
                "shunt_v": solar_sample.shunt_v,
                "power_w": getattr(solar_sample, "power_w", 0.0),
                "raw_current_a": getattr(solar_sample, "raw_current_a", solar_sample.current_a),
                "raw_power_w": getattr(solar_sample, "raw_power_w", getattr(solar_sample, "power_w", 0.0)),
                "temperature_c": getattr(solar_sample, "temperature_c", 0.0),
                "last_update": now_ts,
            })
        else:
            state.solar_sensor.update({
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
            })

        motor_sample, motor_failure_backoff_until, motor_sensor = read_sensor_sample(
            motor_sensor,
            motor_failure_backoff_until,
            "APP_MOTOR",
        )
        if (
            motor_sample is not None
            and solar_sensor is not None
            and getattr(motor_sensor, "bus_id", None) == getattr(solar_sensor, "bus_id", None)
            and getattr(motor_sensor, "address", None) == getattr(solar_sensor, "address", None)
        ):
            motor_sensor.close()
            motor_sensor = None
            motor_sample = None
            motor_failure_backoff_until = now_ts + 2.0
        if motor_sample is not None:
            generator_a = float(data[13]) if data is not None else (
                float(state.latest_raw_values[13]) if state.latest_raw_values else 0.0
            )
            solar_a = float(state.solar_sensor.get("current_a") or 0.0) if solar_roof_enabled else 0.0
            corrected_a = corrected_motor_current(motor_sample.current_a, solar_a, generator_a)
            state.motor_sensor.update({
                "enabled": True,
                "source": motor_sample.source,
                "address": getattr(motor_sensor, "address", None),
                "manufacturer_id": getattr(motor_sensor, "manufacturer_id", None),
                "device_id": getattr(motor_sensor, "device_id", None),
                "current_a": motor_sample.current_a,
                "bus_v": motor_sample.bus_v,
                "shunt_v": motor_sample.shunt_v,
                "power_w": motor_sample.power_w,
                "raw_current_a": getattr(motor_sample, "raw_current_a", motor_sample.current_a),
                "raw_power_w": getattr(motor_sample, "raw_power_w", motor_sample.power_w),
                "temperature_c": motor_sample.temperature_c,
                "corrected_current_a": corrected_a,
                "corrected_power_w": motor_sample.bus_v * corrected_a,
                "solar_correction_a": solar_a,
                "generator_correction_a": generator_a,
                "valid": True,
                "last_update": now_ts,
            })
        else:
            state.motor_sensor.update({
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
            })

        # Reopen if port changed
        from . import modes as _modes
        SERIAL_PORT = _modes.SERIAL_PORT
        if SERIAL_PORT != last_port:
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
                proc = None
            serial_port_opened = False
            last_port = SERIAL_PORT

        if is_test_mode():
            data = generate_fake_data()
            last_data_time = time.time()
        else:
            try:
                if isinstance(SERIAL_PORT, str) and SERIAL_PORT.startswith("exec:"):
                    if proc is None or proc.poll() is not None:
                        cmd = SERIAL_PORT[len("exec:"):].strip()
                        proc = subprocess.Popen(
                            shlex.split(cmd),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                            bufsize=1,
                        )
                    line = proc.stdout.readline() if proc.stdout else ""
                    if line == "" and proc.poll() is not None:
                        proc = None
                        continue
                else:
                    if ser is None or not serial_port_opened:
                        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
                        serial_port_opened = True
                    raw = ser.readline()
                    try:
                        line = raw.decode(errors="ignore")
                    except Exception:
                        line = str(raw)

                if not line:
                    data = None
                else:
                    data = parse_line(line.strip())
                    if not data or len(data) != 15:
                        data = None

                if data is not None:
                    last_data_time = time.time()
            except Exception:
                # brief backoff on source error
                time.sleep(0.3)
                serial_port_opened = False
                proc = None
                ser = None
                data = None

        if data is not None:
            state.latest_raw_values = data
            if state.motor_sensor.get("valid"):
                generator_a = float(data[13])
                solar_a = float(state.solar_sensor.get("current_a") or 0.0) if solar_roof_enabled else 0.0
                corrected_a = corrected_motor_current(
                    state.motor_sensor.get("current_a"), solar_a, generator_a
                )
                state.motor_sensor.update({
                    "corrected_current_a": corrected_a,
                    "corrected_power_w": float(state.motor_sensor.get("bus_v") or 0.0) * corrected_a,
                    "solar_correction_a": solar_a,
                    "generator_correction_a": generator_a,
                })

        live_gps_update = float(state.gps_state.get("last_update") or 0.0)
        if (
            data is not None
            or solar_sample is not None
            or motor_sample is not None
            or live_gps_update > last_live_state_write_time
        ) and now_ts - last_live_state_write_time >= 1:
            last_live_state_write_time = now_ts
            state.save_session_metrics_to_file()

        if not state.session_active:
            continue

        now = time.time()
        if data is not None:
            update_metrics(data, now, solar_sample=solar_sample)
            _update_live_gps_climb()
            if os.getenv("APP_SCHEDULE_PHOTOS", "1").strip().lower() not in {"0", "false", "no", "off"}:
                maybe_schedule_photo_capture(state.session_metrics.get("distance_km"))
        elif solar_sample is not None:
            update_solar_only_metrics(solar_sample, now)

        if now - last_db_write_time >= 1 and (data is not None or solar_sample is not None or motor_sample is not None):
            last_db_write_time = now
            if bool(state.session_metrics.get("solar_enabled", state.solar_roof_enabled)):
                try:
                    from .solar_range import persist_estimate
                    voltage = state.latest_raw_values[1] if state.latest_raw_values else None
                    persist_estimate(
                        state.session_id,
                        state.session_metrics,
                        voltage,
                        gps_state=getattr(state, "gps_state", None),
                        solar_voltage=state.solar_sensor.get("bus_v"),
                    )
                except Exception:
                    pass
            state.save_session_metrics_to_file()
            raw_line = " ".join(map(str, data)) if data is not None else None
            timestamp = datetime.utcnow().isoformat()
            session_solar_enabled = bool(state.session_metrics.get("solar_enabled", state.solar_roof_enabled))
            user_profile = getattr(state, "current_user_profile", None)
            if not user_profile:
                try:
                    from .user_profiles import get_profile
                    user_profile = get_profile(getattr(state, "current_user_id", None) or state.current_user)
                except Exception:
                    user_profile = None
            try:
                from .user_profiles import profile_snapshot_json
                user_snapshot_json = profile_snapshot_json(user_profile)
            except Exception:
                user_snapshot_json = None
            user_id = user_profile.get("user_id") if user_profile else getattr(state, "current_user_id", None)
            user_initials = user_profile.get("initials") if user_profile else state.current_user
            # Snapshot GPS at the same tick
            gps = getattr(state, 'gps_state', {}) or {}
            try:
                with sqlite3.connect(get_db_file()) as conn:
                    conn.execute(
                        """
                        INSERT INTO logs (
                            timestamp, session, raw, user,
                            user_id, user_initials, user_snapshot_json,
                            gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                            solar_current_a, solar_bus_v, solar_shunt_v, solar_power_w, solar_temperature_c,
                            solar_enabled,
                            motor_sensor_current_a, motor_sensor_bus_v, motor_corrected_current_a, motor_sensor_valid
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            timestamp,
                            state.session_id,
                            raw_line,
                            state.current_user,
                            user_id,
                            user_initials,
                            user_snapshot_json,
                            gps.get("lat"),
                            gps.get("lon"),
                            gps.get("alt"),
                            gps.get("speed_kph"),
                            gps.get("track_deg"),
                            1 if gps.get("has_fix") else 0,
                            gps.get("sats"),
                            gps.get("hdop"),
                            state.solar_sensor.get("current_a") if session_solar_enabled else None,
                            state.solar_sensor.get("bus_v") if session_solar_enabled else None,
                            state.solar_sensor.get("shunt_v") if session_solar_enabled else None,
                            state.solar_sensor.get("power_w") if session_solar_enabled else None,
                            state.solar_sensor.get("temperature_c") if session_solar_enabled else None,
                            1 if session_solar_enabled else 0,
                            state.motor_sensor.get("current_a") if state.motor_sensor.get("enabled") else None,
                            state.motor_sensor.get("bus_v") if state.motor_sensor.get("enabled") else None,
                            state.motor_sensor.get("corrected_current_a") if state.motor_sensor.get("valid") else None,
                            1 if state.motor_sensor.get("valid") else 0,
                        ),
                    )
            except Exception:
                pass

        # stale clear already handled at loop start
