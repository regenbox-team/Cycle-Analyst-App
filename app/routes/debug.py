from __future__ import annotations
import json
import os
from flask import jsonify, render_template, request, make_response
import cantools

DEFAULT_JSONL_PATH = os.getenv("ALL_SIGNALS_JSONL", "var/debug/all_signals.jsonl")
DEFAULT_DBCS = os.getenv(
    "ALL_SIGNALS_DBCS",
    "can_util/Cockpit_CAN_Database_V1.4.dbc,can_util/Act2.5_database_can_A_V1.5.dbc",
)


def _read_last_jsonl(path: str):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            # Read up to last 64KB to find the last full line
            block = 65536
            offset = max(0, size - block)
            f.seek(offset)
            data = f.read()
            try:
                text = data.decode('utf-8', errors='ignore')
            except Exception:
                return None
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if not lines:
                return None
            # In case we cut mid-line at the start, only consider complete lines
            last_line = lines[-1]
            return json.loads(last_line)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def all_signals_json():
    path = request.args.get('path') or DEFAULT_JSONL_PATH
    snapshot = _read_last_jsonl(path)
    payload = {
        "epoch": snapshot.get("epoch"),
        "acticycle": snapshot.get("acticycle", {}),
        "signals": snapshot.get("signals", {}),
    } if snapshot else {"epoch": None, "signals": {}, "note": "No snapshot yet"}
    resp = make_response(jsonify(payload))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


def all_signals_page():
    return render_template("all_signals.html")


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("debug_routes", __name__)
    bp.add_url_rule("/debug/all_signals.json", view_func=all_signals_json)
    bp.add_url_rule("/debug/all_signals", view_func=all_signals_page)
    return bp


def register(app):
    app.add_url_rule("/debug/all_signals.json", view_func=all_signals_json)
    app.add_url_rule("/debug/all_signals", view_func=all_signals_page)

# --- DBC schema exposure ---

def _load_db_merged(dbc_arg: str):
    paths = [p.strip() for p in str(dbc_arg).split(',') if p and p.strip()]
    if not paths:
        return None
    if len(paths) == 1:
        return cantools.database.load_file(paths[0])
    db = cantools.database.Database()
    for p in paths:
        sub = cantools.database.load_file(p)
        db.messages.extend(getattr(sub, "messages", []))
        db.nodes.extend(getattr(sub, "nodes", []))
        if hasattr(sub, "buses"):
            db._buses.extend(sub.buses)
    return db


def all_signal_names_json():
    dbc = request.args.get('dbc') or DEFAULT_DBCS
    try:
        db = _load_db_merged(dbc)
        names = []
        for m in db.messages:
            for s in m.signals:
                names.append(f"{m.name}.{s.name}")
        names.sort()
        resp = make_response(jsonify({"dbc": dbc, "count": len(names), "names": names}))
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp
    except Exception as e:
        resp = make_response(jsonify({"error": str(e)}), 500)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp


# Register endpoint in both styles
def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("debug_routes", __name__)
    bp.add_url_rule("/debug/all_signals.json", view_func=all_signals_json)
    bp.add_url_rule("/debug/all_signals", view_func=all_signals_page)
    bp.add_url_rule("/debug/all_signal_names.json", view_func=all_signal_names_json)
    return bp


def register(app):
    app.add_url_rule("/debug/all_signals.json", view_func=all_signals_json)
    app.add_url_rule("/debug/all_signals", view_func=all_signals_page)
    app.add_url_rule("/debug/all_signal_names.json", view_func=all_signal_names_json)
