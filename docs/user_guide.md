# Cycle Analyst App - User Guide

## Overview
- Purpose: live dashboard and session logging for Cycle Analyst / vehicle telemetry.
- Supported sources: Supercycle live serial data, test-mode fake data, optional GPS, optional INA228 solar sensor, optional camera photos.
- Runtime data lives under `var/` by default.

## Quick Start
- Run dev server: `python cycle_server.py`
- Open: `http://localhost:5050`
- Switch mode: dashboard UI or POST `{"mode":"supercycle_test"}` to `/set_vehicle_mode`
- Start session: use Start page, choose user, click Start

## Project Structure
- `cycle_server.py`: app factory `create_app()`, runtime initialization, compatibility exports.
- `cycle_recorder.py`: recorder entrypoint for split-service Raspberry Pi installs.
- `cycle_web.py`: dashboard entrypoint for split-service Raspberry Pi installs.
- `app/`: config, modes, state, metrics, reader, DB, routes.
- `templates/`, `static/`: UI.
- `scripts/`: utility scripts and Pi service setup.
- `var/`: runtime data, per-mode DBs, session metrics JSON, user/mode/session flags.

## Runtime Storage
- Default directory: `var/`; override with `APP_VAR_DIR=/custom/path`.
- Per-mode DB: `var/ride_data_<vehicle_mode>.db`
- Session metrics snapshots: `var/session_metrics/*_session_metrics.json`
- State files: `current_session.txt`, `session_state.txt`, `current_user.txt`, `vehicle_mode.txt`, `test_mode.txt`
- Legacy migration: on startup, root-level legacy files are moved or copied into `var/`.

## Vehicle Modes
- Built-in modes:
  - `supercycle_live`: serial `/dev/ttyUSB0`, test mode false.
  - `supercycle_test`: serial `/dev/ttyUSB0`, test mode true.
- Switch mode:
  - UI control if present.
  - POST `/set_vehicle_mode` with `{"mode":"supercycle_test"}`.
- Reader behavior:
  - Starts in development with `python cycle_server.py`.
  - Starts in production through `cycle-recorder.service`.
  - In live mode without data for more than 3 seconds, connection shows inactive.

## Sessions
- Start: `/start`, choose user, click Start.
- Resume: choose a recent session ID on `/start`.
- End: end session button redirects to summary.
- Delete: POST `/delete_session` with `{"session":"<id>"}`.
- Select session: `/select_session`.

## Live Metrics
- Endpoint: `/metrics`
- `raw_CA_values`: 15-item list or null if inactive.
- `calculated_CA_values`: speed, power, energy, per-km efficiency, regen/human/solar percentages, temperature, autonomy estimates.
- Staleness: no new data for more than 3 seconds clears `raw_CA_values`.
- In split-service mode, the web service reads live metrics from JSON files written by the recorder.

## Summary Report
- Page: `/summary?session=<id>[&mode=<vehicle_mode>]`
- Summary is built from DB rows, not browser state.
- Exports and maintenance scripts can use the same per-mode DB files.

## Admin Actions
- Switch user: POST `/switch_user`
- Add Ah offset: POST `/add_ah` with `{"added_ah": 2.5}`
- Reset session state: POST `/reset`
- Restart web service: POST `/restart_service`

## Configuration
- `APP_VAR_DIR`: runtime directory, default `var`.
- `APP_START_READER=1`: force reader thread in-process. Prefer `cycle-recorder.service` on Raspberry Pi.
- `APP_START_GPS=0`: skip GPS thread in the web process.
- `APP_START_MONITOR=0`: skip monitor sync in the web process.
- `APP_LIVE_STATE_FROM_FILES=1`: web dashboard reads state written by recorder.
- `APP_SCHEDULE_PHOTOS=0`: disables photo scheduling from the reader. The split-service setup uses this in the recorder to protect data recording.
- `APP_PHOTO_WORKER_INTERVAL_SECONDS=1`: photo worker polling interval.
- `APP_PHOTO_UPLOAD_RETRY_SECONDS=15`: retry interval for queued photo uploads.
- `APP_SOLAR_SENSOR=ina228`: enable optional INA228 sensor.
- `APP_CAMERA_COMMAND`: camera command for local captures.
- `MONITOR_URL`: remote monitor URL; leave empty to disable upload.

## Scripts
- `scripts/export_sessions.py`: export sessions to CSV.
- `scripts/db_viewer.py`: micro viewer API.
- `scripts/checkdb.py`: summarize DB/session health.
- `scripts/merge_sessions.py`: merge session logs.
- `scripts/user_change.py`: annotate user changes.
- `scripts/ina228_debug.py`: debug INA228 sensor on the Pi.
- `scripts/setup_pi_services.py`: install/update Raspberry Pi systemd and nginx services.

## Raspberry Pi Deployment
- Recommended road install: split services.
- `cycle-recorder.service`: reads serial/GPS/I2C and writes SQLite + metrics snapshots.
- `cycle-photo.service`: captures/uploads photos from live recorder snapshots.
- `cycle-analyst.service`: serves the web dashboard only.
- Setup dry run:

```bash
python3 scripts/setup_pi_services.py
```

- Apply setup:

```bash
python3 scripts/setup_pi_services.py --apply
```

- More detail: `docs/services_setup.md`.

## Health Checks
- Service status:

```bash
systemctl status cycle-recorder.service cycle-photo.service cycle-analyst.service --no-pager
```

- Confirm dashboard:

```bash
curl http://127.0.0.1:5050/metrics
curl http://127.0.0.1/
```

- Confirm DB recording:

```bash
cd /home/jeandard/Cycle-Analyst-App
for db in var/ride_data*.db; do
  echo "$db"
  sqlite3 "$db" "SELECT COUNT(*), MAX(timestamp), MAX(session) FROM logs;"
done
```

## Troubleshooting
- Connection inactive:
  - Check serial port and mode.
  - Check `cycle-recorder.service`, not only the web service.
- 502 from nginx:
  - The web backend is not accepting connections yet or has restarted.
- 504 from nginx:
  - The web backend accepted the connection but did not respond in time.
  - Recorder should continue in split-service mode.
- Data-first emergency:

```bash
sed -i 's/^MONITOR_URL=.*/MONITOR_URL=/' cycle-analyst.env
sed -i 's/^APP_CAMERA_COMMAND=.*/APP_CAMERA_COMMAND=/' cycle-analyst.env
sudo systemctl stop cycle-photo.service
sudo systemctl restart cycle-recorder.service cycle-analyst.service
```

## Development
- App factory: `create_app(start_reader=False)`.
- Use `start_reader=True` for local dev runs.
- Route modules live in `app/routes/*`.
- Metrics logic resides in `app/metrics.py`.
