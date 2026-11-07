"""Offline contracts for the integration credential boundary."""

import importlib.util
from pathlib import Path

import pytest

HELPER = Path(__file__).parent / "integration" / "ci_credentials.py"


def helper():
    spec = importlib.util.spec_from_file_location("ci_credentials", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_requires_explicit_reveal():
    secret = helper().Secret("FAKE-OPAQUE-CREDENTIAL")
    assert str(secret) == repr(secret) == "***"
    assert "FAKE-OPAQUE-CREDENTIAL" not in repr({"password": secret})
    assert secret.reveal() == "FAKE-OPAQUE-CREDENTIAL"


@pytest.mark.parametrize("success", [0, 1])
async def test_login_returns_only_success(monkeypatch, success):
    module = helper()
    monkeypatch.setenv("ZLIBRARY_EMAIL", "fake@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "FAKE-LOGIN-CREDENTIAL")

    class Client:
        async def login(self, email, password):
            assert email == "fake@example.test"
            assert password == "FAKE-LOGIN-CREDENTIAL"
            return {"success": success, "user": {"remix_userkey": "FAKE-SESSION"}}

    assert await module.login_with_credentials(
        Client(), module.environment_credentials()
    ) is bool(success)


async def test_login_exception_has_fixed_message():
    module = helper()

    class Client:
        async def login(self, *_):
            raise ValueError("FAKE-EXCEPTION-CREDENTIAL")

    with pytest.raises(RuntimeError, match="^Integration login failed$") as excinfo:
        await module.login_with_credentials(
            Client(), {"email": module.Secret("e"), "password": module.Secret("p")}
        )

    assert excinfo.value.__suppress_context__ is True
