#!/usr/bin/env python3
import argparse, time, math, sys, csv
from collections import defaultdict

try:
    import can
    HAS_CAN = True
except Exception:
    HAS_CAN = False

import cantools

# ---- Mapping (from your message) ----
MAP = {
    # frame_id, extended?, message_name, signal_name
    "current":  {"id": 0x0D2,  "ext": False, "msg": "BMS_Information",                     "sig": "BMSBatteryCurrent"},
    "voltage":  {"id": 0x0D2,  "ext": False, "msg": "BMS_Information",                     "sig": "BMSBatteryVoltage"},
    "speed":    {"id": 0x610,  "ext": False, "msg": "DISPLAY_Moteur_statut_controleur",    "sig": "Vehicle_speed"},
    "distance": {"id": 0x620,  "ext": False, "msg": "DISPLAY_Odo_trip_controleur",         "sig": "Trip"},
    "mot_temp": {"id": 0x1014, "ext": True,  "msg": "MIC_id20_Status4",                    "sig": "Status_MotorTemp"},
}

FIELDS = ["ah", "voltage", "current", "speed", "distance", "temp", "cyclist_rpm",
          "unused8", "unused9", "unused10", "unused11", "unused12",
          "solar_Ah", "solar_A", "flags"]

def load_db(dbc_path):
    return cantools.database.load_file(dbc_path)

def fmt_line(vals):
    """
    Emit a space-separated line with exactly 15 fields, in the order your cycle_server expects.
    Only indices 1,2,3,4,5,13 are used by your server; others set to 0.
    """
    # defaults
    out = {
        "ah": vals.get("ah", 0.0),
        "voltage": vals.get("voltage", 0.0),
        "current": vals.get("current", 0.0),
        "speed": vals.get("speed", 0.0),
        "distance": vals.get("distance", 0.0),
        "temp": vals.get("temp", 0.0),
        "cyclist_rpm": 0.0,
        "unused8": 0.0,
        "unused9": 0.0,
        "unused10": 0.0,
        "unused11": 0.0,
        "unused12": 0.0,          # ← add this missing field
        "solar_Ah": 0.0,
        "solar_A": vals.get("solar_A", 0.0),
        "flags": "2B",
    }
    # ordering + formatting
    line = [
        f"{out['ah']:.4f}",       # 1 Ah consumed
        f"{out['voltage']:.2f}",  # 2 Voltage
        f"{out['current']:.2f}",  # 3 Current
        f"{out['speed']:.2f}",    # 4 Speed
        f"{out['distance']:.3f}", # 5 Distance
        f"{out['temp']:.1f}",     # 6 Motor temp
        f"{out['cyclist_rpm']:.0f}",  # 7
        f"{out['unused8']:.0f}",      # 8
        f"{out['unused9']:.1f}",      # 9
        f"{out['unused10']:.1f}",     # 10
        f"{out['unused11']:.0f}",     # 11
        f"{out['unused12']:.0f}",     # 12  ← now present
        f"{out['solar_Ah']:.3f}",     # 13
        f"{out['solar_A']:.2f}",      # 14
        out['flags'],                 # 15
    ]
    return " ".join(map(str, line))

def make_index(db):
    """Index messages by (frame_id, is_extended)."""
    idx = {}
    for m in db.messages:
        key = (m.frame_id, bool(getattr(m, "is_extended_frame", False)))
        idx.setdefault(key, []).append(m)
    return idx

def pick_msg(candidates, name=None):
    if not candidates:
        return None
    if name:
        for m in candidates:
            if m.name == name:
                return m
    return candidates[0]

def live_mode(args):
    if not HAS_CAN:
        print("python-can not installed. Run: pip install python-can", file=sys.stderr)
        sys.exit(1)

    db = load_db(args.dbc)
    idx = make_index(db)

    # Resolve message objects we care about
    targets = {}
    for key, meta in MAP.items():
        mlist = idx.get((meta["id"], meta["ext"]), [])
        targets[key] = pick_msg(mlist, meta["msg"])

    bus = can.interface.Bus(channel=args.channel, bustype="socketcan")

    # State
    last_vals = {"solar_A": 0.0}
    last_seen = {"current": 0, "voltage": 0, "speed": 0, "distance": 0, "temp": 0}
    ah_used = 0.0
    last_time = time.time()
    next_pub = last_time

    # Staleness thresholds (seconds)
    STALE_CURRENT_S = 0.5   # if no new current for >0.5s, stop integrating
    STALE_SPEED_S   = 0.7   # if speed stale, keep last value a bit for UX
    STALE_VOLT_S    = 2.0
    STALE_DIST_S    = 2.0
    STALE_TEMP_S    = 5.0

    # Optional UI smoothing (exponential)
    def smooth(prev, new, alpha=0.25):
        if prev is None: return new
        return prev + alpha * (new - prev)

    smoothed = {"speed": None}

    while True:
        now = time.time()
        timeout = 0.02  # ~50 Hz poll to keep latency low
        msg = bus.recv(timeout=timeout)

        if msg:
            key = (msg.arbitration_id, msg.is_extended_id)
            mlist = idx.get(key, [])
            m = pick_msg(mlist)
            if m:
                try:
                    decoded = m.decode(bytes(msg.data), decode_choices=True, allow_truncated=True)
                    # Update any mapped field present in this message
                    for field, meta in MAP.items():
                        if m is targets[field]:
                            sig = meta["sig"]
                            if sig in decoded:
                                val = float(decoded[sig])
                                if field == "current":
                                    last_vals["current"] = val; last_seen["current"] = now
                                elif field == "voltage":
                                    last_vals["voltage"] = val; last_seen["voltage"] = now
                                elif field == "speed":
                                    last_vals["speed"]   = val; last_seen["speed"]   = now
                                elif field == "distance":
                                    last_vals["distance"]= val; last_seen["distance"]= now
                                elif field == "mot_temp":
                                    last_vals["temp"]    = val; last_seen["temp"]    = now
                except Exception:
                    pass

        # --- Safe Ah integration with staleness guard ---
        dt = now - last_time
        if dt > 0 and dt < 1.0:
            cur = float(last_vals.get("current", 0.0) or 0.0)
            if (now - last_seen["current"]) <= STALE_CURRENT_S:
                # integrate only while current is fresh
                ah_used += cur * dt / 3600.0
        last_time = now

        # --- Publish at fixed 10 Hz ---
        if now >= next_pub:
            next_pub = now + 0.1

            # Apply staleness policies for publishing (don’t zero immediately)
            pub_voltage  = last_vals.get("voltage", 0.0)  if (now - last_seen["voltage"])  <= STALE_VOLT_S else last_vals.get("voltage", 0.0)
            pub_distance = last_vals.get("distance", 0.0) if (now - last_seen["distance"]) <= STALE_DIST_S else last_vals.get("distance", 0.0)
            pub_temp     = last_vals.get("temp", 0.0)     if (now - last_seen["temp"])     <= STALE_TEMP_S else last_vals.get("temp", 0.0)

            # Speed: keep last value for a short time then let it gently fall (optional smoothing)
            raw_speed = last_vals.get("speed", 0.0) if (now - last_seen["speed"]) <= STALE_SPEED_S else last_vals.get("speed", 0.0)
            smoothed["speed"] = smooth(smoothed["speed"], raw_speed, alpha=0.3)
            pub_speed = smoothed["speed"] if smoothed["speed"] is not None else raw_speed

            # Current for display: show last value, but it might be stale; that’s OK visually
            pub_current = last_vals.get("current", 0.0)

            line = fmt_line({
                "ah": ah_used,
                "voltage": pub_voltage,
                "current": pub_current,
                "speed": pub_speed,
                "distance": pub_distance,
                "temp": pub_temp,
                "solar_A": 0.0
            })
            print(line, flush=True)


def csv_mode(args):
    db = load_db(args.dbc)
    # Pre-index messages by id/extended for quick lookup
    idx = make_index(db)

    # state
    last_vals = {"solar_A": 0.0}
    ah_used = 0.0
    last_epoch = None

    with open(args.csv, newline="") as fi:
        r = csv.DictReader(fi)
        for row in r:
            try:
                arb_id = int(row["id_hex"], 16)
                is_ext = bool(int(row.get("extended", "0")))
                data = []
                for i in range(8):
                    key = f"b{i}"
                    if key in row and row[key] not in (None, "", "nan", "NaN"):
                        v = float(row[key])
                        if math.isnan(v):
                            continue
                        data.append(int(v) & 0xFF)
                data = bytes(data)
            except Exception:
                continue

            mlist = idx.get((arb_id, is_ext), [])
            m = pick_msg(mlist)
            if not m:
                continue

            try:
                decoded = m.decode(data, decode_choices=True, allow_truncated=True)
            except Exception:
                decoded = {}

            # map fields
            for field, meta in MAP.items():
                if m is not None and m.name == meta["msg"]:
                    sig = meta["sig"]
                    if sig in decoded:
                        if field == "current":
                            last_vals["current"] = float(decoded[sig])
                        elif field == "voltage":
                            last_vals["voltage"] = float(decoded[sig])
                        elif field == "speed":
                            last_vals["speed"] = float(decoded[sig])
                        elif field == "distance":
                            last_vals["distance"] = float(decoded[sig])
                        elif field == "mot_temp":
                            last_vals["temp"] = float(decoded[sig])

            # integrate Ah using CSV epoch timing if present
            try:
                epoch = float(row.get("epoch", "") or 0.0)
            except:
                epoch = None

            if last_epoch is not None and epoch and epoch > last_epoch:
                dt = epoch - last_epoch
                a = float(last_vals.get("current", 0.0) or 0.0)
                if a > 0:
                    ah_used += a * dt / 3600.0
            last_epoch = epoch if epoch else last_epoch

            # Emit a line whenever we have a new row (approx original cadence)
            line = fmt_line({
                "ah": ah_used,
                "voltage": last_vals.get("voltage", 0.0),
                "current": last_vals.get("current", 0.0),
                "speed": last_vals.get("speed", 0.0),
                "distance": last_vals.get("distance", 0.0),
                "temp": last_vals.get("temp", 0.0),
                "solar_A": 0.0
            })
            print(line)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bridge CAN+DBC → Cycle Analyst raw line format (15 fields).")
    ap.add_argument("--dbc", required=True, help="Path to DBC file")
    sub = ap.add_subparsers(dest="mode", required=True)

    live = sub.add_parser("live", help="Decode live from SocketCAN")
    live.add_argument("--channel", default="can0", help="SocketCAN channel (default: can0)")

    csvp = sub.add_parser("csv", help="Decode from a recorded CAN CSV (candump→csv)")
    csvp.add_argument("--csv", required=True, help="Path to can_log.csv")

    args = ap.parse_args()
    if args.mode == "live":
        live_mode(args)
    else:
        csv_mode(args)

