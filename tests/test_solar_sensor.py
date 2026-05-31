import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.solar_sensor import (
    DEVICE_ID_REGISTER,
    EXPECTED_MANUFACTURER_ID,
    INA228Sensor,
    MANUFACTURER_ID_REGISTER,
)


class INA228DetectionTest(unittest.TestCase):
    def _sensor_with_registers(self, registers_by_address):
        sensor = INA228Sensor.__new__(INA228Sensor)

        def read_u16(register, address=None):
            try:
                return registers_by_address[address][register]
            except KeyError as exc:
                raise OSError("no response") from exc

        sensor._read_u16 = read_u16
        return sensor

    def test_accepts_expected_ids_with_revision_nibble(self):
        sensor = self._sensor_with_registers(
            {
                0x45: {
                    MANUFACTURER_ID_REGISTER: EXPECTED_MANUFACTURER_ID,
                    DEVICE_ID_REGISTER: 0x2281,
                }
            }
        )

        self.assertEqual(sensor._detect_address(0x45), 0x45)

    def test_error_includes_actual_ids_for_fixed_address(self):
        sensor = self._sensor_with_registers(
            {
                0x45: {
                    MANUFACTURER_ID_REGISTER: 0x1234,
                    DEVICE_ID_REGISTER: 0x5678,
                }
            }
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"unexpected INA228 ids at 0x45: 0x45: manufacturer=0x1234, device=0x5678",
        ):
            sensor._detect_address(0x45)

    def test_probe_error_includes_mismatched_addresses(self):
        sensor = self._sensor_with_registers(
            {
                0x41: {
                    MANUFACTURER_ID_REGISTER: 0x0000,
                    DEVICE_ID_REGISTER: 0x0000,
                },
                0x44: {
                    MANUFACTURER_ID_REGISTER: EXPECTED_MANUFACTURER_ID,
                    DEVICE_ID_REGISTER: 0x2260,
                },
            }
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"ids read: 0x41: manufacturer=0x0000, device=0x0000; "
            r"0x44: manufacturer=0x5449, device=0x2260",
        ):
            sensor._detect_address(0)


class INA228LifecycleTest(unittest.TestCase):
    def test_constructor_closes_bus_when_initialization_fails(self):
        class FakeBus:
            instances = []

            def __init__(self, bus_id):
                self.bus_id = bus_id
                self.closed = False
                FakeBus.instances.append(self)

            def close(self):
                self.closed = True

        fake_smbus2 = SimpleNamespace(SMBus=FakeBus, i2c_msg=SimpleNamespace())

        with patch.dict("sys.modules", {"smbus2": fake_smbus2}):
            with patch.object(INA228Sensor, "_detect_address", side_effect=RuntimeError("no sensor")):
                with self.assertRaisesRegex(RuntimeError, "no sensor"):
                    INA228Sensor()

        self.assertEqual(len(FakeBus.instances), 1)
        self.assertTrue(FakeBus.instances[0].closed)

    def test_close_is_idempotent(self):
        class FakeBus:
            def __init__(self):
                self.close_count = 0

            def close(self):
                self.close_count += 1

        sensor = INA228Sensor.__new__(INA228Sensor)
        bus = FakeBus()
        sensor._bus = bus

        sensor.close()
        sensor.close()

        self.assertEqual(bus.close_count, 1)
        self.assertIsNone(sensor._bus)


if __name__ == "__main__":
    unittest.main()
