from threading import Lock
import os

from app.env_file import load_env_file


load_env_file()

# --- Files & Paths ---
BASE_DIR = os.getenv("APP_VAR_DIR", "var")
os.makedirs(BASE_DIR, exist_ok=True)
LIVE_PHOTO_DIR = os.path.join(BASE_DIR, "live_photo")
PENDING_PHOTO_DIR = os.path.join(BASE_DIR, "pending_photos")

TEST_MODE_FILE = os.path.join(BASE_DIR, "test_mode.txt")
VEHICLE_MODE_FILE = os.path.join(BASE_DIR, "vehicle_mode.txt")
SOLAR_ROOF_FILE = os.path.join(BASE_DIR, "solar_roof.txt")
SOLAR_BATTERY_STATE_FILE = os.path.join(BASE_DIR, "solar_battery_state.json")
SESSION_FILE = os.path.join(BASE_DIR, "current_session.txt")
SESSION_STATE_FILE = os.path.join(BASE_DIR, "session_state.txt")
SESSION_METRICS_DIR = os.path.join(BASE_DIR, "session_metrics")
DB_FILE = os.path.join(BASE_DIR, "ride_data.db")
USER_FILE = os.path.join(BASE_DIR, "current_user.txt")
CURRENT_USER_ID_FILE = os.path.join(BASE_DIR, "current_user_id.txt")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
SCORES_DB_FILE = os.path.join(BASE_DIR, "game_scores.db")

# GPX route file path (persist during session, cleared on end)
GPX_ROUTE_FILE = os.path.join(BASE_DIR, "route.gpx")

# Ensure directories exist
os.makedirs(SESSION_METRICS_DIR, exist_ok=True)
os.makedirs(LIVE_PHOTO_DIR, exist_ok=True)
os.makedirs(PENDING_PHOTO_DIR, exist_ok=True)

# --- Serial/Bridge Configuration ---
SERIAL_PORT_DEFAULT = "/dev/ttyUSB0"
BAUDRATE = 9600

# --- GPS Configuration ---
# Default USB GPS dongles like VK-162 often appear as /dev/ttyACM0.
# Override with env var APP_GPS_PORT if needed.
GPS_SERIAL_PORT_DEFAULT = os.getenv("APP_GPS_PORT", "/dev/ttyACM0")
GPS_BAUDRATE = int(os.getenv("APP_GPS_BAUDRATE", "9600"))

# --- PMTiles (offline basemap) ---
# Path to a .pmtiles file on disk, e.g., western-europe.pmtiles under /home/pi/Documents
PMTILES_PATH = os.getenv("APP_PMTILES_PATH", "/home/jeandard/Documents/western-europe.pmtiles")

# --- Test/Vehicle modes ---
test_mode_lock = Lock()

VEHICLE_CONFIGS = {
    "supercycle_live": {
        "serial_port": "/dev/ttyUSB0",
        "test_mode": False,
        "battery_capacity_ah": 64,
        "battery_nominal_voltage": 48.1,
    },
    "supercycle_test": {
        "serial_port": "/dev/ttyUSB0",
        "test_mode": True,
        "battery_capacity_ah": 64,
        "battery_nominal_voltage": 48.1,
    },
}

# --- Solar range estimation ---
# These defaults are intentionally conservative and can be overridden on the Pi.
SOLAR_PANEL_MAX_W = float(os.getenv("APP_SOLAR_PANEL_MAX_W", "690"))
SOLAR_LOCATION_LAT = float(os.getenv("APP_SOLAR_LAT", "48.8566"))
SOLAR_LOCATION_LON = float(os.getenv("APP_SOLAR_LON", "2.3522"))
SOLAR_BATTERY_CURVE_FILE = os.getenv("APP_BATTERY_DISCHARGE_CURVE_FILE", "")

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

    # --- Scores DB helper ---
def get_scores_db_file() -> str:
    return SCORES_DB_FILE
