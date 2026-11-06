"""Offline transfer-to-sidecar tests: retain mirror identity and final byte host."""

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from lib import python_bridge as bridge
from lib.acquisition import acquire
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.libgen import LibgenAdapter
from zlibrary.eapi import EAPIClient

PAYLOAD = b"%PDF-1.7\n" + b"offline serving host fixture\n" * 4000
DIGEST = hashlib.md5(PAYLOAD).hexdigest()
BOOK = {"title": "Host fixture", "author": "Fixture", "extension": "pdf"}


def offline_http(monkeypatch, handler):
    """Keep real httpx redirects/streaming, replacing only the network transport."""
    client_type = httpx.AsyncClient
    requests = []

    def serve(request):
        requests.append(request)
        return handler(request)

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(serve), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


def libgen_router(monkeypatch):
    config = SourceConfig(libgen_mirror="li", download_resume_attempts=1)
    adapter = LibgenAdapter(config)
    monkeypatch.setattr(
        adapter, "_resolve_key", AsyncMock(return_value=("key", "fixture"))
    )
    monkeypatch.setattr(
        adapter, "_serves_bytes", AsyncMock(return_value=(True, "fixture"))
    )
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=None))
    router = SimpleNamespace(
        config=config,
        iter_download_candidates=lambda md5, source: adapter.iter_download_candidates(
            md5
        ),
    )
    monkeypatch.setattr(bridge, "get_source_router", AsyncMock(return_value=router))


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["whole", "same-url-range"])
async def test_later_libgen_mirror_and_redirect_host_reach_receipt(
    tmp_path, monkeypatch, resume
):
    """Dropping the admitted mirror or overriding the transfer host loses provenance."""
    libgen_router(monkeypatch)

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PAYLOAD[:65536]
            raise httpx.RemoteProtocolError("offline interrupted body")

    def handler(request):
        if request.url.host in {"libgen.li", "libgen.vg"}:
            return httpx.Response(503)
        if request.url.host == "libgen.la":
            return httpx.Response(
                302,
                headers={
                    "location": "https://bytes.example.invalid/book?token=private"
                },
            )
        assert request.url.host == "bytes.example.invalid"
        if resume and "range" not in request.headers:
            return httpx.Response(200, stream=Interrupted())
        if resume:
            assert request.headers["range"] == "bytes=65536-"
            return httpx.Response(
                206,
                content=PAYLOAD[65536:],
                headers={
                    "content-range": f"bytes 65536-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
                },
            )
        return httpx.Response(200, content=PAYLOAD)

    requests = offline_http(monkeypatch, handler)
    result = await acquire(
        {**BOOK, "source": "libgen", "md5": DIGEST},
        str(tmp_path),
        transfer=bridge.download_book,
    )
    assert Path(result["path"]).read_bytes() == PAYLOAD
    record = json.loads(Path(result["record"]).read_text())
    assert record["provenance"] == {
        "route": "mirror_download",
        "mirror": "libgen.la",
        "host": "bytes.example.invalid",
    }
    assert [r.url.host for r in requests[:3]] == ["libgen.li", "libgen.vg", "libgen.la"]
    if resume:
        attempts = [r for r in requests if r.url.host == "libgen.la"]
        assert len(attempts) == 2
        assert attempts[0].url == attempts[1].url
        assert attempts[1].headers["range"] == "bytes=65536-"


@pytest.mark.asyncio
async def test_annas_resolution_mirror_and_redirect_host_reach_receipt(
    tmp_path, monkeypatch
):
    """The Anna's API hostname must not replace the host serving the book."""
    config = SourceConfig(
        annas_base_url="https://ANNAS-ARCHIVE.GL:8443/catalog",
        annas_secret_key="offline",
    )
    adapter = AnnasArchiveAdapter(config)

    async def candidates(md5, source):
        yield await adapter.get_download_url(md5)

    monkeypatch.setattr(
        bridge,
        "get_source_router",
        AsyncMock(
            return_value=SimpleNamespace(
                config=config, iter_download_candidates=candidates
            )
        ),
    )

    def handler(request):
        if request.url.host == "annas-archive.gl":
            return httpx.Response(
                200, json={"download_url": "https://resolved.example.invalid/book"}
            )
        if request.url.host == "resolved.example.invalid":
            return httpx.Response(
                302, headers={"location": "https://annas-bytes.example.invalid/book"}
            )
        assert request.url.host == "annas-bytes.example.invalid"
        return httpx.Response(200, content=PAYLOAD)

    offline_http(monkeypatch, handler)
    try:
        result = await acquire(
            {**BOOK, "source": "annas", "md5": DIGEST},
            str(tmp_path),
            transfer=bridge.download_book,
        )
    finally:
        await adapter.close()
    record = json.loads(Path(result["record"]).read_text())
    assert record["provenance"] == {
        "route": "fast_download",
        "mirror": "annas-archive.gl",
        "host": "annas-bytes.example.invalid",
    }


@pytest.fixture
def eapi_file_writer(monkeypatch):
    """Keep real local writes while isolating the observed sandbox executor wait."""

    @asynccontextmanager
    async def async_open(path, mode):
        with open(path, mode) as handle:

            class Writer:
                async def write(self, data):
                    return handle.write(data)

            yield Writer()

    monkeypatch.setattr("zlibrary.eapi.aiofiles.open", async_open)


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True], ids=["direct", "receipt-reuse"])
@pytest.mark.parametrize(
    "domain",
    [
        "eapi.example.invalid",
        "EAPI.EXAMPLE.INVALID",
        "https://EAPI.EXAMPLE.INVALID:8443/catalog?token=private",
        "EAPI.EXAMPLE.INVALID/catalog",
    ],
)
async def test_eapi_file_host_and_original_receipt_authority(
    tmp_path, monkeypatch, reuse, domain, eapi_file_writer
):
    """EAPI must retain its response host; later reuse cannot stamp a new client domain."""

    eapi = EAPIClient(domain=domain, remix_userid="offline", remix_userkey="offline")
    eapi.get_download_link = AsyncMock(
        return_value={"downloadLink": "https://eapi-bytes.example.invalid/book.pdf"}
    )
    monkeypatch.setattr(bridge, "get_eapi_client", AsyncMock(return_value=eapi))

    def handler(request):
        assert request.url.host == "eapi-bytes.example.invalid"
        return httpx.Response(200, content=PAYLOAD)

    requests = offline_http(monkeypatch, handler)
    book = {**BOOK, "source": "zlibrary", "id": "123", "hash": "offline"}
    result = await acquire(book, str(tmp_path), transfer=bridge.download_book)
    original = Path(result["record"]).read_bytes()
    if reuse:
        monkeypatch.setattr(
            bridge,
            "get_eapi_client",
            AsyncMock(
                side_effect=AssertionError("receipt reuse must not authenticate")
            ),
        )
        result = await acquire(book, str(tmp_path), transfer=bridge.download_book)
        assert result["transfer_skipped"] is True
        assert Path(result["record"]).read_bytes() == original
    record = json.loads(Path(result["record"]).read_text())
    assert record["provenance"] == {
        "route": "eapi",
        "mirror": "eapi.example.invalid",
        "host": "eapi-bytes.example.invalid",
    }
    assert len(requests) == 1
    assert "host" not in result and "provenance" not in result


@pytest.mark.asyncio
async def test_eapi_receipt_names_domain_after_link_recovery(
    tmp_path, monkeypatch, eapi_file_writer
):
    """Capturing domain before get_download_link loses the successful recovery host."""
    eapi = EAPIClient("stale.example.invalid", "offline", "offline")

    async def recover():
        eapi.domain = "RECOVERED.EXAMPLE.INVALID"
        return True

    eapi.on_domain_failed = recover
    monkeypatch.setattr(bridge, "get_eapi_client", AsyncMock(return_value=eapi))

    def handler(request):
        if request.url.host == "stale.example.invalid":
            raise httpx.ConnectError("offline stale domain", request=request)
        if request.url.host == "recovered.example.invalid":
            assert request.url.path == "/eapi/book/123/offline/file"
            return httpx.Response(
                200, json={"downloadLink": "https://bytes.example.invalid/book.pdf"}
            )
        assert request.url.host == "bytes.example.invalid"
        return httpx.Response(200, content=PAYLOAD)

    requests = offline_http(monkeypatch, handler)
    try:
        result = await acquire(
            {**BOOK, "source": "zlibrary", "id": "123", "hash": "offline"},
            str(tmp_path),
            transfer=bridge.download_book,
        )
    finally:
        await eapi.close()
    record = json.loads(Path(result["record"]).read_text())
    assert record["provenance"] == {
        "route": "eapi",
        "mirror": "recovered.example.invalid",
        "host": "bytes.example.invalid",
    }
    assert [r.url.host for r in requests] == [
        "stale.example.invalid",
        "recovered.example.invalid",
        "bytes.example.invalid",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_url", ["absent", None])
async def test_eapi_observer_falls_back_to_request_host_without_response_url(
    tmp_path, monkeypatch, eapi_file_writer, response_url
):
    """Removing request-host fallback must produce an empty observation and fail."""
    eapi = EAPIClient("eapi.example.invalid", "offline", "offline")
    request_url = "https://REQUEST.EXAMPLE.INVALID:8443/book?token=private"
    eapi.get_download_link = AsyncMock(return_value={"downloadLink": request_url})

    async def chunks():
        yield PAYLOAD

    response = SimpleNamespace(
        headers={"content-disposition": 'attachment; filename="book.pdf"'},
        raise_for_status=lambda: None,
        aiter_bytes=chunks,
    )
    if response_url is None:
        response.url = None

    @asynccontextmanager
    async def stream(method, url):
        assert method == "GET" and url == request_url
        yield response

    @asynccontextmanager
    async def client(**kwargs):
        yield SimpleNamespace(stream=stream)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    hosts = []
    result = await eapi.download_file(
        123, "offline", str(tmp_path), host_observer=hosts.append
    )
    assert Path(result).read_bytes() == PAYLOAD
    assert hosts == ["request.example.invalid"]
