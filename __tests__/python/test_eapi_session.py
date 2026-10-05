# Tests for lib/eapi_session.py — the persistent EAPI session cache.

import json
import os
import sys

import pytest

# Add lib directory to sys.path explicitly (mirrors test_python_bridge.py)
sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
)

import python_bridge  # noqa: E402
from eapi_session import (  # noqa: E402
    clear_eapi_session,
    default_session_path,
    load_eapi_session,
    save_eapi_session,
)

# Deliberately fake credentials: these tests must never carry real secrets.
FAKE_EMAIL = "unit-test@example.com"
FAKE_PASSWORD = "unit-test-not-a-real-secret"


class FakeEAPIClient:
    """Offline stand-in for EAPIClient recording what the bridge did to it."""

    instances: list["FakeEAPIClient"] = []
    profile_response: object = {"success": 1, "user": {"downloads_limit": 10}}

    def __init__(self, domain, remix_userid=None, remix_userkey=None):
        self.domain = domain
        self.remix_userid = remix_userid
        self.remix_userkey = remix_userkey
        self.login_calls = 0
        self.profile_calls = 0
        self.closed = False
        FakeEAPIClient.instances.append(self)

    async def login(self, email, password):
        self.login_calls += 1
        self.remix_userid = "42"
        self.remix_userkey = "fresh-key"
        return {"success": 1, "user": {"id": "42", "remix_userkey": "fresh-key"}}

    async def get_profile(self):
        self.profile_calls += 1
        return FakeEAPIClient.profile_response

    async def get_domains(self):
        return {"domains": []}

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_bridge_state():
    python_bridge._eapi_client = None
    FakeEAPIClient.instances = []
    FakeEAPIClient.profile_response = {"success": 1, "user": {"downloads_limit": 10}}
    yield
    python_bridge._eapi_client = None


@pytest.fixture
def bridge_env(monkeypatch, tmp_path):
    """Credentials + cache location pointed at a disposable file."""
    session_path = tmp_path / "session.json"
    monkeypatch.setenv("ZLIBRARY_EMAIL", FAKE_EMAIL)
    monkeypatch.setenv("ZLIBRARY_PASSWORD", FAKE_PASSWORD)
    monkeypatch.setenv("ZLIBRARY_SESSION_FILE", str(session_path))
    # Pinning the domain keeps domain discovery (and its probes) out of tests.
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "z-library.ec")
    return session_path


class TestSessionCacheUnit:
    def test_save_then_load_roundtrip(self, tmp_path):
        path = tmp_path / "nested" / "session.json"
        save_eapi_session("z-library.ec", 42, "abc123", path)
        session = load_eapi_session(path)
        assert session == {
            "domain": "z-library.ec",
            "remix_userid": "42",
            "remix_userkey": "abc123",
        }

    def test_save_creates_parents_and_restricts_mode(self, tmp_path):
        path = tmp_path / "deep" / "deeper" / "session.json"
        save_eapi_session("z-library.ec", "1", "k", path)
        assert path.exists()
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600

    def test_save_leaves_no_temporary_litter(self, tmp_path):
        path = tmp_path / "session.json"
        save_eapi_session("z-library.ec", "1", "k", path)
        leftovers = [p for p in tmp_path.iterdir() if p.name != "session.json"]
        assert leftovers == []

    def test_save_with_missing_values_writes_nothing(self, tmp_path):
        path = tmp_path / "session.json"
        save_eapi_session("z-library.ec", "", "k", path)
        save_eapi_session("", "1", "k", path)
        assert not path.exists()

    def test_load_missing_file_is_none(self, tmp_path):
        assert load_eapi_session(tmp_path / "absent.json") is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not json {",
            '"a string"',
            "[1, 2, 3]",
            json.dumps({"domain": "z-library.ec"}),  # missing cookie keys
            json.dumps(
                {"domain": "z-library.ec", "remix_userid": "", "remix_userkey": "k"}
            ),
            json.dumps({"domain": "", "remix_userid": "1", "remix_userkey": "k"}),
        ],
    )
    def test_load_malformed_is_none(self, tmp_path, payload):
        path = tmp_path / "session.json"
        path.write_text(payload, encoding="utf-8")
        assert load_eapi_session(path) is None

    def test_env_var_overrides_default_path(self, monkeypatch, tmp_path):
        override = tmp_path / "elsewhere.json"
        monkeypatch.setenv("ZLIBRARY_SESSION_FILE", str(override))
        assert default_session_path() == override

    def test_clear_removes_file_and_tolerates_absence(self, tmp_path):
        path = tmp_path / "session.json"
        save_eapi_session("z-library.ec", "1", "k", path)
        clear_eapi_session(path)
        assert not path.exists()
        clear_eapi_session(path)  # no error on the second call


class TestInitializeReusesCachedSession:
    async def test_valid_cache_skips_login(self, bridge_env, mocker):
        save_eapi_session("cached.example", "7", "cached-key", bridge_env)
        resolve_mock = mocker.patch.object(
            python_bridge,
            "resolve_eapi_domain",
            new=mocker.AsyncMock(return_value="resolved.example"),
        )
        mocker.patch.object(python_bridge, "EAPIClient", FakeEAPIClient)

        client = await python_bridge.initialize_eapi_client()

        assert client.remix_userid == "7"
        assert client.remix_userkey == "cached-key"
        # A pinned domain (bridge_env) wins over the cached one.
        assert client.domain == "z-library.ec"
        assert client.login_calls == 0
        resolve_mock.assert_not_awaited()
        assert python_bridge._eapi_client is client

    async def test_valid_cache_uses_cached_domain_when_not_pinned(
        self, bridge_env, monkeypatch, mocker
    ):
        monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
        save_eapi_session("cached.example", "7", "cached-key", bridge_env)
        mocker.patch.object(python_bridge, "EAPIClient", FakeEAPIClient)

        client = await python_bridge.initialize_eapi_client()

        assert client.domain == "cached.example"
        assert client.login_calls == 0

    async def test_rejected_cache_falls_back_to_login(self, bridge_env, mocker):
        save_eapi_session("cached.example", "7", "dead-key", bridge_env)
        FakeEAPIClient.profile_response = {"success": 0}
        mocker.patch.object(
            python_bridge,
            "resolve_eapi_domain",
            new=mocker.AsyncMock(return_value="z-library.ec"),
        )
        mocker.patch.object(python_bridge, "EAPIClient", FakeEAPIClient)

        client = await python_bridge.initialize_eapi_client()

        candidate, fresh = FakeEAPIClient.instances
        assert candidate.closed is True
        assert fresh.login_calls == 1
        assert client is fresh
        assert client.remix_userid == "42"
        # The dead cache was replaced with the fresh login result.
        assert load_eapi_session(bridge_env) == {
            "domain": "z-library.ec",
            "remix_userid": "42",
            "remix_userkey": "fresh-key",
        }

    async def test_network_failure_on_cache_falls_back_to_login(
        self, bridge_env, mocker
    ):
        save_eapi_session("cached.example", "7", "cached-key", bridge_env)

        class ExplodingProfile(FakeEAPIClient):
            async def get_profile(self):
                raise OSError("network down")

        mocker.patch.object(
            python_bridge,
            "resolve_eapi_domain",
            new=mocker.AsyncMock(return_value="z-library.ec"),
        )
        mocker.patch.object(python_bridge, "EAPIClient", ExplodingProfile)

        client = await python_bridge.initialize_eapi_client()

        assert client.login_calls == 1
        assert load_eapi_session(bridge_env) is not None


class TestInitializeSavesSession:
    async def test_fresh_login_persists_session(self, bridge_env, mocker):
        mocker.patch.object(
            python_bridge,
            "resolve_eapi_domain",
            new=mocker.AsyncMock(return_value="z-library.ec"),
        )
        mocker.patch.object(python_bridge, "EAPIClient", FakeEAPIClient)

        client = await python_bridge.initialize_eapi_client()

        assert client.login_calls == 1
        saved = load_eapi_session(bridge_env)
        assert saved == {
            "domain": "z-library.ec",
            "remix_userid": "42",
            "remix_userkey": "fresh-key",
        }
        if os.name != "nt":
            assert bridge_env.stat().st_mode & 0o777 == 0o600

    async def test_saved_session_survives_reload_without_login(
        self, bridge_env, mocker
    ):
        """The whole point: a second process initializes without logging in."""
        mocker.patch.object(
            python_bridge,
            "resolve_eapi_domain",
            new=mocker.AsyncMock(return_value="z-library.ec"),
        )
        mocker.patch.object(python_bridge, "EAPIClient", FakeEAPIClient)

        await python_bridge.initialize_eapi_client()
        python_bridge._eapi_client = None
        FakeEAPIClient.instances = []
        # get_profile now succeeds (default profile_response), so the cache validates.
        second = await python_bridge.initialize_eapi_client()

        assert second.login_calls == 0
        assert second.profile_calls == 1
