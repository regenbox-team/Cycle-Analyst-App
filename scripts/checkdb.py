#!/usr/bin/env python3
import argparse
import sqlite3
from datetime import datetime
from statistics import median
from typing import Dict, Tuple, Optional, List

def parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s2 = s.strip()
    if 'T' in s2:
        s2 = s2.replace('T', ' ', 1)
    if s2.endswith('Z'):
        s2 = s2[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(s2, fmt)
            except Exception:
                pass
    return None

def fetch_basic(con) -> List[Tuple[str, int, Optional[str], Optional[str]]]:
    cur = con.cursor()
    cur.execute('SELECT session, COUNT(*), MIN(timestamp), MAX(timestamp) FROM logs GROUP BY session')
    return cur.fetchall()

def fetch_timestamps(con, session: str) -> List[datetime]:
    cur = con.cursor()
    cur.execute('SELECT timestamp FROM logs WHERE session = ? ORDER BY timestamp', (session,))
    out = []
    for (ts,) in cur.fetchall():
        dt = parse_ts(ts)
        if dt: out.append(dt)
    return out

def active_duration_seconds(timestamps: List[datetime], max_gap: Optional[float],
                            auto_mult: float, auto_min: float, auto_max: float) -> Tuple[float, float, int]:
    """Returns (active_seconds, threshold_used, segments).
    active_seconds sums only deltas <= threshold; segments counts contiguous chunks."""
    if len(timestamps) < 2:
        return 0.0, max_gap or 0.0, len(timestamps)
    # Compute inter-sample deltas in seconds
    deltas = [(b - a).total_seconds() for a, b in zip(timestamps[:-1], timestamps[1:])]
    # Determine threshold
    if max_gap is not None and max_gap > 0:
        thr = float(max_gap)
    else:
        med = median(deltas) if deltas else 0.1
        thr = med * auto_mult
        if auto_min > 0: thr = max(thr, auto_min)
        if auto_max > 0: thr = min(thr, auto_max)
    # Sum only small deltas, count segments
    active = 0.0
    segments = 1 if timestamps else 0
    prev_small = True  # first point starts a segment
    for d in deltas:
        if d <= thr:
            active += d
            prev_small = True
        else:
            # big gap: recording stopped; next small delta starts a new segment
            if prev_small:
                segments += 1
            prev_small = False
    return active, thr, segments

def fmt_hms(seconds: Optional[float]) -> str:
    if seconds is None:
        return ''
    seconds = max(0, int(round(seconds)))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f'{h:d}:{m:02d}:{s:02d}'

def summarize(db_path: str, max_gap: Optional[float], auto_mult: float, auto_min: float, auto_max: float):
    con = sqlite3.connect(db_path)
    basics = fetch_basic(con)
    results = []
    for session, n, t0, t1 in basics:
        ts_list = fetch_timestamps(con, session)
        active, thr, segments = active_duration_seconds(ts_list, max_gap, auto_mult, auto_min, auto_max)
        dt0 = parse_ts(t0)
        dt1 = parse_ts(t1)
        wall = (dt1 - dt0).total_seconds() if (dt0 and dt1) else None
        cov = (active / wall * 100.0) if (wall and wall > 0) else None
        results.append({
            'session': session, 'rows': int(n), 'start_ts': t0, 'end_ts': t1,
            'wall_s': wall, 'active_s': active, 'coverage': cov, 'segments': segments, 'thr': thr
        })
    con.close()
    # sort by start_ts
    results.sort(key=lambda r: (r['start_ts'] or '', r['session']))
    return results

def print_table(results: List[dict], show_thr: bool):
    sess_w = max(7, max((len(str(r['session'])) for r in results), default=7))
    cols = [('rows',8), ('start_ts',19), ('end_ts',19), ('wall',9), ('active',9), ('cov%',7), ('seg',4)]
    if show_thr: cols.append(('thr',6))
    header = f"{'session'.ljust(sess_w)}  " + '  '.join([
        f"{name:>{w}}" for name, w in cols
    ])
    sep = '-' * len(header)
    print(header)
    print(sep)
    total_rows = 0
    for r in results:
        total_rows += r['rows']
        wall = fmt_hms(r['wall_s']) if r['wall_s'] is not None else ''
        active = fmt_hms(r['active_s'])
        cov = '' if r['coverage'] is None else f"{r['coverage']:.1f}"
        items = [
            f"{r['rows']:>8d}", f"{(r['start_ts'] or '')[:19]:19}", f"{(r['end_ts'] or '')[:19]:19}",
            f"{wall:>9}", f"{active:>9}", f"{cov:>7}", f"{r['segments']:>4d}",
        ]
        if show_thr: items.append(f"{r['thr']:.2f}")
        print(f"{str(r['session']).ljust(sess_w)}  " + '  '.join(items))
    print(sep)
    print(f"{'TOTAL'.ljust(sess_w)}  {total_rows:8d}")

def print_csv(results: List[dict], out):
    w = csv.writer(out)
    w.writerow(['session','rows','start_ts','end_ts','wall_s','active_s','wall_hms','active_hms','coverage_pct','segments','threshold_s'])
    for r in results:
        w.writerow([r['session'], r['rows'], r['start_ts'], r['end_ts'],
                    '' if r['wall_s'] is None else int(round(r['wall_s'])),
                    int(round(r['active_s'])),
                    fmt_hms(r['wall_s']), fmt_hms(r['active_s']),
                    '' if r['coverage'] is None else f"{r['coverage']:.1f}",
                    r['segments'], f"{r['thr']:.2f}"])

def main():
    p = argparse.ArgumentParser(description='Summarize sessions with ACTIVE recording duration (ignoring long gaps).')
    try:
        from app.config import DB_FILE as DEFAULT_DB
    except Exception:
        DEFAULT_DB = 'var/ride_data.db'
    p.add_argument('db', nargs='?', default=DEFAULT_DB, help='Path to DB (default: var/ride_data.db)')
    p.add_argument('--compare', help='Optional: second DB to check sessions/row counts against')
    p.add_argument('--csv', action='store_true', help='Output CSV instead of a table')
    p.add_argument('--max-gap', type=float, default=None,
                   help='Treat gaps larger than this (seconds) as recording stops. If omitted, threshold is auto = median_gap * mult, clamped.')
    p.add_argument('--auto-mult', type=float, default=10.0, help='Multiplier for auto threshold (default: 10× median gap)')
    p.add_argument('--auto-min', type=float, default=0.5, help='Minimum auto threshold in seconds (default: 0.5)')
    p.add_argument('--auto-max', type=float, default=5.0, help='Maximum auto threshold in seconds (default: 5.0)')
    p.add_argument('--show-threshold', action='store_true', help='Show threshold used per session (s)')
    args = p.parse_args()

    results = summarize(args.db, args.max_gap, args.auto_mult, args.auto_min, args.auto_max)
    if args.csv:
        print_csv(results, out=io.TextIOWrapper(buffer=None, encoding='utf-8'))  # will be ignored; fall back to print
    else:
        print_table(results, show_thr=args.show_threshold)

    if args.compare:
        # Basic compare (names and row counts)
        con_main = sqlite3.connect(args.db)
        con_cmp = sqlite3.connect(args.compare)
        a = {s: n for s, n, _, _ in fetch_basic(con_main)}
        b = {s: n for s, n, _, _ in fetch_basic(con_cmp)}
        con_main.close(); con_cmp.close()
        miss_in_main = sorted(set(b) - set(a))
        miss_in_cmp = sorted(set(a) - set(b))
        diffs = sorted([s for s in set(a)&set(b) if a[s] != b[s]])
        print('\n=== COMPARE ===')
        if miss_in_main:
            print('Sessions present in --compare DB but MISSING in main:')
            for s in miss_in_main: print(' ', s)
        if miss_in_cmp:
            print('Sessions present in main but MISSING in --compare DB:')
            for s in miss_in_cmp: print(' ', s)
        if diffs:
            print('Sessions with row-count differences (main vs compare):')
            for s in diffs: print(f'  {s}: {a[s]} vs {b[s]} (Δ {a[s]-b[s]:+d})')
        if not (miss_in_main or miss_in_cmp or diffs):
            print('OK: All sessions match between DBs (names and row counts).')

if __name__ == '__main__':
    main()
