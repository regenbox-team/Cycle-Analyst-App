import gzip
import json
import os
import unittest
from unittest.mock import patch

from app import monitor_client


class MonitorClientUploadTest(unittest.TestCase):
    def tearDown(self):
        for key in (
            "MONITOR_UPLOAD_CHUNK_MAX_BYTES",
            "MONITOR_UPLOAD_GZIP_MIN_BYTES",
            "MONITOR_USER",
            "MONITOR_PASS",
        ):
            os.environ.pop(key, None)

    def sample(self, index: int, raw_size: int = 400) -> dict:
        return {
            "timestamp": f"2026-05-07 10:00:{index:02d}",
            "raw": "x" * raw_size,
            "gps_lat": 48.0,
            "gps_lon": 2.0,
        }

    def test_split_upload_samples_honors_json_byte_limit(self):
        samples = [self.sample(0), self.sample(1), self.sample(2)]
        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 1.0},
            "telemetry_samples": samples,
        }
        one_sample_bytes = monitor_client._chunk_payload_size(payload, [samples[0]])
        two_sample_bytes = monitor_client._chunk_payload_size(payload, samples[:2])

        chunks = monitor_client._split_upload_samples(
            payload,
            samples,
            chunk_size=1000,
            max_bytes=two_sample_bytes - 1,
        )

        self.assertLessEqual(one_sample_bytes, two_sample_bytes - 1)
        self.assertEqual([len(chunk) for chunk in chunks], [1, 1, 1])

    def test_request_json_can_send_gzipped_payload(self):
        os.environ["MONITOR_UPLOAD_GZIP_MIN_BYTES"] = "0"
        payload = {"telemetry_samples": [{"raw": "x" * 5000}]}

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"status":"ok"}'

        with patch("urllib.request.urlopen", return_value=DummyResponse()) as urlopen:
            response = monitor_client._request_json(
                "POST",
                "http://monitor.test/api/upload_session_chunk",
                payload,
                gzip_payload=True,
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(response["status"], "ok")
        self.assertEqual(request.get_header("Content-encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(request.data).decode("utf-8")), payload)


if __name__ == "__main__":
    unittest.main()
