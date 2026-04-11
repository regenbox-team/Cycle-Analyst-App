#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.solar_sensor import INA228Sensor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Debug INA228 readings on the Raspberry Pi using the same calibration path as ArduPilot."
    )
    parser.add_argument("--bus", type=int, default=1, help="I2C bus number (default: 1)")
    parser.add_argument(
        "--addr",
        type=lambda value: int(value, 0),
        default=0,
        help="I2C address, or 0 to probe 0x41/0x44/0x45 (default: 0)",
    )
    parser.add_argument("--shunt-ohms", type=float, default=0.0002, help="Shunt resistor value in ohms (default: 0.0002)")
    parser.add_argument("--max-amps", type=float, default=204.8, help="Configured full-scale current for CURRENT_LSB (default: 204.8)")
    parser.add_argument("--gain", type=float, default=1.0, help="Optional current gain trim (default: 1.0)")
    parser.add_argument("--offset", type=float, default=0.0, help="Optional current offset trim in amps (default: 0.0)")
    parser.add_argument("--invert-sign", action="store_true", help="Invert current sign")
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds (default: 1.0)")
    parser.add_argument("--count", type=int, default=0, help="Number of samples to print, 0 means endless (default: 0)")
    parser.add_argument("--json", action="store_true", help="Emit one JSON object per sample")
    parser.add_argument("--once", action="store_true", help="Print one sample and exit")
    return parser


def apply_env(args: argparse.Namespace) -> None:
    os.environ["APP_SOLAR_SENSOR"] = "ina228"
    os.environ["APP_SOLAR_I2C_BUS"] = str(args.bus)
    os.environ["APP_SOLAR_I2C_ADDR"] = hex(args.addr)
    os.environ["APP_SOLAR_SHUNT_OHMS"] = str(args.shunt_ohms)
    os.environ["APP_SOLAR_MAX_AMPS"] = str(args.max_amps)
    os.environ["APP_SOLAR_CURRENT_GAIN"] = str(args.gain)
    os.environ["APP_SOLAR_CURRENT_OFFSET"] = str(args.offset)
    if args.invert_sign:
        os.environ["APP_SOLAR_INVERT_SIGN"] = "true"
    else:
        os.environ.pop("APP_SOLAR_INVERT_SIGN", None)


def print_banner(sensor: INA228Sensor) -> None:
    sample = sensor.read_debug_sample()
    print(f"INA228 found on I2C bus {sample.bus_id}, address 0x{sample.address:02X}")
    print(
        "IDs: "
        f"manufacturer=0x{sample.manufacturer_id:04X}, "
        f"device=0x{sample.device_id:04X}"
    )
    print(
        "Config: "
        f"CONFIG=0x{sample.config:04X}, "
        f"ADC_CONFIG=0x{sample.adc_config:04X}, "
        f"SHUNT_CAL=0x{sample.shunt_cal:04X}, "
        f"expected=0x{sample.expected_shunt_cal:04X}"
    )
    print(
        "Scaling: "
        f"current_lsb={sample.current_lsb * 1e6:.6f} uA/LSB, "
        f"bus_lsb={sample.voltage_lsb * 1e6:.4f} uV/LSB"
    )
    if sample.shunt_cal != sample.expected_shunt_cal:
        print("Warning: SHUNT_CAL on device does not match expected ArduPilot-style value.")


def format_sample(sample) -> str:
    reg_current = sample.current_a
    shunt_current = sample.current_from_shunt_a
    mismatch = abs(reg_current - shunt_current)
    mismatch_note = ""
    if abs(shunt_current) > 0.2 and mismatch > max(0.5, abs(shunt_current) * 0.15):
        mismatch_note = " current-mismatch"
    return (
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"Vbus={sample.bus_v:7.3f} V  "
        f"Vshunt={sample.shunt_v * 1e3:8.3f} mV  "
        f"Ireg={reg_current:8.3f} A  "
        f"Ishunt={shunt_current:8.3f} A  "
        f"Preg={sample.power_w:8.3f} W  "
        f"Pvi={sample.power_calc_w:8.3f} W  "
        f"Tdie={sample.die_temp_c:6.2f} C  "
        f"raw[vbus={sample.vbus_raw}, vshunt={sample.vshunt_raw}, current={sample.current_raw}, power={sample.power_raw}]"
        f"{mismatch_note}"
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.once:
        args.count = 1

    apply_env(args)

    try:
        sensor = INA228Sensor()
    except Exception as exc:
        print(f"INA228 init failed: {exc}")
        return 1

    print_banner(sensor)

    remaining = args.count
    try:
        while True:
            sample = sensor.read_debug_sample()
            if args.json:
                print(json.dumps(asdict(sample), sort_keys=True))
            else:
                print(format_sample(sample))

            if remaining == 1:
                break
            if remaining > 1:
                remaining -= 1
            time.sleep(max(0.05, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        sensor.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
