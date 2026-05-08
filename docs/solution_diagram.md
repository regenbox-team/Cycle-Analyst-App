# Cycle Analyst App Solution Diagram

This document maps the current solution, the integrations, and the direction of each data flow.

## System Context

```mermaid
flowchart LR
    Rider["Rider / Operator<br/>touch browser on Pi or laptop"]
    Public["Public viewers<br/>SunTrip / session map"]

    subgraph Pi["Vehicle Raspberry Pi"]
        LocalApp["Cycle Analyst Flask app<br/>cycle_server.py"]
        Dashboard["Dashboard UI<br/>templates + static JS"]
        Reader["Background CA reader<br/>app.reader.read_serial"]
        GPS["Background GPS reader<br/>app.gps.read_gps"]
        MonitorSync["Monitor sync loop<br/>app.monitor_client"]
        PhotoCapture["Photo capture worker<br/>app.photo_capture"]

        subgraph Var["APP_VAR_DIR / var"]
            RideDB["ride_data_mode.db<br/>logs table"]
            MetricsJSON["session_metrics/*.json"]
            StateFiles["state txt files<br/>current session, user, mode"]
            UsersJSON["users.json"]
            ScoresDB["game_scores.db"]
            GPX["route.gpx"]
            LivePhotos["live_photo/*.jpg"]
        end
    end

    subgraph Hardware["Vehicle / Pi hardware"]
        CA["Cycle Analyst serial<br/>/dev/ttyUSB0 or exec bridge"]
        Fake["Fake generator<br/>test mode"]
        GPSDongle["GPS NMEA dongle<br/>/dev/ttyACM0"]
        Solar["INA228 solar sensor<br/>I2C / smbus2"]
        Camera["Pi camera / USB cam<br/>libcamera-still, fswebcam, or APP_CAMERA_COMMAND"]
        Systemd["systemd service<br/>cycle-analyst.service"]
        PMTiles["Offline PMTiles file"]
    end

    subgraph Monitor["Remote monitor server"]
        MonitorApp["monitor_server Flask app"]
        MonitorDB["monitor.db<br/>devices, sessions, telemetry_samples, photos, users"]
        Media["media/photos"]
        TerrainCache["terrain_elevation_cache"]
    end

    subgraph External["External services, optional"]
        IGN["IGN elevation API<br/>data.geopf.fr"]
        OpenTopo["OpenTopoData fallback"]
        OSM["Online raster map / terrain tiles<br/>when offline map is unavailable"]
    end

    Rider -->|"HTTP GET/POST"| LocalApp
    LocalApp -->|"renders /dashboard, /start, /summary, /users, /game"| Dashboard
    Dashboard -->|"poll /metrics every 100 ms"| LocalApp
    Dashboard -->|"poll /power_history and /gps_status"| LocalApp
    Dashboard -->|"upload/erase GPX"| LocalApp
    Dashboard -->|"request /tiles/basemap.pmtiles"| LocalApp

    CA -->|"raw 15-field CA line"| Reader
    Fake -->|"synthetic CA values when test mode is on"| Reader
    GPSDongle -->|"NMEA GGA/RMC"| GPS
    Solar -->|"current, bus V, shunt V, power, temp"| Reader
    Camera -->|"JPEG image bytes"| PhotoCapture
    Systemd <-->|"POST /restart_service calls sudo systemctl restart"| LocalApp
    PMTiles -->|"range reads"| LocalApp

    Reader -->|"latest_raw_values + session_metrics"| LocalApp
    GPS -->|"gps_state"| LocalApp
    Reader -->|"1 Hz telemetry samples"| RideDB
    Reader -->|"metrics snapshots"| MetricsJSON
    LocalApp -->|"session/user/mode state"| StateFiles
    LocalApp -->|"profile CRUD"| UsersJSON
    LocalApp -->|"game score writes"| ScoresDB
    LocalApp -->|"GPX route writes / reads"| GPX
    PhotoCapture -->|"local preview"| LivePhotos

    MonitorSync -->|"Basic Auth POST /api/heartbeat"| MonitorApp
    MonitorSync -->|"GET /api/known_sessions"| MonitorApp
    MonitorSync -->|"POST /api/upload_session<br/>completed sessions only"| MonitorApp
    MonitorSync -->|"GET /api/users, POST /api/users/sync"| MonitorApp
    PhotoCapture -->|"POST /api/upload_photo"| MonitorApp

    MonitorApp -->|"upsert devices/sessions/telemetry/users/photos"| MonitorDB
    MonitorApp -->|"store image files"| Media
    MonitorApp -->|"lookup/cache altitude"| TerrainCache
    MonitorApp -->|"POST terrain batch"| IGN
    MonitorApp -->|"fallback terrain batch"| OpenTopo
    Public -->|"GET /public/suntrip, /public/suntrip.json,<br/>/session_map, /media/photos/*"| MonitorApp
    Dashboard -->|"optional online map fallback"| OSM
```

## Local Runtime Flow

```mermaid
sequenceDiagram
    autonumber
    actor Rider
    participant UI as Browser UI
    participant App as Local Flask app
    participant Reader as CA/Solar reader thread
    participant GPS as GPS reader thread
    participant DB as ride_data_mode.db
    participant JSON as session_metrics JSON
    participant Camera as Photo worker
    participant Monitor as Remote monitor server

    Rider->>UI: Select user, solar option, photo interval, start session
    UI->>App: POST /start_session
    App->>JSON: reset and persist session metrics
    App->>DB: ensure mode database schema
    App-->>UI: redirect /dashboard

    loop every ~100 ms
        Reader->>Reader: read CA serial or fake test data
        Reader->>Reader: read optional INA228 solar sample
        GPS->>GPS: parse NMEA GGA/RMC into gps_state
        Reader->>App: update latest_raw_values and session_metrics
    end

    loop every ~1 second while session active
        Reader->>DB: insert telemetry row with raw CA, user, GPS, solar
        Reader->>JSON: save current metrics snapshot
    end

    loop dashboard polling
        UI->>App: GET /metrics
        App-->>UI: live gauges, energy, range, photo status
        UI->>App: GET /power_history
        App->>DB: query recent samples
        App-->>UI: smoothed power series
        UI->>App: GET /gps_status
        App-->>UI: latest GPS fix and altitude
    end

    opt photo capture enabled and distance checkpoint crossed
        Reader->>Camera: schedule capture
        Camera->>Camera: run libcamera-still, fswebcam, or APP_CAMERA_COMMAND
        Camera->>JSON: update capture status
        Camera->>Monitor: POST /api/upload_photo with image, GPS, metrics, solar snapshot
        Monitor-->>Camera: public image URL
    end

    Rider->>UI: End session
    UI->>App: POST /end_session
    App->>App: mark session inactive and remove route.gpx
    App-->>UI: redirect /summary?session=session_id
    UI->>App: GET /summary
    App->>DB: load session samples
    App-->>UI: total and per-user summary metrics
```

## Remote Monitor Sync Flow

```mermaid
sequenceDiagram
    autonumber
    participant Pi as Vehicle Pi monitor_client
    participant Monitor as Remote monitor_server
    participant MDB as monitor.db
    participant Terrain as Terrain APIs
    participant Public as Public views

    loop every 60 seconds when MONITOR_URL is configured
        Pi->>Monitor: POST /api/heartbeat<br/>device, active session, mode, user, GPS
        Monitor->>MDB: upsert devices row
        Pi->>Monitor: POST /api/users/sync
        Monitor->>MDB: upsert users
        Pi->>Monitor: GET /api/known_sessions?device_id&mode
        Monitor-->>Pi: already uploaded session IDs
        Pi->>Pi: skip active session and known sessions
        Pi->>Monitor: POST /api/upload_session<br/>telemetry_samples + metrics
        Monitor->>Terrain: request missing terrain altitude<br/>IGN first, fallback optional
        Terrain-->>Monitor: altitude results
        Monitor->>MDB: insert sessions and telemetry_samples
    end

    opt distance photo checkpoint
        Pi->>Monitor: POST /api/upload_photo<br/>image_b64 + telemetry snapshot
        Monitor->>MDB: insert photos row
        Monitor->>Monitor: write media/photos file
    end

    Public->>Monitor: GET /public/suntrip.json
    Monitor->>MDB: latest devices, sessions, photos, telemetry
    Monitor-->>Public: public tracking payload

    Public->>Monitor: GET /session_map or /api/export_gpx
    Monitor->>MDB: session track and altitude
    Monitor-->>Public: map page or GPX
```

## Local Route And Integration Map

```mermaid
flowchart TB
    Browser["Browser UI"]
    Flask["Local Flask app"]

    subgraph Pages["Rendered pages"]
        Start["/start"]
        Dashboard["/dashboard"]
        Summary["/summary"]
        Users["/users"]
        Game["/game, /game/play, /game/leaderboard"]
        LogsPage["/live_logs"]
    end

    subgraph LiveApis["Live JSON APIs"]
        Metrics["GET /metrics"]
        Power["GET /power_history"]
        Logs["GET /logs"]
        SessionsList["GET /sessions"]
        GPSStatus["GET /gps_status"]
        Sys["GET /sys_metrics"]
    end

    subgraph Commands["Command APIs"]
        StartSession["POST /start_session"]
        EndSession["POST /end_session"]
        Resume["POST /resume_session"]
        DeleteSession["POST /delete_session"]
        SwitchUser["POST /switch_user"]
        AddAh["POST /add_ah"]
        Reset["POST /reset"]
        Modes["POST /set_vehicle_mode<br/>POST /set_test_mode<br/>POST /set_solar_roof"]
        Restart["POST /restart_service"]
    end

    subgraph Files["File-backed APIs"]
        PMTilesRoute["GET /tiles/basemap.pmtiles"]
        GPXUpload["POST /gpx/upload"]
        GPXTrack["GET /gpx/track"]
        GPXErase["POST or DELETE /gpx/erase"]
        LatestPhoto["GET /photo_capture/latest"]
    end

    Browser --> Flask
    Flask --> Pages
    Browser --> LiveApis
    Browser --> Commands
    Browser --> Files
```

## Storage Ownership

| Store | Written by | Read by | Purpose |
| --- | --- | --- | --- |
| `var/ride_data_<mode>.db` | `app.reader`, session row deletion tools | dashboard APIs, summaries, monitor sync | 1 Hz telemetry log per vehicle mode |
| `var/session_metrics/*.json` | metrics/session/photo code | restore, dashboard, monitor sync | Fast current-session metrics snapshot |
| `var/current_session.txt`, `session_state.txt`, `current_user*.txt`, `vehicle_mode.txt`, `test_mode.txt`, `solar_roof.txt` | route handlers and state helpers | startup, route handlers, reader | Runtime state across restarts |
| `var/users.json` | user pages, monitor import/sync | start page, switch user, monitor sync | Local user profiles |
| `var/game_scores.db` | game score endpoint | leaderboard page | Mini-game scores |
| `var/route.gpx` | GPX upload endpoint | map overlay and GPX endpoint | Planned route overlay for dashboard map |
| `var/live_photo/*.jpg` | photo capture worker | `/photo_capture/latest`, dashboard metrics | Local photo preview |
| `var/pending_photos/*` | photo capture worker | monitor sync, photo capture worker | Durable queue for photos waiting for network upload |
| `monitor.db` | monitor server API handlers | monitor pages, public APIs, exports | Aggregated remote telemetry, sessions, devices, users, terrain cache, photos |
| `media/photos` | monitor photo upload | public photo routes | Uploaded camera images |

## Integration Directions

| Integration | Direction | Protocol / trigger | Notes |
| --- | --- | --- | --- |
| Cycle Analyst | Hardware to Pi | Serial line at `SERIAL_PORT` / 9600 baud | Parsed as 15 fields; fake generator replaces it in test mode |
| GPS dongle | Hardware to Pi | NMEA serial, `APP_GPS_PORT`, default `/dev/ttyACM0` | GPS state is kept in memory and copied into each telemetry row |
| INA228 solar | Hardware to Pi | I2C via `smbus2`, enabled by `APP_SOLAR_SENSOR=ina228` | Adds solar current, voltage, power, temperature |
| Camera | Pi to local temp file, then Pi to monitor | `libcamera-still`, `fswebcam`, or `APP_CAMERA_COMMAND`; HTTP upload | Triggered by distance checkpoints when enabled for the session |
| Dashboard | Browser to Pi | HTTP GET/POST plus polling | `/metrics` is high frequency; `/power_history` and `/gps_status` are lower frequency |
| Offline basemap | Browser to Pi to local file | HTTP range requests to `/tiles/basemap.pmtiles` | Falls back to online raster/terrain sources when configured/needed |
| GPX route | Browser to Pi | Multipart upload, GET, POST/DELETE erase | Cleared automatically when the session ends |
| Monitor heartbeat | Pi to monitor | Basic Auth JSON POST `/api/heartbeat` every sync pass | Keeps device presence, active session, GPS, user, and mode fresh |
| Monitor user sync | Pi to monitor and monitor to Pi | Basic Auth JSON GET/POST | Setup page can import remote users; Pi syncs local profiles outward |
| Session upload | Pi to monitor | Basic Auth JSON POST `/api/upload_session` | Only completed sessions are uploaded; known sessions are skipped |
| Photo upload | Pi to monitor | Basic Auth JSON POST `/api/upload_photo` | Includes image, GPS, user, metrics, raw CA, and solar snapshot; failed uploads stay in `var/pending_photos` until a later sync succeeds |
| Terrain enrichment | Monitor to external APIs | HTTPS POST to IGN; optional OpenTopoData fallback | Cached in `terrain_elevation_cache` |
| Public monitor views | Public/browser to monitor | HTTP GET | `/public/suntrip`, `/public/suntrip.json`, `/session_map`, media routes |
| System restart | Browser to Pi | POST `/restart_service`, shell-out to `sudo systemctl restart` | Requires a matching sudoers rule on the Pi |
