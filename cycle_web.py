from __future__ import annotations

import os

os.environ["CYCLE_ANALYST_SKIP_AUTO_APP"] = "1"
from cycle_server import create_app
from app import state


app = create_app(start_reader=False, start_gps=False, start_monitor=False)


if __name__ == "__main__":
    print(f"[INIT] Loaded current user: {state.current_user}", flush=True)
    app.run(host="0.0.0.0", port=5050, threaded=True)
