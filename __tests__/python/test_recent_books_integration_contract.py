"""Check malformed recent-books replies through the bridge entirely offline."""

import json
import sys

import httpx
import pytest

import python_bridge
from lib.sources.errors import ProviderResponseError
from zlibrary.eapi import EAPIClient


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


@pytest.mark.parametrize("row", ["x", None, 1, False, []])
@pytest.mark.parametrize("outside_count", [False, True])
async def test_bridge_rejects_non_object_recent_rows(monkeypatch, row, outside_count):
    """Catch malformed rows reaching normalization or hiding beyond the count slice."""
    books = [{"id": 17, "title": "Offline book"}, row] if outside_count else [row]
    eapi = EAPIClient("zlibrary.example")
    monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"success": 1, "books": books})
        )
    ) as client:
        eapi._client = client
        with pytest.raises(
            ProviderResponseError, match="Recent-books provider contract error"
        ) as failure:
            await python_bridge.get_recent_books(count=1)
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


@pytest.mark.parametrize("books_field", [{}, {"books": ["x"]}])
async def test_recent_contract_error_survives_bridge_envelope(
    monkeypatch, capsys, books_field
):
    """Catches the EAPI diagnostic filter dropping the reason or hiding the contract error."""
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"success": 1, "error": "untrusted-provider-text", **books_field}
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
    assert error["error"] == (
        "Recent-books provider contract error: expected a books list of objects"
    )
    assert "untrusted-provider-text" not in streams.err
    assert "offline-cookie-marker" not in streams.err
    assert requests == [("GET", "/eapi/book/recently")]
