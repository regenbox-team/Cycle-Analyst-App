import os
import unittest
from unittest.mock import patch

from app.reader import corrected_motor_current
from app.solar_sensor import sensor_enabled


class CorrectedMotorCurrentTest(unittest.TestCase):
    def test_subtracts_solar_and_generator_from_bus_sensor(self):
        self.assertEqual(corrected_motor_current(25.0, 4.0, 2.0), 19.0)

    def test_preserves_signed_currents(self):
        self.assertEqual(corrected_motor_current(-5.0, 1.0, 0.5), -6.5)


class MotorSensorEnableTest(unittest.TestCase):
    def test_motor_sensor_is_disabled_when_setting_is_absent(self):
        env = dict(os.environ)
        env.pop("APP_MOTOR_SENSOR", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(sensor_enabled("APP_MOTOR"))

    def test_motor_sensor_can_be_enabled_independently(self):
        with patch.dict(os.environ, {"APP_MOTOR_SENSOR": "ina228"}, clear=True):
            self.assertTrue(sensor_enabled("APP_MOTOR"))
            self.assertFalse(sensor_enabled("APP_SOLAR"))


if __name__ == "__main__":
    unittest.main()
