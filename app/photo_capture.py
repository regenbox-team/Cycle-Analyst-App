from __future__ import annotations

import base64
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import state
from .config import LIVE_PHOTO_DIR, PENDING_PHOTO_DIR
from .gps import get_status
from .monitor_client import build_photo_upload_payload, upload_photo_payload


_capture_lock = threading.Lock()
_pending_upload_lock = threading.Lock()


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
            "pending_upload_count": pending_photo_count(),
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
            payload = build_photo_upload_payload(
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
            _queue_photo(temp_path, payload)
            cfg["capture_count"] = int(cfg.get("capture_count") or 0) + 1
            result = flush_pending_photo_uploads()
            if result.get("sent"):
                cfg["last_uploaded_at"] = result.get("latest_uploaded_at") or captured_at
                if result.get("latest_public_url"):
                    cfg["latest_public_url"] = result.get("latest_public_url")
            cfg["pending_upload_count"] = result.get("remaining", pending_photo_count())
            cfg["last_error"] = result.get("last_error")
        except Exception as exc:
            cfg["last_error"] = str(exc)
            cfg["pending_upload_count"] = pending_photo_count()
        finally:
            state.save_session_metrics_to_file()
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


def pending_photo_count() -> int:
    return len(_pending_photo_entries())


def flush_pending_photo_uploads(limit: int | None = None) -> dict:
    with _pending_upload_lock:
        sent = 0
        latest_uploaded_at = None
        latest_public_url = None
        last_error = None
        entries = _pending_photo_entries()
        if limit is not None:
            entries = entries[: max(0, limit)]

        for entry in entries:
            try:
                payload = _pending_payload(entry)
                response = upload_photo_payload(payload)
                _remove_pending_entry(entry)
                sent += 1
                latest_uploaded_at = payload.get("captured_at") or datetime.now(ZoneInfo("Europe/Paris")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                latest_public_url = (
                    response.get("public_latest_image_url") or response.get("image_url") or latest_public_url
                )
            except Exception as exc:
                last_error = str(exc)
                break

        remaining = pending_photo_count()
        cfg = _photo_config()
        cfg["pending_upload_count"] = remaining
        if sent:
            cfg["last_uploaded_at"] = latest_uploaded_at
            if latest_public_url:
                cfg["latest_public_url"] = latest_public_url
        cfg["last_error"] = last_error
        state.save_session_metrics_to_file()
        return {
            "sent": sent,
            "remaining": remaining,
            "latest_uploaded_at": latest_uploaded_at,
            "latest_public_url": latest_public_url,
            "last_error": last_error,
        }


def latest_local_photo_path() -> str | None:
    cfg = _photo_config()
    path = cfg.get("latest_local_path")
    if not path:
        return None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return None


def _queue_photo(source_path: str, payload: dict) -> dict:
    os.makedirs(PENDING_PHOTO_DIR, exist_ok=True)
    ext = os.path.splitext(str(payload.get("filename") or ""))[1].lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        ext = ".jpg"
    entry_id = _pending_entry_id(payload.get("captured_at"))
    image_name = f"{entry_id}{ext}"
    image_path = os.path.join(PENDING_PHOTO_DIR, image_name)
    meta_path = os.path.join(PENDING_PHOTO_DIR, f"{entry_id}.json")
    shutil.copyfile(source_path, image_path)

    payload_meta = dict(payload)
    payload_meta.pop("image_b64", None)
    metadata = {
        "queued_at": datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S"),
        "image_file": image_name,
        "payload": payload_meta,
    }
    tmp_meta_path = f"{meta_path}.tmp"
    with open(tmp_meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False)
    os.replace(tmp_meta_path, meta_path)
    return {"meta_path": meta_path, "image_path": image_path}


def _pending_entry_id(captured_at) -> str:
    raw_ts = str(captured_at or datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d %H:%M:%S"))
    safe_ts = "".join(ch if ch.isalnum() else "-" for ch in raw_ts).strip("-")
    return f"{safe_ts}-{uuid.uuid4().hex[:8]}"


def _pending_photo_entries() -> list[dict]:
    try:
        names = sorted(name for name in os.listdir(PENDING_PHOTO_DIR) if name.endswith(".json"))
    except Exception:
        return []

    entries = []
    for name in names:
        meta_path = os.path.join(PENDING_PHOTO_DIR, name)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            image_file = metadata.get("image_file")
            if not image_file:
                continue
            entries.append(
                {
                    "meta_path": meta_path,
                    "image_path": os.path.join(PENDING_PHOTO_DIR, os.path.basename(image_file)),
                    "metadata": metadata,
                }
            )
        except Exception:
            continue
    return entries


def _pending_payload(entry: dict) -> dict:
    image_path = entry["image_path"]
    metadata = entry["metadata"]
    payload = dict(metadata.get("payload") or {})
    with open(image_path, "rb") as f:
        payload["image_b64"] = base64.b64encode(f.read()).decode("ascii")
    return payload


def _remove_pending_entry(entry: dict) -> None:
    for path in (entry.get("meta_path"), entry.get("image_path")):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


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
        control_command = _resolve_v4l2_control_command(command)
        if control_command:
            subprocess.run(
                control_command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
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


def _camera_device_from_command(command: list[str]) -> str:
    for flag in ("-d", "--device"):
        try:
            index = command.index(flag)
        except ValueError:
            continue
        if index + 1 < len(command):
            return command[index + 1]
    return "/dev/video0"


def _parse_v4l2_controls(raw_value: str) -> list[str]:
    normalized = str(raw_value or "").replace(",", " ")
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = normalized.split()
    controls: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token.startswith("--set-ctrl="):
            token = token.split("=", 1)[1]
        if "=" not in token:
            raise RuntimeError(f"invalid APP_CAMERA_V4L2_CTRLS token: {token}")
        controls.append(token)
    return controls


def _resolve_v4l2_control_command(capture_command: list[str]) -> list[str]:
    controls = _parse_v4l2_controls(os.getenv("APP_CAMERA_V4L2_CTRLS", ""))
    if not controls:
        return []
    v4l2_ctl = shutil.which("v4l2-ctl")
    if not v4l2_ctl:
        raise RuntimeError("APP_CAMERA_V4L2_CTRLS is set but v4l2-ctl is not installed")
    command = [v4l2_ctl, "-d", _camera_device_from_command(capture_command)]
    command.extend(f"--set-ctrl={control}" for control in controls)
    return command


def _validate_capture_command(command: list[str], source: str) -> None:
    executable = command[0]
    if os.path.sep in executable:
        if not os.path.exists(executable):
            raise RuntimeError(f"{source} points to missing executable: {executable}")
        return
    if not shutil.which(executable):
        raise RuntimeError(f"{source} points to missing executable: {executable}")
