from __future__ import annotations

import math
from datetime import datetime
from typing import Any


CA_RESET_DROP_KM = 0.1
MAX_CA_DISTANCE_KPH = 250.0
DISTANCE_JUMP_FLOOR_KM = 1.0
DISTANCE_JUMP_MARGIN_KM = 0.25


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if not math.isfinite(number):
        return None
    return number


def _timestamp_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return _safe_float(value)


def _max_plausible_delta_km(previous_time: Any, current_time: Any) -> float:
    previous = _timestamp_seconds(previous_time)
    current = _timestamp_seconds(current_time)
    if previous is None or current is None:
        return DISTANCE_JUMP_FLOOR_KM
    elapsed = max(0.0, current - previous)
    speed_limited = (MAX_CA_DISTANCE_KPH * elapsed / 3600.0) + DISTANCE_JUMP_MARGIN_KM
    return max(DISTANCE_JUMP_FLOOR_KM, speed_limited)


def _clear_pending_reset(store: dict[str, Any]) -> None:
    store.pop("pending_distance_reset_raw", None)
    store.pop("pending_distance_reset_offset", None)
    store.pop("pending_distance_reset_time", None)


def _mark_reset(store: dict[str, Any], reset_count_key: str | None) -> None:
    if reset_count_key:
        store[reset_count_key] = int(store.get(reset_count_key) or 0) + 1
    else:
        store["ca_reset_detected"] = True


def update_ca_distance(
    store: dict[str, Any],
    raw_distance: Any,
    *,
    now: Any = None,
    distance_key: str = "distance_km",
    reset_count_key: str | None = None,
) -> tuple[float, float, int]:
    """Update CA trip distance while rejecting isolated glitches.

    The Cycle Analyst distance is cumulative. A real reset is a sustained drop to
    a lower scale, while field glitches can be a single low value or a single
    very high value. This tracker waits one sample before accepting a reset and
    ignores implausible forward jumps.
    """
    distance = _safe_float(raw_distance)
    previous_display = max(0.0, _safe_float(store.get(distance_key)) or 0.0)
    if distance is None:
        return previous_display, previous_display, 0

    if store.get("distance_start") is None:
        store["distance_start"] = distance

    last_raw = _safe_float(store.get("last_raw_distance"))
    if last_raw is None:
        offset = _safe_float(store.get("distance_offset")) or 0.0
        start_value = _safe_float(store.get("distance_start"))
        start = distance if start_value is None else start_value
        current_display = max(0.0, distance - start + offset)
        store["last_raw_distance"] = distance
        store["last_distance_update_time"] = _timestamp_seconds(now)
        store[distance_key] = current_display
        return previous_display, current_display, int(current_display) - int(previous_display)

    pending_raw = _safe_float(store.get("pending_distance_reset_raw"))
    if pending_raw is not None:
        pending_time = store.get("pending_distance_reset_time")
        pending_offset = _safe_float(store.get("pending_distance_reset_offset")) or previous_display
        candidate_delta = distance - pending_raw
        if (
            distance < last_raw - CA_RESET_DROP_KM
            and candidate_delta >= -CA_RESET_DROP_KM
            and candidate_delta <= _max_plausible_delta_km(pending_time, now)
        ):
            store["distance_offset"] = pending_offset
            store["distance_start"] = pending_raw
            _clear_pending_reset(store)
            _mark_reset(store, reset_count_key)
            current_display = max(0.0, distance - pending_raw + pending_offset)
            store["last_raw_distance"] = distance
            store["last_distance_update_time"] = _timestamp_seconds(now)
            store[distance_key] = current_display
            return previous_display, current_display, int(current_display) - int(previous_display)
        _clear_pending_reset(store)

    if distance < last_raw - CA_RESET_DROP_KM:
        store["pending_distance_reset_raw"] = distance
        store["pending_distance_reset_offset"] = previous_display
        store["pending_distance_reset_time"] = _timestamp_seconds(now)
        store["distance_reset_pending_count"] = int(store.get("distance_reset_pending_count") or 0) + 1
        return previous_display, previous_display, 0

    delta = distance - last_raw
    if delta > _max_plausible_delta_km(store.get("last_distance_update_time"), now):
        store["distance_glitch_count"] = int(store.get("distance_glitch_count") or 0) + 1
        store["last_rejected_distance"] = distance
        store["last_rejected_distance_time"] = _timestamp_seconds(now)
        return previous_display, previous_display, 0

    offset = _safe_float(store.get("distance_offset")) or 0.0
    start_value = _safe_float(store.get("distance_start"))
    start = distance if start_value is None else start_value
    current_display = max(0.0, distance - start + offset)
    if current_display < previous_display and previous_display - current_display < CA_RESET_DROP_KM:
        current_display = previous_display

    store["last_raw_distance"] = distance
    store["last_distance_update_time"] = _timestamp_seconds(now)
    store[distance_key] = current_display
    return previous_display, current_display, int(current_display) - int(previous_display)
