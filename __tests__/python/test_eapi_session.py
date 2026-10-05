"""Cross-call authentication behavior; no third-party requests."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import python_bridge
from zlibrary.eapi import EAPIClient


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("ZLIBRARY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "test-credential-only")
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "library.example.test")
    state = {
        "logins": 0,
        "profiles": 0,
        "expired": False,
        "profile_error": None,
        "limited": False,
    }

    def handle(request):
        if request.url.path == "/eapi/user/login":
            state["logins"] += 1
            if state["limited"]:
                return httpx.Response(
                    400,
                    json={
                        "success": 0,
                        "error": "Too many logins #2. Try again later.",
                    },
                )
            state["expired"] = False
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"id": 123, "remix_userkey": "test-cookie-value"},
                },
            )
        assert request.url.path == "/eapi/user/profile"
        state["profiles"] += 1
        assert "remix_userkey=test-cookie-value" in request.headers["cookie"]
        if state["profile_error"]:
            raise httpx.ConnectError("offline", request=request)
        if state["expired"]:
            return httpx.Response(400, json={"success": 0, "error": "Please login"})
        return httpx.Response(200, json={"success": 1, "user": {"id": 123}})

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle), cookies=self._cookies
            )
        return self._client

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    return state


async def call_and_close():
    client = await python_bridge.initialize_eapi_client()
    await client.close()
    python_bridge._eapi_client = None  # next bridge has no module-level state


async def test_consecutive_bridge_calls_login_only_once(service):
    await call_and_close()
    await call_and_close()
    assert service["logins"] == 1
    assert service["profiles"] == 1


async def test_expired_cookie_is_replaced_once(service):
    await call_and_close()
    service["expired"] = True
    await call_and_close()
    await call_and_close()
    assert service["logins"] == 2


async def test_network_failure_does_not_trigger_login(service):
    await call_and_close()
    service["profile_error"] = True
    with pytest.raises(httpx.ConnectError):
        await call_and_close()
    assert service["logins"] == 1
    service["profile_error"] = None
    await call_and_close()
    assert service["logins"] == 1


async def test_changed_password_cannot_reuse_session(service, monkeypatch):
    await call_and_close()
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "different-test-credential")
    await call_and_close()
    assert service["logins"] == 2


async def test_concurrent_calls_share_one_login(service):
    await asyncio.gather(*(call_and_close() for _ in range(4)))
    assert service["logins"] == 1


async def test_login_limit_has_clear_error_and_suppresses_immediate_retries(service):
    service["limited"] = True
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Too many logins"):
            await call_and_close()
    assert service["logins"] == 1


async def test_session_is_private_and_contains_no_password(service, monkeypatch):
    await call_and_close()
    root = Path(os.environ["ZLIBRARY_SESSION_DIR"])
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700
    files = list(root.glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert "test-credential-only" not in files[0].read_text()
    assert "reader@example.test" not in files[0].read_text()


async def test_account_and_domain_changes_do_not_share_cookies(service, monkeypatch):
    await call_and_close()
    monkeypatch.setenv("ZLIBRARY_EMAIL", "another-reader@example.test")
    await call_and_close()
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "another.example.test")
    await call_and_close()
    assert service["logins"] == 3


async def test_corrupt_session_is_replaced(service):
    await call_and_close()
    path = next(Path(os.environ["ZLIBRARY_SESSION_DIR"]).glob("*.json"))
    path.write_text("{broken")
    await call_and_close()
    await call_and_close()
    assert service["logins"] == 2


async def test_public_session_directory_is_rejected_before_login(service):
    root = Path(os.environ["ZLIBRARY_SESSION_DIR"])
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(RuntimeError, match="0700"):
        await call_and_close()
    assert service["logins"] == 0


async def test_symlink_session_directory_is_rejected(service, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("ZLIBRARY_SESSION_DIR", str(link))
    with pytest.raises(RuntimeError, match="symlink"):
        await call_and_close()
    assert service["logins"] == 0


async def test_cancelled_lock_holder_does_not_block_next_call(tmp_path):
    from lib.eapi_session import _locked

    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with _locked(tmp_path / "session.lock"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def reacquire():
        async with _locked(tmp_path / "session.lock"):
            pass

    await asyncio.wait_for(reacquire(), timeout=1)


async def test_separate_python_processes_share_one_login(service, tmp_path):
    """Exercise the actual bridge lifecycle, not just a cached Python global."""
    script = r"""
import asyncio, os, sys
from pathlib import Path
import httpx
sys.path.insert(0, "lib")
import python_bridge
from zlibrary.eapi import EAPIClient

def handle(request):
    if request.url.path == "/eapi/user/login":
        with open(os.environ["LOGIN_COUNTER"], "a") as out:
            out.write("login\n")
        return httpx.Response(200, json={"success": 1, "user": {"id": 123, "remix_userkey": "test-cookie"}})
    assert request.url.path == "/eapi/user/profile"
    assert "remix_userkey=test-cookie" in request.headers["cookie"]
    return httpx.Response(200, json={"success": 1, "user": {"id": 123}})

async def get_client(self):
    if self._client is None or self._client.is_closed:
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handle), cookies=self._cookies)
    return self._client

EAPIClient._get_client = get_client
async def run():
    client = await python_bridge.initialize_eapi_client()
    await client.close()
asyncio.run(run())
"""
    counter = tmp_path / "logins.txt"
    env = {**os.environ, "LOGIN_COUNTER": str(counter)}
    root = Path(__file__).resolve().parents[2]

    async def child():
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        assert proc.returncode == 0, stderr.decode()
        assert stdout == b""

    await asyncio.gather(*(child() for _ in range(3)))
    assert counter.read_text().splitlines() == ["login"]


@pytest.fixture
def rotating_domains(monkeypatch, tmp_path):
    from zlibrary import eapi

    monkeypatch.setenv("ZLIBRARY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "offline-domain-recovery-credential")
    monkeypatch.delenv("ZLIBRARY_ACCOUNT_CREDENTIALS", raising=False)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN", raising=False)
    monkeypatch.setattr(
        eapi, "DEFAULT_EAPI_DOMAINS", ["a.example.test", "b.example.test"]
    )
    state = {
        "failure": None,
        "b_failure": None,
        "b_profile_failure": None,
        "logins": 0,
        "profiles": [],
        "clients": [],
    }

    def handle(request):
        domain = request.url.host
        failure = state["failure"] if domain == "a.example.test" else state["b_failure"]
        if request.url.path == "/eapi/user/profile":
            state["profiles"].append(domain)
            assert "remix_userkey=offline-cookie" in request.headers["cookie"]
            if domain == "b.example.test":
                failure = state["b_profile_failure"] or failure
        if failure == "network":
            raise httpx.ConnectError("domain unavailable", request=request)
        if failure == "wall":
            return httpx.Response(513, text="DiamWall")
        if failure == "upstream":
            return httpx.Response(503, text="Unavailable")
        if request.url.path == "/eapi/info/domains":
            return httpx.Response(200, json={"domains": []})
        if request.url.path == "/eapi/user/login":
            state["logins"] += 1
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "user": {"id": 123, "remix_userkey": "offline-cookie"},
                },
            )
        assert request.url.path == "/eapi/user/profile"
        if failure == "expired":
            return httpx.Response(401, json={"error": "Please login"})
        return httpx.Response(200, json={"success": 1, "user": {"id": 123}})

    transport = httpx.MockTransport(handle)

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=transport, cookies=self._cookies)
            state["clients"].append(self._client)
        return self._client

    async def resolve():
        return await eapi.resolve_eapi_domain(transport=transport)

    monkeypatch.setattr(EAPIClient, "_get_client", get_client)
    monkeypatch.setattr(python_bridge, "resolve_eapi_domain", resolve)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    return state


@pytest.mark.parametrize("failure", ["network", "wall", "upstream"])
async def test_cached_session_recovers_domain_without_login(rotating_domains, failure):
    await call_and_close()
    rotating_domains["failure"] = failure
    await call_and_close()
    await call_and_close()
    assert rotating_domains["logins"] == 1
    assert rotating_domains["profiles"] == [
        "a.example.test",
        "b.example.test",
        "b.example.test",
    ]
    record = json.loads(
        next(Path(os.environ["ZLIBRARY_SESSION_DIR"]).glob("*.json")).read_text()
    )
    assert record["domain"] == "b.example.test"
    assert all(client.is_closed for client in rotating_domains["clients"])


@pytest.mark.parametrize("failure", ["network", "wall"])
async def test_pinned_cached_domain_does_not_switch(
    rotating_domains, monkeypatch, failure
):
    from zlibrary.eapi import DiamWallError

    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "a.example.test")
    await call_and_close()
    rotating_domains["failure"] = failure
    with pytest.raises((httpx.ConnectError, DiamWallError)):
        await call_and_close()
    assert rotating_domains["logins"] == 1
    assert rotating_domains["profiles"] == ["a.example.test"]
    assert all(client.is_closed for client in rotating_domains["clients"])


async def test_failed_domain_recovery_retains_cookies_without_login(rotating_domains):
    await call_and_close()
    path = next(Path(os.environ["ZLIBRARY_SESSION_DIR"]).glob("*.json"))
    original = path.read_bytes()
    rotating_domains["failure"] = "network"
    rotating_domains["b_failure"] = "network"
    with pytest.raises(httpx.ConnectError):
        await call_and_close()
    assert path.read_bytes() == original
    assert rotating_domains["logins"] == 1
    assert all(client.is_closed for client in rotating_domains["clients"])
    rotating_domains["b_failure"] = None
    await call_and_close()
    assert rotating_domains["logins"] == 1


async def test_expired_cookies_on_recovered_domain_allow_login(rotating_domains):
    await call_and_close()
    rotating_domains["failure"] = "network"
    rotating_domains["b_failure"] = "expired"
    await call_and_close()
    assert rotating_domains["logins"] == 2
    assert rotating_domains["profiles"] == ["a.example.test", "b.example.test"]
    assert all(client.is_closed for client in rotating_domains["clients"])


@pytest.mark.parametrize("failure", ["network", "wall", "upstream"])
async def test_recovered_domain_profile_failure_keeps_cache(rotating_domains, failure):
    from zlibrary.eapi import DiamWallError

    await call_and_close()
    path = next(Path(os.environ["ZLIBRARY_SESSION_DIR"]).glob("*.json"))
    original = path.read_bytes()
    rotating_domains["failure"] = "network"
    rotating_domains["b_profile_failure"] = failure
    with pytest.raises((httpx.ConnectError, httpx.HTTPStatusError, DiamWallError)):
        await call_and_close()
    assert rotating_domains["profiles"] == ["a.example.test", "b.example.test"]
    assert rotating_domains["logins"] == 1
    assert path.read_bytes() == original
    assert all(client.is_closed for client in rotating_domains["clients"])
