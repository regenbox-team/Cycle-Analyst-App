from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class SolarFilterConfig:
    enabled: bool = True
    tau_seconds: float = 4.0
    fast_tau_seconds: float = 0.8
    median_window: int = 5
    jump_threshold_a: float = 2.5
    output_deadband_a: float = 0.10


class AdaptiveSolarFilter:
    """Small online smoother for noisy solar current readings."""

    def __init__(self, config: SolarFilterConfig | None = None) -> None:
        self.config = config or SolarFilterConfig()
        self._current_a: float | None = None
        self._raw_window: deque[float] = deque(maxlen=max(1, self.config.median_window))

    def reset(self) -> None:
        self._current_a = None
        self._raw_window.clear()

    def update_current(self, raw_current_a: float, dt_seconds: float | None = None) -> float:
        try:
            raw = float(raw_current_a)
        except Exception:
            raw = 0.0

        if not self.config.enabled:
            return self._apply_output_deadband(raw)

        self._raw_window.append(raw)
        candidate = median(self._raw_window)

        if self._current_a is None:
            self._current_a = candidate
            return self._apply_output_deadband(candidate)

        dt = _valid_dt(dt_seconds)
        delta = abs(candidate - self._current_a)
        tau = self.config.fast_tau_seconds if delta >= self.config.jump_threshold_a else self.config.tau_seconds
        alpha = _alpha(dt, tau)
        self._current_a += alpha * (candidate - self._current_a)
        return self._apply_output_deadband(self._current_a)

    def _apply_output_deadband(self, current_a: float) -> float:
        return 0.0 if abs(current_a) < self.config.output_deadband_a else current_a


def median(values) -> float:
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    if count == 0:
        return 0.0
    midpoint = count // 2
    if count % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _valid_dt(dt_seconds: float | None) -> float:
    try:
        dt = float(dt_seconds)
    except Exception:
        return 1.0
    if dt <= 0 or dt > 10:
        return 1.0
    return dt


def _alpha(dt_seconds: float, tau_seconds: float) -> float:
    tau = max(0.05, float(tau_seconds))
    return max(0.01, min(1.0, 1.0 - math.exp(-dt_seconds / tau)))
