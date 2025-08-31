from __future__ import annotations
import os
from .config import (
    TEST_MODE_FILE, VEHICLE_MODE_FILE, VEHICLE_CONFIGS, test_mode_lock,
)

# runtime state
test_mode_flag = None  # initialized by load_test_mode()
vehicle_mode = None    # initialized by load_vehicle_mode()
SERIAL_PORT = None     # current effective source (port or exec bridge)


def load_test_mode() -> bool:
    if os.path.exists(TEST_MODE_FILE):
        try:
            with open(TEST_MODE_FILE, "r") as f:
                return f.read().strip().lower() == "true"
        except Exception:
            return False
    return False


def save_test_mode(enabled: bool) -> None:
    with open(TEST_MODE_FILE, "w") as f:
        f.write("true" if enabled else "false")


def is_test_mode() -> bool:
    with test_mode_lock:
        return bool(test_mode_flag)


def load_vehicle_mode() -> str:
    if os.path.exists(VEHICLE_MODE_FILE):
        try:
            with open(VEHICLE_MODE_FILE, "r") as f:
                mode = f.read().strip()
                if mode in VEHICLE_CONFIGS:
                    return mode
        except Exception:
            pass
    return "supercycle_live"


def save_vehicle_mode(mode: str) -> None:
    with open(VEHICLE_MODE_FILE, "w") as f:
        f.write(mode)


def apply_vehicle_mode(mode: str) -> None:
    global SERIAL_PORT, vehicle_mode, test_mode_flag
    vehicle_mode = mode if mode in VEHICLE_CONFIGS else "supercycle_live"
    cfg = VEHICLE_CONFIGS[vehicle_mode]
    SERIAL_PORT = cfg["serial_port"]
    with test_mode_lock:
        test_mode_flag = cfg.get("test_mode", False)
    save_test_mode(test_mode_flag)
    save_vehicle_mode(vehicle_mode)


# initialize on import
test_mode_flag = load_test_mode()
vehicle_mode = load_vehicle_mode()
apply_vehicle_mode(vehicle_mode)

