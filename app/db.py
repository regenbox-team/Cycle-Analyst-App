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
