import sqlite3
from .config import DB_FILE


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
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

