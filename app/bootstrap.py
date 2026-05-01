import os
import shutil
from .config import (
    BASE_DIR, TEST_MODE_FILE, VEHICLE_MODE_FILE, SESSION_FILE, SESSION_STATE_FILE,
    SESSION_METRICS_DIR, DB_FILE, USER_FILE, SOLAR_ROOF_FILE, USERS_FILE, CURRENT_USER_ID_FILE
)

LEGACY_FILES = {
    "test_mode.txt": TEST_MODE_FILE,
    "vehicle_mode.txt": VEHICLE_MODE_FILE,
    "solar_roof.txt": SOLAR_ROOF_FILE,
    "current_session.txt": SESSION_FILE,
    "session_state.txt": SESSION_STATE_FILE,
    "current_user.txt": USER_FILE,
    "current_user_id.txt": CURRENT_USER_ID_FILE,
    "users.json": USERS_FILE,
    "ride_data.db": DB_FILE,
}


def migrate_legacy_files():
    os.makedirs(BASE_DIR, exist_ok=True)
    os.makedirs(SESSION_METRICS_DIR, exist_ok=True)

    # Move top-level files if destination doesn't already exist
    for legacy_name, new_path in LEGACY_FILES.items():
        if os.path.exists(new_path):
            continue
        if os.path.exists(legacy_name):
            try:
                shutil.move(legacy_name, new_path)
            except Exception:
                try:
                    shutil.copy2(legacy_name, new_path)
                except Exception:
                    pass

    # Move session_metrics dir contents
    legacy_metrics_dir = "session_metrics"
    if os.path.isdir(legacy_metrics_dir):
        try:
            for name in os.listdir(legacy_metrics_dir):
                src = os.path.join(legacy_metrics_dir, name)
                dst = os.path.join(SESSION_METRICS_DIR, name)
                if not os.path.exists(dst):
                    try:
                        shutil.move(src, dst)
                    except Exception:
                        try:
                            shutil.copy2(src, dst)
                        except Exception:
                            pass
        except Exception:
            pass

    # After files are in place, refresh modes state from migrated files
    try:
        from . import modes
        modes.test_mode_flag = modes.load_test_mode()
        modes.apply_vehicle_mode(modes.load_vehicle_mode())
    except Exception:
        pass

