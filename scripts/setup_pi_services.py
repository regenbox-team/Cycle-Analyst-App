#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _default_repo() -> Path:
    return ROOT


def _default_user() -> str:
    return os.getenv("SUDO_USER") or getpass.getuser()


def _default_server_name() -> str:
    host = socket.gethostname()
    return f"{host}.local {host}"


def _service_env(repo: Path) -> str:
    venv_bin = repo / ".venv" / "bin"
    return "\n".join(
        [
            f"WorkingDirectory={repo}",
            f"EnvironmentFile=-{repo / 'cycle-analyst.env'}",
            "Environment=PYTHONUNBUFFERED=1",
            f"Environment=PATH={venv_bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ]
    )


def recorder_service(repo: Path, user: str, group: str) -> str:
    return f"""[Unit]
Description=Cycle Analyst Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups=dialout i2c video
{_service_env(repo)}
Environment=APP_SCHEDULE_PHOTOS=0
Environment=APP_PRESERVE_PHOTO_STATE_FROM_FILE=1
ExecStart={repo / '.venv' / 'bin' / 'python'} {repo / 'cycle_recorder.py'}
Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


def photo_service(repo: Path, user: str, group: str) -> str:
    return f"""[Unit]
Description=Cycle Analyst Photo Worker
After=network-online.target cycle-recorder.service
Wants=network-online.target cycle-recorder.service

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups=dialout i2c video
{_service_env(repo)}
Environment=APP_START_READER=0
Environment=APP_START_GPS=0
Environment=APP_START_MONITOR=0
Environment=APP_LIVE_STATE_FROM_FILES=1
Environment=APP_SCHEDULE_PHOTOS=1
ExecStart={repo / '.venv' / 'bin' / 'python'} {repo / 'cycle_photo_worker.py'}
Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=35

[Install]
WantedBy=multi-user.target
"""


def web_service(repo: Path, user: str, group: str) -> str:
    return f"""[Unit]
Description=Cycle Analyst Web Dashboard
After=network-online.target cycle-recorder.service
Wants=network-online.target cycle-recorder.service

[Service]
Type=simple
User={user}
Group={group}
SupplementaryGroups=dialout i2c video
{_service_env(repo)}
Environment=APP_START_READER=0
Environment=APP_START_GPS=0
Environment=APP_START_MONITOR=0
Environment=APP_LIVE_STATE_FROM_FILES=1
ExecStart={repo / '.venv' / 'bin' / 'python'} {repo / 'cycle_web.py'}
Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


def nginx_site(server_name: str) -> str:
    return f"""server {{
    listen 80;
    server_name {server_name};

    location / {{
        proxy_pass http://127.0.0.1:5050;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }}
}}
"""


def sudo_write(path: str, content: str, *, apply: bool) -> None:
    if not apply:
        print(f"\n--- {path} ---\n{content.rstrip()}\n")
        return

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        subprocess.run(["sudo", "cp", tmp_path, path], check=True)
        subprocess.run(["sudo", "chmod", "644", path], check=True)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def run(cmd: list[str], *, apply: bool) -> None:
    print("+ " + " ".join(cmd))
    if apply:
        subprocess.run(cmd, check=True)


def ensure_env_file(repo: Path, *, apply: bool) -> None:
    env_path = repo / "cycle-analyst.env"
    example_path = repo / "cycle-analyst.env.example"
    if env_path.exists() or not example_path.exists():
        return
    print(f"+ copy {example_path} -> {env_path}")
    if apply:
        shutil.copy2(example_path, env_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install/update Cycle Analyst recorder, photo, web, and nginx services on a Raspberry Pi."
    )
    parser.add_argument("--apply", action="store_true", help="write files and run systemctl/nginx commands")
    parser.add_argument("--repo", type=Path, default=_default_repo(), help="repository path")
    parser.add_argument("--user", default=_default_user(), help="Linux user running the app")
    parser.add_argument("--group", default=None, help="Linux group running the app, defaults to --user")
    parser.add_argument("--server-name", default=_default_server_name(), help="nginx server_name value")
    parser.add_argument("--no-nginx", action="store_true", help="skip nginx site installation")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = args.repo.expanduser().resolve()
    group = args.group or args.user

    if not repo.exists():
        print(f"Repo not found: {repo}", file=sys.stderr)
        return 2
    venv_python = repo / ".venv" / "bin" / "python"
    if not venv_python.exists():
        message = f"Missing virtualenv python: {venv_python}"
        if args.apply:
            print(message, file=sys.stderr)
            print("Create it first with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt", file=sys.stderr)
            return 2
        print(f"Warning: {message}")

    if not args.apply:
        print("Dry run only. Re-run with --apply to install/update services.")

    ensure_env_file(repo, apply=args.apply)
    sudo_write("/etc/systemd/system/cycle-recorder.service", recorder_service(repo, args.user, group), apply=args.apply)
    sudo_write("/etc/systemd/system/cycle-photo.service", photo_service(repo, args.user, group), apply=args.apply)
    sudo_write("/etc/systemd/system/cycle-analyst.service", web_service(repo, args.user, group), apply=args.apply)

    run(["sudo", "systemctl", "daemon-reload"], apply=args.apply)
    run(["sudo", "systemctl", "enable", "--now", "cycle-recorder.service"], apply=args.apply)
    run(["sudo", "systemctl", "enable", "--now", "cycle-photo.service"], apply=args.apply)
    run(["sudo", "systemctl", "enable", "--now", "cycle-analyst.service"], apply=args.apply)

    if not args.no_nginx:
        sudo_write("/etc/nginx/sites-available/cycle-analyst", nginx_site(args.server_name), apply=args.apply)
        run(["sudo", "ln", "-sf", "/etc/nginx/sites-available/cycle-analyst", "/etc/nginx/sites-enabled/cycle-analyst"], apply=args.apply)
        run(["sudo", "rm", "-f", "/etc/nginx/sites-enabled/default"], apply=args.apply)
        run(["sudo", "nginx", "-t"], apply=args.apply)
        run(["sudo", "systemctl", "enable", "--now", "nginx"], apply=args.apply)
        run(["sudo", "systemctl", "reload", "nginx"], apply=args.apply)

    print("\nDone." if args.apply else "\nDry run complete.")
    print("Check with:")
    print("  systemctl status cycle-recorder.service cycle-photo.service cycle-analyst.service --no-pager")
    print("  curl http://127.0.0.1:5050/metrics")
    print("  curl http://127.0.0.1/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
