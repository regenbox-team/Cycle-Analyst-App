import unittest

from app.solar_filter import AdaptiveSolarFilter, SolarFilterConfig


class SolarFilterTest(unittest.TestCase):
    def test_smooths_stationary_noise(self):
        filt = AdaptiveSolarFilter(SolarFilterConfig(tau_seconds=4.0, median_window=5, output_deadband_a=0.0))
        raw_values = [5.0, 8.0, 4.0, 7.0, 5.0, 6.0, 4.5, 7.5, 5.5]

        filtered = [filt.update_current(value, dt_seconds=1.0) for value in raw_values]

        self.assertLess(max(filtered) - min(filtered), max(raw_values) - min(raw_values))
        self.assertAlmostEqual(filtered[-1], 5.6, delta=0.8)

    def test_median_window_rejects_single_sample_spike(self):
        filt = AdaptiveSolarFilter(SolarFilterConfig(tau_seconds=1.0, median_window=5, output_deadband_a=0.0))
        outputs = [filt.update_current(value, dt_seconds=1.0) for value in [5.0, 5.1, 4.9, 80.0, 5.0]]

        self.assertLess(outputs[3], 20.0)
        self.assertLess(outputs[4], 10.0)

    def test_fast_tau_tracks_real_step(self):
        filt = AdaptiveSolarFilter(
            SolarFilterConfig(
                tau_seconds=8.0,
                fast_tau_seconds=0.5,
                median_window=1,
                jump_threshold_a=2.0,
                output_deadband_a=0.0,
            )
        )
        filt.update_current(1.0, dt_seconds=1.0)
        jumped = filt.update_current(8.0, dt_seconds=1.0)

        self.assertGreater(jumped, 6.0)

    def test_can_be_disabled(self):
        filt = AdaptiveSolarFilter(SolarFilterConfig(enabled=False, output_deadband_a=0.0))

        self.assertEqual(filt.update_current(12.3, dt_seconds=1.0), 12.3)


if __name__ == "__main__":
    unittest.main()
