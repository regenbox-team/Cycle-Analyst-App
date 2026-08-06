import base64
import os
import types
import sys
import tempfile
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

from app import photo_capture, state
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


class PhotoCaptureCameraCommandTest(unittest.TestCase):
    def setUp(self):
        self.previous_controls = os.environ.get("APP_CAMERA_V4L2_CTRLS")
        self.previous_which = photo_capture.shutil.which

    def tearDown(self):
        if self.previous_controls is None:
            os.environ.pop("APP_CAMERA_V4L2_CTRLS", None)
        else:
            os.environ["APP_CAMERA_V4L2_CTRLS"] = self.previous_controls
        photo_capture.shutil.which = self.previous_which

    def test_parse_v4l2_controls_accepts_commas_and_spaces(self):
        self.assertEqual(
            photo_capture._parse_v4l2_controls("auto_exposure=1, exposure_time_absolute=80 gain=0"),
            ["auto_exposure=1", "exposure_time_absolute=80", "gain=0"],
        )

    def test_resolve_v4l2_control_command_uses_capture_device(self):
        os.environ["APP_CAMERA_V4L2_CTRLS"] = "auto_exposure=1,exposure_time_absolute=80"
        photo_capture.shutil.which = lambda name: "/usr/bin/v4l2-ctl" if name == "v4l2-ctl" else None

        command = photo_capture._resolve_v4l2_control_command(["fswebcam", "-d", "/dev/video1", "out.jpg"])

        self.assertEqual(
            command,
            [
                "/usr/bin/v4l2-ctl",
                "-d",
                "/dev/video1",
                "--set-ctrl=auto_exposure=1",
                "--set-ctrl=exposure_time_absolute=80",
            ],
        )


class PhotoCaptureQueueTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_pending_dir = photo_capture.PENDING_PHOTO_DIR
        self.previous_upload = photo_capture.upload_photo_payload
        photo_capture.PENDING_PHOTO_DIR = self.temp_dir.name
        state.session_metrics["photo_capture"] = state.default_photo_capture_settings()

    def tearDown(self):
        photo_capture.upload_photo_payload = self.previous_upload
        photo_capture.PENDING_PHOTO_DIR = self.previous_pending_dir
        self.temp_dir.cleanup()

    def _queue_sample_photo(self):
        image_bytes = b"fake-jpeg-bytes"
        source = Path(self.temp_dir.name) / "source.jpg"
        source.write_bytes(image_bytes)
        payload = {
            "session_id": "session-before-network-loss",
            "captured_at": "2026-05-08 12:00:00",
            "filename": "source.jpg",
            "mime_type": "image/jpeg",
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        }
        photo_capture._queue_photo(str(source), payload)
        return image_bytes

    def test_failed_flush_keeps_photo_pending(self):
        self._queue_sample_photo()

        def fail_upload(payload):
            raise RuntimeError("network down")

        photo_capture.upload_photo_payload = fail_upload
        result = photo_capture.flush_pending_photo_uploads()

        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["remaining"], 1)
        self.assertIn("network down", result["last_error"])
        self.assertEqual(photo_capture.pending_photo_count(), 1)

    def test_successful_flush_sends_then_deletes_photo(self):
        image_bytes = self._queue_sample_photo()
        uploaded_payloads = []

        def upload(payload):
            uploaded_payloads.append(payload)
            return {"status": "ok", "public_latest_image_url": "https://monitor/latest.jpg"}

        photo_capture.upload_photo_payload = upload
        result = photo_capture.flush_pending_photo_uploads()

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(photo_capture.pending_photo_count(), 0)
        self.assertEqual(uploaded_payloads[0]["session_id"], "session-before-network-loss")
        self.assertEqual(
            uploaded_payloads[0]["image_b64"],
            base64.b64encode(image_bytes).decode("ascii"),
        )
        self.assertEqual(
            state.session_metrics["photo_capture"]["latest_public_url"],
            "https://monitor/latest.jpg",
        )


if __name__ == "__main__":
    unittest.main()
