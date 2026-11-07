"""Exercise the live test's assertion/cleanup contract without importing providers."""

import ast
import builtins
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def run_invalid_login(client):
    source_path = Path(__file__).parent / "integration" / "test_real_zlibrary.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_authentication_fails_with_invalid_credentials"
    )
    module = ast.Module(body=[method], type_ignores=[])

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "zlibrary.eapi" and fromlist == ("EAPIClient",):
            return SimpleNamespace(EAPIClient=lambda domain: client)
        raise AssertionError(f"Unexpected import in offline auth regression: {name}")

    helper_spec = importlib.util.spec_from_file_location(
        "offline_ci_credentials", source_path.with_name("ci_credentials.py")
    )
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    namespace = {
        "__builtins__": {**vars(builtins), "__import__": fake_import},
        "os": SimpleNamespace(environ={}),
        "Secret": helper.Secret,
        "login_with_credentials": helper.login_with_credentials,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    coroutine = namespace[method.name](None, "offline.example.test")
    try:
        coroutine.send(None)
    except StopIteration:
        return
    finally:
        coroutine.close()
    raise AssertionError("Fake login/close unexpectedly suspended")


class FakeClient:
    def __init__(self, result=None, login_error=None, close_error=None):
        self.result = result
        self.login_error = login_error
        self.close_error = close_error
        self.closed = 0

    async def login(self, email, password):
        assert (email, password) == ("invalid@example.com", "wrongpassword")
        if self.login_error is not None:
            raise self.login_error
        return self.result

    async def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def test_unexpected_invalid_login_success_fails_and_closes():
    client = FakeClient(result={"success": 1})
    with pytest.raises(AssertionError):
        run_invalid_login(client)
    assert client.closed == 1


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(result={"success": 0}),
        FakeClient(login_error=RuntimeError("rejected")),
    ],
)
def test_existing_rejection_and_login_exception_are_accepted_and_closed(client):
    run_invalid_login(client)
    assert client.closed == 1


@pytest.mark.parametrize("outcome", ["success", "rejection", "exception"])
def test_cleanup_failure_propagates(outcome):
    close_error = RuntimeError("synthetic cleanup failure")
    client = FakeClient(
        result={"success": 1 if outcome == "success" else 0},
        login_error=RuntimeError("rejected") if outcome == "exception" else None,
        close_error=close_error,
    )
    with pytest.raises(RuntimeError) as caught:
        run_invalid_login(client)
    assert caught.value is close_error
    assert client.closed == 1
