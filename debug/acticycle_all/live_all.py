#!/usr/bin/env python3
import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict

try:
    import can
except Exception as e:
    print("python-can is required: pip install python-can", file=sys.stderr)
    raise

import cantools


DEFAULT_DBC = "can_util/Cockpit_CAN_Database_V1.4.dbc,can_util/Act2.5_database_can_A_V1.5.dbc"


def load_db_merged(dbc_arg: str):
    paths = [p.strip() for p in str(dbc_arg).split(',') if p and p.strip()]
    if not paths:
        raise SystemExit("No DBC paths provided")
    if len(paths) == 1:
        return cantools.database.load_file(paths[0])

    db = cantools.database.Database()
    for p in paths:
        sub = cantools.database.load_file(p)
        db.messages.extend(getattr(sub, "messages", []))
        db.nodes.extend(getattr(sub, "nodes", []))
        if hasattr(sub, "buses"):
            db._buses.extend(sub.buses)
    return db


def build_index(db):
    idx = defaultdict(list)
    for m in db.messages:
        key = (m.frame_id, bool(getattr(m, "is_extended_frame", False)))
        idx[key].append(m)
    return idx


def safe_decode(message, data: bytes):
    try:
        return message.decode(data, decode_choices=True, allow_truncated=True)
    except Exception:
        return None


def ensure_dir(path: str):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def fmt_acticycle_line(vals: dict) -> str:
    # Build the 15-field line in the same order the app expects.
    out = {
        "ah": float(vals.get("ah", 0.0) or 0.0),
        "voltage": float(vals.get("voltage", 0.0) or 0.0),
        "current": float(vals.get("current", 0.0) or 0.0),
        "speed": float(vals.get("speed", 0.0) or 0.0),
        "distance": float(vals.get("distance", 0.0) or 0.0),
        "temp": float(vals.get("temp", 0.0) or 0.0),
        "cyclist_rpm": 0.0,
        "unused8": 0.0,
        "unused9": 0.0,
        "unused10": 0.0,
        "unused11": 0.0,
        "unused12": 0.0,
        "solar_Ah": 0.0,
        "solar_A": float(vals.get("solar_A", 0.0) or 0.0),
        "flags": "2B",
    }
    line = [
        f"{out['ah']:.4f}",
        f"{out['voltage']:.2f}",
        f"{out['current']:.2f}",
        f"{out['speed']:.2f}",
        f"{out['distance']:.3f}",
        f"{out['temp']:.1f}",
        f"{out['cyclist_rpm']:.0f}",
        f"{out['unused8']:.0f}",
        f"{out['unused9']:.1f}",
        f"{out['unused10']:.1f}",
        f"{out['unused11']:.0f}",
        f"{out['unused12']:.0f}",
        f"{out['solar_Ah']:.3f}",
        f"{out['solar_A']:.2f}",
        out['flags'],
    ]
    return " ".join(map(str, line))


def live_all(args):
    db = load_db_merged(args.dbc)
    idx = build_index(db)

    # Use modern python-can API (avoid deprecation of bustype)
    bus = can.Bus(interface="socketcan", channel=args.channel)

    # State: latest decoded signals across all messages
    latest_values = {}
    last_seen = {}

    # Derived values for the Acticycle 15-field line
    derived = {"ah": 0.0, "solar_A": 0.0}
    last_time = time.time()

    # Optional smoothing store
    smooth_speed = None

    # JSONL output
    ensure_dir(args.outfile)
    outf = open(args.outfile, "a", buffering=1)

    publish_dt = 1.0 / max(1.0, float(args.rate))
    next_pub = time.time()

    # Staleness settings for integration and display (seconds)
    STALE_CURRENT_S = 0.5
    STALE_SPEED_S = 0.7
    STALE_VOLT_S = 2.0
    STALE_DIST_S = 2.0
    STALE_TEMP_S = 5.0

    def sset(name, val):
        # Store as-is (already converted to float or string by caller)
        latest_values[name] = val
        last_seen[name] = time.time()

    try:
        while True:
            now = time.time()
            msg = bus.recv(timeout=0.02)
            if msg is not None:
                key = (msg.arbitration_id, bool(msg.is_extended_id))
                messages = idx.get(key, [])
                for m in messages:
                    decoded = safe_decode(m, bytes(msg.data))
                    if not decoded:
                        continue
                    # Store every decoded signal under "Message.Signal"
                    for sig_name, raw_val in decoded.items():
                        try:
                            # Numeric values
                            val = float(raw_val)
                        except Exception:
                            # Non-numeric (e.g., NamedSignalValue or other) → stringify for JSON safety
                            try:
                                # If it's a cantools NamedSignalValue, prefer its name when available
                                name_attr = getattr(raw_val, "name", None)
                                if isinstance(name_attr, str):
                                    val = name_attr
                                else:
                                    val = str(raw_val)
                            except Exception:
                                val = str(raw_val)
                        sset(f"{m.name}.{sig_name}", val)

                    # Try to map common fields for the 15-field line when possible
                    # Heuristics based on known message/signal names from the provided DBCs.
                    try:
                        # Battery current/voltage
                        if m.name == "BMS_Information":
                            if "BMSBatteryCurrent" in decoded:
                                sset("current", float(decoded["BMSBatteryCurrent"]))
                            if "BMSBatteryVoltage" in decoded:
                                sset("voltage", float(decoded["BMSBatteryVoltage"]))
                        # Vehicle speed
                        if m.name == "DISPLAY_Moteur_statut_controleur" and "Vehicle_speed" in decoded:
                            sset("speed", float(decoded["Vehicle_speed"]))
                        # Distance
                        if m.name == "DISPLAY_Odo_trip_controleur" and "Trip" in decoded:
                            sset("distance", float(decoded["Trip"]))
                        # Motor temp
                        if m.name in ("MIC_id20_Status4",) and "Status_MotorTemp" in decoded:
                            sset("temp", float(decoded["Status_MotorTemp"]))
                        # Pedal power → compute equivalent current at pack voltage
                        if m.name == "Display_Riding_Power" and "displayPedallingPower" in decoded:
                            p_w = float(decoded["displayPedallingPower"]) or 0.0
                            v = float(latest_values.get("voltage", 0.0) or 0.0)
                            derived["solar_A"] = (p_w / v) if v >= 5.0 else 0.0
                    except Exception:
                        pass

            # Integrate Ah only when current is fresh
            dt = now - last_time
            if 0 < dt < 1.0:
                cur = float(latest_values.get("current", 0.0) or 0.0)
                if (now - last_seen.get("current", 0)) <= STALE_CURRENT_S:
                    derived["ah"] += cur * dt / 3600.0
            last_time = now

            # Periodic publish
            if now >= next_pub:
                next_pub = now + publish_dt

                # Build acticycle values with staleness
                pub_voltage = latest_values.get("voltage", 0.0)
                pub_distance = latest_values.get("distance", 0.0)
                pub_temp = latest_values.get("temp", 0.0)

                # speed smoothing
                raw_speed = latest_values.get("speed", 0.0)
                if smooth_speed is None:
                    smooth_speed = raw_speed
                else:
                    alpha = 0.3
                    smooth_speed = smooth_speed + alpha * (raw_speed - smooth_speed)

                line = fmt_acticycle_line({
                    "ah": derived.get("ah", 0.0),
                    "voltage": pub_voltage,
                    "current": latest_values.get("current", 0.0),
                    "speed": smooth_speed,
                    "distance": pub_distance,
                    "temp": pub_temp,
                    "solar_A": derived.get("solar_A", 0.0),
                })

                # Emit the 15-field line to stdout (consumed by app in exec mode)
                print(line, flush=True)

                # Write a JSONL snapshot with all latest decoded values
                snapshot = {
                    "epoch": now,
                    "acticycle": {
                        "ah": derived.get("ah", 0.0),
                        "voltage": latest_values.get("voltage"),
                        "current": latest_values.get("current"),
                        "speed": latest_values.get("speed"),
                        "distance": latest_values.get("distance"),
                        "temp": latest_values.get("temp"),
                        "solar_A": derived.get("solar_A", 0.0),
                    },
                    "signals": latest_values,  # includes Message.Signal keys for all decoded
                }
                try:
                    outf.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"[warn] failed to write snapshot: {e}", file=sys.stderr)
    finally:
        try:
            outf.close()
        except Exception:
            pass
        try:
            bus.shutdown()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Acticycle all-signals live debugger (stdout: 15 fields; file: all signals)")
    ap.add_argument("--dbc", default=DEFAULT_DBC, help="Comma-separated DBC paths to merge")
    ap.add_argument("--channel", default="can0", help="SocketCAN channel (default: can0)")
    ap.add_argument("--rate", type=float, default=10.0, help="Publish rate in Hz for stdout/file (default: 10)")
    ap.add_argument("--outfile", default="var/debug/all_signals.jsonl", help="Path to JSONL snapshot file")
    args = ap.parse_args()

    bus = None
    outf = None
    try:
        live_all(args)
    except KeyboardInterrupt:
        pass
    finally:
        # Best-effort clean shutdown when exceptions occur higher up
        try:
            if bus is not None:
                bus.shutdown()
        except Exception:
            pass
        try:
            if outf is not None:
                outf.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
