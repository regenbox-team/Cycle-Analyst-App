from __future__ import annotations

import os
import time
from dataclasses import dataclass


CONFIG_REGISTER = 0x00
VSHUNT_REGISTER = 0x04
VBUS_REGISTER = 0x05
MANUFACTURER_ID_REGISTER = 0x3E

EXPECTED_MANUFACTURER_ID = 0x5449


@dataclass
class SolarSample:
    current_a: float
    bus_v: float
    shunt_v: float
    source: str = "ina228"


def sensor_enabled() -> bool:
    return os.getenv("APP_SOLAR_SENSOR", "").strip().lower() == "ina228"


class INA228Sensor:
    def __init__(self) -> None:
        from smbus2 import SMBus, i2c_msg  # lazy import: optional dependency

        self._SMBus = SMBus
        self._i2c_msg = i2c_msg
        self.bus_id = int(os.getenv("APP_SOLAR_I2C_BUS", "1"))
        self.address = int(os.getenv("APP_SOLAR_I2C_ADDR", "0x45"), 0)
        self.shunt_ohms = float(os.getenv("APP_SOLAR_SHUNT_OHMS", "0.0002"))
        self.current_gain = float(os.getenv("APP_SOLAR_CURRENT_GAIN", "1.0"))
        self.current_offset = float(os.getenv("APP_SOLAR_CURRENT_OFFSET", "0.0"))
        self.current_sign = -1.0 if os.getenv("APP_SOLAR_INVERT_SIGN", "").strip().lower() in {"1", "true", "yes", "on"} else 1.0
        self._bus = self._SMBus(self.bus_id)

        manufacturer_id = self._read_u16(MANUFACTURER_ID_REGISTER)
        if manufacturer_id != EXPECTED_MANUFACTURER_ID:
            raise RuntimeError(
                f"unexpected INA228 manufacturer id 0x{manufacturer_id:04x} at 0x{self.address:02x}"
            )

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass

    def read_sample(self) -> SolarSample:
        config = self._read_u16(CONFIG_REGISTER)
        adc_range = (config >> 4) & 0x1

        vshunt_raw = self._read_s24_shifted(VSHUNT_REGISTER)
        vbus_raw = self._read_s24_shifted(VBUS_REGISTER)

        shunt_lsb = 78.125e-9 if adc_range else 312.5e-9
        bus_lsb = 195.3125e-6

        shunt_v = vshunt_raw * shunt_lsb
        bus_v = max(0.0, vbus_raw * bus_lsb)
        current_a = ((shunt_v / self.shunt_ohms) * self.current_sign * self.current_gain) + self.current_offset

        return SolarSample(
            current_a=current_a,
            bus_v=bus_v,
            shunt_v=shunt_v,
        )

    def _read_u16(self, register: int) -> int:
        data = self._read_bytes(register, 2)
        return (data[0] << 8) | data[1]

    def _read_s24_shifted(self, register: int) -> int:
        data = self._read_bytes(register, 3)
        value = (data[0] << 16) | (data[1] << 8) | data[2]
        value >>= 4
        return _sign_extend(value, 20)

    def _read_bytes(self, register: int, count: int) -> bytes:
        write = self._i2c_msg.write(self.address, [register & 0xFF])
        read = self._i2c_msg.read(self.address, count)
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
