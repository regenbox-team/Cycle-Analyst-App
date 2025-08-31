from threading import Lock
import os

# --- Files & Paths ---
BASE_DIR = os.getenv("APP_VAR_DIR", "var")
os.makedirs(BASE_DIR, exist_ok=True)

TEST_MODE_FILE = os.path.join(BASE_DIR, "test_mode.txt")
VEHICLE_MODE_FILE = os.path.join(BASE_DIR, "vehicle_mode.txt")
SESSION_FILE = os.path.join(BASE_DIR, "current_session.txt")
SESSION_STATE_FILE = os.path.join(BASE_DIR, "session_state.txt")
SESSION_METRICS_DIR = os.path.join(BASE_DIR, "session_metrics")
DB_FILE = os.path.join(BASE_DIR, "ride_data.db")
USER_FILE = os.path.join(BASE_DIR, "current_user.txt")

# Ensure directories exist
os.makedirs(SESSION_METRICS_DIR, exist_ok=True)

# --- Serial/Bridge Configuration ---
SERIAL_PORT_DEFAULT = "/dev/ttyUSB0"
BAUDRATE = 9600

# --- Test/Vehicle modes ---
test_mode_lock = Lock()

VEHICLE_CONFIGS = {
    "supercycle_live": {
        "serial_port": "/dev/ttyUSB0",
        "test_mode": False,
    },
    "supercycle_test": {
        "serial_port": "/dev/ttyUSB0",
        "test_mode": True,
    },
    "acticycle_live": {
        "serial_port": "exec:python3 scripts/can_bridge.py --dbc Act2.5_database_can_A_V1.5.dbc live --channel can0",
        "test_mode": False,
    },
    "acticycle_test": {
        "serial_port": "exec:python3 scripts/can_bridge.py --dbc Act2.5_database_can_A_V1.5.dbc csv --csv can_log.csv",
        "test_mode": False,
    },
}
