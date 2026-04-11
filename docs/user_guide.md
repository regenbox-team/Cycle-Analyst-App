# Cycle Analyst App — User Guide

## Overview
- Purpose: Live dashboard and session logging for Cycle Analyst/vehicle telemetry. Supports Supercycle test/live sources.
- Stack: Flask app with background reader (serial) publishing raw values and session metrics. Per-mode SQLite databases under `var/`.

## Quick Start
- Install deps: `pip install -r requirements.txt`
- Run dev server: `python cycle_server.py`
- Open: `http://localhost:5000`
- Switch mode: Dashboard UI or POST `{"mode":"supercycle_test"}` to `/set_vehicle_mode`
- Start session: Use Start page → choose user and click Start

## Project Structure
- `cycle_server.py`: App factory `create_app()`, starts reader if requested, registers routes.
- `app/`:
  - `config.py`: Paths, baudrate, mode config, per‑mode DB helpers.
  - `modes.py`: Current mode + test mode, apply/save/load functions.
  - `state.py`: Runtime state (session_id, current_user, latest_raw_values, session_active), metric store, JSON persistence.
  - `metrics.py`: Metric updates, reset, restore from DB/JSON.
  - `reader.py`: Background loop. Test mode generates fake data, live mode reads serial.
  - `db.py`: Initializes SQLite schema for the active mode DB.
  - `bootstrap.py`: Migrates legacy files into `var/` at startup.
  - `routes/`: Flask route groups (core, sessions, admin, modes).
- `templates/`, `static/`: UI.
- `scripts/`: Utilities (DB tools).
- `var/`: Runtime data (per‑mode DB, session metrics JSON, user/mode flags).

## Runtime Storage
- Default directory: `var/` (override with `APP_VAR_DIR=/custom/path`).
- Files created under `var/`:
  - Per‑mode DB: `ride_data_<vehicle_mode>.db`
  - Session metrics snapshots: `session_metrics/*_session_metrics.json`
  - Flags/state: `current_session.txt`, `session_state.txt`, `current_user.txt`, `vehicle_mode.txt`, `test_mode.txt`
- Legacy migration: On startup, files in project root are moved/copied into `var/`.

## Vehicle Modes
- Built‑in modes:
  - `supercycle_live`: serial `/dev/ttyUSB0`, test_mode=false
  - `supercycle_test`: serial `/dev/ttyUSB0`, test_mode=true (fake data)
- Switch mode:
  - UI control (if present), or
  - POST `/set_vehicle_mode` with body `{"mode":"supercycle_test"}`
- Test mode toggle (advanced):
  - POST `/set_test_mode` with `{"enabled": true|false}`
  - GET `/get_test_mode` → `{"test_mode": true|false}`
- Reader behavior:
  - Starts automatically when entering test mode (even under WSGI).
  - In live mode without data for >3s, connection shows inactive.

## Per‑Mode Databases
- Each mode writes to its own DB: `var/ride_data_<mode>.db`
- The app reads/writes the DB for the current vehicle mode by default.
- Browsing other DBs: add `?mode=<vehicle_mode>` to list/fetch routes:
  - `/sessions?mode=supercycle_live`
  - `/logs?mode=supercycle_live`
  - `/select_session?mode=supercycle_test`
  - `/summary?session=<id>&mode=supercycle_test`

## Sessions Workflow
- Start:
  - Page `/start`: choose user (JD/LL) → Start
  - New `session_id` assigned, metrics reset, `session_active=true`
- Resume:
  - On `/start`, choose a recent session ID and Resume
- End:
  - End session button → redirect to summary for the session
- Delete:
  - POST `/delete_session` with `{"session":"<id>"}` (also removes metrics JSON)
- Select session:
  - `/select_session` to choose session for editing or viewing
- Edit rows:
  - Fetch first 200 rows: `/api/session_rows?session=<id>`
  - Delete row: POST `/api/delete_row` `{"id": <row_id>}`

## Live Metrics
- Endpoint: `/metrics`
  - `raw_CA_values`: 15‑item list or null if inactive
  - `calculated_CA_values`:
    - Speed: `speed_avg`, `speed_max`
    - Power: `power_live`, `power_avg`, `power_max`, `power_min`
    - Energy: `Wh_pos`, `Wh_regen`, `human_Wh`, `solar_Wh`, `net_Wh` (pos - regen - human - solar), `%_regen`, `%_human`, `%_solar`
    - Efficiency: `net_Wh_per_km`, per‑km lists (`Wh_per_km_last`, `net_Wh_per_km_last`)
    - Human/solar/regen per‑km %: `human_pct_per_km_last`, `solar_pct_per_km_last`, `regen_pct_per_km_last`
    - Temperature: `temp_avg`, `temp_max`
    - Autonomy estimates: `autonomy.range_session_avg`, plus last km / 10km metrics when available
- Staleness:
  - No new data for >3s → `raw_CA_values` cleared; UI shows “inactive”.

## Summary Report
- Page: `/summary?session=<id>[&mode=<vehicle_mode>]`
- Groups per user and total:
  - Duration & Distance, Speed, Power, Energy, Efficiency, Percentages, Temperature, Human Effort
- Aggregation uses DB rows; robust timing based on stored timestamps.

## Admin Actions
- Switch user: POST `/switch_user` → flips JD/LL during a session
- Add Ah offset: POST `/add_ah` `{"added_ah": 2.5}` → adjusts `ah_offset`
- Reset session state: POST `/reset` → new session id, metrics cleared

## Scripts
- `scripts/export_sessions.py`:
  - Exports each session to CSV in `sessions_csv/` from the current mode DB.
- `scripts/db_viewer.py` (micro viewer API):
  - Endpoints accept `?mode=<vehicle_mode>`, returns sessions and downsampled time‑series.
- `scripts/checkdb.py`:
  - Summarizes sessions per DB; defaults to current mode DB. Example: `python scripts/checkdb.py`
- `scripts/merge_sessions.py`:
  - Merge rows from one session into another inside a DB (backup created automatically).
- `scripts/user_change.py`:
  - Simple web UI to annotate/override user changes within a session for the current mode DB.

## Configuration
- Env vars:
  - `APP_VAR_DIR`: override runtime directory (default: `var/`)
  - `APP_START_READER=1`: force reader thread to start under factory (not needed for test mode)
- Serial:
  - `SERIAL_PORT_DEFAULT` in `app/config.py` (`/dev/ttyUSB0`)
- Optional INA228 solar sensor on Raspberry Pi I2C:
  - Enable with `APP_SOLAR_SENSOR=ina228`
  - Bus with `APP_SOLAR_I2C_BUS=1`
  - Address with `APP_SOLAR_I2C_ADDR=0x45` (`0x44` and `0x41` also supported by the board, `0` probes them)
  - Shunt with `APP_SOLAR_SHUNT_OHMS=0.0002`
  - Full-scale current with `APP_SOLAR_MAX_AMPS=204.8`
  - Invert sign if needed with `APP_SOLAR_INVERT_SIGN=true`
  - Optional calibration trims: `APP_SOLAR_CURRENT_GAIN=1.0`, `APP_SOLAR_CURRENT_OFFSET=0.0`
  - Small-current deadband with `APP_SOLAR_CURRENT_DEADBAND_A=0.15` (default: values below this are treated as `0 A` in the app/DB/UI)
  - Debug with `python scripts/ina228_debug.py --addr 0 --interval 1`

## API Reference (Selected)
- GET `/metrics`
- POST `/set_vehicle_mode` `{ "mode": "supercycle_test" | "supercycle_live" }`
- GET `/get_vehicle_mode`
- POST `/set_test_mode` `{ "enabled": true|false }`
- GET `/get_test_mode`
- GET `/sessions[?mode=...]`
- GET `/logs[?mode=...]`
- GET `/summary?session=<id>[&mode=...]`
- GET `/api/session_rows?session=<id>[&mode=...]`
- POST `/api/delete_row` `{ "id": <row_id> }`
- POST `/delete_session` `{ "session": "<id>" }`

## Troubleshooting
- Connection shows “inactive”:
  - Verify test mode is enabled (`/get_test_mode`). In test mode, the reader should auto‑start and publish fake data.
  - For live mode, check the serial port. After >3s without data, connection goes inactive by design.
- Switching test → live remains “active”:
  - The app clears simulated values immediately when switching to a non‑test mode; refresh `/metrics` to see `raw_CA_values: null`.

## Backups & Maintenance
- DBs are simple SQLite files per mode under `var/`:
  - Backup by copying `var/ride_data_<mode>.db` while the app is idle, or export sessions via `scripts/export_sessions.py`.
- `scripts/merge_sessions.py` creates timestamped DB backups before changes.
- To purge metrics snapshots, delete files under `var/session_metrics/` (they’re regenerated if needed).

## Deployment
- WSGI entrypoint: `wsgi:application`
  - Example: `gunicorn -w 2 wsgi:application`
  - For test mode demo under WSGI: POST `/set_test_mode` `{"enabled": true}` → the reader auto‑starts.
- Dev: `python cycle_server.py` (port 5000). Starts reader thread automatically.

## Development
- App factory: `create_app(start_reader=False)` registers blueprints and initializes state/DB. Use `start_reader=True` for dev runs.
- Route modules in `app/routes/*` — add endpoints by extending these modules or creating new blueprints.
- Metrics logic resides in `app/metrics.py`; changes here affect both live calculations and summary.
