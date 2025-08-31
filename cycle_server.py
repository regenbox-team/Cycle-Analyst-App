from flask import Flask, render_template, jsonify, request, redirect
import threading
import time
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import json

# Internal modules
from app.config import DB_FILE, SESSION_METRICS_DIR, VEHICLE_CONFIGS
from app.db import init_db
from app.modes import apply_vehicle_mode, vehicle_mode, is_test_mode, test_mode_lock
from app import state
from app.metrics import update_metrics, reset_session_state, restore_session_metrics
from app.reader import read_serial, parse_line, generate_fake_data


app = Flask(__name__)

# Re-export for test compatibility
session_metrics = state.session_metrics

def _register_routes():
    from app.routes import core as routes_core, sessions as routes_sessions, admin as routes_admin, modes as routes_modes
    if hasattr(app, "register_blueprint"):
        try:
            app.register_blueprint(routes_core.create_blueprint())
            app.register_blueprint(routes_sessions.create_blueprint())
            app.register_blueprint(routes_admin.create_blueprint())
            app.register_blueprint(routes_modes.create_blueprint())
            return
        except Exception:
            pass
    routes_core.register(app)
    routes_sessions.register(app)
    routes_admin.register(app)
    routes_modes.register(app)

# --- SESSION MANAGEMENT --- (logic lives in app.state and app.metrics)

state.session_id = state.session_id or state.load_session_id()
state.session_start_time = time.time()
state.latest_raw_values = None
state.current_user = state.load_current_user()
state.session_active = state.load_session_active()

_register_routes()

# --- STARTUP ---
if __name__ == "__main__":
    init_db()
    restore_session_metrics(state.session_id, DB_FILE, parse_line)
    print(f"[INIT] Loaded current user: {state.current_user}")

    threading.Thread(target=read_serial, daemon=True).start()
    app.run(host="0.0.0.0", port=5050)
