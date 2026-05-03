import unittest
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import app.solar_range as solar_range
from app.solar_range import (
    build_estimate,
    initialize_solar_session,
    potential_solar_wh_remaining_today,
    select_estimation_voltage,
    soc_from_voltage,
    theoretical_solar_power_w,
)


class SolarRangeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_state_file = solar_range.SOLAR_BATTERY_STATE_FILE
        solar_range.SOLAR_BATTERY_STATE_FILE = f"{self._tmp.name}/solar_battery_state.json"

    def tearDown(self):
        solar_range.SOLAR_BATTERY_STATE_FILE = self._old_state_file
        self._tmp.cleanup()

    def test_voltage_curve_interpolates_soc(self):
        self.assertAlmostEqual(soc_from_voltage(48.6), 40.0)
        self.assertAlmostEqual(soc_from_voltage(48.95), 45.0)
        self.assertEqual(soc_from_voltage(30.0), 0.0)
        self.assertEqual(soc_from_voltage(60.0), 100.0)

    def test_prefers_solar_sensor_voltage_for_estimation(self):
        self.assertEqual(select_estimation_voltage(47.0, 49.3), (49.3, "solar_sensor"))
        self.assertEqual(select_estimation_voltage(47.0, 0.0), (47.0, "cycle_analyst"))

    def test_session_estimate_uses_virtual_solar_battery(self):
        metrics = {
            "solar_enabled": True,
            "positive_Wh": 300.0,
            "regen_Wh": 25.0,
            "human_Wh": 50.0,
            "solar_Wh": 75.0,
        }
        initialize_solar_session(metrics, voltage=49.3, capacity_ah=64)

        estimate = build_estimate(
            metrics,
            voltage=49.3,
            capacity_ah=64,
            solar_voltage=50.1,
            gps_state={"has_fix": False},
            when=datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("Europe/Paris")),
        )

        self.assertAlmostEqual(estimate["start_wh"], estimate["capacity_wh"] * 0.5)
        self.assertAlmostEqual(estimate["used_wh"], 150.0)
        self.assertAlmostEqual(estimate["remaining_wh"], estimate["start_wh"] - 150.0)
        self.assertEqual(estimate["voltage_source"], "solar_sensor")

    def test_theoretical_solar_power_drops_to_zero_at_night(self):
        noon = datetime(2026, 6, 21, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))
        night = datetime(2026, 6, 21, 23, 0, tzinfo=ZoneInfo("Europe/Paris"))

        self.assertGreater(theoretical_solar_power_w(noon, latitude=48.8566, longitude=2.3522, panel_max_w=400), 0)
        self.assertEqual(theoretical_solar_power_w(night, latitude=48.8566, longitude=2.3522, panel_max_w=400), 0)

    def test_remaining_solar_potential_decreases_late_in_day(self):
        morning = datetime(2026, 6, 21, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
        evening = datetime(2026, 6, 21, 19, 0, tzinfo=ZoneInfo("Europe/Paris"))

        self.assertGreater(
            potential_solar_wh_remaining_today(morning, latitude=48.8566, longitude=2.3522, panel_max_w=400),
            potential_solar_wh_remaining_today(evening, latitude=48.8566, longitude=2.3522, panel_max_w=400),
        )


if __name__ == "__main__":
    unittest.main()
