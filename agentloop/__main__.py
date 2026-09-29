"""`agentloop`: admin CLI (serve the platform, create workspaces, smoke-test agent CLIs)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging


def main() -> None:
    p = argparse.ArgumentParser(prog="agentloop")
    sub = p.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the control plane and web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    init = sub.add_parser("init", help="create a workspace")
    init.add_argument("name")
    init.add_argument("--repo", help="git URL or local path to clone (default: empty repo)")
    init.add_argument("--base", help="base branch")
    sub.add_parser("smoke", help="run one tiny turn through each agent CLI")
    a = p.parse_args()

    if a.cmd == "serve":
        import uvicorn

        from .server import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        uvicorn.run(create_app(f"http://{a.host}:{a.port}"), host=a.host, port=a.port, log_level="warning",
                    timeout_graceful_shutdown=5)
    elif a.cmd == "init":
        from .workspace import create_workspace
        print(create_workspace(a.name, a.repo, a.base))
    elif a.cmd == "smoke":
        from .agents import smoke
        for name in ("claude", "codex"):
            print(name, json.dumps(asyncio.run(smoke(name))))


if __name__ == "__main__":
    main()
