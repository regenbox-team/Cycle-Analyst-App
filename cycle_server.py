from flask import Flask
import time

from app.config import DB_FILE
from app.db import init_db
from app import state
from app.metrics import update_metrics, reset_session_state, restore_session_metrics
from app import modes
from app.reader import read_serial, parse_line
from app.bootstrap import migrate_legacy_files


# Re-export for test compatibility
session_metrics = state.session_metrics


def create_app(start_reader: bool = False) -> Flask:
    app = Flask(__name__)

    # Register routes (blueprints or fallback for tests)
    from app.routes import core as routes_core, sessions as routes_sessions, admin as routes_admin, modes as routes_modes
    if hasattr(app, "register_blueprint"):
        try:
            app.register_blueprint(routes_core.create_blueprint())
            app.register_blueprint(routes_sessions.create_blueprint())
            app.register_blueprint(routes_admin.create_blueprint())
            app.register_blueprint(routes_modes.create_blueprint())
        except Exception:
            routes_core.register(app)
            routes_sessions.register(app)
            routes_admin.register(app)
            routes_modes.register(app)
    else:
        routes_core.register(app)
        routes_sessions.register(app)
        routes_admin.register(app)
        routes_modes.register(app)

    # Initialize basic state
    state.session_id = state.session_id or state.load_session_id()
    state.session_start_time = time.time()
    state.latest_raw_values = None
    state.current_user = state.load_current_user()
    state.session_active = state.load_session_active()

    # Migrate legacy files, then init DB and restore metrics snapshot
    migrate_legacy_files()
    init_db()
    restore_session_metrics(state.session_id, DB_FILE, parse_line)

    # Optional background reader thread
    # Always ensure reader runs when test mode is enabled, even under WSGI.
    if start_reader or modes.is_test_mode():
        import threading
        threading.Thread(target=read_serial, daemon=True).start()
        state.reader_started = True

    return app


# Create app for import-time usage
import os as _os
_start_reader_flag = _os.getenv("APP_START_READER", "0") == "1"
app = create_app(start_reader=_start_reader_flag)


if __name__ == "__main__":
    app = create_app(start_reader=True)
    print(f"[INIT] Loaded current user: {state.current_user}")
    app.run(host="0.0.0.0", port=5050)
