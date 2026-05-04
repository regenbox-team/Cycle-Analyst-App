# Cycle Analyst App

## New Project Structure

- app/
  - config.py, modes.py, state.py, metrics.py, reader.py, db.py, bootstrap.py
  - routes/
    - core.py (metrics, dashboard, logs, sessions list)
    - sessions.py (start/resume/end/delete, summary, edit/select)
    - admin.py (switch_user, add_ah, reset)
    - modes.py (vehicle/test mode endpoints)
- scripts/
  - checkdb.py, db_viewer.py, export_sessions.py, merge_sessions.py, user_change.py (moved from root)
- templates/, static/ (unchanged)
- var/ (runtime data; created on first run)
  - ride_data.db, session_metrics/, current_session.txt, session_state.txt, current_user.txt, vehicle_mode.txt, test_mode.txt
- cycle_server.py (entrypoint with create_app factory)
- wsgi.py (WSGI entrypoint)

## Runtime Data Directory

- Runtime files now live under `var/` by default. You can override with `APP_VAR_DIR=/custom/path`.
- On startup, the app automatically migrates any legacy files from the project root into `var/` (non-destructive where possible):
  - ride_data.db, session_metrics/*.json
  - current_session.txt, session_state.txt, current_user.txt
  - vehicle_mode.txt, test_mode.txt

## Running the App

- Development: `python cycle_server.py`
  - Starts the background reader thread and Flask dev server on port 5050.

- WSGI (gunicorn/uwsgi): use `wsgi:application`
  - Example: `gunicorn -w 2 wsgi:application`
  - The factory does not start the background reader by default. Run the reader as a separate process if needed or adapt your deployment to start it.

## Command-Line Utilities (scripts/)

- `scripts/checkdb.py` (summaries and integrity checks)
- `scripts/db_viewer.py` (viewer)
- `scripts/export_sessions.py` (export)
- `scripts/merge_sessions.py` (merge session logs)
- `scripts/user_change.py` (user timeline tool)

Run via: `python scripts/<tool>.py ...`

## Notes

- Tests importing `update_metrics`, `reset_session_state`, and `session_metrics` from `cycle_server` keep working; these symbols are re-exported.
- To switch runtime storage location, set `APP_VAR_DIR` before starting the app.
- Optional Raspberry Pi INA228 integration can add solar telemetry alongside the Cycle Analyst human-power metric by setting `APP_SOLAR_SENSOR=ina228`. The reader now follows the same INA228 calibration path as ArduPilot (`SHUNT_CAL` + `CURRENT` register), and `python scripts/ina228_debug.py --addr 0` prints raw/debug data directly on the Pi.
