from __future__ import annotations
import time
import random
import re
import shlex, subprocess
import serial
import sqlite3
from datetime import datetime

from .config import BAUDRATE, get_db_file, VEHICLE_CONFIGS
from .modes import is_test_mode
from .metrics import update_metrics, update_solar_only_metrics
from . import state
from .modes import vehicle_mode
from .solar_sensor import read_solar_sample


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
    capacity_ah = VEHICLE_CONFIGS.get(vehicle_mode, {}).get("battery_capacity_ah", 64)
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


def read_serial():
    last_db_write_time = time.time()
    last_data_time = time.time()
    serial_port_opened = False
    ser = None
    proc = None
    last_port = None
    solar_sensor = None
    solar_failure_backoff_until = 0.0

    while True:
        time.sleep(0.1)
        data = None

        # Clear stale values regardless of source errors
        now_ts = time.time()
        if now_ts - last_data_time > 3:
            if state.latest_raw_values is not None:
                state.latest_raw_values = None

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
                "temperature_c": 0.0,
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

        if not state.session_active:
            continue

        now = time.time()
        if data is not None:
            update_metrics(data, now, solar_sample=solar_sample)
        elif solar_sample is not None:
            update_solar_only_metrics(solar_sample, now)

        if now - last_db_write_time >= 1 and (data is not None or solar_sample is not None):
            last_db_write_time = now
            state.save_session_metrics_to_file()
            raw_line = " ".join(map(str, data)) if data is not None else None
            timestamp = datetime.utcnow().isoformat()
            # Snapshot GPS at the same tick
            gps = getattr(state, 'gps_state', {}) or {}
            try:
                with sqlite3.connect(get_db_file()) as conn:
                    conn.execute(
                        """
                        INSERT INTO logs (
                            timestamp, session, raw, user,
                            gps_lat, gps_lon, gps_alt, gps_speed_kph, gps_track_deg, gps_fix, gps_sats, gps_hdop,
                            solar_current_a, solar_bus_v, solar_shunt_v
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            timestamp,
                            state.session_id,
                            raw_line,
                            state.current_user,
                            gps.get("lat"),
                            gps.get("lon"),
                            gps.get("alt"),
                            gps.get("speed_kph"),
                            gps.get("track_deg"),
                            1 if gps.get("has_fix") else 0,
                            gps.get("sats"),
                            gps.get("hdop"),
                            state.solar_sensor.get("current_a"),
                            state.solar_sensor.get("bus_v"),
                            state.solar_sensor.get("shunt_v"),
                        ),
                    )
            except Exception:
                pass

        # stale clear already handled at loop start
