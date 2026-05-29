from __future__ import annotations

import signal
import time
import os

os.environ["CYCLE_ANALYST_SKIP_AUTO_APP"] = "1"
from cycle_server import initialize_runtime


_running = True


def _stop(_signum, _frame) -> None:
    global _running
    _running = False


def main() -> int:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    initialize_runtime(
        start_reader=True,
        start_gps=True,
        start_monitor=False,
    )
    print("[INIT] Cycle Analyst recorder started", flush=True)

    while _running:
        time.sleep(1)
    print("[STOP] Cycle Analyst recorder stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
