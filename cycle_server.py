from flask import Flask, render_template, jsonify, request, redirect
import threading
from threading import Lock
import serial
import time
import sqlite3
import random
from datetime import datetime
import os
from zoneinfo import ZoneInfo
import json
from collections import defaultdict
import re



app = Flask(__name__)

# --- CONFIGURATION ---
SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 9600
TEST_MODE_FILE = "test_mode.txt"

test_mode_lock = Lock()

def load_test_mode():
    if os.path.exists(TEST_MODE_FILE):
        with open(TEST_MODE_FILE, "r") as f:
            return f.read().strip().lower() == "true"
    return False  # default

def save_test_mode(enabled):
    with open(TEST_MODE_FILE, "w") as f:
        f.write("true" if enabled else "false")

test_mode_flag = load_test_mode()  # ✅ now valid

def is_test_mode():
    with test_mode_lock:
        return test_mode_flag

SESSION_FILE = "current_session.txt"
SESSION_STATE_FILE = "session_state.txt"
SESSION_METRICS_DIR = "session_metrics"
os.makedirs(SESSION_METRICS_DIR, exist_ok=True)
DB_FILE = "ride_data.db"
USER_FILE = "current_user.txt"
ah_offset = 0.0
TEST_MODE = load_test_mode()



def save_session_active(flag):
    with open(SESSION_STATE_FILE, "w") as f:
        f.write("active" if flag else "inactive")

def load_session_active():
    if os.path.exists(SESSION_STATE_FILE):
        with open(SESSION_STATE_FILE, "r") as f:
            return f.read().strip() == "active"
    return False

session_active = load_session_active()

def generate_fake_data():
    # Static variables
    if not hasattr(generate_fake_data, "distance"):
        generate_fake_data.distance = session_metrics.get("distance_total", 0.0)
    if not hasattr(generate_fake_data, "ah"):
        generate_fake_data.ah = 0.0
    if not hasattr(generate_fake_data, "amps"):
        generate_fake_data.amps = 0.0  # Start at zero amps

    dt = 0.1 / 3600  # seconds → hours
    speed = round(random.uniform(40, 45), 1)

    # Update distance
    generate_fake_data.distance += speed * dt

    # 🔁 Smoothly vary amps with small random drift
    drift = random.uniform(-5, 5)
    generate_fake_data.amps += drift
    generate_fake_data.amps *= 0.98  # smoothing
    generate_fake_data.amps = max(-50, min(100, generate_fake_data.amps))  # clamp to -50..100

    amps = round(generate_fake_data.amps, 2)
    voltage = 50

    # 🔋 Accumulate Ah from positive amps only (discharge)
    generate_fake_data.ah += max(0, amps) * dt
    generate_fake_data.ah = max(0, min(64, generate_fake_data.ah))  # Clamp

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



# --- SESSION MANAGEMENT ---


def save_current_user(user):
    with open(USER_FILE, "w") as f:
        f.write(user)

def load_current_user():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            return f.read().strip()
    return "JD"  # default fallback

def load_session_id():
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                return f.read().strip()
        except:
            pass
    sid = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    save_session_id(sid)
    return sid

def save_session_id(sid):
    with open(SESSION_FILE, "w") as f:
        f.write(str(sid))


def reset_session_state():
    global session_metrics
    session_metrics.update({
        "speed_max": 0,
        "speed_sum": 0,
        "speed_count": 0,
        "power_sum": 0,
        "power_max": float('-inf'),
        "power_min": float('inf'),
        "solar_power_max": 0,
        "solar_power_sum": 0,
        "solar_power_count": 0,
        "temp_sum": 0,
        "temp_max": 0,
        "temp_count": 0,
        "positive_Wh": 0,
        "regen_Wh": 0,
        "solar_Wh": 0,
        "calories_burned": 0,
        "distance_km": 0,
        "last_km_checkpoints": [],
        "distance_total": 0.0,
        "ah_offset": 0.0,
        "Wh_per_km_last": [],
        "net_Wh_per_km_last": [],
        "solar_pct_per_km_last": [],
        "last_regen_checkpoint": 0,
        "regen_pct_per_km_last": []
        
    })

    if hasattr(generate_fake_data, "distance"):
        del generate_fake_data.distance

    save_session_metrics_to_file()


# --- STATE ---
session_id = load_session_id()
session_start_time = time.time()
latest_raw_values = None
current_user = load_current_user()


session_metrics = {
    "speed_max": 0,
    "speed_sum": 0,
    "speed_count": 0,
    "power_sum": 0,
    "power_max": float('-inf'),
    "power_min": float('inf'),
    "solar_power_max": 0,
    "solar_power_sum": 0,
    "solar_power_count": 0,
    "temp_sum": 0,
    "temp_max": 0,
    "temp_count": 0,
    "positive_Wh": 0,
    "regen_Wh": 0,
    "solar_Wh": 0,
    "distance_km": 0,
    "last_km_checkpoints": [],
    "distance_total": 0.0,  # for TEST_MODE only
    "Wh_per_km_last": [],
    "net_Wh_per_km_last": []
}


# --- DATABASE INIT ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        # Crée la table si elle n'existe pas
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                session TEXT,
                raw TEXT,
                user TEXT
            )
        ''')
        conn.commit()


@app.route("/switch_user", methods=["POST"])
def switch_user():
    global current_user
    current_user = "LL" if current_user == "JD" else "JD"
    save_current_user(current_user)

    print(f"[INFO] Switched user to {current_user}")
    return jsonify({"user": current_user})

# --- PARSING ---
def parse_line(line):
    try:
        if not line:
            return None
        parts = re.split(r'\s+', line.strip())  # Allow tabs or multiple spaces
        if len(parts) != 15:
            print(f"[DEBUG] Line split into {len(parts)} parts (expected 15): {parts}")
            return None
        return [float(x) for x in parts[:14]] + [parts[14]]
    except Exception as e:
        print(f"[ERROR] parse_line failed: {e} | line: '{line}'")
        return None


# --- SERIAL READER ---
def read_serial():
    global latest_raw_values

    last_db_write_time = time.time()
    last_data_time = time.time()
    serial_port_opened = False

    while True:
        time.sleep(0.1)

        if is_test_mode():
            # --- TEST MODE ---
            if not hasattr(read_serial, "warned") or not read_serial.warned:
                print("[TEST MODE] Simulating 10Hz data...")
                read_serial.warned = True

            data = generate_fake_data()
            latest_raw_values = data
            last_data_time = time.time()

        else:
            # --- REAL SERIAL MODE ---
            try:
                if not hasattr(read_serial, "ser") or not serial_port_opened:
                    print(f"[INFO] Opening serial port {SERIAL_PORT}...")
                    read_serial.ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
                    serial_port_opened = True
                    print("[INFO] Serial port opened.")

                line = read_serial.ser.readline().decode(errors='ignore').strip()
                if not line:
                    continue

                data = parse_line(line)
                if not data or len(data) != 15:
                    print(f"[WARN] Invalid line received or bad parse: '{line}'")
                    continue

                latest_raw_values = data
                last_data_time = time.time()

            except serial.SerialException as e:
                print(f"[ERROR] Serial port error: {e}")
                time.sleep(1)
                serial_port_opened = False
                continue

        # Skip metric updates/logging if no session
        if not session_active:
            continue

        now = time.time()
        update_metrics(data, now)

        if now - last_db_write_time >= 1:
            last_db_write_time = now
            save_session_metrics_to_file()
            raw_line = " ".join(map(str, data))
            timestamp = datetime.utcnow().isoformat()
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute(
                    "INSERT INTO logs (timestamp, session, raw, user) VALUES (?, ?, ?, ?)",
                    (timestamp, session_id, raw_line, current_user)
                )

        # --- Clear stale values if no data for 3+ seconds ---
        if time.time() - last_data_time > 3:
            if latest_raw_values is not None:
                print("[INFO] No new data received in 3s — clearing latest_raw_values")
                latest_raw_values = None



# --- METRICS CALCULATION ---
def update_metrics(data, now=None):
    v = data[1]         # Voltage
    a = data[2]         # Battery current
    speed = data[3]     # Speed
    distance = data[4]  # Distance
    temp = data[5]      # Motor temperature
    solar_a = data[13]  # Solar current

    if now is None:
        now = time.time()

    if not hasattr(update_metrics, "last_time"):
        update_metrics.last_time = now
    dt = now - update_metrics.last_time
    update_metrics.last_time = now

    # Only include values in averages if speed >= 1 km/h
    if speed >= 1:
        # Speed metrics
        session_metrics["speed_sum"] += speed
        session_metrics["speed_count"] += 1
        session_metrics["speed_max"] = max(session_metrics["speed_max"], speed)

        # Power metrics
        power = v * a
        session_metrics["power_sum"] += power
        session_metrics["power_max"] = max(session_metrics["power_max"], power)
        session_metrics["power_min"] = min(session_metrics["power_min"], power)

        # Solar power metrics
        solar_power = v * solar_a
        session_metrics["solar_power_sum"] += solar_power
        session_metrics["solar_power_count"] += 1
        session_metrics["solar_power_max"] = max(session_metrics["solar_power_max"], solar_power)

        # Temperature tracking
        session_metrics["temp_sum"] += temp
        session_metrics["temp_count"] += 1

    # Energy accumulation happens regardless of speed
    power = v * a
    if abs(power) > 2:
        if a > 0:
            session_metrics["positive_Wh"] += power * dt / 3600
        elif a < 0:
            session_metrics["regen_Wh"] += abs(power) * dt / 3600

    # Solar energy
    session_metrics["solar_Wh"] += (v * solar_a) * dt / 3600

    session_metrics["calories_burned"] = session_metrics["solar_Wh"] * 1.433


    # Distance tracking
    prev_distance = session_metrics["distance_km"]
    session_metrics["distance_km"] = distance
    prev_km = int(prev_distance)
    curr_km = int(distance)
    MAX_HISTORY = 50

    # Reconstruct all km checkpoints between prev and curr
    for km in range(prev_km + 1, curr_km + 1):
        # Append the current positive_Wh for this new km
        session_metrics["last_km_checkpoints"].append(session_metrics["positive_Wh"])

        # Compute Wh/km using last two checkpoints
        if len(session_metrics["last_km_checkpoints"]) >= 2:
            prev_Wh = session_metrics["last_km_checkpoints"][-2]
            delta_Wh = session_metrics["positive_Wh"] - prev_Wh
        else:
            delta_Wh = session_metrics["positive_Wh"]

        session_metrics["Wh_per_km_last"].append(round(delta_Wh, 3))

        # Compute net Wh/km
        km_distance = max(session_metrics["distance_km"], 1e-6)
        solar_correction = session_metrics["solar_Wh"] / km_distance
        regen_correction = session_metrics["regen_Wh"] / km_distance
        net_Wh = delta_Wh - solar_correction - regen_correction
        session_metrics["net_Wh_per_km_last"].append(round(net_Wh, 3))

        # Limit history to 60 km (for 50 km mode + headroom)
        session_metrics["last_km_checkpoints"] = session_metrics["last_km_checkpoints"][-60:]
        session_metrics["Wh_per_km_last"] = session_metrics["Wh_per_km_last"][-60:]
        session_metrics["net_Wh_per_km_last"] = session_metrics["net_Wh_per_km_last"][-60:]

        # Solar %
        if delta_Wh > 0:
            avg_solar = session_metrics["solar_Wh"] / km_distance
            percent_solar = min(100, max(0, 100 * avg_solar / (delta_Wh)))
        else:
            percent_solar = 0
        session_metrics.setdefault("solar_pct_per_km_last", []).append(round(percent_solar, 1))
        session_metrics["solar_pct_per_km_last"] = session_metrics["solar_pct_per_km_last"][-60:]

        # Regen %
        regen_prev = session_metrics.get("last_regen_checkpoint", 0)
        regen_delta = session_metrics["regen_Wh"] - regen_prev
        session_metrics["last_regen_checkpoint"] = session_metrics["regen_Wh"]

        if delta_Wh > 0:
            percent_regen = min(100, max(0, 100 * regen_delta / delta_Wh))
        else:
            percent_regen = 0
        session_metrics.setdefault("regen_pct_per_km_last", []).append(round(percent_regen, 1))
        session_metrics["regen_pct_per_km_last"] = session_metrics["regen_pct_per_km_last"][-60:]


def restore_session_metrics(session_id):
    global session_metrics
    json_path = os.path.join(SESSION_METRICS_DIR, f"{session_id}_session_metrics.json")
    
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                session_metrics.update(json.load(f))
            print(f"[INFO] Restored session_metrics from {json_path}")
            return
        except Exception as e:
            print(f"[WARN] Failed to load session_metrics from JSON: {e}")
    
    # fallback to rebuilding from DB
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT raw FROM logs WHERE session = ? ORDER BY id", (session_id,)).fetchall()
        for row in rows:
            parsed = parse_line(row[0])
            if parsed:
                update_metrics(parsed)


def save_session_metrics_to_file():
    try:
        # Save session-specific version
        if session_id:
            per_session_path = os.path.join(SESSION_METRICS_DIR, f"{session_id}_session_metrics.json")
            with open(per_session_path, "w") as f:
                json.dump(session_metrics, f)
    except Exception as e:
        print(f"[WARN] Failed to save session_metrics: {e}")

# --- API ROUTES ---
@app.route("/metrics")
def get_metrics():
    total_Wh = session_metrics["positive_Wh"] + session_metrics["regen_Wh"]
    net_Wh = session_metrics["positive_Wh"] - session_metrics["regen_Wh"] - session_metrics["solar_Wh"]
    distance = max(session_metrics["distance_km"], 0.001)

    ah_used = (latest_raw_values[0] if latest_raw_values else 0) + session_metrics.get("ah_offset", 0.0)
    voltage = latest_raw_values[1] if latest_raw_values else 0
    Wh_remaining = (64 - ah_used) * voltage

    net_Wh_per_km_list = session_metrics.get("net_Wh_per_km_last", [])
    net_Wh_last_km = net_Wh_per_km_list[-1] if net_Wh_per_km_list else 0
    net_Wh_10km_avg = sum(net_Wh_per_km_list[-10:]) / max(1, len(net_Wh_per_km_list[-10:]))

    session_avg_net_Wh_per_km = net_Wh / distance if distance > 0 else 0

    autonomy = {
        "range_session_avg": Wh_remaining / max(0.1, session_avg_net_Wh_per_km),
    }

    # Only include if at least 1 km history exists
    if len(net_Wh_per_km_list) >= 1:
        autonomy["range_last_km"] = Wh_remaining / max(0.1, net_Wh_last_km)

    # Only include if at least 10 km history exists
    if len(net_Wh_per_km_list) >= 10:
        autonomy["range_10km_avg"] = Wh_remaining / max(0.1, net_Wh_10km_avg)

    return jsonify({
        "raw_CA_values": latest_raw_values,
        "session_id": session_id,
        "user": current_user,
        "calculated_CA_values": {
            "speed_avg": session_metrics["speed_sum"] / max(1, session_metrics["speed_count"]),
            "speed_max": session_metrics["speed_max"],
            "power_live": latest_raw_values[1] * latest_raw_values[2] if latest_raw_values else 0,
            "power_avg": session_metrics["power_sum"] / max(1, session_metrics["speed_count"]),
            "power_max": session_metrics["power_max"] if session_metrics["power_max"] != float('-inf') else 0,
            "power_min": session_metrics["power_min"] if session_metrics["power_min"] != float('inf') else 0,
            "Wh_pos": session_metrics["positive_Wh"],
            "Wh_regen": session_metrics["regen_Wh"],
            "%_regen": session_metrics["regen_Wh"] / max(1e-6, total_Wh),
            "solar_power_live": latest_raw_values[1] * latest_raw_values[13] if latest_raw_values else 0,
            "solar_Wh": session_metrics["solar_Wh"],
            "calories_burned": session_metrics.get("calories_burned", 0),
            "solar_power_max": session_metrics["solar_power_max"],
            "solar_power_avg": session_metrics["solar_power_sum"] / max(1, session_metrics["solar_power_count"]),
            "%_solar": session_metrics["solar_Wh"] / max(1e-6, total_Wh),
            "net_Wh": net_Wh,
            "distance_km": session_metrics["distance_km"],
            "net_Wh_per_km": session_avg_net_Wh_per_km,
            "live_Wh_per_km": (
                (latest_raw_values[1] * latest_raw_values[2]) / max(0.1, latest_raw_values[3])
                if latest_raw_values and latest_raw_values[3] >= 1 else 0
            ),
            "live_net_Wh_per_km": (
                ((latest_raw_values[1] * latest_raw_values[2]) - (latest_raw_values[1] * latest_raw_values[13]))
                / max(0.1, latest_raw_values[3])
                if latest_raw_values and latest_raw_values[3] >= 1 else 0
            ),
            "regen_power_live": abs(latest_raw_values[1] * latest_raw_values[2]) if latest_raw_values and latest_raw_values[2] < 0 else 0,
            "Wh_per_km_last": session_metrics.get("Wh_per_km_last", []),
            "net_Wh_per_km_last": session_metrics.get("net_Wh_per_km_last", []),
            "solar_pct_per_km_last": session_metrics.get("solar_pct_per_km_last", []),
            "regen_pct_per_km_last": session_metrics.get("regen_pct_per_km_last", []),
            "temp_avg": session_metrics["temp_sum"] / max(1, session_metrics["temp_count"]),
            "temp_max": session_metrics["temp_max"],
            "autonomy": autonomy,
            "ah_offset": session_metrics.get("ah_offset", 0.0)
        }
    })

@app.route("/add_ah", methods=["POST"])
def add_ah():
    data = request.json
    try:
        extra_ah = float(data.get("added_ah", 0))
        session_metrics["ah_offset"] = session_metrics.get("ah_offset", 0.0) - extra_ah
        save_session_metrics_to_file()
        return jsonify({"status": "ok", "new_ah_offset": session_metrics["ah_offset"]})
        print(f"[INFO] Ah offset adjusted: +{extra_ah}, total offset now: {session_metrics['ah_offset']:.2f}")
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/reset", methods=["POST"])
def reset_session():
    global session_id, session_start_time
    session_id = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    save_session_id(session_id)
    session_start_time = time.time()
    reset_session_state()
    print("[INFO] Session reset.")
    return jsonify({"status": "Session reset", "session_id": session_id})


@app.route("/logs")
def get_logs():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 10").fetchall()
        logs = [{"id": row[0], "timestamp": row[1], "session": row[2], "raw": row[3], "user": row[4]} for row in rows]
        return jsonify(logs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/sessions")
def list_sessions():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("""
            SELECT session, SUM(LENGTH(raw)) as size_bytes
            FROM logs
            GROUP BY session
            ORDER BY session DESC
        """).fetchall()

    sessions = [
        {
            "session": row[0],
            "size_kb": round(row[1] / 1024, 2) if row[1] else 0
        }
        for row in rows
    ]
    return jsonify(sessions)

@app.route("/")
def root():
    return redirect("/dashboard" if session_active else "/start")
    
@app.route("/start")
def start_page():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC LIMIT 5").fetchall()
    recent_sessions = [row[0] for row in rows]
    return render_template("start.html", session_active=session_active, recent_sessions=recent_sessions)


@app.route("/start_session", methods=["POST"])
def start_session():
    global session_id, session_start_time, current_user, session_active

    selected_user = request.form.get("user", "JD").strip()
    current_user = selected_user if selected_user in ("JD", "LL") else "JD"

    session_id = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    save_session_id(session_id)
    save_current_user(current_user)
    session_start_time = time.time()

    reset_session_state()

    # ✅ mark session as active
    session_active = True
    save_session_active(True)

    print(f"[INFO] Started new session as {current_user}: {session_id}")
    return redirect("/dashboard")

@app.route("/resume_session", methods=["POST"])
def resume_session():
    global session_id, session_start_time, session_active

    session_id = request.form.get("session_id")
    if not session_id:
        return "Missing session ID", 400

    save_session_id(session_id)
    restore_session_metrics(session_id)
    session_start_time = time.time()
    session_active = True
    save_session_active(True)

    print(f"[INFO] Resumed session {session_id}")
    return redirect("/dashboard")



@app.route("/end_session", methods=["POST"])
def end_session():
    global session_active
    session_active = False
    save_session_active(False)
    return redirect(f"/summary?session={session_id}")

@app.route("/dashboard")
def dashboard():
    if not session_active:
        return redirect("/start")
    return render_template("index.html")  # keep existing index.html as dashboard

@app.route("/delete_session", methods=["POST"])
def delete_session():
    data = request.json
    session_to_delete = data.get("session")
    if not session_to_delete:
        return jsonify({"error": "No session specified"}), 400

    # Delete from DB
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM logs WHERE session = ?", (session_to_delete,))
        conn.commit()

    # Delete metrics JSON file if it exists
    json_path = os.path.join(SESSION_METRICS_DIR, f"{session_to_delete}_session_metrics.json")
    if os.path.exists(json_path):
        try:
            os.remove(json_path)
            print(f"[INFO] Deleted metrics file for session {session_to_delete}")
        except Exception as e:
            print(f"[WARN] Failed to delete metrics file for session {session_to_delete}: {e}")

    return jsonify({"status": f"Session {session_to_delete} deleted."})

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/select_session")
def select_session():
    with sqlite3.connect(DB_FILE) as conn:
        sessions = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC").fetchall()
    return render_template("select_session.html", sessions=[s[0] for s in sessions])


@app.route("/edit_session")
def edit_session_page():
    return render_template("edit_session.html")

@app.route("/api/session_rows")
def session_rows():
    session_id = request.args.get("session")
    if not session_id:
        return jsonify({"error": "Missing session ID"}), 400

    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, raw FROM logs WHERE session = ? ORDER BY id LIMIT 200",
            (session_id,)
        ).fetchall()

    return jsonify([
        {"id": r[0], "timestamp": r[1], "raw": r[2]} for r in rows
    ])

@app.route("/api/delete_row", methods=["POST"])
def delete_row():
    row_id = request.json.get("id")
    if row_id is None:
        return jsonify({"error": "Missing row ID"}), 400

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM logs WHERE id = ?", (row_id,))
        conn.commit()

    return jsonify({"status": "deleted", "id": row_id})


@app.route("/summary")
def summary():
    session_id = request.args.get("session")
    if not session_id:
        return "Missing session ID", 400

    import datetime
    from collections import defaultdict

    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT user, timestamp, raw FROM logs WHERE session = ? ORDER BY id",
            (session_id,)
        ).fetchall()

    def parse_line(line):
        try:
            parts = line.strip().split()
            if len(parts) != 15:
                return None
            return [float(x) for x in parts[:14]] + [parts[14]]
        except:
            return None

    user_data = defaultdict(list)
    timestamps = defaultdict(list)

    for user, ts, raw in rows:
        parsed = parse_line(raw)
        if not parsed or not user:
            continue
        user_data[user].append(parsed)
        timestamps[user].append(ts)

    def compute_metrics(data, ts_list):
        m = {
            "speed_sum": 0, "speed_max": 0, "speed_count": 0,
            "power_sum": 0, "power_max": float('-inf'), "power_min": float('inf'),
            "solar_power_sum": 0, "solar_power_max": 0, "solar_power_count": 0,
            "positive_Wh": 0, "regen_Wh": 0, "solar_Wh": 0,
            "temp_sum": 0, "temp_max": 0, "temp_count": 0,
            "distance_start": None, "distance_end": None,
            "Ah": 0  # Added for direct Ah tracking
        }

        last_ts = None
        for i, d in enumerate(data):
            try:
                ah = d[0]
                v = d[1]
                a = d[2]
                speed = d[3]
                dist = d[4]
                temp = d[5]
                solar_a = d[13]
                power = v * a
                solar_power = v * solar_a

                # Distance boundaries
                if m["distance_start"] is None:
                    m["distance_start"] = dist
                m["distance_end"] = dist

                # Time delta
                if i < len(ts_list):
                    current_ts = datetime.datetime.fromisoformat(ts_list[i])
                    if last_ts:
                        dt = (current_ts - last_ts).total_seconds()
                    else:
                        dt = 0.1  # fallback
                    last_ts = current_ts
                else:
                    dt = 0.1  # fallback if timestamps missing

                # Speed-based metrics
                if speed >= 1:
                    m["speed_sum"] += speed
                    m["speed_count"] += 1
                    m["speed_max"] = max(m["speed_max"], speed)

                    m["power_sum"] += power
                    m["power_max"] = max(m["power_max"], power)
                    m["power_min"] = min(m["power_min"], power)

                    m["solar_power_sum"] += solar_power
                    m["solar_power_count"] += 1
                    m["solar_power_max"] = max(m["solar_power_max"], solar_power)

                    m["temp_sum"] += temp
                    m["temp_count"] += 1
                    m["temp_max"] = max(m["temp_max"], temp)

                # Accumulate Ah from current
                m["Ah"] += a * dt / 3600

                # Energy tracking
                if a > 0:
                    m["positive_Wh"] += power * dt / 3600
                elif a < 0:
                    m["regen_Wh"] += abs(power) * dt / 3600

                m["solar_Wh"] += solar_power * dt / 3600

            except Exception as e:
                print(f"[WARN] compute_metrics failed on line {i}: {e}")

        # Duration
        if ts_list:
            try:
                t0 = datetime.datetime.fromisoformat(ts_list[0])
                t1 = datetime.datetime.fromisoformat(ts_list[-1])
                m["duration"] = (t1 - t0).total_seconds()
            except:
                m["duration"] = 0
        else:
            m["duration"] = 0

        # Distance fallback
        if m["distance_start"] is not None and m["distance_end"] is not None:
            m["distance"] = max(0.0, m["distance_end"] - m["distance_start"])
        else:
            m["distance"] = 0.0

        return m

    def compute_total_metrics(all_user_data, all_timestamps):
        all_points = sum(all_user_data.values(), [])
        all_ts = sum(all_timestamps.values(), [])
        m = compute_metrics(all_points, all_ts)
        distances = [p[4] for p in all_points if len(p) > 4]
        if distances:
            m["distance"] = max(distances) - min(distances)
        return m

    metrics_by_user = {
        user: compute_metrics(user_data[user], timestamps[user]) for user in user_data
    }
    metrics_by_user["Total"] = compute_total_metrics(user_data, timestamps)

    all_users = list(user_data.keys()) + ["Total"]

    def safe_div(n, d): return n / max(d, 1e-6)

    # --- Rows grouped by category ---
    grouped_rows = [
        ("Duration & distance", [
            ("Duration (min)", lambda m: m["duration"] / 60),
            ("Distance (km)", lambda m: m["distance"]),
        ]),
        ("Speed", [
            ("Avg Speed (km/h)", lambda m: safe_div(m["speed_sum"], m["speed_count"])),
            ("Max Speed (km/h)", lambda m: m["speed_max"]),
        ]),
        ("Power", [
            ("Avg Power (W)", lambda m: safe_div(m["power_sum"], m["speed_count"])),
            ("Max Power (W)", lambda m: m["power_max"]),
            ("Min Power (W)", lambda m: m["power_min"]),
        ]),
        ("Energy", [
            ("Battery Used (Ah)", lambda m: m["Ah"]),
            ("Regen Energy (Wh)", lambda m: m["regen_Wh"]),
            ("Solar Energy (Wh)", lambda m: m["solar_Wh"]),
            ("Net Energy (Wh)", lambda m: m["positive_Wh"] - m["regen_Wh"] - m["solar_Wh"]),
        ]),
        ("Efficiency", [
            ("Total Wh/km", lambda m: safe_div(m["positive_Wh"], m["distance"])),
            ("Net Wh/km", lambda m: safe_div(m["positive_Wh"] - m["regen_Wh"] - m["solar_Wh"], m["distance"])),
        ]),
        ("Percentages", [
            ("Regen %", lambda m: 100 * safe_div(m["regen_Wh"], m["positive_Wh"] + m["regen_Wh"])),
            ("Solar %", lambda m: 100 * safe_div(m["solar_Wh"], m["positive_Wh"] + m["regen_Wh"])),
        ]),
        ("Temperature", [
            ("Avg Temp (°C)", lambda m: safe_div(m["temp_sum"], m["temp_count"])),
            ("Max Temp (°C)", lambda m: m["temp_max"]),
        ]),
        ("Human effort", [
            ("Calories Burned (kcal)", lambda m: m["positive_Wh"] * 0.086),
        ])
    ]

    # Build table
    table = [["Metric"] + all_users]
    for category, metrics in grouped_rows:
        table.append([f"—— {category} ——"] + [""] * len(all_users))
        for label, func in metrics:
            row = [label]
            for u in all_users:
                value = func(metrics_by_user[u])
                unit = " min" if "Duration" in label else \
                    " km" if "Distance" in label else \
                    " km/h" if "Speed" in label else \
                    " W" if "Power" in label else \
                    " Ah" if "Battery" in label else \
                    " Wh/km" if "/km" in label else \
                    " Wh" if "Energy" in label or "Energy" in label else \
                    " %" if "%" in label else \
                    " °C" if "Temp" in label else \
                    " kcal" if "Calories" in label else ""
                row.append(f"{value:.2f}{unit}")
            table.append(row)

    return render_template("summary.html", session_id=session_id, table=table)

@app.route("/live_logs")
def live_logs_page():
    return render_template("live_logs.html")


@app.route("/set_test_mode", methods=["POST"])
def set_test_mode():
    global test_mode_flag, latest_raw_values
    try:
        data = request.get_json()
        enabled = bool(data.get("enabled", False))
        with test_mode_lock:
            test_mode_flag = enabled
        save_test_mode(enabled)

        if not enabled:
            # 🧹 Clear stale simulated values so UI knows connection is inactive
            latest_raw_values = None

        return jsonify({"test_mode": enabled})
    except Exception as e:
        print(f"[ERROR] Failed to set test mode: {e}")
        return jsonify({"error": str(e)}), 500



@app.route("/get_test_mode")
def get_test_mode():
    return jsonify({"test_mode": is_test_mode()})

# --- STARTUP ---
if __name__ == "__main__":
    init_db()
    restore_session_metrics(session_id)
    print(f"[INIT] Loaded current user: {current_user}")

        
    threading.Thread(target=read_serial, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
