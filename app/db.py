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
    ]
    for name, typ in desired:
        if name not in cols:
            try:
                conn.execute(f"ALTER TABLE logs ADD COLUMN {name} {typ}")
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
        conn.execute('''
            CREATE TABLE IF NOT EXISTS gps_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                session TEXT,
                lat REAL,
                lon REAL,
                alt REAL,
                speed_kph REAL,
                track_deg REAL,
                fix INTEGER,
                sats INTEGER,
                hdop REAL
            )
        ''')
        conn.commit()
