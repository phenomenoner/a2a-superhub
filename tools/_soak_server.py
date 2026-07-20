"""Private process boundary used by the public single-hub soak harness."""

from __future__ import annotations

import argparse
import json
import sys
import threading

from a2a_superhub.server import make_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--principals", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    with open(args.principals, encoding="utf-8") as handle:
        principals = json.load(handle)
    server = make_server(
        args.state,
        host="127.0.0.1",
        port=args.port,
        principals=principals,
        enable_memory=True,
        enable_delivery=True,
        enable_task_log=True,
        enable_watcher_side_effects=True,
        task_log_intents={"soak.observe"},
        enable_derivers=True,
        max_artifact_bytes=2_000_000,
    )

    def control() -> None:
        for line in sys.stdin:
            if line.strip() == "shutdown":
                server.shutdown()
                return

    threading.Thread(target=control, daemon=True).start()
    print(json.dumps({"ready": True, "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
