from __future__ import annotations

from pathlib import Path
import base64
import json
import os
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

from analyzer import (
    build_curve,
    list_databases,
    load_samples,
    load_export_payload,
    merge_sample_sets,
    point_to_dict,
    python_curve_snippet,
    resolve_db,
    session_series,
    stable_rest_points,
    summarize_sessions,
    write_outputs,
)


LAB_DIR = Path(__file__).resolve().parent
ROOT_DIR = LAB_DIR.parents[1]
OUTPUT_DIR = LAB_DIR / "outputs"

app = Flask(__name__, static_folder="static", static_url_path="/static")
JSON_SOURCES: dict[str, dict] = {}


def _db_from_request() -> Path:
    return resolve_db(ROOT_DIR, request.args.get("db") or (request.json or {}).get("db"))


def _float_payload(name: str, default: float) -> float:
    data = request.json or {}
    try:
        return float(data.get(name, default))
    except Exception:
        return default


def _int_payload(name: str, default: int) -> int:
    data = request.json or {}
    try:
        return int(data.get(name, default))
    except Exception:
        return default


@app.get("/")
def index():
    return send_from_directory(LAB_DIR / "templates", "index.html")


@app.get("/api/databases")
def api_databases():
    return jsonify({"databases": list_databases(ROOT_DIR)})


def _monitor_request(path: str, params: dict) -> dict:
    base = os.getenv("MONITOR_URL", "").rstrip("/")
    if not base:
        raise ValueError("MONITOR_URL is not configured")
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    user, password = os.getenv("MONITOR_USER", ""), os.getenv("MONITOR_PASS", "")
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


@app.post("/api/json_sources")
def api_json_sources():
    uploads = request.files.getlist("files")
    loaded = []
    for upload in uploads:
        try:
            payload = json.loads(upload.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return jsonify({"error": f"Invalid JSON in {upload.filename}: {exc}"}), 400
        key = f"json:{len(JSON_SOURCES)}:{upload.filename}"
        JSON_SOURCES[key] = payload
        sample_sets = load_export_payload(payload)
        loaded.append({"key": key, "name": upload.filename, "sessions": summarize_sample_sets(sample_sets)})
    return jsonify({"sources": loaded})


def summarize_sample_sets(sample_sets):
    result = []
    for session, samples in sample_sets.items():
        if not samples:
            continue
        result.append({"session": session, "samples": len(samples), "duration_min": 0, "voltage_min": round(min(s.voltage for s in samples), 2), "voltage_max": round(max(s.voltage for s in samples), 2), "ah_min": round(min(s.ah for s in samples), 2), "ah_max": round(max(s.ah for s in samples), 2), "distance_km": round(max(s.distance_km for s in samples)-min(s.distance_km for s in samples), 2), "power_min": round(min(s.power_w for s in samples), 1), "power_max": round(max(s.power_w for s in samples), 1)})
    return sorted(result, key=lambda item: item["session"], reverse=True)


@app.get("/api/monitor_sessions")
def api_monitor_sessions():
    device = request.args.get("device") or os.getenv("MONITOR_DEVICE_ID", "")
    mode = request.args.get("mode", "supercycle_live")
    if not device:
        return jsonify({"error": "Enter a monitor device id"}), 400
    payload = _monitor_request("/api/known_sessions", {"device_id": device, "mode": mode})
    return jsonify({"device": device, "mode": mode, "sessions": [{"session": sid} for sid in payload.get("sessions", [])]})


def load_request_samples(data: dict, sessions: set[str] | None):
    source = data.get("source", "db")
    if source == "json":
        return merge_sample_sets(*(load_export_payload(JSON_SOURCES[key], sessions) for key in data.get("json_keys", []) if key in JSON_SOURCES))
    if source == "monitor":
        device, mode = data.get("device"), data.get("mode", "supercycle_live")
        return merge_sample_sets(*(load_export_payload(_monitor_request("/api/export_session", {"device_id": device, "session_id": sid, "mode": mode}), sessions) for sid in sorted(sessions or [])))
    return load_samples(_db_from_request(), sessions)


@app.get("/api/sessions")
def api_sessions():
    db_path = _db_from_request()
    return jsonify({"db": str(db_path), "sessions": summarize_sessions(db_path)})


@app.route("/api/session_series", methods=["GET", "POST"])
def api_session_series():
    data = request.get_json(silent=True) or {}
    session = request.args.get("session") or data.get("session")
    if not session:
        return jsonify({"error": "missing session"}), 400
    try:
        max_points = int(request.args.get("max_points", "1600"))
    except Exception:
        max_points = 1600
    samples = load_request_samples(data, {session}).get(session, [])
    return jsonify(session_series(samples, max_points=max_points))


@app.post("/api/generate")
def api_generate():
    data = request.json or {}
    sessions = set(data.get("sessions") or [])
    if not sessions:
        return jsonify({"error": "Select at least one session."}), 400

    by_session = load_request_samples(data, sessions)
    points = []
    for samples in by_session.values():
        points.extend(
            stable_rest_points(
                samples,
                capacity_ah=_float_payload("capacity_ah", 64.0),
                max_speed_kph=_float_payload("max_speed_kph", 1.0),
                max_abs_current_a=_float_payload("max_abs_current_a", 1.5),
                min_rest_seconds=_float_payload("min_rest_seconds", 120.0),
                tail_seconds=_float_payload("tail_seconds", 60.0),
                max_voltage_std=_float_payload("max_voltage_std", 0.05),
                min_samples=_int_payload("min_samples", 20),
                fallback_hz=_float_payload("fallback_hz", 1.0),
            )
        )

    curve = build_curve(
        points,
        bin_percent=_float_payload("bin_percent", 5.0),
        min_points_per_bin=_int_payload("min_points_per_bin", 1),
    )
    paths = write_outputs(points, curve, OUTPUT_DIR)

    return jsonify({
        "points": [point_to_dict(point) for point in points],
        "curve": curve,
        "python_snippet": python_curve_snippet(curve),
        "json_snippet": [
            {"voltage": point["voltage"], "soc": point["soc"]}
            for point in curve
        ],
        "downloads": {
            "csv": f"/outputs/{paths['csv'].name}",
            "json": f"/outputs/{paths['json'].name}",
        },
    })


@app.get("/outputs/<path:name>")
def outputs(name: str):
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=True)
