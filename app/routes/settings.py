from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from flask import jsonify, redirect, render_template, request, send_file

from app.env_file import current_device_hint, env_file_path, grouped_settings, save_settings
from app.config import LIVE_PHOTO_DIR, get_db_file
from app import state


CA_RAW_LABELS = [
    "Ah used",
    "Voltage V",
    "Current A",
    "Speed km/h",
    "Distance km",
    "Motor temp C",
    "Cyclist RPM",
    "unused8",
    "unused9",
    "unused10",
    "unused11",
    "unused12",
    "Human Ah legacy",
    "Human current A",
    "Flags",
]
DIAGNOSTIC_CAMERA_IMAGE = Path(LIVE_PHOTO_DIR) / "settings_camera_test.jpg"


def settings_page():
    status = request.args.get("status", "")
    html = render_template(
        "settings.html",
        groups=grouped_settings(),
        env_path=str(env_file_path()),
        status=status,
        device_hint=current_device_hint(),
    )
    return html, {"Cache-Control": "no-store"}


def save_settings_page():
    save_settings(request.form)
    try:
        from app.monitor_client import start_monitor_sync

        start_monitor_sync()
    except Exception:
        pass
    return redirect("/settings?status=saved")


def _json_status(status: str, lines: list[str], **extra):
    payload = {"status": status, "lines": lines, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")}
    payload.update(extra)
    return jsonify(payload)


def diagnostics_solar_sensor():
    lines: list[str] = []
    live = dict(getattr(state, "solar_sensor", {}) or {})
    lines.append(f"Live state: enabled={live.get('enabled')} source={live.get('source')}")
    lines.append(
        "Live values: "
        f"{float(live.get('bus_v') or 0):.3f} V, "
        f"{float(live.get('current_a') or 0):.3f} A, "
        f"{float(live.get('power_w') or 0):.3f} W"
    )
    if live.get("raw_current_a") is not None:
        lines.append(
            "Live raw/filter: "
            f"raw={float(live.get('raw_current_a') or 0):.3f} A, "
            f"filtered={float(live.get('current_a') or 0):.3f} A"
        )

    try:
        from app.solar_sensor import INA228Sensor, sensor_enabled

        if not sensor_enabled():
            lines.append("APP_SOLAR_SENSOR is not set to ina228; direct I2C test skipped.")
            return _json_status("warning", lines, live=live)

        sensor = INA228Sensor()
        try:
            sample = sensor.read_debug_sample()
        finally:
            sensor.close()
        data = asdict(sample)
        lines.extend(
            [
                f"INA228 detected on bus {sample.bus_id}, address 0x{sample.address:02x}.",
                f"IDs: manufacturer=0x{sample.manufacturer_id:04x}, device=0x{sample.device_id:04x}.",
                f"Voltage={sample.bus_v:.3f} V, current={sample.current_a:.3f} A, power={sample.power_calc_w:.3f} W.",
                f"Shunt={sample.shunt_v:.6f} V, die temperature={sample.die_temp_c:.2f} C.",
                f"Calibration: shunt_cal={sample.shunt_cal}, expected={sample.expected_shunt_cal}.",
            ]
        )
        return _json_status("ok", lines, sample=data, live=live)
    except Exception as exc:
        lines.append(f"Direct INA228 test failed: {exc}")
        return _json_status("error", lines, live=live), 500


def diagnostics_motor_sensor():
    lines: list[str] = []
    live = dict(getattr(state, "motor_sensor", {}) or {})
    lines.append(f"Live state: enabled={live.get('enabled')} valid={live.get('valid')}")
    lines.append(
        f"Raw bus: {float(live.get('bus_v') or 0):.3f} V, "
        f"{float(live.get('current_a') or 0):.3f} A"
    )
    lines.append(
        f"Correction: solar={float(live.get('solar_correction_a') or 0):.3f} A, "
        f"generator={float(live.get('generator_correction_a') or 0):.3f} A"
    )
    lines.append(
        f"Pure motor: {float(live.get('corrected_current_a') or 0):.3f} A, "
        f"{float(live.get('corrected_power_w') or 0):.3f} W"
    )
    try:
        from app.solar_sensor import INA228Sensor, sensor_enabled
        if not sensor_enabled("APP_MOTOR"):
            lines.append("APP_MOTOR_SENSOR is disabled; direct I2C test skipped.")
            return _json_status("warning", lines, live=live)
        sensor = INA228Sensor("APP_MOTOR")
        try:
            sample = sensor.read_debug_sample()
        finally:
            sensor.close()
        lines.extend([
            f"INA228 detected on bus {sample.bus_id}, address 0x{sample.address:02x}.",
            f"Voltage={sample.bus_v:.3f} V, current={sample.current_a:.3f} A, power={sample.power_calc_w:.3f} W.",
            f"Shunt={sample.shunt_v:.6f} V, die temperature={sample.die_temp_c:.2f} C.",
        ])
        return _json_status("ok", lines, sample=asdict(sample), live=live)
    except Exception as exc:
        lines.append(f"Direct motor INA228 test failed: {exc}")
        return _json_status("error", lines, live=live), 500


def diagnostics_camera():
    lines: list[str] = []
    try:
        from app.photo_capture import _capture_image, _resolve_capture_command, _resolve_v4l2_control_command

        preview_command = _resolve_capture_command("__output__.jpg")
        control_command = _resolve_v4l2_control_command(preview_command)
        if control_command:
            lines.append(f"Pre-command: {' '.join(control_command)}")
        lines.append(f"Command: {' '.join(preview_command)}")
        temp_path = _capture_image()
        try:
            os.makedirs(LIVE_PHOTO_DIR, exist_ok=True)
            shutil.copyfile(temp_path, DIAGNOSTIC_CAMERA_IMAGE)
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass
        size_kb = DIAGNOSTIC_CAMERA_IMAGE.stat().st_size / 1024
        lines.append(f"Capture OK: {DIAGNOSTIC_CAMERA_IMAGE.name} ({size_kb:.1f} KB).")
        return _json_status(
            "ok",
            lines,
            image_url=f"/settings/diagnostics/camera_image?ts={int(time.time())}",
        )
    except Exception as exc:
        lines.append(f"Camera test failed: {exc}")
        return _json_status("error", lines), 500


def diagnostics_camera_image():
    if not DIAGNOSTIC_CAMERA_IMAGE.exists():
        return jsonify({"error": "no diagnostic image available"}), 404
    return send_file(str(DIAGNOSTIC_CAMERA_IMAGE), mimetype="image/jpeg")


def diagnostics_cycle_analyst():
    lines: list[str] = []
    raw = getattr(state, "latest_raw_values", None)
    if isinstance(raw, list) and len(raw) >= 15:
        lines.append("Live Cycle Analyst values:")
        for index, label in enumerate(CA_RAW_LABELS):
            lines.append(f"{index + 1:02d}. {label}: {raw[index]}")
    else:
        lines.append("No live Cycle Analyst frame currently available.")

    try:
        with sqlite3.connect(get_db_file()) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, session, raw
                FROM logs
                WHERE raw IS NOT NULL
                ORDER BY id DESC
                LIMIT 8
                """
            ).fetchall()
        if rows:
            lines.append("")
            lines.append("Last stored raw frames:")
            for timestamp, session, raw_line in rows:
                lines.append(f"{timestamp} | {session} | {raw_line}")
        else:
            lines.append("No stored raw frames in the current database.")
        return _json_status("ok", lines)
    except Exception as exc:
        lines.append(f"Database log read failed: {exc}")
        return _json_status("error", lines), 500


def diagnostics_monitor():
    lines: list[str] = []
    try:
        from app.modes import is_test_mode
        from app.monitor_client import _device_id, _mode_from_db_path, _monitor_url, _request_json
        from app.gps import get_status

        url = (_monitor_url() or "").rstrip("/")
        if not url:
            lines.append("MONITOR_URL is empty; monitor sync is disabled.")
            return _json_status("warning", lines)

        device_id = _device_id()
        gps = get_status()
        payload = {
            "device_id": device_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": state.session_id,
            "session_active": 1 if state.session_active else 0,
            "mode": _mode_from_db_path(get_db_file()),
            "test_mode": 1 if is_test_mode() else 0,
            "solar_enabled": 1 if bool(state.session_metrics.get("solar_enabled", state.solar_roof_enabled)) else 0,
            "user_id": getattr(state, "current_user_id", None),
            "user_initials": getattr(state, "current_user", None),
            "gps_available": 1 if gps.get("has_fix") and not gps.get("stale") else 0,
            "gps_lat": gps.get("lat"),
            "gps_lon": gps.get("lon"),
            "gps_timestamp_utc": gps.get("timestamp_utc"),
        }
        started = time.time()
        response = _request_json("POST", f"{url}/api/heartbeat", payload, timeout=8)
        try:
            from app.monitor_client import start_monitor_sync

            start_monitor_sync()
        except Exception:
            pass
        elapsed_ms = int((time.time() - started) * 1000)
        lines.append(f"Heartbeat OK to {url}/api/heartbeat in {elapsed_ms} ms.")
        lines.append(f"Device: {device_id}")
        if isinstance(response, dict) and response.get("last_seen"):
            lines.append(f"Monitor last_seen: {response.get('last_seen')}")
        if isinstance(response, dict) and response.get("active_window_sec"):
            lines.append(f"Monitor online window: {response.get('active_window_sec')} sec")
        lines.append(f"Response: {response}")
        return _json_status("ok", lines, response=response)
    except Exception as exc:
        lines.append(f"Monitor connection failed: {exc}")
        return _json_status("error", lines), 500


def solar_profile_status():
    from app.solar_range import imported_solar_profile_status

    return jsonify(imported_solar_profile_status())


def import_solar_profile():
    from app.solar_range import save_imported_solar_profile

    try:
        upload = request.files.get("profile")
        if upload:
            raw = upload.read().decode("utf-8")
            data = json.loads(raw)
        else:
            data = request.get_json(silent=True) or {}
        status = save_imported_solar_profile(data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    return jsonify({"status": "ok", "profile": status})


def delete_solar_profile():
    from app.solar_range import delete_imported_solar_profile, imported_solar_profile_status

    deleted = delete_imported_solar_profile()
    return jsonify({"status": "ok", "deleted": deleted, "profile": imported_solar_profile_status()})


def create_blueprint():
    from flask import Blueprint

    bp = Blueprint("settings", __name__)
    bp.add_url_rule("/settings", view_func=settings_page)
    bp.add_url_rule("/settings/save", methods=["POST"], view_func=save_settings_page)
    bp.add_url_rule("/settings/diagnostics/solar_sensor", view_func=diagnostics_solar_sensor)
    bp.add_url_rule("/settings/diagnostics/motor_sensor", view_func=diagnostics_motor_sensor)
    bp.add_url_rule("/settings/diagnostics/camera", methods=["POST"], view_func=diagnostics_camera)
    bp.add_url_rule("/settings/diagnostics/camera_image", view_func=diagnostics_camera_image)
    bp.add_url_rule("/settings/diagnostics/cycle_analyst", view_func=diagnostics_cycle_analyst)
    bp.add_url_rule("/settings/diagnostics/monitor", methods=["POST"], view_func=diagnostics_monitor)
    bp.add_url_rule("/settings/solar_profile", view_func=solar_profile_status)
    bp.add_url_rule("/settings/solar_profile/import", methods=["POST"], view_func=import_solar_profile)
    bp.add_url_rule("/settings/solar_profile", methods=["DELETE"], view_func=delete_solar_profile)
    return bp


def register(app):
    app.add_url_rule("/settings", view_func=settings_page)
    app.add_url_rule("/settings/save", methods=["POST"], view_func=save_settings_page)
    app.add_url_rule("/settings/diagnostics/solar_sensor", view_func=diagnostics_solar_sensor)
    app.add_url_rule("/settings/diagnostics/motor_sensor", view_func=diagnostics_motor_sensor)
    app.add_url_rule("/settings/diagnostics/camera", methods=["POST"], view_func=diagnostics_camera)
    app.add_url_rule("/settings/diagnostics/camera_image", view_func=diagnostics_camera_image)
    app.add_url_rule("/settings/diagnostics/cycle_analyst", view_func=diagnostics_cycle_analyst)
    app.add_url_rule("/settings/diagnostics/monitor", methods=["POST"], view_func=diagnostics_monitor)
    app.add_url_rule("/settings/solar_profile", view_func=solar_profile_status)
    app.add_url_rule("/settings/solar_profile/import", methods=["POST"], view_func=import_solar_profile)
    app.add_url_rule("/settings/solar_profile", methods=["DELETE"], view_func=delete_solar_profile)
