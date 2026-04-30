from __future__ import annotations

import mimetypes
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import state
from .config import LIVE_PHOTO_DIR
from .gps import get_status
from .monitor_client import monitor_upload_photo


_capture_lock = threading.Lock()


def _photo_config() -> dict:
    cfg = state.session_metrics.get("photo_capture")
    if not isinstance(cfg, dict):
        from .state import default_photo_capture_settings

        cfg = default_photo_capture_settings()
        state.session_metrics["photo_capture"] = cfg
    return cfg


def normalize_interval_km(raw_value, default: float = 1.0) -> float:
    try:
        value = float(raw_value)
    except Exception:
        return default
    return min(1000.0, max(0.1, value))


def configure_session_photo_capture(enabled: bool, interval_km) -> None:
    cfg = _photo_config()
    cfg.update(
        {
            "enabled": bool(enabled),
            "interval_km": normalize_interval_km(interval_km),
            "last_trigger_distance_km": 0.0,
            "capture_count": 0,
            "last_captured_at": None,
            "last_uploaded_at": None,
            "latest_local_path": None,
            "latest_public_url": None,
            "last_error": None,
        }
    )


def next_capture_distance(distance_km: float, last_trigger_distance_km: float, interval_km: float) -> float | None:
    interval = normalize_interval_km(interval_km)
    if distance_km + 1e-9 < last_trigger_distance_km + interval:
        return None
    crossed_steps = int(distance_km / interval)
    candidate = round(crossed_steps * interval, 3)
    if candidate <= last_trigger_distance_km + 1e-9:
        return None
    return candidate


def maybe_schedule_photo_capture(distance_km: float | None = None) -> bool:
    if not state.session_active or not state.session_id:
        return False

    cfg = _photo_config()
    if not cfg.get("enabled"):
        return False

    if distance_km is None:
        try:
            distance_km = float(state.session_metrics.get("distance_km") or 0.0)
        except Exception:
            distance_km = 0.0

    trigger_distance = next_capture_distance(
        float(distance_km or 0.0),
        float(cfg.get("last_trigger_distance_km") or 0.0),
        float(cfg.get("interval_km") or 1.0),
    )
    if trigger_distance is None or _capture_lock.locked():
        return False

    cfg["last_trigger_distance_km"] = trigger_distance
    state.save_session_metrics_to_file()

    thread = threading.Thread(
        target=_capture_and_upload,
        args=(trigger_distance, float(cfg.get("interval_km") or 1.0)),
        daemon=True,
    )
    thread.start()
    return True


def _capture_and_upload(trigger_distance_km: float, interval_km: float) -> None:
    cfg = _photo_config()
    with _capture_lock:
        temp_path = None
        try:
            gps_snapshot = get_status()
            metrics_snapshot = dict(state.session_metrics)
            raw_values_snapshot = list(state.latest_raw_values) if isinstance(state.latest_raw_values, list) else None
            solar_snapshot = dict(state.solar_sensor)
            temp_path = _capture_image()
            captured_at = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S")
            cfg["last_captured_at"] = captured_at
            cfg["latest_local_path"] = _persist_local_preview(temp_path)
            mime_type = mimetypes.guess_type(temp_path)[0] or "image/jpeg"
            with open(temp_path, "rb") as f:
                image_bytes = f.read()
            response = monitor_upload_photo(
                image_bytes=image_bytes,
                filename=os.path.basename(temp_path),
                mime_type=mime_type,
                captured_at=captured_at,
                distance_km=trigger_distance_km,
                interval_km=interval_km,
                gps_snapshot=gps_snapshot,
                metrics_snapshot=metrics_snapshot,
                raw_values_snapshot=raw_values_snapshot,
                solar_snapshot=solar_snapshot,
            )
            cfg["capture_count"] = int(cfg.get("capture_count") or 0) + 1
            cfg["last_uploaded_at"] = captured_at
            cfg["latest_public_url"] = response.get("public_latest_image_url") or response.get("image_url")
            cfg["last_error"] = None
        except Exception as exc:
            cfg["last_error"] = str(exc)
        finally:
            state.save_session_metrics_to_file()
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


def latest_local_photo_path() -> str | None:
    cfg = _photo_config()
    path = cfg.get("latest_local_path")
    if not path:
        return None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


def _persist_local_preview(source_path: str) -> str:
    session_name = state.session_id or "current"
    safe_name = session_name.replace("/", "_")
    output_path = os.path.join(LIVE_PHOTO_DIR, f"{safe_name}.jpg")
    shutil.copyfile(source_path, output_path)
    return output_path


def _capture_image() -> str:
    fd, output_path = tempfile.mkstemp(prefix="cycle-photo-", suffix=".jpg")
    os.close(fd)
    command = _resolve_capture_command(output_path)
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except FileNotFoundError as exc:
        try:
            os.remove(output_path)
        except Exception:
            pass
        cmd_name = command[0] if command else "unknown"
        raise RuntimeError(f"camera command not found: {cmd_name}") from exc
    except Exception:
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("camera command produced no image")
    return output_path


def _resolve_capture_command(output_path: str) -> list[str]:
    custom = os.getenv("APP_CAMERA_COMMAND", "").strip()
    if custom:
        rendered = custom.format(output=output_path)
        if "{output}" not in custom:
            rendered = f"{rendered} {shlex.quote(output_path)}"
        command = shlex.split(rendered)
        if not command:
            raise RuntimeError("APP_CAMERA_COMMAND is empty after parsing")
        _validate_capture_command(command, source="APP_CAMERA_COMMAND")
        return command

    libcamera = shutil.which("libcamera-still")
    if libcamera:
        return [
            libcamera,
            "-n",
            "-o",
            output_path,
            "--width",
            "1280",
            "--height",
            "720",
            "--quality",
            "85",
        ]

    fswebcam = shutil.which("fswebcam")
    if fswebcam:
        return [
            fswebcam,
            "-q",
            "-r",
            "1280x720",
            "--jpeg",
            "85",
            "--no-banner",
            output_path,
        ]

    raise RuntimeError("no supported camera command found; set APP_CAMERA_COMMAND")


def _validate_capture_command(command: list[str], source: str) -> None:
    executable = command[0]
    if os.path.sep in executable:
        if not os.path.exists(executable):
            raise RuntimeError(f"{source} points to missing executable: {executable}")
        return
    if not shutil.which(executable):
        raise RuntimeError(f"{source} points to missing executable: {executable}")
