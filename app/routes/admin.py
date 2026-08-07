from __future__ import annotations
from flask import jsonify, request
import subprocess
from app import state


def switch_user():
    from app.user_profiles import active_profiles, get_profile

    profiles = active_profiles()
    if profiles:
        current_id = getattr(state, "current_user_id", None)
        current_index = next(
            (index for index, profile in enumerate(profiles) if profile["user_id"] == current_id),
            -1,
        )
        profile = profiles[(current_index + 1) % len(profiles)]
    else:
        next_initials = "LL" if state.current_user == "JD" else "JD"
        profile = get_profile(next_initials)

    if profile:
        state.current_user_profile = profile
        state.current_user_id = profile["user_id"]
        state.current_user = profile["initials"]
        state.save_current_user_id(state.current_user_id)
        state.session_metrics["user_id"] = state.current_user_id
        state.session_metrics["user_initials"] = state.current_user
    else:
        state.current_user = "LL" if state.current_user == "JD" else "JD"
    state.save_current_user(state.current_user)
    state.save_session_metrics_to_file()
    return jsonify({"user": state.current_user, "user_id": getattr(state, "current_user_id", None)})


def add_ah():
    data = request.json
    try:
        extra_ah = float(data.get("added_ah", 0))
        state.session_metrics["ah_offset"] = state.session_metrics.get("ah_offset", 0.0) - extra_ah
        state.save_session_metrics_to_file()
        return jsonify({"status": "ok", "new_ah_offset": state.session_metrics["ah_offset"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def reset_session():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.metrics import reset_session_state

    state.session_id = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d_%H-%M-%S")
    state.save_session_id(state.session_id)
    state.session_start_time = datetime.now().timestamp()
    reset_session_state()
    return jsonify({"status": "Session reset", "session_id": state.session_id})


def reset_battery_full():
    if not state.session_active:
        return jsonify({"error": "No active session"}), 409
    metrics = state.session_metrics
    net_wh = float(metrics.get("positive_Wh") or 0.0) - float(metrics.get("regen_Wh") or 0.0) - float(metrics.get("human_Wh") or 0.0) - float(metrics.get("solar_Wh") or 0.0)
    if float(metrics.get("distance_km") or 0.0) > 0.2 or abs(net_wh) > 20.0:
        return jsonify({"error": "Battery reset is only available at the beginning of a session"}), 409
    from app.solar_range import force_full_battery
    from app.config import VEHICLE_CONFIGS
    from app import modes
    capacity_ah = VEHICLE_CONFIGS.get(modes.vehicle_mode, {}).get("battery_capacity_ah", 64)
    force_full_battery(metrics, capacity_ah)
    state.save_session_metrics_to_file()
    return jsonify({"status": "ok", "percent": 100.0})


def restart_service():
    """Attempt to restart the systemd service. Requires sudoers rule.
    Example sudoers (no password) for user 'pi':
      pi ALL=NOPASSWD: /bin/systemctl restart cycle-analyst.service
    """
    try:
        # Keep timeout short to avoid hanging the request
        subprocess.run([
            'sudo', 'systemctl', 'restart', 'cycle-analyst.service'
        ], check=True, timeout=5)
        return jsonify({"status": "ok", "message": "Service restart requested"})
    except subprocess.TimeoutExpired:
        return jsonify({"status": "pending", "message": "Restart initiated (timeout waiting for confirmation)"}), 202
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("admin", __name__)
    bp.add_url_rule("/switch_user", methods=["POST"], view_func=switch_user)
    bp.add_url_rule("/add_ah", methods=["POST"], view_func=add_ah)
    bp.add_url_rule("/reset", methods=["POST"], view_func=reset_session)
    bp.add_url_rule("/battery/reset_full", methods=["POST"], view_func=reset_battery_full)
    bp.add_url_rule("/restart_service", methods=["POST"], view_func=restart_service)
    return bp


def register(app):
    app.add_url_rule("/switch_user", methods=["POST"], view_func=switch_user)
    app.add_url_rule("/add_ah", methods=["POST"], view_func=add_ah)
    app.add_url_rule("/reset", methods=["POST"], view_func=reset_session)
    app.add_url_rule("/battery/reset_full", methods=["POST"], view_func=reset_battery_full)
    app.add_url_rule("/restart_service", methods=["POST"], view_func=restart_service)
