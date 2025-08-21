import sqlite3
import csv
import os

DB_FILE = "ride_data.db"
OUTPUT_DIR = "sessions_csv"

os.makedirs(OUTPUT_DIR, exist_ok=True)

conn = sqlite3.connect(DB_FILE)
cur = conn.cursor()

# Get all unique session IDs
cur.execute("SELECT DISTINCT session FROM logs")
sessions = [row[0] for row in cur.fetchall()]

for session_id in sessions:
    cur.execute("SELECT id, timestamp, session, raw, user FROM logs WHERE session = ? ORDER BY id", (session_id,))
    rows = cur.fetchall()

    csv_path = os.path.join(OUTPUT_DIR, f"session_{session_id}.csv")
    with open(csv_path, "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["id", "timestamp", "session", "raw", "user"])
        writer.writerows(rows)

    print(f"[✓] Exported session {session_id} to {csv_path}")

conn.close()
