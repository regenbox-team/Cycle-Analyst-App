Acticycle All-Signals Live Debugger
===================================

This debug-only tool streams SocketCAN, decodes all available signals from both DBC files, and:

- Emits the standard 15-field Acticycle live line to stdout (compatible with the app's live mode).
- Writes a JSON Lines (one JSON object per line) snapshot of all latest decoded signals at a fixed rate.

Usage
-----

- Live (SocketCAN `can0`):

  `python debug/acticycle_all/live_all.py --channel can0`

- Custom DBC paths (comma-separated to merge):

  `python debug/acticycle_all/live_all.py --dbc can_util/Cockpit_CAN_Database_V1.4.dbc,can_util/Act2.5_database_can_A_V1.5.dbc`

- Change publish rate (Hz) and output file:

  `python debug/acticycle_all/live_all.py --rate 10 --outfile var/debug/all_signals.jsonl`

Notes
-----

- Stdout contains only the 15-field line (so it can be piped to the app via `exec:`). All debug detail goes to the JSONL file.
- stderr logs brief status messages and exceptions if any.
- Requires `python-can` and `cantools` (already in `requirements.txt`).

