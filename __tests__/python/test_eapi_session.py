"""In-memory parent/child EAPI contract, real JSON client over fake HTTP only."""

import json
import sys

import httpx
import pytest
import python_bridge
from zlibrary import eapi


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("ZLIBRARY_SESSION_MODE", "memory")
    monkeypatch.setattr(python_bridge, "_session_input", None)
    updates = []
    monkeypatch.setattr(
        python_bridge.os,
        "write",
        lambda fd, data: updates.append(json.loads(data)) or len(data),
    )
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "fake-password-marker")
    monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "a.example.test")
    monkeypatch.delenv("ZLIBRARY_EAPI_SESSION", raising=False)
    monkeypatch.setattr(
        eapi, "DEFAULT_EAPI_DOMAINS", ["a.example.test", "b.example.test"]
    )
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    state = {
        "updates": updates,
        "logins": 0,
        "requests": [],
        "profile": None,
        "operation": None,
        "failures": {},
        "clients": [],
        "login_result": None,
        "directory": tmp_path,
    }

    def handle(request):
        host, path = request.url.host, request.url.path
        state["requests"].append((host, path, request.headers.get("cookie", "")))
        failure = state["failures"].get(host)
        if failure in ("network", "timeout", "connect_timeout", "protocol"):
            cls = {
                "network": httpx.ConnectError,
                "timeout": httpx.ReadTimeout,
                "connect_timeout": httpx.ConnectTimeout,
                "protocol": httpx.RemoteProtocolError,
            }[failure]
            raise cls("offline", request=request)
        if failure == "wall":
            return httpx.Response(513, text="DiamWall")
        if failure == "html":
            return httpx.Response(200, text="<html>Challenge</html>")
        if failure == "http":
            return httpx.Response(503, text="unavailable")
        if path == "/eapi/user/login":
            state["logins"] += 1
            if isinstance(state["login_result"], httpx.Response):
                return state["login_result"]
            return httpx.Response(
                200,
                json=state["login_result"]
                or {
                    "success": 1,
                    "user": {
                        "id": str(state["logins"]),
                        "remix_userkey": f"fake-cookie-marker-{state['logins']}",
                    },
                },
            )
        if path == "/eapi/info/domains":
            return httpx.Response(200, json={"domains": []})
        payload = (
            state["profile"] if path == "/eapi/user/profile" else state["operation"]
        )
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, httpx.Response):
            return payload
        if payload is None:
            payload = (
                {"success": 1, "user": {"id": str(state["logins"])}}
                if path == "/eapi/user/profile"
                else {"success": 1, "books": []}
            )
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handle)

    async def get_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=transport, cookies=self._cookies)
            state["clients"].append(self._client)
        return self._client

    async def resolve():
        return await eapi.resolve_eapi_domain(transport=transport)

    monkeypatch.setattr(eapi.EAPIClient, "_get_client", get_client)
    monkeypatch.setattr(python_bridge, "resolve_eapi_domain", resolve)
    return state


def session(
    action="operation",
    identity="reader@example.test",
    domain="a.example.test",
    key="fake-cookie-marker-1",
):
    return {
        "generation": 12,
        "identity": identity,
        "action": action,
        "domain": domain,
        "remix_userid": "1",
        "remix_userkey": key,
    }


async def seed(service, monkeypatch):
    monkeypatch.setattr(python_bridge, "_session_input", session("login"))
    client = await python_bridge.initialize_eapi_client()
    await client.close()
    monkeypatch.setattr(python_bridge, "_session_input", session())
    monkeypatch.setattr(python_bridge, "_eapi_client", None)


async def test_warm_operation_needs_no_login_or_profile(service, monkeypatch):
    await seed(service, monkeypatch)
    service["requests"].clear()
    client = await python_bridge.initialize_eapi_client()
    assert (await client.search("offline"))["books"] == []
    await client.close()
    assert service["logins"] == 1
    assert [r[1] for r in service["requests"]] == ["/eapi/book/search"]
    assert list(service["directory"].iterdir()) == []


async def test_acquisition_and_idle_actions_validate_profile(service, monkeypatch):
    await seed(service, monkeypatch)
    assert [r[1] for r in service["requests"]].count("/eapi/user/profile") == 1
    monkeypatch.setattr(python_bridge, "_session_input", session("validate"))
    client = await python_bridge.initialize_eapi_client()
    await client.close()
    assert [r[1] for r in service["requests"]].count("/eapi/user/profile") == 2
    assert service["logins"] == 1


async def test_changed_identity_rejects_carrier_without_sending_it(
    service, monkeypatch
):
    monkeypatch.setattr(
        python_bridge, "_session_input", session(identity="other@example.test")
    )
    with pytest.raises(ValueError, match="identity"):
        await python_bridge.initialize_eapi_client()
    assert service["requests"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"success": 0, "error": "Please login"},
        {"success": 0, "error": "Authentication required", "user": {"id": "1"}},
        httpx.Response(401, json={"error": "rejected"}),
        httpx.Response(400, json={"success": 0, "error": "Authentication required"}),
    ],
)
async def test_child_never_refreshes_after_explicit_operation_rejection(
    service, monkeypatch, payload
):
    await seed(service, monkeypatch)
    client = await python_bridge.initialize_eapi_client()
    service["requests"].clear()
    service["operation"] = payload
    with pytest.raises(eapi.EAPIAuthenticationError):
        await client.search("offline")
    await client.close()
    assert service["logins"] == 1
    assert len(service["requests"]) == 1


@pytest.mark.parametrize(
    "payload",
    [{"success": 0, "error": "Daily limit #1"}, {"success": 0, "user": {"id": "1"}}],
)
async def test_business_envelopes_same_as_per_call(service, monkeypatch, payload):
    await seed(service, monkeypatch)
    client = await python_bridge.initialize_eapi_client()
    service["operation"] = payload
    assert await client.search("offline") == payload
    await client.close()
    assert service["logins"] == 1


@pytest.mark.parametrize("failure", ["network", "timeout", "wall", "http", "html"])
async def test_operation_uncertainty_never_refreshes_and_honours_pin(
    service, monkeypatch, failure
):
    await seed(service, monkeypatch)
    client = await python_bridge.initialize_eapi_client()
    service["failures"]["a.example.test"] = failure
    with pytest.raises(
        (httpx.HTTPError, eapi.DiamWallError, eapi.NonJSONResponseError)
    ):
        await client.search("offline")
    await client.close()
    assert service["logins"] == 1
    assert client.remix_userkey == "fake-cookie-marker-1"
    assert {r[0] for r in service["requests"]} == {"a.example.test"}


@pytest.mark.parametrize("failure", ["network", "connect_timeout", "wall", "html"])
async def test_domain_recovery_same_cookies_validates_and_publishes(
    service, monkeypatch, failure
):
    await seed(service, monkeypatch)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
    client = await python_bridge.initialize_eapi_client()
    service["failures"]["a.example.test"] = failure
    assert (await client.search("offline"))["books"] == []
    python_bridge._publish_eapi_session()
    assert service["updates"][0]["domain"] == "b.example.test"
    assert service["updates"][0]["generation"] == 12
    assert client.remix_userkey == "fake-cookie-marker-1"
    assert service["logins"] == 1
    await client.close()
    assert all(c.is_closed for c in service["clients"])


@pytest.mark.parametrize(
    "payload,token",
    [
        (
            {"success": 0, "error": "New expiry #2", "user": {"id": "1"}},
            "New expiry #2",
        ),
        ({"success": 0, "user": {"id": "1"}}, "<redacted>"),
        ({"success": 0, "error": "unsafe / value"}, "<redacted>"),
        ({"success": 0, "error": "x" * 65}, "<redacted>"),
    ],
)
async def test_any_profile_success_zero_is_typed_rejection(
    service, monkeypatch, payload, token
):
    await seed(service, monkeypatch)
    monkeypatch.setattr(python_bridge, "_session_input", session("validate"))
    service["profile"] = payload
    with pytest.raises(eapi.EAPIAuthenticationError, match=token):
        await python_bridge.initialize_eapi_client()
    await python_bridge._eapi_client.close()
    assert service["logins"] == 1


async def test_profile_user_id_must_match(service, monkeypatch):
    await seed(service, monkeypatch)
    service["profile"] = {"success": 1, "user": {"id": "other"}}
    monkeypatch.setattr(python_bridge, "_session_input", session("validate"))
    with pytest.raises(RuntimeError, match="Unexpected EAPI profile"):
        await python_bridge.initialize_eapi_client()
    await python_bridge._eapi_client.close()
    assert service["logins"] == 1


async def test_recovery_rejection_reaches_parent_without_child_login(
    service, monkeypatch
):
    await seed(service, monkeypatch)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
    client = await python_bridge.initialize_eapi_client()
    service["failures"]["a.example.test"] = "network"
    service["profile"] = {"success": 0, "error": "Expiry #2"}
    with pytest.raises(eapi.EAPIAuthenticationError):
        await client.search("offline")
    await client.close()
    assert service["logins"] == 1
    assert client.domain == "a.example.test"


async def test_failed_recovery_retains_pair(service, monkeypatch):
    await seed(service, monkeypatch)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
    client = await python_bridge.initialize_eapi_client()
    service["failures"] = {"a.example.test": "network", "b.example.test": "wall"}
    with pytest.raises(httpx.ConnectError):
        await client.search("offline")
    await client.close()
    assert client.domain == "a.example.test"
    assert service["logins"] == 1


async def test_per_call_and_restart_login_again(service, monkeypatch):
    await seed(service, monkeypatch)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    for _ in range(2):
        client = await python_bridge.initialize_eapi_client()
        await client.close()
    assert service["logins"] == 3


async def test_main_bearer_only_on_fd3_not_stdout_stderr(
    service, monkeypatch, capsys, caplog
):
    monkeypatch.setattr(python_bridge, "_session_input", session("login"))
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    captured = capsys.readouterr()
    assert service["updates"][0]["remix_userkey"] == "fake-cookie-marker-1"
    assert json.loads(json.loads(captured.out)["content"][0]["text"]) == {
        "authenticated": True
    }
    assert "fake-cookie-marker" not in captured.out + captured.err + caplog.text
    assert list(service["directory"].iterdir()) == []


async def test_main_typed_rejection_has_generation_no_carrier(
    service, monkeypatch, capsys
):
    await seed(service, monkeypatch)
    service["operation"] = {"success": 0, "error": "Please login"}
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    captured = capsys.readouterr()
    result = json.loads(json.loads(captured.out)["content"][0]["text"])
    assert result["auth_rejected"] == {"generation": 12}
    assert service["updates"] == []
    assert "fake-cookie-marker" not in captured.out + captured.err
    assert service["logins"] == 1


async def test_main_transport_error_diagnostic_scrubs_bearer(
    service, monkeypatch, capsys
):
    await seed(service, monkeypatch)
    service["operation"] = RuntimeError(
        "fake-password-marker fake-cookie-marker-1 reader@example.test"
    )
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    with pytest.raises(SystemExit):
        await python_bridge.main()
    captured = capsys.readouterr()
    for secret in [
        "fake-cookie-marker-1",
        "fake-password-marker",
        "reader@example.test",
    ]:
        assert secret not in captured.out + captured.err


async def test_login_failure_has_no_secret(service, caplog):
    service["login_result"] = {
        "success": 0,
        "user": {"remix_userkey": "fake-cookie-marker"},
        "error": "fake-password-marker reader@example.test",
    }
    with pytest.raises(RuntimeError) as caught:
        await python_bridge.initialize_eapi_client()
    for value in ("fake-cookie-marker", "fake-password-marker", "reader@example.test"):
        assert value not in str(caught.value) + caplog.text
    assert all(c.is_closed for c in service["clients"])


async def test_wall_401_does_not_refresh(service, monkeypatch):
    await seed(service, monkeypatch)
    client = await python_bridge.initialize_eapi_client()
    service["operation"] = httpx.Response(401, text="DiamWall challenge")
    with pytest.raises(eapi.DiamWallError):
        await client.search("offline")
    await client.close()
    assert service["logins"] == 1


async def test_success_without_pair_is_not_session(service):
    service["login_result"] = {"success": 1, "user": {}}
    with pytest.raises(RuntimeError):
        await python_bridge.initialize_eapi_client()
    assert all(c.is_closed for c in service["clients"])


def test_bridge_debug_suppresses_wire_cookie_logs(service, monkeypatch, caplog):
    import logging

    names = ("zlibrary", "httpx", "httpcore")
    previous = {name: logging.getLogger(name).level for name in names}
    try:
        monkeypatch.setattr(python_bridge, "DEBUG_MODE", True)
        caplog.set_level(logging.DEBUG)
        python_bridge._configure_debug_logging()
        logging.getLogger("httpcore.http11").debug("Set-Cookie: fake-cookie-marker-1")
        logging.getLogger("httpx").info("fake-cookie-marker-1")
        assert "fake-cookie-marker-1" not in caplog.text
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize(
    "function,args",
    [
        ("full_text_search", {"query": "offline", "phrase": True}),
        ("get_book_metadata_complete", {"book_id": "1", "book_hash": "fake"}),
        ("eapi_health_check", {}),
    ],
)
async def test_wrappers_preserve_typed_rejection_for_parent(
    service, monkeypatch, capsys, function, args
):
    await seed(service, monkeypatch)
    service["requests"].clear()
    service["operation"] = {"success": 0, "error": "Please login"}
    monkeypatch.setattr(sys, "argv", ["python_bridge.py", function, json.dumps(args)])
    await python_bridge.main()
    output = capsys.readouterr()
    result = json.loads(json.loads(output.out)["content"][0]["text"])
    assert result["auth_rejected"] == {"generation": 12}
    assert service["logins"] == 1
    assert len(service["requests"]) == 1


@pytest.mark.parametrize(
    "status,upstream,expected",
    [
        (401, "Invalid credentials #1", "Authentication rejected"),
        (400, "Login denied #2", "Upstream request failed"),
        (401, "fake-password-marker", "Authentication rejected"),
        (401, "unsafe / value", "Authentication rejected"),
    ],
)
async def test_login_failure_returns_safe_http_cause(
    service, monkeypatch, capsys, status, upstream, expected
):
    monkeypatch.setattr(python_bridge, "_session_input", session("login"))
    service["login_result"] = httpx.Response(
        status, json={"success": 0, "error": upstream}
    )
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    captured = capsys.readouterr()
    data = json.loads(json.loads(captured.out)["content"][0]["text"])
    assert data["login_failed"] == {"status_code": status, "token": expected}
    assert "fake-password-marker" not in captured.out + captured.err
    assert "fake-cookie-marker" not in captured.out + captured.err


@pytest.mark.parametrize("failure", ["timeout", "protocol", "http"])
async def test_uncertain_operation_is_not_resent_on_another_domain(
    service, monkeypatch, failure
):
    await seed(service, monkeypatch)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
    client = await python_bridge.initialize_eapi_client()
    service["requests"].clear()
    service["failures"]["a.example.test"] = failure
    with pytest.raises(httpx.HTTPError):
        await client.search("offline")
    await client.close()
    assert len(service["requests"]) == 1
    assert service["requests"][0][0] == "a.example.test"
    assert service["logins"] == 1


async def test_validated_recovered_domain_is_published_even_on_later_error(
    service, monkeypatch, capsys
):
    await seed(service, monkeypatch)
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN")
    service["failures"]["a.example.test"] = "network"
    service["operation"] = lambda request: (
        {"success": 1, "books": []}
        if "fake-cookie-marker" not in request.headers.get("cookie", "")
        else RuntimeError("operation failed after recovery")
    )
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    with pytest.raises(SystemExit):
        await python_bridge.main()
    output = capsys.readouterr()
    assert service["updates"][0]["domain"] == "b.example.test"
    assert "fake-cookie-marker" not in output.out + output.err


async def test_short_password_does_not_rewrite_book_results(
    service, monkeypatch, capsys
):
    await seed(service, monkeypatch)
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "art")
    service["operation"] = {
        "success": 1,
        "books": [{"id": "1", "title": "The art of starting", "hash": "fake"}],
    }
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    output = capsys.readouterr()
    result = json.loads(json.loads(output.out)["content"][0]["text"])
    assert result["books"][0]["name"] == "The art of starting"


async def test_business_success_zero_signals_validation_on_private_channel(
    service, monkeypatch, capsys
):
    await seed(service, monkeypatch)
    service["operation"] = {"success": 0, "error": "Unknown expiry"}
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    output = capsys.readouterr()
    assert service["updates"][0]["validate_next"] is True
    result = json.loads(json.loads(output.out)["content"][0]["text"])
    assert result["books"] == []
    assert "fake-cookie-marker" not in output.out + output.err


async def test_short_password_cannot_rewrite_diagnostic_schema(
    service, monkeypatch, capsys
):
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "a")
    monkeypatch.setattr(python_bridge, "_session_input", session("login"))
    service["login_result"] = httpx.Response(401, json={"error": "Invalid credentials"})
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    await python_bridge.main()
    output = capsys.readouterr()
    result = json.loads(json.loads(output.out)["content"][0]["text"])
    assert result["login_failed"]["status_code"] == 401


@pytest.mark.parametrize("status", [404, 503])
async def test_operation_http_status_crosses_error_boundary_without_message_guessing(
    service, monkeypatch, capsys, status
):
    await seed(service, monkeypatch)
    service["operation"] = httpx.Response(status, text="fixture upstream response")
    monkeypatch.setattr(
        sys, "argv", ["python_bridge.py", "search", '{"query":"offline"}']
    )
    with pytest.raises(SystemExit) as stopped:
        await python_bridge.main()
    assert stopped.value.code == 1
    envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert envelope["type"] == "HTTPStatusError"
    assert envelope["status_code"] == status
    # Design point 1 adds route facts, not a multi-source failure verdict.
    via = envelope["details"]["via"]
    assert set(envelope["details"]) == {"via"}
    assert len(via) == 1
    assert set(via[0]) == {"source", "route", "ms"}
    assert via[0]["source"] == "zlibrary"
    assert via[0]["route"] == "api"
    assert via[0]["ms"] >= 0
