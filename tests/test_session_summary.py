import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.session_summary import build_summary_sections, compute_session_metrics


def raw_line(*, ah=0.0, amps=10.0, speed=20.0, distance=0.0, human_amps=1.0):
    values = [0.0] * 15
    values[0] = ah
    values[1] = 50.0
    values[2] = amps
    values[3] = speed
    values[4] = distance
    values[5] = 25.0
    values[13] = human_amps
    values[14] = "2B"
    return " ".join(str(v) for v in values)


class SessionSummaryTest(unittest.TestCase):
    def test_computes_motor_sensor_comparison_and_shows_section(self):
        samples = [
            {
                "timestamp": "2026-04-30T10:00:00",
                "raw": raw_line(amps=10.0),
                "motor_sensor_bus_v": 51.0,
                "motor_corrected_current_a": 11.0,
                "motor_sensor_valid": 1,
            },
            {
                "timestamp": "2026-04-30T10:00:01",
                "raw": raw_line(amps=14.0),
                "motor_sensor_bus_v": 49.0,
                "motor_corrected_current_a": 13.0,
                "motor_sensor_valid": 1,
            },
        ]

        metrics = compute_session_metrics(samples)
        sections = build_summary_sections({"Total": metrics}, ["Total"])

        self.assertEqual(metrics["motor_sensor_samples"], 2)
        self.assertAlmostEqual(metrics["motor_sensor_current_sum"] / 2, 12.0)
        self.assertAlmostEqual(metrics["motor_ca_current_sum"] / 2, 12.0)
        self.assertAlmostEqual(metrics["motor_current_delta_max"], 1.0)
        self.assertIn("Motor sensor comparison", [section["category"] for section in sections])

    def test_hides_motor_sensor_section_without_valid_samples(self):
        metrics = compute_session_metrics([
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(), "motor_sensor_valid": 0}
        ])
        sections = build_summary_sections({"Total": metrics}, ["Total"])
        self.assertNotIn("Motor sensor comparison", [section["category"] for section in sections])

    def test_computes_gps_and_solar_observations(self):
        samples = [
            {
                "timestamp": "2026-04-30T10:00:00",
                "raw": raw_line(distance=0.0),
                "gps_lat": 48.8566,
                "gps_lon": 2.3522,
                "gps_alt": 30,
                "gps_speed_kph": 18,
                "gps_fix": 1,
                "gps_sats": 8,
                "gps_hdop": 0.9,
                "solar_current_a": 2,
                "solar_bus_v": 20,
                "solar_power_w": 40,
            },
            {
                "timestamp": "2026-04-30T10:00:01",
                "raw": raw_line(distance=0.1),
                "gps_lat": 48.8576,
                "gps_lon": 2.3522,
                "gps_alt": 36,
                "gps_speed_kph": 19,
                "gps_fix": 1,
                "gps_sats": 10,
                "gps_hdop": 0.8,
                "solar_current_a": 2,
                "solar_bus_v": 20,
                "solar_power_w": 40,
            },
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["distance"], 0.1)
        self.assertGreater(metrics["gps_distance_km"], 0.1)
        self.assertAlmostEqual(metrics["gps_uphill_m"], 6.0)
        self.assertAlmostEqual(metrics["raw_gps_uphill_m"], 6.0)
        self.assertEqual(metrics["gps_points"], 2)
        self.assertEqual(metrics["gps_fix_count"], 2)
        self.assertAlmostEqual(metrics["solar_Wh"], 40 / 3600)
        self.assertAlmostEqual(metrics["solar_power_max"], 40)

    def test_rejects_implausible_gps_distance_jumps(self):
        samples = [
            {
                "timestamp": "2026-04-30T10:00:00.000000",
                "gps_lat": 48.0000,
                "gps_lon": 2.0000,
            },
            {
                "timestamp": "2026-04-30T10:00:01.000000",
                "gps_lat": 48.0001,
                "gps_lon": 2.0000,
            },
            {
                "timestamp": "2026-04-30T10:00:01.500000",
                "gps_lat": 48.0100,
                "gps_lon": 2.0000,
            },
            {
                "timestamp": "2026-04-30T10:00:02.000000",
                "gps_lat": 48.0002,
                "gps_lon": 2.0000,
            },
        ]

        metrics = compute_session_metrics(samples)

        self.assertLess(metrics["gps_distance_km"], 0.03)
        self.assertEqual(metrics["gps_distance_rejected_count"], 1)

    def test_ignores_energy_across_large_timestamp_gap(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(distance=0.0)},
            {"timestamp": "2026-04-30T10:10:00", "raw": raw_line(distance=0.1)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertEqual(metrics["positive_Wh"], 0)
        self.assertEqual(metrics["solar_Wh"], 0)

    def test_uses_cycle_analyst_raw_ah_across_logging_gaps(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(ah=12.5, amps=10.0, distance=0.0)},
            {"timestamp": "2026-04-30T10:30:00", "raw": raw_line(ah=16.75, amps=10.0, distance=12.0)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertEqual(metrics["Ah"], 0)
        self.assertAlmostEqual(metrics["ca_Ah_raw"], 4.25)

    def test_rejects_isolated_high_cycle_analyst_ah_spike(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(ah=9.45)},
            {"timestamp": "2026-04-30T10:00:01", "raw": raw_line(ah=95122.0)},
            {"timestamp": "2026-04-30T10:00:02", "raw": raw_line(ah=9.51)},
            {"timestamp": "2026-04-30T10:00:03", "raw": raw_line(ah=9.61)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["ca_Ah_raw"], 0.16)

    def test_rejects_recovery_from_isolated_low_cycle_analyst_ah_spike(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(ah=34.8)},
            {"timestamp": "2026-04-30T10:00:01", "raw": raw_line(ah=3.48)},
            {"timestamp": "2026-04-30T10:00:02", "raw": raw_line(ah=34.9)},
            {"timestamp": "2026-04-30T10:00:03", "raw": raw_line(ah=35.0)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["ca_Ah_raw"], 0.2)

    def test_keeps_regen_out_of_gross_cycle_analyst_ah(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(ah=10.0)},
            {"timestamp": "2026-04-30T10:00:01", "raw": raw_line(ah=11.0)},
            {"timestamp": "2026-04-30T10:00:02", "raw": raw_line(ah=10.5)},
            {"timestamp": "2026-04-30T10:00:03", "raw": raw_line(ah=11.5)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["ca_Ah_raw"], 2.0)

    def test_keeps_solar_only_samples_without_cycle_analyst_raw(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "solar_power_w": 30},
            {"timestamp": "2026-04-30T10:00:01", "solar_power_w": 30},
        ]

        metrics = compute_session_metrics(samples)

        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["solar_samples"], 2)
        self.assertAlmostEqual(metrics["solar_Wh"], 30 / 3600)
        self.assertAlmostEqual(metrics["solar_power_max"], 30)

    def test_tracks_cycle_analyst_distance_reset(self):
        samples = [
            {"timestamp": "2026-04-30T10:00:00", "raw": raw_line(distance=5.0)},
            {"timestamp": "2026-04-30T10:00:01", "raw": raw_line(distance=6.0)},
            {"timestamp": "2026-04-30T10:00:02", "raw": raw_line(distance=0.2)},
            {"timestamp": "2026-04-30T10:00:03", "raw": raw_line(distance=1.2)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertEqual(metrics["ca_reset_count"], 1)
        self.assertAlmostEqual(metrics["distance"], 2.0)

    def test_ignores_isolated_cycle_analyst_distance_spikes(self):
        samples = [
            {"timestamp": "2026-05-04T12:14:02", "raw": raw_line(distance=33.8)},
            {"timestamp": "2026-05-04T12:14:03", "raw": raw_line(distance=33.909)},
            {"timestamp": "2026-05-04T12:14:04", "raw": raw_line(distance=3946.0)},
            {"timestamp": "2026-05-04T12:14:05", "raw": raw_line(distance=33.953)},
            {"timestamp": "2026-05-04T12:16:00", "raw": raw_line(distance=34.652)},
            {"timestamp": "2026-05-04T12:16:01", "raw": raw_line(distance=346.0)},
            {"timestamp": "2026-05-04T12:16:02", "raw": raw_line(distance=34.692)},
            {"timestamp": "2026-05-04T16:20:05", "raw": raw_line(distance=103.18)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertEqual(metrics["ca_reset_count"], 0)
        self.assertAlmostEqual(metrics["distance"], 103.18 - 33.8)
        self.assertEqual(metrics["distance_glitch_count"], 2)

    def test_sorts_samples_before_computing_cycle_analyst_distance(self):
        samples = [
            {"timestamp": "2026-06-02T07:00:00", "raw": raw_line(distance=0.0)},
            {"timestamp": "2026-06-02T14:00:00", "raw": raw_line(distance=100.0)},
            {"timestamp": "2026-06-02T07:00:01", "raw": raw_line(distance=1.0)},
            {"timestamp": "2026-06-02T14:00:01", "raw": raw_line(distance=101.0)},
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["distance"], 101.0)
        self.assertEqual(metrics["ca_reset_count"], 0)

    def test_filters_small_gps_altitude_noise_from_uphill(self):
        samples = [
            {
                "timestamp": f"2026-04-30T10:00:0{i}",
                "raw": raw_line(distance=i * 0.1),
                "gps_lat": 48.8566 + i * 0.0001,
                "gps_lon": 2.3522,
                "gps_alt": alt,
            }
            for i, alt in enumerate([100.0, 101.2, 100.4, 101.5, 100.8, 106.0])
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["gps_uphill_m"], 6.0)

    def test_prefers_terrain_altitude_for_uphill_when_available(self):
        samples = [
            {
                "timestamp": f"2026-04-30T10:00:0{i}",
                "raw": raw_line(distance=i * 0.1),
                "gps_lat": 48.8566 + i * 0.0001,
                "gps_lon": 2.3522,
                "gps_alt": gps_alt,
                "terrain_alt_m": terrain_alt,
            }
            for i, (gps_alt, terrain_alt) in enumerate([(100.0, 30.0), (180.0, 36.0), (90.0, 42.0)])
        ]

        metrics = compute_session_metrics(samples)

        self.assertAlmostEqual(metrics["gps_uphill_m"], 12.0)
        self.assertAlmostEqual(metrics["raw_gps_uphill_m"], 80.0)
        self.assertAlmostEqual(metrics["raw_gps_downhill_m"], 90.0)


if __name__ == "__main__":
    unittest.main()
