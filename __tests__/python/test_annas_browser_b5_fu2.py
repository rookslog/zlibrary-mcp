"""FU-2: real acquisition/transfer/session; synthetic DNS, HTTP and Chrome only."""

import asyncio
import hashlib
import json
from pathlib import Path
import socket

import httpx
import pytest

from lib import python_bridge as bridge
from lib.acquisition import acquire
from lib.sources.errors import AllSourcesFailedError, ProviderTimeoutError
from lib.sources.net import WalkDeadline
from test_annas_browser_download import (
    BOOK,
    DIGEST,
    MD5,
    PARTNERS,
    PAYLOAD,
    integration_route,
    payload_variant,
    browser,
    first_url,
    saved,
    spent,
)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.2.3.4"],
        ["172.16.0.1"],
        ["192.168.1.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["192.0.2.1"],
        ["198.18.0.1"],
        ["224.0.0.1"],
        ["240.0.0.1"],
        ["0.0.0.0"],
        ["::1"],
        ["fc00::1"],
        ["fe80::1"],
        ["ff02::1"],
        ["2001:db8::1"],
        ["::ffff:127.0.0.1"],
        ["8.8.8.8", "10.0.0.1"],
        [],
        None,
        ["fec0::1"],
        ["::"],
    ],
)
@pytest.mark.parametrize("redirect", [False, True])
async def test_browser_transfer_refuses_nonpublic_dns_before_request(
    tmp_path, monkeypatch, addresses, redirect
):
    """Removing the request guard admits private DNS or a private redirect hop."""

    def serve(request):
        if request.url.host == "wbsg8v.xyz":
            return httpx.Response(
                302, headers={"location": "https://hop.example.invalid/next.pdf"}
            )
        if request.url.host == "hop.example.invalid":
            return httpx.Response(
                307, headers={"location": "https://internal.example.invalid/book.pdf"}
            )
        return httpx.Response(200, content=PAYLOAD)

    router, io, requests = integration_route(
        tmp_path, monkeypatch, serve, annas_browser_max_servers=1
    )
    if not redirect:
        io.pages[PARTNERS[0].replace(MD5, DIGEST)] = payload_variant(
            "https://internal.example.invalid/book.pdf"
        ).replace(MD5, DIGEST)

    async def resolve(host, port, **kwargs):
        answers = addresses if host == "internal.example.invalid" else ["8.8.8.8"]
        if answers is None:
            raise socket.gaierror("synthetic lookup failure")
        return [
            (
                socket.AF_INET6 if ":" in a else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (a, port),
            )
            for a in answers
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as caught:
            await acquire(
                BOOK, str(output), filename="fu2.pdf", transfer=bridge.download_book
            )
        assert [f.reason for f in caught.value.failures] == ["protocol_error"]
        assert [r.url.host for r in requests] == (
            ["wbsg8v.xyz", "hop.example.invalid"] if redirect else []
        )
        assert not (output / "fu2.pdf").exists()
        assert not list(output.glob(".source-*"))
        assert all(
            json.loads(p.read_text())["state"] == "intent"
            for p in output.glob("*.acq.json")
        )
    finally:
        await router.close()


@pytest.mark.parametrize("operation", ["search", "download"])
@pytest.mark.parametrize(
    "failure", ["launch", "partial_launch", "new_page", "timeout", "cancel"]
)
async def test_previsit_launch_failure_does_not_spend_slot(
    tmp_path, monkeypatch, operation, failure
):
    """Spending before launch/page creation loses daily visits on startup failures."""
    session, io = browser(tmp_path, monkeypatch, annas_browser_daily_limit=2)
    io.pages["/search"] = saved("kant-settled.html")
    entered = asyncio.Event()

    async def broken_launch():
        if failure == "partial_launch":
            await io.launch()
        if failure in {"timeout", "cancel"}:
            entered.set()
            await asyncio.Event().wait()
        if failure == "new_page":
            context = await io.launch()

            async def no_page():
                raise RuntimeError("synthetic new-page failure")

            context.new_page = no_page
            return context
        raise RuntimeError("synthetic launch failure")

    monkeypatch.setattr(session, "_ensure_context", broken_launch)

    async def run(deadline):
        if operation == "search":
            return await session.search_html("kant", deadline=deadline)
        return await first_url(session, deadline=deadline)

    if failure == "cancel":
        task = asyncio.create_task(run(WalkDeadline(3)))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        error = ProviderTimeoutError if failure == "timeout" else RuntimeError
        with pytest.raises(error):
            await run(WalkDeadline(0.03 if failure == "timeout" else 3))
    assert io.visits == []
    assert session._process_lock._fd is None and not session._lock.locked()
    assert session._context is None and session._playwright is None
    assert not (tmp_path / "annas-browser-usage.json").exists()
    # A later visit still has its entire allowance, with no refund race.
    monkeypatch.setattr(session, "_ensure_context", io.launch)
    await run(WalkDeadline(3))
    assert spent(tmp_path) == (1 if operation == "search" else 2)
    assert session._process_lock._fd is None and not session._lock.locked()


async def test_browser_resume_rechecks_dns_before_another_get(tmp_path, monkeypatch):
    """A public first connection must not authorize a later private resolution."""

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield PAYLOAD[:65536]
            raise httpx.RemoteProtocolError("synthetic interrupted body")

    def serve(request):
        if "range" not in request.headers:
            return httpx.Response(200, stream=Interrupted())
        return httpx.Response(
            206,
            content=PAYLOAD[65536:],
            headers={"content-range": f"bytes 65536-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"},
        )

    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        serve,
        annas_browser_max_servers=1,
        download_resume_attempts=1,
    )

    async def resolve(host, port, **kwargs):
        address = "fec0::1" if requests else "8.8.8.8"
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    output = tmp_path / "output"
    try:
        with pytest.raises(AllSourcesFailedError) as caught:
            await acquire(
                BOOK, str(output), filename="fu2.pdf", transfer=bridge.download_book
            )
        assert [f.reason for f in caught.value.failures] == ["protocol_error"]
        assert len(requests) == 1 and len(io.visits) == 2
        assert not list(output.glob(".source-*")) and not (output / "fu2.pdf").exists()
    finally:
        await router.close()


@pytest.mark.parametrize("provider", ["annas", "libgen"])
async def test_other_transfer_sources_keep_existing_dns_policy(
    tmp_path, monkeypatch, provider
):
    """Guarding keyed/LibGen by default would change the explicitly retained policy."""

    async def forbidden_dns(*args, **kwargs):
        raise AssertionError("DNS must remain untouched for other source routes")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden_dns)
    client_type = httpx.AsyncClient

    def serve(request):
        if request.url.host == "keyed.example.invalid":
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1/book.pdf"}
            )
        return httpx.Response(200, content=PAYLOAD)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(serve), **kw),
    )
    path = await bridge._download_url_to_file(
        "https://keyed.example.invalid/book.pdf", str(tmp_path), DIGEST, provider
    )
    assert Path(path).read_bytes() == PAYLOAD


async def test_browser_transfer_adopting_keyed_canonical_receipt_has_unknown_quota(
    tmp_path, monkeypatch
):
    """Format correction misses early reuse; receipt authority must not spend quota."""
    output = tmp_path / "output"
    output.mkdir()
    canonical = output / "fu2.pdf"
    canonical.write_bytes(PAYLOAD)
    receipt_path = Path(str(canonical) + ".acq.json")
    receipt = {
        "record_version": 1,
        "state": "complete",
        "path": str(canonical),
        "bytes": len(PAYLOAD),
        "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        "source": "annas",
        "identifiers": {"md5": DIGEST},
        "reused": False,
        "transfer_skipped": False,
        "format": "pdf",
        "provenance": {
            "route": "fast_download",
            "host": "keyed.example.invalid",
            "mirror": "annas-archive.gl",
        },
    }
    receipt_path.write_text(json.dumps(receipt))
    before = receipt_path.read_bytes()
    router, io, requests = integration_route(
        tmp_path,
        monkeypatch,
        lambda r: httpx.Response(200, content=PAYLOAD),
        annas_browser_daily_limit=2,
    )
    book = {**BOOK, "extension": "epub"}
    try:
        early = await acquire(
            book,
            str(output),
            filename="fu2.epub",
            receipt_only=True,
            transfer=bridge.download_book,
        )
        assert early == {"receipt_hit": False} and io.visits == [] and requests == []
        result = await acquire(
            book, str(output), filename="fu2.epub", transfer=bridge.download_book
        )
        assert len(io.visits) == 2 and len(requests) == 1
        assert result["path"] == str(canonical) and result["reused"] is True
        assert result["transfer_skipped"] is False
        assert receipt_path.read_bytes() == before
        assert canonical.read_bytes() == PAYLOAD
        assert json.loads((output / "fu2.epub.acq.json").read_text()) == receipt
        assert "quota_estimate" in result and result["quota_estimate"] is None
    finally:
        await router.close()
