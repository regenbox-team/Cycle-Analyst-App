from __future__ import annotations
import re
import time
import serial
import sqlite3
from datetime import datetime

from .config import GPS_SERIAL_PORT_DEFAULT, GPS_BAUDRATE, get_db_file
from . import state


def _nmea_latlon_to_decimal(lat_str: str, ns: str, lon_str: str, ew: str):
    try:
        # Latitude: DDMM.MMMM, Longitude: DDDMM.MMMM
        if not lat_str or not lon_str:
            return None, None
        lat_deg = int(float(lat_str) // 100)
        lat_min = float(lat_str) - lat_deg * 100
        lat = lat_deg + lat_min / 60.0
        if ns.upper() == 'S':
            lat = -lat

        lon_deg = int(float(lon_str) // 100)
        lon_min = float(lon_str) - lon_deg * 100
        lon = lon_deg + lon_min / 60.0
        if ew.upper() == 'W':
            lon = -lon
        return lat, lon
    except Exception:
        return None, None


def _parse_gga(fields: list[str]):
    # $GPGGA,hhmmss.sss,lat,NS,lon,EW,fix,sats,hdop,alt,M,geoid_sep,M,...
    try:
        lat_str, ns, lon_str, ew = fields[2], fields[3], fields[4], fields[5]
        fix_quality = int(fields[6] or 0)
        sats = int(fields[7] or 0)
        hdop = float(fields[8] or 0)
        alt = float(fields[9] or 0)
        lat, lon = _nmea_latlon_to_decimal(lat_str, ns, lon_str, ew)
        return {
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "fix_quality": fix_quality,
            "sats": sats,
            "hdop": hdop,
        }
    except Exception:
        return None


def _parse_rmc(fields: list[str]):
    # $GPRMC,hhmmss.sss,A,lat,NS,lon,EW,speed_knots,track_deg,date,...
    try:
        status = fields[2].upper() == 'A'
        lat_str, ns, lon_str, ew = fields[3], fields[4], fields[5], fields[6]
        speed_knots = float(fields[7] or 0)
        track_deg = float(fields[8] or 0)
        lat, lon = _nmea_latlon_to_decimal(lat_str, ns, lon_str, ew)
        return {
            "has_fix": status,
            "lat": lat,
            "lon": lon,
            "speed_kph": speed_knots * 1.852,
            "track_deg": track_deg,
        }
    except Exception:
        return None


def _update_state(partial: dict):
    s = state.gps_state
    s.update({k: v for k, v in partial.items() if v is not None})
    # Derive has_fix if fix_quality available
    if "fix_quality" in partial:
        s["has_fix"] = bool(partial.get("fix_quality", 0) >= 1)
    s["last_update"] = time.time()


def read_gps():
    port = GPS_SERIAL_PORT_DEFAULT
    baud = GPS_BAUDRATE
    ser = None
    # DB writes moved to vehicle writer to keep a single tick/timestamp
    while True:
        time.sleep(0.1)
        try:
            if ser is None:
                ser = serial.Serial(port, baudrate=baud, timeout=1)
            raw = ser.readline()
            if not raw:
                continue
            try:
                line = raw.decode(errors="ignore").strip()
            except Exception:
                line = str(raw)

            if not line.startswith("$"):
                continue
            # Remove checksum
            if '*' in line:
                line = line.split('*', 1)[0]
            parts = line.split(',')
            talker = parts[0][3:6] if len(parts[0]) >= 6 else parts[0][3:]

            if talker in ("GGA", "GNGGA"):
                gga = _parse_gga(parts)
                if gga:
                    _update_state(gga)
            elif talker in ("RMC", "GNRMC"):
                rmc = _parse_rmc(parts)
                if rmc:
                    _update_state(rmc)

            # Timestamp for status (approximate UTC now)
            state.gps_state["timestamp_utc"] = datetime.utcnow().isoformat()


        except Exception:
            # backoff and reopen on error
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(0.5)


def get_status():
    s = state.gps_state.copy()
    # Consider stale if older than 5s
    age = time.time() - (s.get("last_update") or 0)
    s["stale"] = age > 5
    return s
