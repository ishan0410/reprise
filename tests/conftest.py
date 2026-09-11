"""Shared fixtures: an in-process mock target app served over real HTTP."""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.request
from collections.abc import Iterator

import pytest
from werkzeug.serving import make_server

from cua.target_app.server import create_app

logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def mock_app_url() -> Iterator[str]:
    port = _free_port()
    server = make_server("127.0.0.1", port, create_app())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def admin_post(base_url: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    """Call the mock app's test-only /__admin endpoints."""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{base_url}{path}", data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return dict(json.loads(resp.read()))


@pytest.fixture
def mock_app(mock_app_url: str) -> str:
    """Base URL of the mock app with faults cleared and data reset for this test."""
    admin_post(mock_app_url, "/__admin/reset")
    return mock_app_url
