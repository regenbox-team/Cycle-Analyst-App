from flask import Flask
import time

from app.config import get_db_file
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

    # Register routes (each in isolation so one failure won't block others)
    from app.routes import core as routes_core, sessions as routes_sessions, admin as routes_admin, modes as routes_modes, gps as routes_gps, tiles as routes_tiles, tracks as routes_tracks, game as routes_game, sys as routes_sys
    # Optional debug routes (safe to import; if missing, ignored below)
    try:
        from app.routes import debug as routes_debug
    except Exception:
        routes_debug = None

    def _register_group(module):
        try:
            if hasattr(app, "register_blueprint") and hasattr(module, "create_blueprint"):
                app.register_blueprint(module.create_blueprint())
                return
        except Exception:
            # Fall back to direct registration
            pass
        try:
            if hasattr(module, "register"):
                module.register(app)
        except Exception:
            # Silently continue; other groups should still register
            pass

    for mod in (
        routes_core,
        routes_sessions,
        routes_admin,
        routes_modes,
        routes_gps,
        routes_tiles,
        routes_tracks,
        routes_game,
        routes_sys,
        routes_debug,
    ):
        _register_group(mod)

    # Initialize basic state
    state.session_id = state.session_id or state.load_session_id()
    state.session_start_time = time.time()
    state.latest_raw_values = None
    state.current_user = state.load_current_user()
    state.session_active = state.load_session_active()

    # Migrate legacy files, then init DB and restore metrics snapshot
    migrate_legacy_files()
    # Initialize DB for current mode and restore metrics snapshot from that DB
    init_db()
    # Initialize game scores DB
    try:
        from app.game_db import init_game_db
        init_game_db()
    except Exception:
        pass
    db_path = get_db_file()
    restore_session_metrics(state.session_id, db_path, parse_line)

    # Optional background reader thread
    # Always ensure reader runs when test mode is enabled, even under WSGI.
    if start_reader or modes.is_test_mode():
        import threading
        threading.Thread(target=read_serial, daemon=True).start()
        state.reader_started = True

    # Start GPS reader thread (safe to run even if device missing)
    try:
        if not getattr(state, 'gps_reader_started', False):
            import threading
            from app.gps import read_gps
            threading.Thread(target=read_gps, daemon=True).start()
            state.gps_reader_started = True
    except Exception:
        pass

    return app


# Create app for import-time usage
import os as _os
_start_reader_flag = _os.getenv("APP_START_READER", "0") == "1"
app = create_app(start_reader=_start_reader_flag)


if __name__ == "__main__":
    app = create_app(start_reader=True)
    print(f"[INIT] Loaded current user: {state.current_user}")
    app.run(host="0.0.0.0", port=5050)
