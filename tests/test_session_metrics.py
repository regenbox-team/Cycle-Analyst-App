import types
import sys
import unittest

# Stub out Flask and serial modules for testing without external deps
flask_stub = types.ModuleType("flask")
def _dummy(*a, **k):
    return None

class DummyApp:
    def route(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def run(self, *args, **kwargs):
        pass

flask_stub.Flask = lambda *a, **k: DummyApp()
flask_stub.render_template = _dummy
flask_stub.jsonify = _dummy
flask_stub.request = None
flask_stub.redirect = _dummy
sys.modules['flask'] = flask_stub

serial_stub = types.ModuleType("serial")
class DummySerial:
    def __init__(self, *a, **k):
        pass
    def readline(self):
        return b""
serial_stub.Serial = DummySerial
serial_stub.SerialException = Exception
sys.modules['serial'] = serial_stub

import cycle_server


class SessionMetricsTest(unittest.TestCase):
    def setUp(self):
        cycle_server.reset_session_state()
        if hasattr(cycle_server.update_metrics, "last_time"):
            delattr(cycle_server.update_metrics, "last_time")

    def test_reset_seeds_last_km_checkpoint(self):
        self.assertEqual(cycle_server.session_metrics["last_km_checkpoints"], [0])

    def test_distribution_on_distance_jump(self):
        sm = cycle_server.session_metrics

        sm["positive_Wh"] = 0
        data = [0] * 15
        data[1] = 50  # voltage
        data[2] = 0   # current
        data[3] = 0   # speed
        data[4] = 1.0 # raw distance from CA (non-reset)
        data[5] = 25  # temp
        data[13] = 0  # solar current
        data[14] = "2B"
        cycle_server.update_metrics(data, now=0)

        sm["positive_Wh"] = 300
        data[4] = 4.0
        cycle_server.update_metrics(data, now=1)

        self.assertEqual(sm["last_km_checkpoints"], [0, 100, 200, 300])
        self.assertEqual(sm["Wh_per_km_last"], [100, 100, 100])
        self.assertEqual(sm["net_Wh_per_km_last"], [100, 100, 100])

    def test_detects_ca_reset(self):
        sm = cycle_server.session_metrics
        base = [0] * 15
        base[1] = 50  # voltage
        base[2] = 0   # current
        base[3] = 0   # speed
        base[5] = 25  # temp
        base[13] = 0  # solar current
        base[14] = "2B"

        data = base.copy()
        data[4] = 5.0
        cycle_server.update_metrics(data, now=0)

        data[4] = 6.0
        cycle_server.update_metrics(data, now=1)
        self.assertAlmostEqual(sm["distance_km"], 1.0)

        data[4] = 0.2  # CA reset
        cycle_server.update_metrics(data, now=2)
        self.assertTrue(sm["ca_reset_detected"])
        self.assertAlmostEqual(sm["distance_km"], 1.0)

        data[4] = 1.2
        cycle_server.update_metrics(data, now=3)
        self.assertAlmostEqual(sm["distance_km"], 2.0)

    def test_ca_reset_prompt_high_voltage(self):
        sm = cycle_server.session_metrics
        base = [0] * 15
        base[1] = 54.0  # fully charged voltage
        base[2] = 0
        base[3] = 0
        base[5] = 25
        base[13] = 0
        base[14] = "2B"

        data = base.copy()
        data[4] = 5.0
        cycle_server.update_metrics(data, now=0)
        self.assertTrue(sm.get("ca_reset_prompt"))

        data[4] = 0.0
        cycle_server.update_metrics(data, now=1)
        self.assertFalse(sm.get("ca_reset_prompt"))


if __name__ == "__main__":
    unittest.main()
