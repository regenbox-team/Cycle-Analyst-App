import sqlite3
from .config import get_db_file


def _ensure_log_gps_columns(conn: sqlite3.Connection) -> None:
    try:
        cur = conn.execute("PRAGMA table_info(logs)")
        cols = {row[1] for row in cur.fetchall()}  # row[1] is name
    except Exception:
        cols = set()

    desired = [
        ("gps_lat", "REAL"),
        ("gps_lon", "REAL"),
        ("gps_alt", "REAL"),
        ("gps_speed_kph", "REAL"),
        ("gps_track_deg", "REAL"),
        ("gps_fix", "INTEGER"),
        ("gps_sats", "INTEGER"),
        ("gps_hdop", "REAL"),
        ("solar_current_a", "REAL"),
        ("solar_bus_v", "REAL"),
        ("solar_shunt_v", "REAL"),
        ("solar_power_w", "REAL"),
        ("solar_temperature_c", "REAL"),
        ("solar_enabled", "INTEGER DEFAULT 1"),
        ("motor_sensor_current_a", "REAL"),
        ("motor_sensor_bus_v", "REAL"),
        ("motor_corrected_current_a", "REAL"),
        ("motor_sensor_valid", "INTEGER DEFAULT 0"),
        ("user_id", "TEXT"),
        ("user_initials", "TEXT"),
        ("user_snapshot_json", "TEXT"),
    ]
    for name, typ in desired:
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE logs ADD COLUMN {name} {typ}")
            except Exception:
                pass
    try:
        conn.execute("UPDATE logs SET solar_enabled = 1 WHERE solar_enabled IS NULL")
    except Exception:
        pass
    try:
        initials_rows = conn.execute(
            "SELECT DISTINCT user FROM logs WHERE user IS NOT NULL AND TRIM(user) != ''"
        ).fetchall()
        legacy_initials = {str(row[0]).strip().upper() for row in initials_rows if row[0]}
        if legacy_initials:
            from .user_profiles import ensure_profiles_for_legacy_initials, profile_snapshot_json
            profiles = ensure_profiles_for_legacy_initials(legacy_initials)
            by_initials = {p["initials"]: p for p in profiles}
            for initials, profile in by_initials.items():
                conn.execute(
                    """
                    UPDATE logs
                    SET user_id = COALESCE(user_id, ?),
                        user_initials = COALESCE(user_initials, ?),
                        user_snapshot_json = COALESCE(user_snapshot_json, ?)
                    WHERE UPPER(user) = ? AND (user_id IS NULL OR user_initials IS NULL OR user_snapshot_json IS NULL)
                    """,
                    (
                        profile["user_id"],
                        profile["initials"],
                        profile_snapshot_json(profile),
                        initials,
                    ),
                )
    except Exception:
        pass


def init_db(mode: str | None = None):
    db_path = get_db_file(mode)
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                session TEXT,
                raw TEXT,
                user TEXT
            )
        ''')
        _ensure_log_gps_columns(conn)
        conn.commit()
