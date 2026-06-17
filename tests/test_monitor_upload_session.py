import gzip
import json
import os
import shutil
import sqlite3
import unittest


class MonitorUploadSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = os.path.abspath(os.path.join("tests", "monitor_upload_tmp", self._testMethodName))
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.db_path = os.path.join(self.tmp_dir, "monitor.db")
        os.environ["MONITOR_DB"] = self.db_path
        os.environ["MONITOR_USER"] = "admin"
        os.environ["MONITOR_PASS"] = "secret"
        os.environ["MONITOR_TERRAIN_ELEVATION_ENABLED"] = "0"

        import monitor_server.app as monitor_app

        self.monitor_app = monitor_app
        self.monitor_app._init_db()

    def tearDown(self):
        for key in (
            "MONITOR_DB",
            "MONITOR_USER",
            "MONITOR_PASS",
            "MONITOR_TERRAIN_ELEVATION_ENABLED",
            "MONITOR_UPLOAD_CHUNK_MAX_BYTES",
            "MONITOR_RESPONSE_GZIP",
            "MONITOR_RESPONSE_GZIP_MIN_BYTES",
        ):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def sample(self, index):
        return {
            "timestamp": f"2026-05-07 10:00:0{index}",
            "raw": f"0 52 1 10 {index}.0 20 0 0 0 0 0 0 0 0 flags",
            "user": "JD",
            "user_id": "JD",
            "gps_lat": 48.0 + index * 0.001,
            "gps_lon": 2.0,
            "gps_alt": 100 + index,
            "solar_enabled": 1,
        }

    def test_heartbeat_updates_device_last_seen_with_server_time(self):
        with sqlite3.connect(self.db_path) as conn:
            payload = self.monitor_app._record_heartbeat(
                conn,
                {
                    "device_id": "bike",
                    "timestamp": "2000-01-01 00:00:00",
                    "session_active": 1,
                    "mode": "supercycle_live",
                },
                "127.0.0.1",
            )
            conn.commit()

            row = conn.execute(
                "SELECT last_seen, session_active, mode FROM devices WHERE device_id = ?",
                ("bike",),
            ).fetchone()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["device_id"], "bike")
        self.assertIn("last_seen", payload)
        self.assertNotEqual(payload["last_seen"], "2000-01-01 00:00:00")
        self.assertEqual(row[0], payload["last_seen"])
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "supercycle_live")

    def test_heartbeat_requires_device_id(self):
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(ValueError):
                self.monitor_app._record_heartbeat(
                    conn,
                    {
                        "timestamp": "2000-01-01 00:00:00",
                        "session_active": 1,
                    },
                    "127.0.0.1",
                )

    def test_heartbeat_route_returns_server_last_seen_when_flask_client_is_available(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        token = "Basic YWRtaW46c2VjcmV0"
        response = self.monitor_app.app.test_client().post(
            "/api/heartbeat",
            json={
                "device_id": "bike",
                "timestamp": "2000-01-01 00:00:00",
                "session_active": 1,
                "mode": "supercycle_live",
            },
            headers={"Authorization": token},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["device_id"], "bike")
        self.assertIn("last_seen", payload)
        self.assertNotEqual(payload["last_seen"], "2000-01-01 00:00:00")

    def test_chunked_upload_finalizes_session_summary(self):
        base_payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
            "total_chunks": 2,
            "total_rows": 3,
        }
        first_chunk = [self.sample(0), self.sample(1)]
        second_chunk = [self.sample(2)]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.monitor_app._insert_telemetry_samples(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
                first_chunk,
                1,
            )
            self.monitor_app._insert_telemetry_samples(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
                second_chunk,
                1,
            )
            all_samples = self.monitor_app._telemetry_samples_for_session(
                conn,
                base_payload["device_id"],
                base_payload["session_id"],
                base_payload["mode"],
            )
            summary = self.monitor_app._insert_uploaded_session_summary(conn, base_payload, all_samples)
            self.monitor_app._upsert_upload_device(conn, base_payload, 1, "127.0.0.1")
            conn.commit()

        self.assertEqual(summary["rows_count"], 3)

        with sqlite3.connect(self.db_path) as conn:
            session = conn.execute(
                """
                SELECT rows_count, distance_km
                FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                ("bike", "2026-05-07_10-00-00", "supercycle_live"),
            ).fetchone()
            sample_count = conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]

        self.assertEqual(session[0], 3)
        self.assertAlmostEqual(session[1], 2.0)
        self.assertEqual(sample_count, 3)

    def test_export_session_downloads_full_json_payload(self):
        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
        }
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.monitor_app._insert_telemetry_samples(
                conn,
                payload["device_id"],
                payload["session_id"],
                payload["mode"],
                [self.sample(0), self.sample(1)],
                1,
            )
            samples = self.monitor_app._telemetry_samples_for_session(
                conn,
                payload["device_id"],
                payload["session_id"],
                payload["mode"],
            )
            self.monitor_app._insert_uploaded_session_summary(conn, payload, samples)
            conn.execute(
                """
                INSERT INTO photos (
                    device_id, session_id, mode, captured_at, relative_path, uploaded_at,
                    is_public, gps_lat, gps_lon, solar_enabled, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bike",
                    "2026-05-07_10-00-00",
                    "supercycle_live",
                    "2026-05-07 10:00:00",
                    "photos\\bike\\frame.jpg",
                    "2026-05-07 10:01:00",
                    1,
                    48.0,
                    2.0,
                    1,
                    '{"distance_km": 2.0}',
                ),
            )
            conn.commit()

        token = "Basic YWRtaW46c2VjcmV0"
        response = self.monitor_app.app.test_client().get(
            "/api/export_session?device_id=bike&session_id=2026-05-07_10-00-00&mode=supercycle_live",
            headers={"Authorization": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn("bike_2026-05-07_10-00-00_supercycle_live.json", response.headers["Content-Disposition"])
        exported = response.get_json()
        self.assertEqual(exported["device_id"], "bike")
        self.assertEqual(len(exported["telemetry_samples"]), 2)
        self.assertEqual(len(exported["photos"]), 1)
        self.assertEqual(exported["photos"][0]["relative_path"], "photos/bike/frame.jpg")

    def test_known_sessions_excludes_deleted_sessions_from_uploaded_list(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
            "telemetry_samples": [self.sample(0), self.sample(1)],
        }
        token = "Basic YWRtaW46c2VjcmV0"
        client = self.monitor_app.app.test_client()
        upload = client.post("/api/upload_session", json=payload, headers={"Authorization": token})
        self.assertEqual(upload.status_code, 200)

        deleted = self.monitor_app._delete_session_data(
            payload["device_id"],
            payload["session_id"],
            payload["mode"],
        )
        self.assertEqual(deleted["deleted_sessions"], 1)

        response = client.get(
            "/api/known_sessions?device_id=bike&mode=supercycle_live",
            headers={"Authorization": token},
        )

        self.assertEqual(response.status_code, 200)
        known = response.get_json()
        self.assertNotIn(payload["session_id"], known["sessions"])
        self.assertIn(payload["session_id"], known["deleted_sessions"])

    def test_deleted_session_can_be_uploaded_again_and_clears_tombstone(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
            "telemetry_samples": [self.sample(0), self.sample(1)],
        }
        token = "Basic YWRtaW46c2VjcmV0"
        client = self.monitor_app.app.test_client()
        first_upload = client.post("/api/upload_session", json=payload, headers={"Authorization": token})
        self.assertEqual(first_upload.status_code, 200)

        self.monitor_app._delete_session_data(payload["device_id"], payload["session_id"], payload["mode"])
        second_upload = client.post("/api/upload_session", json=payload, headers={"Authorization": token})

        self.assertEqual(second_upload.status_code, 200)
        self.assertEqual(second_upload.get_json()["status"], "ok")
        with sqlite3.connect(self.db_path) as conn:
            session_count = conn.execute(
                """
                SELECT COUNT(*) FROM sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (payload["device_id"], payload["session_id"], payload["mode"]),
            ).fetchone()[0]
            tombstone_count = conn.execute(
                """
                SELECT COUNT(*) FROM deleted_sessions
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                (payload["device_id"], payload["session_id"], payload["mode"]),
            ).fetchone()[0]

        self.assertEqual(session_count, 1)
        self.assertEqual(tombstone_count, 0)

    def test_deleted_session_can_be_uploaded_again_in_chunks(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 2.0, "solar_enabled": True},
            "solar_enabled": 1,
            "telemetry_samples": [self.sample(0), self.sample(1)],
        }
        token = "Basic YWRtaW46c2VjcmV0"
        client = self.monitor_app.app.test_client()
        first_upload = client.post("/api/upload_session", json=payload, headers={"Authorization": token})
        self.assertEqual(first_upload.status_code, 200)

        self.monitor_app._delete_session_data(payload["device_id"], payload["session_id"], payload["mode"])
        first_chunk = dict(payload, telemetry_samples=[self.sample(0)], chunk_index=0, total_chunks=2)
        second_chunk = dict(payload, telemetry_samples=[self.sample(1)], chunk_index=1, total_chunks=2, final=True)

        chunk_response = client.post("/api/upload_session_chunk", json=first_chunk, headers={"Authorization": token})
        self.assertEqual(chunk_response.status_code, 200)
        self.assertEqual(chunk_response.get_json()["status"], "ok")
        final_response = client.post("/api/upload_session_chunk", json=second_chunk, headers={"Authorization": token})

        self.assertEqual(final_response.status_code, 200)
        self.assertEqual(final_response.get_json()["status"], "ok")
        with sqlite3.connect(self.db_path) as conn:
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            sample_count = conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
            tombstone_count = conn.execute("SELECT COUNT(*) FROM deleted_sessions").fetchone()[0]

        self.assertEqual(session_count, 1)
        self.assertEqual(sample_count, 2)
        self.assertEqual(tombstone_count, 0)

    def test_chunked_upload_accepts_gzipped_json(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "metrics": {"distance_km": 1.0, "solar_enabled": True},
            "solar_enabled": 1,
            "telemetry_samples": [self.sample(0)],
            "chunk_index": 0,
            "total_chunks": 1,
            "final": True,
        }
        raw = json.dumps(payload).encode("utf-8")
        response = self.monitor_app.app.test_client().post(
            "/api/upload_session_chunk",
            data=gzip.compress(raw),
            headers={
                "Authorization": "Basic YWRtaW46c2VjcmV0",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)

    def test_chunked_upload_rejects_oversized_uncompressed_json(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        os.environ["MONITOR_UPLOAD_CHUNK_MAX_BYTES"] = "16384"
        payload = {
            "device_id": "bike",
            "session_id": "2026-05-07_10-00-00",
            "mode": "supercycle_live",
            "telemetry_samples": [dict(self.sample(0), raw="x" * 20000)],
            "chunk_index": 0,
            "total_chunks": 1,
        }
        response = self.monitor_app.app.test_client().post(
            "/api/upload_session_chunk",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Basic YWRtaW46c2VjcmV0",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["error"], "request body too large")

    def test_text_responses_can_be_gzipped(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        os.environ["MONITOR_RESPONSE_GZIP"] = "1"
        os.environ["MONITOR_RESPONSE_GZIP_MIN_BYTES"] = "0"
        response = self.monitor_app.app.test_client().get(
            "/",
            headers={
                "Authorization": "Basic YWRtaW46c2VjcmV0",
                "Accept-Encoding": "gzip",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertIn("<title>Cycle Monitor</title>", gzip.decompress(response.data).decode("utf-8"))

    def test_compact_db_endpoint_returns_size_stats(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        response = self.monitor_app.app.test_client().post(
            "/api/compact_db",
            headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("before_bytes", payload)
        self.assertIn("after_bytes", payload)
        self.assertIn("saved_bytes", payload)

    def test_index_groups_sessions_by_month(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (
                    device_id, session_id, mode, start_ts, rows_count, distance_km, uploaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("bike", "2026-05-07_10-00-00", "supercycle_live", "2026-05-07 10:00:00", 2, 12.0, "2026-05-07 10:05:00"),
                    ("bike", "2026-05-01_09-00-00", "supercycle_live", "2026-05-01 09:00:00", 2, 8.0, "2026-05-01 09:05:00"),
                    ("bike", "2026-04-30_08-00-00", "supercycle_live", "2026-04-30 08:00:00", 2, 6.0, "2026-04-30 08:05:00"),
                ],
            )
            conn.execute(
                """
                INSERT INTO photos (device_id, session_id, mode, captured_at, relative_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "bike",
                    "2026-05-07_10-00-00",
                    "supercycle_live",
                    "2026-05-07 10:02:00",
                    "photos/bike/2026-05-07_10-00-00/frame.jpg",
                ),
            )
            conn.commit()

        response = self.monitor_app.app.test_client().get(
            "/",
            headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="month-separator"'), 2)
        self.assertIn("<span>mai 2026</span>", html)
        self.assertIn("<span>avril 2026</span>", html)
        self.assertIn('data-session-row="1"', html)
        self.assertIn('data-month-key="2026-05"', html)
        self.assertIn('data-photo-count="1"', html)
        self.assertIn('title="1 photos"', html)
        self.assertIn('id="bulk-video-btn"', html)
        self.assertIn('id="bulk-solar-profile-btn"', html)
        self.assertIn('id="solar-profile-card"', html)
        self.assertIn("solarProfileExcludedKeys", html)
        self.assertIn("buildSolarPotentialLine", html)
        self.assertIn("max potential", html)
        self.assertIn("solar-profile-panel-wc", html)
        self.assertIn("solar-profile-utc-offset", html)
        self.assertIn("solar-profile-export", html)
        self.assertIn("cycle_analyst_solar_profile", html)
        self.assertIn("solar-control-point", html)
        self.assertIn("buildIdealSolarLine", html)
        self.assertIn("estimatedUtcOffsetFromLon", html)

    def test_solar_profile_endpoint_returns_overlay_series_for_selected_sessions(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        sessions = [
            {
                "device_id": "bike",
                "session_id": "2026-05-07_10-00-00",
                "mode": "supercycle_live",
                "metrics": {"solar_enabled": True},
                "solar_enabled": 1,
                "samples": [
                    dict(self.sample(0), timestamp="2026-05-07 06:00:00", solar_power_w=50),
                    dict(self.sample(1), timestamp="2026-05-07 12:30:00", solar_power_w=180),
                ],
            },
            {
                "device_id": "bike",
                "session_id": "2026-05-08_10-00-00",
                "mode": "supercycle_live",
                "metrics": {"solar_enabled": True},
                "solar_enabled": 1,
                "samples": [
                    dict(self.sample(0), timestamp="2026-05-08 06:00:00", solar_power_w=30),
                    dict(self.sample(1), timestamp="2026-05-08 12:30:00", solar_power_w=220),
                ],
            },
        ]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for payload in sessions:
                self.monitor_app._insert_telemetry_samples(
                    conn,
                    payload["device_id"],
                    payload["session_id"],
                    payload["mode"],
                    payload["samples"],
                    1,
                )
                stored_samples = self.monitor_app._telemetry_samples_for_session(
                    conn,
                    payload["device_id"],
                    payload["session_id"],
                    payload["mode"],
                )
                self.monitor_app._insert_uploaded_session_summary(conn, payload, stored_samples)
            conn.commit()

        response = self.monitor_app.app.test_client().post(
            "/api/sessions/solar_profile",
            json={
                "sessions": [
                    {"device_id": "bike", "session_id": "2026-05-07_10-00-00", "mode": "supercycle_live"},
                    {"device_id": "bike", "session_id": "2026-05-08_10-00-00", "mode": "supercycle_live"},
                ],
                "max_points_per_session": 1440,
            },
            headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["session_count"], 2)
        self.assertEqual(payload["raw_sample_count"], 4)
        self.assertEqual(payload["bucket_minutes"], 1)
        self.assertIn("reference", payload)
        self.assertGreater(payload["reference"]["default_panel_max_w"], 0)
        self.assertEqual(len(payload["profiles"]), 2)
        self.assertEqual([point["w"] for point in payload["profiles"][0]["points"]], [50, 180])
        self.assertAlmostEqual(payload["profiles"][0]["points"][0]["hour"], 6.0083, places=4)
        self.assertAlmostEqual(payload["profiles"][0]["avg_lat"], 48.0005, places=4)
        self.assertEqual(payload["profiles"][0]["date"], "2026-05-07")

    def test_photo_video_requires_video_encoder(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (device_id, session_id, mode, start_ts, rows_count, distance_km, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("bike", "2026-05-07_10-00-00", "supercycle_live", "2026-05-07 10:00:00", 2, 2.0, "2026-05-07 10:05:00"),
            )
            conn.execute(
                """
                INSERT INTO photos (device_id, session_id, mode, captured_at, relative_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "bike",
                    "2026-05-07_10-00-00",
                    "supercycle_live",
                    "2026-05-07 10:02:00",
                    "photos/bike/2026-05-07_10-00-00/frame.jpg",
                ),
            )
            conn.commit()

        original_which = self.monitor_app.shutil.which
        self.monitor_app.shutil.which = lambda name: None
        try:
            response = self.monitor_app.app.test_client().post(
                "/api/photos/video",
                json={
                    "sessions": [
                        {
                            "device_id": "bike",
                            "session_id": "2026-05-07_10-00-00",
                            "mode": "supercycle_live",
                        }
                    ]
                },
                headers={"Authorization": "Basic YWRtaW46c2VjcmV0"},
            )
        finally:
            self.monitor_app.shutil.which = original_which

        self.assertEqual(response.status_code, 503)
        self.assertIn("ffmpeg", response.get_json()["error"])

    def test_suntrip_stage_toggle_feeds_analysis_page(self):
        if not hasattr(self.monitor_app.app, "test_client"):
            self.skipTest("Flask test client is not available in this test environment.")

        sessions = [
            {
                "device_id": "Supercycle-1",
                "session_id": "2026-05-07_10-00-00",
                "mode": "supercycle_live",
                "metrics": {"solar_enabled": True},
                "solar_enabled": 1,
            },
            {
                "device_id": "sc-vehicule-2",
                "session_id": "2026-05-07_10-05-00",
                "mode": "supercycle_live",
                "metrics": {"solar_enabled": True},
                "solar_enabled": 1,
            },
        ]
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for index, payload in enumerate(sessions):
                samples = [
                    dict(self.sample(0), timestamp=f"2026-05-07 10:0{index}:00"),
                    dict(self.sample(1), timestamp=f"2026-05-07 10:0{index}:01"),
                ]
                self.monitor_app._insert_telemetry_samples(
                    conn,
                    payload["device_id"],
                    payload["session_id"],
                    payload["mode"],
                    samples,
                    1,
                )
                stored_samples = self.monitor_app._telemetry_samples_for_session(
                    conn,
                    payload["device_id"],
                    payload["session_id"],
                    payload["mode"],
                )
                self.monitor_app._insert_uploaded_session_summary(conn, payload, stored_samples)
            conn.commit()

        client = self.monitor_app.app.test_client()
        token = "Basic YWRtaW46c2VjcmV0"
        response = client.patch(
            "/api/sessions/suntrip_stage",
            json={"sessions": sessions, "suntrip_stage": True},
            headers={"Authorization": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["updated_count"], 2)

        page = client.get(
            "/suntrip_analysis?start=2026-05-04&end=2026-06-04",
            headers={"Authorization": token},
        )

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("2 included sessions", html)
        self.assertIn("Supercycle 1", html)
        self.assertIn("Supercycle 2", html)
        self.assertIn("Totals", html)
        self.assertIn("1 included stages", html)
        self.assertIn("chartMetricGroups", html)
        self.assertIn("metric-chart-check", html)
        self.assertIn("Track Explorer", html)
        self.assertIn("trace-select", html)
        self.assertIn("trace-detailed", html)
        self.assertIn("ca-correction-toggle", html)
        self.assertIn("Corrected CA/GPS", html)
        self.assertIn("data-corrected-value", html)
        self.assertIn("2026-05-07_10-00-00", html)
        self.assertIn("2026-05-07_10-05-00", html)
        self.assertIn("CA distance", html)
        self.assertIn("Avg GPS/CA speed delta", html)
        self.assertIn("Battery Used", html)
        self.assertIn("CA Ah raw", html)

        trace = client.get(
            "/api/suntrip_analysis/session_trace"
            "?device_id=Supercycle-1"
            "&session_id=2026-05-07_10-00-00"
            "&mode=supercycle_live"
            "&metric=ca_speed_kph"
            "&compare=1",
            headers={"Authorization": token},
        )
        self.assertEqual(trace.status_code, 200)
        payload = trace.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["metric"]["key"], "ca_speed_kph")
        self.assertEqual(len(payload["series"]), 2)
        self.assertGreaterEqual(payload["series"][0]["point_count"], 1)

        standard_trace = client.get(
            "/api/suntrip_analysis/session_trace"
            "?device_id=Supercycle-1"
            "&session_id=2026-05-07_10-00-00"
            "&mode=supercycle_live"
            "&metric=ca_speed_kph"
            "&max_points=5000",
            headers={"Authorization": token},
        )
        self.assertEqual(standard_trace.status_code, 200)
        self.assertEqual(standard_trace.get_json()["max_points"], 1400)

        detailed_trace = client.get(
            "/api/suntrip_analysis/session_trace"
            "?device_id=Supercycle-1"
            "&session_id=2026-05-07_10-00-00"
            "&mode=supercycle_live"
            "&metric=ca_speed_kph"
            "&detailed=1"
            "&max_points=5000",
            headers={"Authorization": token},
        )
        self.assertEqual(detailed_trace.status_code, 200)
        detailed_payload = detailed_trace.get_json()
        self.assertTrue(detailed_payload["detailed"])
        self.assertEqual(detailed_payload["max_points"], 5000)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET distance_km = 16
                WHERE device_id = ? AND session_id = ? AND mode = ?
                """,
                ("Supercycle-1", "2026-05-07_10-00-00", "supercycle_live"),
            )
            conn.commit()

        long_detailed_trace = client.get(
            "/api/suntrip_analysis/session_trace"
            "?device_id=Supercycle-1"
            "&session_id=2026-05-07_10-00-00"
            "&mode=supercycle_live"
            "&metric=ca_speed_kph"
            "&compare=0"
            "&detailed=1"
            "&max_points=5000",
            headers={"Authorization": token},
        )
        self.assertEqual(long_detailed_trace.status_code, 200)
        long_payload = long_detailed_trace.get_json()
        self.assertFalse(long_payload["detailed"])
        self.assertFalse(long_payload["detailed_allowed"])
        self.assertEqual(long_payload["max_points"], 1400)

        zoom_detailed_trace = client.get(
            "/api/suntrip_analysis/session_trace"
            "?device_id=Supercycle-1"
            "&session_id=2026-05-07_10-00-00"
            "&mode=supercycle_live"
            "&metric=ca_speed_kph"
            "&compare=0"
            "&detailed=1"
            "&range_axis=distance"
            "&range_min=0"
            "&range_max=1"
            "&max_points=5000",
            headers={"Authorization": token},
        )
        self.assertEqual(zoom_detailed_trace.status_code, 200)
        zoom_payload = zoom_detailed_trace.get_json()
        self.assertTrue(zoom_payload["detailed"])
        self.assertTrue(zoom_payload["detailed_allowed"])
        self.assertTrue(zoom_payload["range_applied"])
        self.assertEqual(zoom_payload["max_points"], 5000)
        self.assertIn("overview_points", zoom_payload["series"][0])

    def test_suntrip_ca_gps_correction_uses_reliable_vehicle_ratio(self):
        columns = [
            {
                "vehicle_key": "supercycle_1",
                "session_id": "stage-1",
                "metrics": {
                    "distance": 100.0,
                    "gps_distance_km": 104.0,
                    "gps_points": 1000,
                    "gps_distance_rejected_count": 0,
                    "ca_reset_count": 0,
                    "speed_sum": 2000.0,
                    "speed_max": 42.0,
                },
            },
            {
                "vehicle_key": "supercycle_1",
                "session_id": "stage-2",
                "metrics": {
                    "distance": 200.0,
                    "gps_distance_km": 208.0,
                    "gps_points": 1500,
                    "gps_distance_rejected_count": 2,
                    "ca_reset_count": 0,
                    "speed_sum": 4000.0,
                    "speed_max": 44.0,
                },
            },
            {
                "vehicle_key": "supercycle_1",
                "session_id": "bad-ratio",
                "metrics": {
                    "distance": 100.0,
                    "gps_distance_km": 114.0,
                    "gps_points": 1200,
                    "gps_distance_rejected_count": 0,
                    "ca_reset_count": 0,
                    "speed_sum": 2100.0,
                    "speed_max": 45.0,
                },
            },
            {
                "vehicle_key": "supercycle_2",
                "session_id": "too-short",
                "metrics": {
                    "distance": 4.0,
                    "gps_distance_km": 4.2,
                    "gps_points": 1000,
                    "gps_distance_rejected_count": 0,
                    "ca_reset_count": 0,
                },
            },
        ]

        corrections = self.monitor_app._suntrip_vehicle_corrections(columns)

        sc1 = corrections["supercycle_1"]
        self.assertTrue(sc1["available"])
        self.assertEqual(sc1["session_count"], 3)
        self.assertEqual(sc1["inlier_count"], 2)
        self.assertAlmostEqual(sc1["factor"], 1.04, places=6)
        self.assertFalse(corrections["supercycle_2"]["available"])

        corrected = self.monitor_app._apply_ca_gps_correction(columns[0]["metrics"], sc1["factor"])
        self.assertAlmostEqual(corrected["distance"], 104.0, places=6)
        self.assertAlmostEqual(corrected["speed_sum"], 2080.0, places=6)
        self.assertAlmostEqual(corrected["speed_max"], 43.68, places=6)
        self.assertEqual(corrected["gps_distance_km"], 104.0)

    def test_suntrip_trace_rejects_ca_distance_glitch(self):
        samples = [
            dict(self.sample(0), timestamp="2026-05-09 12:13:20", raw="0 52 1 30 54.084 20 0 0 0 0 0 0 0 0 flags"),
            dict(self.sample(1), timestamp="2026-05-09 12:13:21", raw="0 52 1 30 544.133 20 0 0 0 0 0 0 0 0 flags"),
            dict(self.sample(2), timestamp="2026-05-09 12:13:22", raw="0 52 1 30 54.143 20 0 0 0 0 0 0 0 0 flags"),
            dict(self.sample(3), timestamp="2026-05-09 12:13:23", raw="0 52 1 30 54.200 20 0 0 0 0 0 0 0 0 flags"),
        ]

        points, raw_count, overview_points = self.monitor_app._session_trace_points(samples, 100)

        self.assertEqual(raw_count, 4)
        self.assertIsNone(overview_points)
        self.assertLess(points[-1]["x_distance"], 0.2)
        self.assertGreater(points[-1]["x_distance"], 0.09)


if __name__ == "__main__":
    unittest.main()
