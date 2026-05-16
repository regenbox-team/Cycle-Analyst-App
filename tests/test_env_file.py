import os
import tempfile
import unittest
from pathlib import Path
from werkzeug.datastructures import MultiDict

from app import env_file


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cycle-analyst.env"
        self.previous_app_var_dir = os.environ.get("APP_VAR_DIR")
        self.previous_app_env_file = os.environ.get("APP_ENV_FILE")
        os.environ.pop("APP_VAR_DIR", None)
        os.environ["APP_ENV_FILE"] = str(self.path)

    def tearDown(self):
        if self.previous_app_var_dir is None:
            os.environ.pop("APP_VAR_DIR", None)
        else:
            os.environ["APP_VAR_DIR"] = self.previous_app_var_dir
        if self.previous_app_env_file is None:
            os.environ.pop("APP_ENV_FILE", None)
        else:
            os.environ["APP_ENV_FILE"] = self.previous_app_env_file
        self.tmp.cleanup()

    def test_ensure_env_file_writes_known_defaults(self):
        env_file.ensure_env_file(self.path)

        text = self.path.read_text(encoding="utf-8")

        self.assertIn("APP_VAR_DIR=var", text)
        self.assertIn("# APP_SOLAR_SENSOR=ina228", text)
        self.assertIn("MONITOR_UPLOAD_CHUNK_SIZE=1000", text)
        self.assertIn("MONITOR_UPLOAD_CHUNK_MAX_BYTES=262144", text)
        self.assertIn("MONITOR_UPLOAD_GZIP=1", text)

    def test_parse_disabled_and_enabled_values(self):
        self.path.write_text(
            "APP_VAR_DIR=var\n# APP_SOLAR_SENSOR=ina228\nAPP_CAMERA_COMMAND=fswebcam -q {output}\n",
            encoding="utf-8",
        )

        parsed = env_file.parse_env_file(self.path)

        self.assertEqual(parsed["APP_VAR_DIR"], {"value": "var", "enabled": True})
        self.assertEqual(parsed["APP_SOLAR_SENSOR"], {"value": "ina228", "enabled": False})
        self.assertEqual(parsed["APP_CAMERA_COMMAND"]["value"], "fswebcam -q {output}")

    def test_load_env_file_does_not_override_existing_process_env(self):
        self.path.write_text("APP_VAR_DIR=from_file\n", encoding="utf-8")
        os.environ["APP_VAR_DIR"] = "from_process"

        env_file.load_env_file(self.path)

        self.assertEqual(os.environ["APP_VAR_DIR"], "from_process")

    def test_parse_camera_command_into_fields(self):
        parts = env_file.parse_camera_command(
            "fswebcam -d /dev/video1 -q -S 30 --palette YUYV -r 800x600 --jpeg 90 --no-banner {output}"
        )

        self.assertEqual(parts["program"], "fswebcam")
        self.assertEqual(parts["device"], "/dev/video1")
        self.assertEqual(parts["skip_frames"], "30")
        self.assertEqual(parts["palette"], "YUYV")
        self.assertEqual(parts["resolution"], "800x600")
        self.assertEqual(parts["jpeg_quality"], "90")
        self.assertEqual(parts["quiet"], "1")
        self.assertEqual(parts["no_banner"], "1")

    def test_compose_camera_command_from_fields(self):
        command = env_file.compose_camera_command(
            MultiDict(
                {
                    "APP_CAMERA_COMMAND__program": "fswebcam",
                    "APP_CAMERA_COMMAND__device": "/dev/video0",
                    "APP_CAMERA_COMMAND__quiet": "1",
                    "APP_CAMERA_COMMAND__skip_frames": "60",
                    "APP_CAMERA_COMMAND__palette": "YUYV",
                    "APP_CAMERA_COMMAND__resolution": "640x480",
                    "APP_CAMERA_COMMAND__jpeg_quality": "85",
                    "APP_CAMERA_COMMAND__no_banner": "1",
                    "APP_CAMERA_COMMAND__extra_args": "--rotate 180",
                }
            )
        )

        self.assertEqual(
            command,
            "fswebcam -d /dev/video0 -q -S 60 --palette YUYV -r 640x480 --jpeg 85 --no-banner --rotate 180 {output}",
        )

    def test_grouped_settings_exposes_select_choices_and_details(self):
        env_file.ensure_env_file(self.path)

        groups = env_file.grouped_settings()
        flat = {
            setting["key"]: setting
            for group in groups
            for setting in group["settings"]
        }

        self.assertGreaterEqual(len(flat["APP_GPS_BAUDRATE"]["choices"]), 2)
        self.assertIn("detail", flat["APP_GPS_BAUDRATE"])
        self.assertIn("camera_choices", flat["APP_CAMERA_COMMAND"])
        self.assertGreaterEqual(len(flat["APP_CAMERA_COMMAND"]["camera_choices"]["resolution"]), 2)


if __name__ == "__main__":
    unittest.main()
