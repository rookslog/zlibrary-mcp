"""Offline transport mistakes must fail even when application code swallows them."""

import os
from pathlib import Path
import re
import subprocess
import sys


def test_swallowed_real_transport_attempts_fail_teardown_before_dns(tmp_path):
    (tmp_path / "conftest.py").write_bytes(
        Path(__file__).with_name("conftest.py").read_bytes()
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_swallowed.py").write_text(
        """import asyncio
import socket
import httpx

def test_swallowed(monkeypatch):
    lookups = []
    def refuse_dns(*args, **kwargs):
        lookups.append(True)
        raise AssertionError("DNS must not run")
    monkeypatch.setattr(socket, "getaddrinfo", refuse_dns)
    url = "http://blocked.invalid/?key=transport-key-canary"
    with httpx.Client(trust_env=False) as client:
        try:
            client.get(url)
        except Exception:
            pass
    async def asynchronous():
        async with httpx.AsyncClient(trust_env=False) as client:
            try:
                await client.get(url)
            except Exception:
                pass
    asyncio.run(asynchronous())
    assert lookups == []
"""
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-c", str(tmp_path / "pytest.ini")],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert re.search(r"1 passed.*1 error", output), output
    assert "2 real non-loopback HTTP transport attempt(s) blocked before DNS" in output
    assert "transport-key-canary" not in output


def test_integration_marker_reaches_sync_and_async_real_transports(tmp_path):
    """The offline guard must not disable the deliberately live test suite."""
    (tmp_path / "conftest.py").write_bytes(
        Path(__file__).with_name("conftest.py").read_bytes()
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    integration: intentionally live tests\n"
    )
    (tmp_path / "test_integration.py").write_text(
        """import asyncio
import socket
import httpcore
import httpx
import pytest

@pytest.mark.integration
def test_transports(monkeypatch):
    reached = []
    def no_dns(*args, **kwargs):
        raise AssertionError("DNS must not run")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    def stop_sync(self, request):
        reached.append("sync")
        raise RuntimeError("synthetic pool reached")
    async def stop_async(self, request):
        reached.append("async")
        raise RuntimeError("synthetic pool reached")
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", stop_sync)
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", stop_async)
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(RuntimeError, match="synthetic pool reached"):
            client.get("http://integration.invalid/")
    async def asynchronous():
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(RuntimeError, match="synthetic pool reached"):
                await client.get("http://integration.invalid/")
    asyncio.run(asynchronous())
    assert reached == ["sync", "async"]
"""
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "integration",
         "-c", str(tmp_path / "pytest.ini")],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output, output
