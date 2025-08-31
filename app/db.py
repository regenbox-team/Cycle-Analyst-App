import sqlite3
from .config import get_db_file


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
        conn.commit()
