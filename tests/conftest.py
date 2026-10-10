import atexit
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HOME = tempfile.mkdtemp(prefix="agentloop-test-")
os.environ["AGENTLOOP_HOME"] = HOME
FAKE = Path(__file__).parent / "fake_agent.py"


@atexit.register
def _cleanup():
    """The test home and its scratch root go once the run (and its server thread) has ended."""
    from agentloop.runner import scratch_root
    for path in (scratch_root(), HOME):
        shutil.rmtree(path, ignore_errors=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    """A live control plane on a free port, with a scripted 'fake' agent registered."""
    import httpx
    import uvicorn

    from agentloop import workspace
    from agentloop.adapters import ADAPTERS
    from agentloop.adapters.claude import ClaudeAdapter
    from agentloop.server import create_app

    class FakeAdapter(ClaudeAdapter):
        name = "fake"

        def argv(self, s):
            return [sys.executable, str(FAKE)] + (["--resume", s.resume_id] if s.resume_id else [])

    ADAPTERS["fake"] = FakeAdapter()
    # The shared venv without pip: same layout and activation, without seconds of ensurepip per workspace
    ensure_venv = workspace.ensure_venv
    workspace.ensure_venv = lambda ws, with_pip=False: ensure_venv(ws, with_pip)
    port = free_port()
    app = create_app(f"http://127.0.0.1:{port}")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        yield client, app.state.engine
    srv.should_exit = True
    thread.join(timeout=10)


def wait_for(fn, timeout=60, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")
