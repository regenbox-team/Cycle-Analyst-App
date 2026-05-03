from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from analyzer import (
    build_curve,
    list_databases,
    load_samples,
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


@app.get("/api/sessions")
def api_sessions():
    db_path = _db_from_request()
    return jsonify({"db": str(db_path), "sessions": summarize_sessions(db_path)})


@app.get("/api/session_series")
def api_session_series():
    db_path = _db_from_request()
    session = request.args.get("session")
    if not session:
        return jsonify({"error": "missing session"}), 400
    try:
        max_points = int(request.args.get("max_points", "1600"))
    except Exception:
        max_points = 1600
    samples = load_samples(db_path, {session}).get(session, [])
    return jsonify(session_series(samples, max_points=max_points))


@app.post("/api/generate")
def api_generate():
    data = request.json or {}
    db_path = _db_from_request()
    sessions = set(data.get("sessions") or [])
    if not sessions:
        return jsonify({"error": "Select at least one session."}), 400

    by_session = load_samples(db_path, sessions)
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
