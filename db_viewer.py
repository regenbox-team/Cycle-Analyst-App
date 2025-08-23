import sqlite3
import re
from flask import Flask, render_template, request, jsonify

DB_FILE = "ride_data.db"

# Mapping of metric names to their index in parsed log lines
METRICS = [
    "Ah", "Voltage", "Amps", "Speed", "Distance", "Temp",
    "S6", "S7", "S8", "S9", "S10", "S11", "S12", "SolarCurrent"
]

app = Flask(__name__)


def parse_line(line):
    """Parse a raw log line into a list of numeric values."""
    try:
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 14:
            return None
        return [float(x) for x in parts[:14]]
    except Exception:
        return None


@app.route("/")
def index():
    return render_template("db_viewer.html", metrics=METRICS)


@app.route("/sessions")
def sessions():
    """Return available session IDs from the database."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            rows = conn.execute(
                "SELECT DISTINCT session FROM logs ORDER BY session"
            ).fetchall()
        return jsonify([r[0] for r in rows])
    except Exception:
        return jsonify([])


@app.route("/data")
def data():
    session = request.args.get("session")
    metrics = [m for m in request.args.get("metrics", "").split(",") if m in METRICS]
    if not metrics:
        return jsonify({"labels": [], "series": {}, "minmax": {}})

    query = "SELECT timestamp, raw FROM logs"
    params = []
    if session:
        query += " WHERE session = ?"
        params.append(session)
    query += " ORDER BY id"

    series = {m: [] for m in metrics}
    labels = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            for ts, raw in conn.execute(query, params):
                parsed = parse_line(raw)
                if not parsed:
                    continue
                labels.append(ts)
                for m in metrics:
                    series[m].append(parsed[METRICS.index(m)])
    except Exception:
        pass

    minmax = {m: [min(vals), max(vals)] if vals else [0, 0] for m, vals in series.items()}
    return jsonify({"labels": labels, "series": series, "minmax": minmax})


if __name__ == "__main__":
    app.run(debug=True)
