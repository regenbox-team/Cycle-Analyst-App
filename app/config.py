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

# --- GPS Configuration ---
# Default USB GPS dongles like VK-162 often appear as /dev/ttyACM0.
# Override with env var APP_GPS_PORT if needed.
GPS_SERIAL_PORT_DEFAULT = os.getenv("APP_GPS_PORT", "/dev/ttyACM0")
GPS_BAUDRATE = int(os.getenv("APP_GPS_BAUDRATE", "9600"))

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
        "serial_port": "exec:python3 can_util/can_bridge.py --dbc can_util/Cockpit_CAN_Database_V1.4.dbc,can_util/Act2.5_database_can_A_V1.5.dbc live --channel can0",
        "test_mode": False,
    },
    "acticycle_test": {
        "serial_port": "exec:python3 can_util/can_bridge.py --dbc can_util/Cockpit_CAN_Database_V1.4.dbc,can_util/Act2.5_database_can_A_V1.5.dbc csv --csv can_util/can_log.csv",
        "test_mode": False,
    },
}

# --- DB per mode helpers ---
def db_filename_for_mode(mode: str) -> str:
    # Keep names simple and explicit per vehicle mode
    safe = mode.replace('/', '_')
    return os.path.join(BASE_DIR, f"ride_data_{safe}.db")


def get_db_file(mode: str | None = None) -> str:
    """Return DB path for the given mode, or current vehicle mode if None.
    Falls back to legacy DB_FILE if mode resolution fails.
    """
    try:
        if mode is None:
            # Lazy import to avoid cycles
            from app import modes as _m
            mode = getattr(_m, 'vehicle_mode', None)
        if mode:
            return db_filename_for_mode(mode)
    except Exception:
        pass
    return DB_FILE
