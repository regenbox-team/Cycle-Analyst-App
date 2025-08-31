from __future__ import annotations
from flask import jsonify, request
from app.modes import apply_vehicle_mode, VEHICLE_CONFIGS, is_test_mode, test_mode_lock, save_test_mode
from app import modes, state
from app.reader import read_serial
from app.db import init_db


def set_vehicle_mode():
    mode = request.json.get("mode")
    if mode not in VEHICLE_CONFIGS:
        return jsonify({"error": "invalid mode"}), 400
    apply_vehicle_mode(mode)
    try:
        init_db(mode)
    except Exception:
        pass
    # If switching to a non-test mode, clear any simulated values immediately
    try:
        cfg = VEHICLE_CONFIGS.get(mode, {})
        if not cfg.get("test_mode", False):
            state.latest_raw_values = None
    except Exception:
        pass
    return jsonify({"mode": mode})


def get_vehicle_mode():
    return jsonify({"mode": modes.vehicle_mode})


def set_test_mode():
    try:
        data = request.get_json()
        enabled = bool(data.get("enabled", False))
        with test_mode_lock:
            modes.test_mode_flag = enabled
        save_test_mode(enabled)
        if enabled:
            # Ensure reader thread is running to publish fake data
            if not getattr(state, 'reader_started', False):
                import threading
                threading.Thread(target=read_serial, daemon=True).start()
                state.reader_started = True
        else:
            state.latest_raw_values = None
        return jsonify({"test_mode": enabled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_test_mode():
    return jsonify({"test_mode": is_test_mode()})


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("modes", __name__)
    bp.add_url_rule("/set_vehicle_mode", methods=["POST"], view_func=set_vehicle_mode)
    bp.add_url_rule("/get_vehicle_mode", view_func=get_vehicle_mode)
    bp.add_url_rule("/set_test_mode", methods=["POST"], view_func=set_test_mode)
    bp.add_url_rule("/get_test_mode", view_func=get_test_mode)
    return bp


def register(app):
    app.add_url_rule("/set_vehicle_mode", methods=["POST"], view_func=set_vehicle_mode)
    app.add_url_rule("/get_vehicle_mode", view_func=get_vehicle_mode)
    app.add_url_rule("/set_test_mode", methods=["POST"], view_func=set_test_mode)
    app.add_url_rule("/get_test_mode", view_func=get_test_mode)
