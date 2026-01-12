from __future__ import annotations
import argparse
import base64
import json
import os
import urllib.parse
import urllib.request


def _auth_header(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _request_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def _download_session(base_url: str, headers: dict[str, str], device_id: str, session_id: str, mode: str) -> dict:
    params = urllib.parse.urlencode(
        {"device_id": device_id, "session_id": session_id, "mode": mode}
    )
    url = f"{base_url}/api/export_session?{params}"
    return _request_json(url, headers)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download sessions from Cycle Monitor server.")
    parser.add_argument("--device", required=True, help="Device id to download from")
    parser.add_argument("--mode", default="default", help="Mode name, default: default")
    parser.add_argument("--session", action="append", help="Specific session id to download (can repeat)")
    parser.add_argument("--out-dir", default="monitor_downloads", help="Output directory for JSON files")
    args = parser.parse_args()

    base_url = os.getenv("MONITOR_URL", "").rstrip("/")
    user = os.getenv("MONITOR_USER", "")
    password = os.getenv("MONITOR_PASS", "")
    if not base_url or not user or not password:
        raise SystemExit("Missing MONITOR_URL, MONITOR_USER, or MONITOR_PASS env vars.")

    headers = {"Accept": "application/json"}
    headers.update(_auth_header(user, password))

    os.makedirs(args.out_dir, exist_ok=True)

    sessions = args.session or []
    if not sessions:
        params = urllib.parse.urlencode({"device_id": args.device, "mode": args.mode})
        known_url = f"{base_url}/api/known_sessions?{params}"
        payload = _request_json(known_url, headers)
        sessions = payload.get("sessions", [])

    downloaded = 0
    for sid in sessions:
        if not sid:
            continue
        payload = _download_session(base_url, headers, args.device, sid, args.mode)
        if payload.get("logs") is None:
            continue
        safe_sid = sid.replace("/", "_")
        out_path = os.path.join(args.out_dir, f"{args.device}__{args.mode}__{safe_sid}.json")
        with open(out_path, "w") as f:
            json.dump(payload, f)
        downloaded += 1

    print(f"Downloaded {downloaded} session(s) to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
