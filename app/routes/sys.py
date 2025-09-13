from __future__ import annotations
import os
import time
from flask import jsonify


def _read_file_float(path: str, scale: float = 1.0) -> float | None:
    try:
        with open(path, 'r') as f:
            v = f.read().strip()
        return float(v) / scale
    except Exception:
        return None


def _read_temp_c() -> float | None:
    # Standard Linux thermal sysfs
    t = _read_file_float('/sys/class/thermal/thermal_zone0/temp', 1000.0)
    if t is not None:
        return t
    # Fallback to vcgencmd if available
    try:
        import subprocess
        out = subprocess.check_output(['vcgencmd', 'measure_temp'], text=True).strip()
        # e.g. temp=48.2'C
        if out.startswith('temp='):
            val = out.split('=')[1].split("'")[0]
            return float(val)
    except Exception:
        pass
    return None


def _read_throttled() -> str | None:
    try:
        import subprocess
        out = subprocess.check_output(['vcgencmd', 'get_throttled'], text=True).strip()
        # e.g. throttled=0x0
        return out.split('=')[1] if '=' in out else out
    except Exception:
        return None


def _read_cpu_freq_mhz() -> float | None:
    v = _read_file_float('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 1000.0)
    if v is not None:
        return v
    return None


def _read_mem() -> dict:
    meminfo = {}
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                val = parts[1].strip().split()[0]
                meminfo[key] = float(val)  # kB
    except Exception:
        return {"total_mb": None, "avail_mb": None, "used_mb": None, "used_pct": None}

    total_mb = meminfo.get('MemTotal', 0.0) / 1024.0
    avail_mb = meminfo.get('MemAvailable', 0.0) / 1024.0
    used_mb = total_mb - avail_mb if total_mb and avail_mb is not None else None
    used_pct = (used_mb / total_mb * 100.0) if used_mb is not None and total_mb else None
    return {
        "total_mb": round(total_mb, 1) if total_mb else None,
        "avail_mb": round(avail_mb, 1) if avail_mb is not None else None,
        "used_mb": round(used_mb, 1) if used_mb is not None else None,
        "used_pct": round(used_pct, 1) if used_pct is not None else None,
    }


def _read_uptime_s() -> float | None:
    try:
        with open('/proc/uptime', 'r') as f:
            txt = f.read().split()[0]
            return float(txt)
    except Exception:
        return None


def sys_metrics():
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = None

    payload = {
        "ts": time.time(),
        "load": {"1": load1, "5": load5, "15": load15},
        "temp_c": _read_temp_c(),
        "mem": _read_mem(),
        "uptime_s": _read_uptime_s(),
        "cpu_mhz": _read_cpu_freq_mhz(),
        "throttled": _read_throttled(),
    }
    return jsonify(payload)


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("sys_metrics", __name__)
    bp.add_url_rule('/sys_metrics', view_func=sys_metrics)
    return bp


def register(app):
    app.add_url_rule('/sys_metrics', view_func=sys_metrics)

