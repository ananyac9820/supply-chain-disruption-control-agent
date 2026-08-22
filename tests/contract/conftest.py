"""Run one test file against both sandboxes — §5 sandbox item 2.

StubSandbox is in-process and canned; HttpSandbox talks to the real FastAPI
app over loopback. If both pass the same suite, the hour-12 merge is a
base-URL swap and nothing else. That is the entire insurance policy.

The server runs in a thread inside the test process on an ephemeral port,
against a throwaway database, so the suite needs no external setup and
cannot touch a dev database.
"""

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Must be set before sandbox.db computes DB_PATH at import time.
_DB = Path(tempfile.gettempdir()) / "scda-contract-test.db"
os.environ["SCDA_DB"] = str(_DB)
_DB.unlink(missing_ok=True)

import uvicorn                                    # noqa: E402

from contracts.stub_sandbox import StubSandbox    # noqa: E402
from sandbox import db                            # noqa: E402
from sandbox.app import app                       # noqa: E402
from sandbox.client import HttpSandbox            # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_sandbox() -> str:
    db.init_db(reset=True)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("sandbox did not start within 10s")
        time.sleep(0.02)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def fresh_world():
    """Reseed before every test.

    The chaos injector mutates real state — that is the point of it — and the
    sandbox outlives any single test, so without this the suite passes or
    fails depending on the order pytest happens to pick. StubSandbox is built
    fresh per test anyway; this puts the live sandbox on the same footing.
    """
    db.init_db(reset=True)
    yield


@pytest.fixture(params=["stub", "http"])
def sandbox(request, live_sandbox):
    """The same suite, twice. Any divergence is a merge failure found early."""
    if request.param == "stub":
        return StubSandbox()
    return HttpSandbox(live_sandbox)
