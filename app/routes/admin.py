from __future__ import annotations
from flask import jsonify, request
import subprocess
from app import state


def switch_user():
    state.current_user = "LL" if state.current_user == "JD" else "JD"
    state.save_current_user(state.current_user)
    return jsonify({"user": state.current_user})


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
    bp.add_url_rule("/restart_service", methods=["POST"], view_func=restart_service)
    return bp


def register(app):
    app.add_url_rule("/switch_user", methods=["POST"], view_func=switch_user)
    app.add_url_rule("/add_ah", methods=["POST"], view_func=add_ah)
    app.add_url_rule("/reset", methods=["POST"], view_func=reset_session)
    app.add_url_rule("/restart_service", methods=["POST"], view_func=restart_service)
