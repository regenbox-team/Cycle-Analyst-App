from flask import Flask, request, render_template_string, redirect
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)
DB_FILE = "ride_data.db"

# Ensure user_changes table exists
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT,
                timestamp TEXT,
                user TEXT
            );
        ''')
        conn.commit()

# Utility to get available sessions
def get_sessions():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT DISTINCT session FROM logs ORDER BY session DESC").fetchall()
    return [row[0] for row in rows]

# Infer user segments from logs
def detect_user_segments(session_id):
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("""
            SELECT timestamp, user FROM logs
            WHERE session = ?
            ORDER BY timestamp
        """, (session_id,)).fetchall()

    segments = []
    last_user = None
    start_time = None

    for timestamp, user in rows:
        if user != last_user:
            if last_user is not None:
                segments.append({"start": start_time, "end": timestamp, "user": last_user})
            start_time = timestamp
            last_user = user

    if last_user and start_time:
        segments.append({"start": start_time, "end": None, "user": last_user})

    return segments

# Merge overrides if any
def get_all_user_segments(session_id):
    with sqlite3.connect(DB_FILE) as conn:
        overrides = conn.execute("""
            SELECT timestamp, user FROM user_changes
            WHERE session = ?
            ORDER BY timestamp
        """, (session_id,)).fetchall()

    if overrides:
        segments = []
        for idx, (ts, user) in enumerate(overrides):
            ts_dt = datetime.fromisoformat(ts)
            end_ts = datetime.fromisoformat(overrides[idx+1][0]) if idx+1 < len(overrides) else None
            segments.append({"start": ts_dt.isoformat(), "end": end_ts.isoformat() if end_ts else None, "user": user})
        return segments
    else:
        return detect_user_segments(session_id)

@app.route("/")
def index():
    sessions = get_sessions()
    return render_template_string("""
    <h2>User Change Utility</h2>
    <form action="/user_changes" method="get">
      <label>Select Session:</label>
      <select name="session">
        {% for s in sessions %}
          <option value="{{ s }}">{{ s }}</option>
        {% endfor %}
      </select>
      <button type="submit">View Timeline</button>
    </form>
    """, sessions=sessions)

@app.route("/user_changes")
def view_user_changes():
    session = request.args.get("session")
    if not session:
        return "Missing session ID", 400

    segments = get_all_user_segments(session)
    return render_template_string(USER_TEMPLATE, session=session, segments=segments)

@app.route("/user_changes/add", methods=["POST"])
def add_user_change():
    session = request.form["session"]
    timestamp = request.form["timestamp"]
    user = request.form["user"]

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            INSERT INTO user_changes (session, timestamp, user)
            VALUES (?, ?, ?)
        """, (session, timestamp, user))
        conn.commit()

    return redirect(f"/user_changes?session={session}")

@app.route("/user_changes/update", methods=["POST"])
def update_user_change():
    session = request.form["session"]
    timestamp = request.form["timestamp"]
    new_user = request.form["user"]

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            UPDATE user_changes SET user = ?
            WHERE session = ? AND timestamp = ?
        """, (new_user, session, timestamp))
        conn.commit()

    return redirect(f"/user_changes?session={session}")

@app.route("/user_changes/delete", methods=["POST"])
def delete_user_change():
    session = request.form["session"]
    timestamp = request.form["timestamp"]

    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            DELETE FROM user_changes
            WHERE session = ? AND timestamp = ?
        """, (session, timestamp))
        conn.commit()

    return redirect(f"/user_changes?session={session}")

USER_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <title>User Timeline - {{ session }}</title>
  <style>
    table, th, td { border: 1px solid black; border-collapse: collapse; padding: 0.4rem; }
  </style>
</head>
<body>
<h1>User Timeline for Session {{ session }}</h1>
<table>
  <tr><th>Start</th><th>End</th><th>User</th><th>Actions</th></tr>
  {% for seg in segments %}
  <tr>
    <td>{{ seg.start }}</td>
    <td>{{ seg.end or "ongoing" }}</td>
    <td>{{ seg.user }}</td>
    <td>
      <form method="POST" action="/user_changes/update" style="display:inline">
        <input type="hidden" name="session" value="{{ session }}">
        <input type="hidden" name="timestamp" value="{{ seg.start }}">
        <select name="user">
          <option value="JD" {% if seg.user == 'JD' %}selected{% endif %}>JD</option>
          <option value="LL" {% if seg.user == 'LL' %}selected{% endif %}>LL</option>
        </select>
        <button type="submit">Update</button>
      </form>
      <form method="POST" action="/user_changes/delete" style="display:inline">
        <input type="hidden" name="session" value="{{ session }}">
        <input type="hidden" name="timestamp" value="{{ seg.start }}">
        <button type="submit">Delete</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>

<h2>Add New User Change</h2>
<form method="POST" action="/user_changes/add">
  <input type="hidden" name="session" value="{{ session }}">
  <label>Timestamp (ISO): <input type="text" name="timestamp" placeholder="YYYY-MM-DDTHH:MM:SS" required></label>
  <label>User:
    <select name="user">
      <option value="JD">JD</option>
      <option value="LL">LL</option>
    </select>
  </label>
  <button type="submit">Add</button>
</form>
<p><a href="/">⬅ Back to session list</a></p>
</body>
</html>
"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)