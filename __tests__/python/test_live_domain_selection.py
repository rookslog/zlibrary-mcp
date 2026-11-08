"""Exercise each live login path offline so stale defaults cannot hide as walls."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import zlibrary.eapi


ROOT = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "override,expected",
    [
        (None, zlibrary.eapi.DEFAULT_EAPI_DOMAINS[0]),
        ("", zlibrary.eapi.DEFAULT_EAPI_DOMAINS[0]),
        ("library.example.test", "library.example.test"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    [
        "fixture",
        "test_authentication_succeeds",
        "test_authentication_fails_with_invalid_credentials",
        "test_session_persistence",
    ],
)
async def test_live_login_uses_runtime_default_and_override(
    monkeypatch, entrypoint, override, expected
):
    live = load_module(
        "__tests__.python.integration.offline_real_zlibrary",
        ROOT / "integration/test_real_zlibrary.py",
    )
    fixtures = load_module(
        "__tests__.python.integration.offline_live_conftest",
        ROOT / "integration/conftest.py",
    )
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())
    monkeypatch.setenv("ZLIBRARY_EMAIL", "offline@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "offline")

    if override is not None:
        monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", override)
    else:
        monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN", raising=False)

    class OfflineClient:
        def __init__(self, domain):
            assert domain == expected

        async def login(self, email, password):
            return {"success": int(email != "invalid@example.com")}

        async def search(self, *args, **kwargs):
            return {"books": []}

        async def close(self):
            pass

    monkeypatch.setattr(zlibrary.eapi, "EAPIClient", OfflineClient)
    monkeypatch.setattr(fixtures, "EAPIClient", OfflineClient)
    domain = fixtures.eapi_domain.__wrapped__()
    assert domain == expected
    if entrypoint == "fixture":
        generator = fixtures.zlib_client.__wrapped__(
            domain, fixtures.credentials.__wrapped__()
        )
        await anext(generator)
        with pytest.raises(StopAsyncIteration):
            await anext(generator)
    else:
        test = getattr(live.TestRealAuthentication(), entrypoint)
        if entrypoint == "test_authentication_fails_with_invalid_credentials":
            await test(domain)
        else:
            await test(fixtures.credentials.__wrapped__(), domain)


@pytest.mark.parametrize(
    "override,expected",
    [
        (None, zlibrary.eapi.DEFAULT_EAPI_DOMAINS[0]),
        ("", zlibrary.eapi.DEFAULT_EAPI_DOMAINS[0]),
        ("library.example.test", "library.example.test"),
    ],
)
def test_doctor_uses_runtime_default_and_override(monkeypatch, override, expected):
    if override is None:
        monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN", raising=False)
    else:
        monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", override)
    # Dataclass annotation resolution needs the module registered during import.
    spec = importlib.util.spec_from_file_location(
        "offline_domain_doctor", ROOT.parents[1] / "scripts/check_upstream.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.ZLIB_DOMAIN == expected
