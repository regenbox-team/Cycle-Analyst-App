from __future__ import annotations

import os
import signal
import time

os.environ["CYCLE_ANALYST_SKIP_AUTO_APP"] = "1"

from app import state
from app.photo_capture import flush_pending_photo_uploads, maybe_schedule_photo_capture, pending_photo_count
from cycle_server import initialize_runtime


_running = True


def _stop(_signum, _frame) -> None:
    global _running
    _running = False


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _sync_state_from_recorder() -> None:
    state.session_id = state.load_session_id()
    state.session_active = state.load_session_active()
    state.current_user = state.load_current_user()
    state.current_user_id = state.load_current_user_id()
    state.solar_roof_enabled = state.load_solar_roof_enabled()
    state.load_session_metrics_from_file(state.session_id)
    try:
        from app.user_profiles import get_profile

        state.current_user_profile = get_profile(state.current_user_id or state.current_user)
    except Exception:
        pass


def main() -> int:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    initialize_runtime(start_reader=False, start_gps=False, start_monitor=False)
    print("[INIT] Cycle Analyst photo worker started", flush=True)

    poll_seconds = _env_float("APP_PHOTO_WORKER_INTERVAL_SECONDS", 1.0, 0.2)
    upload_retry_seconds = _env_float("APP_PHOTO_UPLOAD_RETRY_SECONDS", 15.0, 1.0)
    upload_limit = int(_env_float("APP_PHOTO_UPLOAD_RETRY_LIMIT", 3.0, 1.0))
    next_upload_retry = 0.0

    while _running:
        try:
            _sync_state_from_recorder()
            if state.session_active:
                maybe_schedule_photo_capture(state.session_metrics.get("distance_km"))

            now = time.time()
            if now >= next_upload_retry and pending_photo_count() > 0:
                flush_pending_photo_uploads(limit=upload_limit)
                next_upload_retry = now + upload_retry_seconds
        except Exception as exc:
            print(f"[WARN] photo worker loop failed: {exc}", flush=True)
            time.sleep(max(1.0, poll_seconds))
            continue

        time.sleep(poll_seconds)

    print("[STOP] Cycle Analyst photo worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
