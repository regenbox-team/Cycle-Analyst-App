# Cycle Analyst split services

This install mode keeps data recording alive even if the dashboard, nginx, camera upload, or browser refreshes misbehave.

## Services

- `cycle-recorder.service`: reads Cycle Analyst serial data, GPS, I2C solar sensor, writes SQLite logs and live metrics files.
- `cycle-photo.service`: reads the recorder's live metrics file, triggers camera captures at distance checkpoints, keeps failed uploads in `var/pending_photos`, and retries them.
- `cycle-analyst.service`: serves the web dashboard on port `5050`; it does not read serial/GPS/camera directly.
- `nginx`: proxies port `80` to `127.0.0.1:5050`.

The generated recorder service sets `APP_SCHEDULE_PHOTOS=0`. This is deliberate:
camera capture and monitor upload must not be able to block data recording on a
small Raspberry Pi. The photo worker is the only process that should run camera
captures in the split-service install.

## One-command setup

From the repository on the Raspberry Pi:

```bash
cd "$HOME/Cycle-Analyst-App"
python3 scripts/setup_pi_services.py
```

The first run is a dry run and prints the files it will install.

Apply the setup:

```bash
python3 scripts/setup_pi_services.py --apply
```

Common options:

```bash
python3 scripts/setup_pi_services.py --apply --server-name "sc-vehicule-5.local sc-vehicule-5"
python3 scripts/setup_pi_services.py --apply --repo "$HOME/Cycle-Analyst-App" --user "$USER"
python3 scripts/setup_pi_services.py --apply --no-nginx
```

By default the script uses the current repository path, current Linux user, and
current hostname. On `danieldilg@sc-vehicule-5`, running it from
`/home/danieldilg/Cycle-Analyst-App` is enough.

## Check recording

```bash
systemctl status cycle-recorder.service cycle-photo.service cycle-analyst.service --no-pager
curl http://127.0.0.1:5050/metrics
curl http://127.0.0.1/
```

If `/start` says `Connection: Inactive`, check that the recorder is publishing
fresh live snapshots:

```bash
cd "$HOME/Cycle-Analyst-App"
grep -n "last_live_state_write_time" app/reader.py
SID=$(cat var/current_session.txt 2>/dev/null)
ls -lh "var/session_metrics/${SID}_session_metrics.json" 2>/dev/null
curl -s http://127.0.0.1:5050/metrics | head -c 300
```

The `grep` must print a line, and the metrics file timestamp should be current
when the Cycle Analyst is plugged in. If not, pull the latest code and restart:

```bash
git pull --ff-only
sudo systemctl restart cycle-recorder.service cycle-photo.service cycle-analyst.service
```

Watch that database rows keep increasing:

```bash
cd "$HOME/Cycle-Analyst-App"
while true; do
  echo "===== $(date '+%F %T') ====="
  for db in var/ride_data*.db; do
    echo "$db"
    sqlite3 "$db" "SELECT COUNT(*) AS rows, MAX(timestamp) AS latest_utc, MAX(session) AS latest_session FROM logs;"
  done
  sleep 10
done
```

If only `cycle-recorder.service` becomes unstable while the web and photo
services stay alive, check for an I2C file descriptor leak:

```bash
PID=$(systemctl show -p MainPID --value cycle-recorder.service)
ls -l /proc/$PID/fd 2>/dev/null | grep -c '/dev/i2c-1'
ls -l /proc/$PID/fd 2>/dev/null | grep -E '/dev/ttyUSB|/dev/ttyACM|/dev/i2c'
```

The I2C count should stay very small. If it grows into the hundreds, the INA228
sensor is failing detection and the recorder is repeatedly reopening the bus.
For a data-first ride, temporarily disable the solar sensor and restart only
the recorder:

```bash
cd "$HOME/Cycle-Analyst-App"
cp cycle-analyst.env cycle-analyst.env.no-solar.$(date +%H%M%S)
grep -q '^APP_SOLAR_SENSOR=' cycle-analyst.env \
  && sed -i 's/^APP_SOLAR_SENSOR=.*/APP_SOLAR_SENSOR=/' cycle-analyst.env \
  || echo 'APP_SOLAR_SENSOR=' >> cycle-analyst.env
sudo systemctl restart cycle-recorder.service
```

## Logs

```bash
journalctl -u cycle-recorder.service -f
journalctl -u cycle-photo.service -f
journalctl -u cycle-analyst.service -f
sudo tail -f /var/log/nginx/error.log
```

## Emergency data-first mode

If the Pi is under pressure during a ride, keep recording and disable network upload/camera:

```bash
cd "$HOME/Cycle-Analyst-App"
cp cycle-analyst.env cycle-analyst.env.survie.$(date +%H%M%S)
sed -i 's/^MONITOR_URL=.*/MONITOR_URL=/' cycle-analyst.env
sed -i 's/^APP_CAMERA_COMMAND=.*/APP_CAMERA_COMMAND=/' cycle-analyst.env
sudo systemctl stop cycle-photo.service
sudo systemctl restart cycle-recorder.service cycle-analyst.service
```

The dashboard can fail or restart without stopping `cycle-recorder.service`.
