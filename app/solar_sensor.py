from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .solar_filter import AdaptiveSolarFilter, SolarFilterConfig


CONFIG_REGISTER = 0x00
ADC_CONFIG_REGISTER = 0x01
SHUNT_CAL_REGISTER = 0x02
VSHUNT_REGISTER = 0x04
VBUS_REGISTER = 0x05
DIETEMP_REGISTER = 0x06
CURRENT_REGISTER = 0x07
POWER_REGISTER = 0x08
MANUFACTURER_ID_REGISTER = 0x3E
DEVICE_ID_REGISTER = 0x3F

EXPECTED_MANUFACTURER_ID = 0x5449
EXPECTED_DEVICE_ID_MASK = 0xFFF0
EXPECTED_DEVICE_ID = 0x2280

CONFIG_RESET = 0x8000
INA228_BUS_LSB = 195.3125e-6
INA228_SHUNT_LSB_LOW_RANGE = 312.5e-9
INA228_SHUNT_LSB_HIGH_RANGE = 78.125e-9
INA228_TEMP_LSB_C = 7.8125e-3
INA228_CURRENT_LSB_DIVISOR = 1 << 19
INA228_CALIBRATION_FACTOR = 13107.2e6
PROBE_ADDRESSES = (0x41, 0x44, 0x45)


@dataclass
class SolarSample:
    current_a: float
    bus_v: float
    shunt_v: float
    power_w: float = 0.0
    temperature_c: float = 0.0
    source: str = "ina228"
    raw_current_a: float | None = None
    raw_power_w: float | None = None


@dataclass
class SolarDebugSample:
    bus_id: int
    address: int
    manufacturer_id: int
    device_id: int
    config: int
    adc_config: int
    shunt_cal: int
    expected_shunt_cal: int
    current_lsb: float
    voltage_lsb: float
    vbus_raw: int
    vshunt_raw: int
    current_raw: int
    power_raw: int
    dietemp_raw: int
    bus_v: float
    shunt_v: float
    current_a: float
    current_from_shunt_a: float
    power_w: float
    power_calc_w: float
    die_temp_c: float


def sensor_enabled() -> bool:
    return os.getenv("APP_SOLAR_SENSOR", "").strip().lower() == "ina228"


class INA228Sensor:
    def __init__(self) -> None:
        from smbus2 import SMBus, i2c_msg  # lazy import: optional dependency

        self._SMBus = SMBus
        self._i2c_msg = i2c_msg
        self._bus = None
        self.bus_id = int(os.getenv("APP_SOLAR_I2C_BUS", "1"))
        self.address = int(os.getenv("APP_SOLAR_I2C_ADDR", "0x45"), 0)
        self.shunt_ohms = float(os.getenv("APP_SOLAR_SHUNT_OHMS", "0.0002"))
        self.max_amps = float(os.getenv("APP_SOLAR_MAX_AMPS", "204.8"))
        self.current_gain = float(os.getenv("APP_SOLAR_CURRENT_GAIN", "1.0"))
        self.current_offset = float(os.getenv("APP_SOLAR_CURRENT_OFFSET", "0.0"))
        self.current_deadband_a = float(os.getenv("APP_SOLAR_CURRENT_DEADBAND_A", "0.15"))
        self.current_sign = -1.0 if os.getenv("APP_SOLAR_INVERT_SIGN", "").strip().lower() in {"1", "true", "yes", "on"} else 1.0
        self.filter = AdaptiveSolarFilter(_filter_config_from_env(self.current_deadband_a))
        self._last_sample_ts = None
        self.current_lsb = self.max_amps / INA228_CURRENT_LSB_DIVISOR
        self.expected_shunt_cal = int(INA228_CALIBRATION_FACTOR * self.current_lsb * self.shunt_ohms) & 0x7FFF

        try:
            self._bus = self._SMBus(self.bus_id)
            self.address = self._detect_address(self.address)
            self.manufacturer_id = self._read_u16(MANUFACTURER_ID_REGISTER)
            self.device_id = self._read_u16(DEVICE_ID_REGISTER)
            self._configure_like_ardupilot()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        bus = self._bus
        self._bus = None
        if bus is None:
            return
        try:
            bus.close()
        except Exception:
            pass

    def read_sample(self) -> SolarSample:
        debug = self.read_debug_sample()
        now = time.time()
        dt = None if self._last_sample_ts is None else now - self._last_sample_ts
        self._last_sample_ts = now

        raw_current_a = 0.0 if abs(debug.current_a) < self.current_deadband_a else debug.current_a
        current_a = self.filter.update_current(raw_current_a, dt)
        power_w = 0.0 if current_a == 0.0 else debug.bus_v * current_a
        raw_power_w = 0.0 if raw_current_a == 0.0 else debug.bus_v * raw_current_a
        return SolarSample(
            current_a=current_a,
            bus_v=debug.bus_v,
            shunt_v=debug.shunt_v,
            power_w=power_w,
            temperature_c=debug.die_temp_c,
            raw_current_a=raw_current_a,
            raw_power_w=raw_power_w,
        )

    def read_debug_sample(self) -> SolarDebugSample:
        config = self._read_u16(CONFIG_REGISTER)
        adc_config = self._read_u16(ADC_CONFIG_REGISTER)
        shunt_cal = self._read_u16(SHUNT_CAL_REGISTER)

        vshunt_raw = self._read_s24_shifted(VSHUNT_REGISTER)
        vbus_raw = self._read_s24_shifted(VBUS_REGISTER)
        current_raw = self._read_s24_shifted(CURRENT_REGISTER)
        power_raw = self._read_u24(POWER_REGISTER)
        dietemp_raw = self._read_s16(DIETEMP_REGISTER)

        adc_range = (config >> 4) & 0x1
        shunt_lsb = INA228_SHUNT_LSB_HIGH_RANGE if adc_range else INA228_SHUNT_LSB_LOW_RANGE

        shunt_v = vshunt_raw * shunt_lsb
        bus_v = max(0.0, vbus_raw * INA228_BUS_LSB)
        current_from_register = current_raw * self.current_lsb
        current_a = (current_from_register * self.current_sign * self.current_gain) + self.current_offset
        current_from_shunt_a = ((shunt_v / self.shunt_ohms) * self.current_sign * self.current_gain) + self.current_offset
        power_w = power_raw * 3.2 * self.current_lsb
        power_calc_w = bus_v * current_a
        die_temp_c = dietemp_raw * INA228_TEMP_LSB_C

        return SolarDebugSample(
            bus_id=self.bus_id,
            address=self.address,
            manufacturer_id=self.manufacturer_id,
            device_id=self.device_id,
            config=config,
            adc_config=adc_config,
            shunt_cal=shunt_cal,
            expected_shunt_cal=self.expected_shunt_cal,
            current_lsb=self.current_lsb,
            voltage_lsb=INA228_BUS_LSB,
            vbus_raw=vbus_raw,
            vshunt_raw=vshunt_raw,
            current_raw=current_raw,
            power_raw=power_raw,
            dietemp_raw=dietemp_raw,
            bus_v=bus_v,
            shunt_v=shunt_v,
            current_a=current_a,
            current_from_shunt_a=current_from_shunt_a,
            power_w=power_w,
            power_calc_w=power_calc_w,
            die_temp_c=die_temp_c,
        )

    def _configure_like_ardupilot(self) -> None:
        self._write_u16(CONFIG_REGISTER, CONFIG_RESET)
        time.sleep(0.002)
        self._write_u16(CONFIG_REGISTER, 0)
        self._write_u16(SHUNT_CAL_REGISTER, self.expected_shunt_cal)

    def _detect_address(self, requested_address: int) -> int:
        candidates = PROBE_ADDRESSES if requested_address == 0 else (requested_address,)
        last_error = None
        mismatches: list[str] = []

        for address in candidates:
            try:
                manufacturer_id = self._read_u16(MANUFACTURER_ID_REGISTER, address=address)
                device_id = self._read_u16(DEVICE_ID_REGISTER, address=address)
            except Exception as exc:
                last_error = exc
                continue

            if manufacturer_id != EXPECTED_MANUFACTURER_ID:
                mismatches.append(
                    f"0x{address:02x}: manufacturer=0x{manufacturer_id:04x}, device=0x{device_id:04x}"
                )
                continue
            if (device_id & EXPECTED_DEVICE_ID_MASK) != EXPECTED_DEVICE_ID:
                mismatches.append(
                    f"0x{address:02x}: manufacturer=0x{manufacturer_id:04x}, device=0x{device_id:04x}"
                )
                continue
            return address

        details = "; ".join(mismatches)
        if requested_address == 0:
            message = "no INA228 found on supported Matek addresses 0x41/0x44/0x45"
            if details:
                message = f"{message}; ids read: {details}"
            raise RuntimeError(message) from last_error
        if not details:
            details = "no readable identity registers"
        raise RuntimeError(
            f"unexpected INA228 ids at 0x{requested_address:02x}: "
            f"{details}"
        ) from last_error

    def _write_u16(self, register: int, value: int) -> None:
        if self._bus is None:
            raise RuntimeError("INA228 bus is closed")
        payload = [register & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
        write = self._i2c_msg.write(self.address, payload)
        self._bus.i2c_rdwr(write)

    def _read_u16(self, register: int, address: int | None = None) -> int:
        data = self._read_bytes(register, 2, address=address)
        return (data[0] << 8) | data[1]

    def _read_s16(self, register: int, address: int | None = None) -> int:
        return _sign_extend(self._read_u16(register, address=address), 16)

    def _read_u24(self, register: int, address: int | None = None) -> int:
        data = self._read_bytes(register, 3, address=address)
        return (data[0] << 16) | (data[1] << 8) | data[2]

    def _read_s24_shifted(self, register: int, address: int | None = None) -> int:
        value = self._read_u24(register, address=address)
        value >>= 4
        return _sign_extend(value, 20)

    def _read_bytes(self, register: int, count: int, address: int | None = None) -> bytes:
        if self._bus is None:
            raise RuntimeError("INA228 bus is closed")
        target = self.address if address is None else address
        write = self._i2c_msg.write(target, [register & 0xFF])
        read = self._i2c_msg.read(target, count)
        self._bus.i2c_rdwr(write, read)
        return bytes(read)


def read_solar_sample(sensor: INA228Sensor | None, failure_backoff_until: float) -> tuple[SolarSample | None, float, INA228Sensor | None]:
    now = time.time()
    if not sensor_enabled():
        if sensor is not None:
            sensor.close()
        return None, 0.0, None

    if now < failure_backoff_until:
        return None, failure_backoff_until, sensor

    try:
        if sensor is None:
            sensor = INA228Sensor()
        return sensor.read_sample(), 0.0, sensor
    except Exception:
        if sensor is not None:
            sensor.close()
        return None, now + 2.0, None


def _sign_extend(value: int, bits: int) -> int:
    sign_bit = 1 << (bits - 1)
    return (value ^ sign_bit) - sign_bit


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _filter_config_from_env(current_deadband_a: float) -> SolarFilterConfig:
    return SolarFilterConfig(
        enabled=_env_bool("APP_SOLAR_FILTER_ENABLED", True),
        tau_seconds=max(0.05, _env_float("APP_SOLAR_FILTER_TAU_SECONDS", 4.0)),
        fast_tau_seconds=max(0.05, _env_float("APP_SOLAR_FILTER_FAST_TAU_SECONDS", 0.8)),
        median_window=max(1, _env_int("APP_SOLAR_FILTER_MEDIAN_WINDOW", 5)),
        jump_threshold_a=max(0.0, _env_float("APP_SOLAR_FILTER_JUMP_THRESHOLD_A", 2.5)),
        output_deadband_a=max(0.0, _env_float("APP_SOLAR_FILTER_OUTPUT_DEADBAND_A", current_deadband_a)),
    )
