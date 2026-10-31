"""Check the exact credential-gated live method offline, without importing it."""

import ast
import json
import sys
from pathlib import Path

import httpx
import pytest

import python_bridge
from lib.sources.errors import ProviderResponseError
from zlibrary.eapi import EAPIClient


@pytest.mark.parametrize(
    "payload,accepted",
    [
        ({"success": 1, "books": []}, True),
        ({"success": 1, "books": [{"id": "1"}]}, True),
        ({"success": 1}, False),
        ({"success": 1, "books": None}, False),
        ({"success": 1, "books": [1]}, False),
        ({"success": 0, "books": []}, False),
        (["not-an-object"], False),
    ],
)
async def test_live_recent_method_uses_existing_client_and_checks_shape(
    monkeypatch, payload, accepted
):
    # No live module import, credential reads, fixture login or provider calls.
    monkeypatch.delenv("ZLIBRARY_EMAIL", raising=False)
    monkeypatch.delenv("ZLIBRARY_PASSWORD", raising=False)
    source = Path(__file__).parent / "integration" / "test_real_zlibrary.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    basic_search = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "TestRealBasicSearch"
    )
    method = next(
        node
        for node in basic_search.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_recent_books_contract"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET"
        assert request.url.path == "/eapi/book/recently"
        return httpx.Response(200, json=payload)

    # Real EAPI method over a pre-existing client. Any login/new request path
    # fails the stub and the explicit request-count assertion, even on bad data.
    eapi = EAPIClient(
        "zlibrary.example", remix_userid="7", remix_userkey="offline-session"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), cookies=eapi._cookies
    ) as client:
        eapi._client = client
        if accepted:
            await namespace[method.name](None, eapi)
        else:
            with pytest.raises(
                AssertionError, match="Recent-books provider contract error"
            ):
                await namespace[method.name](None, eapi)
    assert requests == [("GET", "/eapi/book/recently")]


@pytest.mark.parametrize(
    "payload",
    [
        {"success": 1},
        {"success": 1, "books": None},
        {"success": 1, "books": {}},
        {"success": 1, "books": ""},
        {"success": 1, "books": 0},
        ["not-an-object"],
        None,
    ],
)
async def test_bridge_rejects_recent_reply_without_a_books_list(monkeypatch, payload):
    """Catches missing/default/coerced lists being presented as empty catalogues."""
    eapi = EAPIClient("zlibrary.example")
    monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(payload))
        )
    ) as client:
        eapi._client = client
        with pytest.raises(
            ProviderResponseError, match="Recent-books provider contract error"
        ) as failure:
            await python_bridge.get_recent_books()
    assert failure.value.reason == "protocol_error"
    assert failure.value.provider == "zlibrary"


@pytest.mark.parametrize("books", [[], [{"id": 17, "title": "Offline book"}]])
async def test_bridge_accepts_explicit_recent_books_lists(monkeypatch, books):
    """Catches rejecting real empty lists or bypassing normal row normalization."""
    eapi = EAPIClient("zlibrary.example")
    monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": 1, "books": books})
        )
    ) as client:
        eapi._client = client
        result = await python_bridge.get_recent_books()
    if books:
        assert len(result["books"]) == 1
        assert result["books"][0]["id"] == "17"
        assert result["books"][0]["title"] == "Offline book"
    else:
        assert result == {"books": []}


async def test_recent_contract_error_survives_bridge_envelope(monkeypatch, capsys):
    """Catches the EAPI diagnostic filter dropping the reason or hiding the contract error."""
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"success": 1, "error": "untrusted-provider-text"}
        )

    eapi = EAPIClient("zlibrary.example", remix_userkey="offline-cookie-marker")
    monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    monkeypatch.setattr(python_bridge, "_source_router", None)
    monkeypatch.setattr(sys, "argv", ["python_bridge.py", "get_recent_books", "{}"])

    async def initialized_client():
        return eapi

    monkeypatch.setattr(python_bridge, "initialize_eapi_client", initialized_client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        eapi._client = client
        with pytest.raises(SystemExit) as stopped:
            await python_bridge.main()
    assert stopped.value.code == 1
    streams = capsys.readouterr()
    assert streams.out == ""
    error = json.loads(streams.err)
    assert error["details"]["reason"] == "protocol_error"
    assert "Recent-books provider contract error" in error["error"]
    assert "books list" in error["error"]
    assert "untrusted-provider-text" not in streams.err
    assert "offline-cookie-marker" not in streams.err
    assert requests == [("GET", "/eapi/book/recently")]
