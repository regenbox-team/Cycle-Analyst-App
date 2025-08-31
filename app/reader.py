from __future__ import annotations
import time
import random
import re
import shlex, subprocess
import serial
import sqlite3
from datetime import datetime

from .config import BAUDRATE, DB_FILE
from .modes import is_test_mode, SERIAL_PORT
from .metrics import update_metrics
from .state import (
    latest_raw_values, session_active, session_metrics,
    save_session_metrics_to_file, session_id, current_user
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
        generate_fake_data.distance = session_metrics.get("distance_total", 0.0)
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
    generate_fake_data.ah = max(0, min(64, generate_fake_data.ah))

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
        round(random.uniform(2.0, 3.0), 2),
        "2B"
    ]


def read_serial():
    global latest_raw_values
    last_db_write_time = time.time()
    last_data_time = time.time()
    serial_port_opened = False
    ser = None
    proc = None
    last_port = None

    while True:
        time.sleep(0.1)

        # Reopen if port changed
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
            latest_raw_values = data
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
                    continue

                data = parse_line(line.strip())
                if not data or len(data) != 15:
                    continue

                latest_raw_values = data
                last_data_time = time.time()
            except Exception:
                time.sleep(1)
                serial_port_opened = False
                proc = None
                ser = None
                continue

        if not session_active:
            continue

        now = time.time()
        update_metrics(data, now)

        if now - last_db_write_time >= 1:
            last_db_write_time = now
            save_session_metrics_to_file()
            raw_line = " ".join(map(str, data))
            timestamp = datetime.utcnow().isoformat()
            try:
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute(
                        "INSERT INTO logs (timestamp, session, raw, user) VALUES (?, ?, ?, ?)",
                        (timestamp, session_id, raw_line, current_user)
                    )
            except Exception:
                pass

        if time.time() - last_data_time > 3:
            if latest_raw_values is not None:
                latest_raw_values = None
