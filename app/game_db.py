import sqlite3
import time
from .config import get_scores_db_file


def init_game_db():
    db_path = get_scores_db_file()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                user TEXT,
                distance_m INTEGER
            )
            """
        )
        conn.commit()


def insert_score(user: str, distance_m: int):
    db_path = get_scores_db_file()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scores (timestamp, user, distance_m) VALUES (?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), user or "Anonymous", int(distance_m)),
        )
        conn.commit()


def top_scores(limit: int = 10):
    db_path = get_scores_db_file()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT user, distance_m, timestamp FROM scores ORDER BY distance_m DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [(r[0], int(r[1]), r[2]) for r in rows]

