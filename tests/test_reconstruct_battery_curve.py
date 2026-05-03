import unittest
from datetime import datetime, timedelta

from scripts.reconstruct_battery_curve import Sample, build_curve, stable_rest_points


class ReconstructBatteryCurveTest(unittest.TestCase):
    def test_extracts_stable_rest_points_and_curve(self):
        samples = []
        start = datetime(2026, 1, 1, 8, 0, 0)
        ah = 0.0
        voltage = 54.0
        index = 0
        for _ in range(6):
            for _ in range(130):
                samples.append(
                    Sample(
                        session="s1",
                        timestamp=start + timedelta(seconds=index),
                        raw_timestamp=(start + timedelta(seconds=index)).isoformat(),
                        ah=ah,
                        voltage=voltage,
                        current_a=0.2,
                        speed_kph=0.0,
                    )
                )
                index += 1
            ah += 8.0
            voltage -= 1.2
            for _ in range(30):
                samples.append(
                    Sample(
                        session="s1",
                        timestamp=start + timedelta(seconds=index),
                        raw_timestamp=(start + timedelta(seconds=index)).isoformat(),
                        ah=ah,
                        voltage=voltage,
                        current_a=10.0,
                        speed_kph=25.0,
                    )
                )
                index += 1

        points = stable_rest_points(
            samples,
            capacity_ah=64.0,
            max_speed_kph=1.0,
            max_abs_current_a=1.5,
            min_rest_seconds=120.0,
            tail_seconds=60.0,
            max_voltage_std=0.05,
            min_samples=20,
            fallback_hz=1.0,
        )
        curve = build_curve(points, bin_percent=5.0, min_points_per_bin=1)

        self.assertGreaterEqual(len(points), 5)
        self.assertGreaterEqual(len(curve), 5)
        self.assertEqual(curve[-1]["soc"], 100.0)
        self.assertGreater(curve[-1]["voltage"], curve[0]["voltage"])


if __name__ == "__main__":
    unittest.main()
