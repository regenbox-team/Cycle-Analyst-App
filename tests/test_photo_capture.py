import types
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

flask_stub = types.ModuleType("flask")


def _dummy(*args, **kwargs):
    return None


class DummyApp:
    def add_url_rule(self, *args, **kwargs):
        pass

    def register_blueprint(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def run(self, *args, **kwargs):
        pass


class DummyBlueprint:
    def __init__(self, *args, **kwargs):
        pass

    def add_url_rule(self, *args, **kwargs):
        pass


flask_stub.Flask = lambda *args, **kwargs: DummyApp()
flask_stub.Blueprint = DummyBlueprint
flask_stub.Response = _dummy
flask_stub.abort = _dummy
flask_stub.render_template = _dummy
flask_stub.jsonify = _dummy
flask_stub.request = None
flask_stub.redirect = _dummy
flask_stub.send_file = _dummy
flask_stub.send_from_directory = _dummy
flask_stub.url_for = _dummy
sys.modules["flask"] = flask_stub

serial_stub = types.ModuleType("serial")


class DummySerial:
    def __init__(self, *args, **kwargs):
        pass

    def readline(self):
        return b""


serial_stub.Serial = DummySerial
serial_stub.SerialException = Exception
sys.modules["serial"] = serial_stub

from app.photo_capture import next_capture_distance, normalize_interval_km


class PhotoCaptureMathTest(unittest.TestCase):
    def test_normalize_interval_bounds(self):
        self.assertEqual(normalize_interval_km("bad"), 1.0)
        self.assertEqual(normalize_interval_km("0"), 0.1)
        self.assertEqual(normalize_interval_km("2500"), 1000.0)

    def test_no_trigger_before_threshold(self):
        self.assertIsNone(next_capture_distance(0.99, 0.0, 1.0))

    def test_trigger_on_crossing_threshold(self):
        self.assertEqual(next_capture_distance(1.01, 0.0, 1.0), 1.0)

    def test_multiple_intervals_collapse_to_latest_crossed_step(self):
        self.assertEqual(next_capture_distance(2.26, 1.0, 0.5), 2.0)


if __name__ == "__main__":
    unittest.main()
