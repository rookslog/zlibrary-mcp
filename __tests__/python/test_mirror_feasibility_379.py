"""Retry feasibility and lazy fallback, with synthetic bytes and virtual time."""

import asyncio
import gzip
import hashlib
from pathlib import Path
import random
from unittest.mock import AsyncMock

import httpx
import pytest

from lib import python_bridge as bridge
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError
from lib.sources.libgen import LibgenAdapter
from lib.sources.models import DownloadResult, SourceType
from lib.sources.router import SourceRouter
from test_libgen_transfers_371 import clock as _virtual_clock
from test_libgen_transfers_371 import loopback, route_to_loopback

clock = _virtual_clock

BODY = b"%PDF-1.7\n" + random.Random(379).randbytes(2 * 1024 * 1024 - 9)
MD5 = hashlib.md5(BODY).hexdigest()


@pytest.fixture(autouse=True)
def scripted_waits(monkeypatch, clock):
    async def sleep(seconds):
        if seconds:
            await clock.advance(seconds)
        else:
            await clock.sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)


def reply(start, count, body=BODY, *, restart=False):
    start = 0 if restart else start
    return (
        206 if start else 200,
        {
            "Content-Length": str(len(body) - start),
            **(
                {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
                if start
                else {}
            ),
        },
        body[start : start + count],
        0,
    )


def install_router(monkeypatch, config, mirrors=("li", "vg"), source="libgen"):
    """Real adapter and router iteration; replace only resolution and transport."""
    resolved = []
    adapter = LibgenAdapter(config)
    monkeypatch.setattr(adapter, "_mirror_candidates", lambda: list(mirrors))
    monkeypatch.setattr(adapter, "_preflight", AsyncMock())
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock())
    monkeypatch.setattr(
        adapter, "_serves_bytes", AsyncMock(return_value=(True, "fixture"))
    )

    async def resolve(_client, mirror, _md5):
        resolved.append(mirror)
        return "synthetic", "fixture"

    monkeypatch.setattr(adapter, "_resolve_key", resolve)
    router = SourceRouter(config)
    router._libgen = adapter
    monkeypatch.setattr(bridge, "get_source_router", AsyncMock(return_value=router))
    return router, resolved


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.asyncio
async def test_infeasible_40kbps_reaches_second_mirror(
    tmp_path, monkeypatch, clock, seed
):
    rng = random.Random(seed)
    requests = []
    config = SourceConfig(download_timeout=15, preflight_enabled=False)
    router, resolved = install_router(monkeypatch, config)

    async def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        requests.append((headers["host"], start))
        if headers["host"] == "libgen.vg":
            assert clock.time() - began < 15
            return 200, {}, BODY, 0
        life = rng.uniform(1, 5)
        await clock.advance(life)
        return reply(start, int(40000 * life))

    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        began = clock.time()
        try:
            result = await bridge._fetch_from_source(
                {"source": "libgen", "md5": MD5}, str(tmp_path)
            )
        finally:
            await router.close()
    assert resolved == ["li", "vg"]
    assert requests[-1] == ("libgen.vg", 0)
    assert len(requests) == (3 if seed == 1 else 2)
    assert Path(result).read_bytes() == BODY
    assert list(tmp_path.iterdir()) == [Path(result)]


BAD_ROWS = [
    "ignored-range",
    "gzip-restart",
    "one-byte-range",
    "65537-fast",
    "65537-at-32000",
    "40000",
    "33000",
    "32000",
    "new-empty",
    "new-replay",
    "new-short-replay",
    "new-wiping-416",
    "ten-7kib",
    "ten-new-nine-empty",
]


@pytest.mark.parametrize("mode", BAD_ROWS)
@pytest.mark.asyncio
async def test_gate_bad_mirror_rows_reach_fallback(tmp_path, monkeypatch, clock, mode):
    # The 1.5x margin permits six fast 65,537-byte replies here rather
    # than four. The same 5 s chain must still reach the healthy mirror.
    requests = []
    budget = 10 if mode.startswith("ten-") else 2
    config = SourceConfig(
        download_timeout=5, download_resume_attempts=budget, preflight_enabled=False
    )
    router, resolved = install_router(monkeypatch, config)
    compressed = gzip.compress(BODY)
    highwater = 0

    async def handler(_path, headers):
        nonlocal highwater
        start = int(headers.get("range", "bytes=0-")[6:-1])
        if headers["host"] == "libgen.vg":
            return 200, {}, BODY, 0
        requests.append(start)
        n = len(requests)
        # Bound broken controls without relying on elapsed host time.
        if n > 24:
            return 400, {}, b"", 0
        count, restart = 65536, False
        if mode == "gzip-restart":
            await clock.advance(0.001)
            return (
                200,
                {"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
                compressed[: len(compressed) // 2],
                0,
            )
        if mode == "ignored-range":
            restart = True
        elif mode == "one-byte-range" and start:
            await clock.advance(0.001)
            return (
                206,
                {"Content-Range": f"bytes {start}-{start}/{len(BODY)}"},
                BODY[start : start + 1],
                0,
            )
        elif mode.startswith("65537"):
            count = 65537
        elif mode in ("40000", "33000", "32000"):
            count = int(mode)
        elif mode == "ten-7kib":
            count = 7168
        elif mode == "ten-new-nine-empty":
            count = 65536 if (n - 1) % 10 == 0 else 0
        elif mode.startswith("new-"):
            if n % 2:
                count = highwater + 65536 - start
            elif mode == "new-empty":
                count = 0
            elif mode == "new-wiping-416":
                await clock.advance(0.001)
                return 416, {}, b"", 0
            else:
                restart = True
                count = highwater if mode == "new-replay" else 5003
        highwater = max(highwater, (0 if restart else start) + count)
        await clock.advance(count / 32000 if mode == "65537-at-32000" else 0.001)
        return reply(start, count, restart=restart)

    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        began = clock.time()
        try:
            result = await bridge._fetch_from_source(
                {"source": "libgen", "md5": MD5}, str(tmp_path)
            )
        finally:
            await router.close()
    assert resolved == ["li", "vg"]
    assert clock.time() - began < 5
    assert len(requests) <= (
        10 if mode == "ten-7kib" else 6 if mode == "65537-fast" else 4
    )
    if mode == "new-wiping-416":
        assert len(requests) == 2
    assert Path(result).read_bytes() == BODY
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.asyncio
async def test_lone_slow_mirror_still_finishes(tmp_path, monkeypatch, clock, seed):
    body = BODY[: 640 * 1024]
    digest = hashlib.md5(body).hexdigest()
    rng = random.Random(seed)
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=30, preflight_enabled=False), ("li",)
    )

    async def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        count = min(len(body) - start, int(40000 * rng.uniform(1, 5)))
        await clock.advance(count / 40000)
        return reply(start, count, body)

    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        try:
            result = await bridge._fetch_from_source(
                {"source": "libgen", "md5": digest}, str(tmp_path)
            )
        finally:
            await router.close()
    assert resolved == ["li"]
    assert Path(result).read_bytes() == body


@pytest.mark.parametrize(
    "scenario", ["lone", "unknown", "feasible", "last", "small-first"]
)
@pytest.mark.asyncio
async def test_retry_guard_preserves_patience_and_feasible_transfers(
    tmp_path, monkeypatch, clock, scenario
):
    unknown = scenario == "unknown"
    cut = 65535 if scenario == "small-first" else 65536
    calls = []
    body = BODY[: 128 * 1024]
    digest = hashlib.md5(body).hexdigest()
    router, resolved = install_router(
        monkeypatch,
        SourceConfig(download_timeout=5, preflight_enabled=False),
        ("li",) if scenario == "lone" else ("li", "vg"),
    )

    class Drop(httpx.AsyncByteStream):
        async def __aiter__(self):
            await clock.advance(1 if scenario == "feasible" else 4)
            yield body[:cut]
            raise httpx.RemoteProtocolError("synthetic drop")

    def handler(request):
        calls.append((request.url.host, request.headers.get("Range")))
        if scenario == "last" and request.url.host == "libgen.li":
            return httpx.Response(400)
        if request.headers.get("Range") is None:
            return httpx.Response(
                200,
                headers={} if unknown else {"Content-Length": str(len(body))},
                stream=Drop(),
            )
        return httpx.Response(
            206,
            headers={
                "Content-Range": f"bytes {cut}-{len(body) - 1}/{'*' if unknown else len(body)}"
            },
            content=body[cut:],
        )

    # Resolution stubs don't use the client transport; transfers use real httpx.
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(**kw, transport=httpx.MockTransport(handler)),
    )
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": digest}, str(tmp_path)
        )
    finally:
        await router.close()
    assert resolved == (["li", "vg"] if scenario == "last" else ["li"])
    host = "libgen.vg" if scenario == "last" else "libgen.li"
    assert calls == ([("libgen.li", None)] if scenario == "last" else []) + [
        (host, None),
        (host, f"bytes={cut}-"),
    ]
    assert Path(result).read_bytes() == body


@pytest.mark.asyncio
async def test_router_marks_cross_provider_fallback_without_resolving_it_early(
    monkeypatch,
):
    router = SourceRouter(
        SourceConfig(annas_secret_key="synthetic", preflight_enabled=False)
    )
    annas = AsyncMock()
    annas.get_download_url.return_value = DownloadResult(
        url="https://fixture.invalid/book", source=SourceType.ANNAS_ARCHIVE
    )
    libgen = AsyncMock()
    libgen.get_download_url.return_value = DownloadResult(
        url="https://fixture.invalid/last", source=SourceType.LIBGEN
    )
    monkeypatch.setattr(router, "_get_annas", lambda: annas)
    monkeypatch.setattr(router, "_get_libgen", lambda: libgen)
    stream = router.iter_download_candidates(MD5, source="auto")
    try:
        first = await anext(stream)
        assert getattr(first, "has_more_candidates", False) is True
        libgen.get_download_url.assert_not_awaited()
        last = await anext(stream)
        assert getattr(last, "has_more_candidates", False) is False
        assert annas.get_download_url.await_count == 1
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_valid_range_total_enables_guard_after_unknown_initial_body(
    tmp_path, monkeypatch, clock
):
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=15, preflight_enabled=False)
    )
    calls = []

    class Drop(httpx.AsyncByteStream):
        def __init__(self, start):
            self.start = start

        async def __aiter__(self):
            await clock.advance(1)
            yield BODY[self.start : self.start + 65536]
            raise httpx.RemoteProtocolError("synthetic drop")

    def handler(request):
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        calls.append((request.url.host, start))
        if request.url.host == "libgen.vg":
            return httpx.Response(200, content=BODY)
        return httpx.Response(
            206 if start else 200,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"}
            if start
            else {},
            stream=Drop(start),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(**kw, transport=httpx.MockTransport(handler)),
    )
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": MD5}, str(tmp_path)
        )
    finally:
        await router.close()
    assert resolved == ["li", "vg"]
    assert calls == [("libgen.li", 0), ("libgen.li", 65536), ("libgen.vg", 0)]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_guard_uses_chain_time_left_after_resolution(
    tmp_path, monkeypatch, clock
):
    body = BODY[: 512 * 1024]
    digest = hashlib.md5(body).hexdigest()
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=15, preflight_enabled=False)
    )
    resolve = router._libgen._resolve_key

    # Six seconds of resolution leaves 6.5 s after the first 2.5 s read;
    # the 10.85 s projection exceeds 1.5 * 6.5, not a fresh 15 s budget.
    async def delayed(client, mirror, md5):
        if mirror == "li":
            await clock.advance(6)
        return await resolve(client, mirror, md5)

    monkeypatch.setattr(router._libgen, "_resolve_key", delayed)
    requests = []

    async def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        requests.append((headers["host"], start))
        if headers["host"] == "libgen.vg":
            return 200, {}, body, 0
        count = min(100000, len(body) - start)
        await clock.advance(count / 40000)
        return reply(start, count, body)

    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        try:
            result = await bridge._fetch_from_source(
                {"source": "libgen", "md5": digest}, str(tmp_path)
            )
        finally:
            await router.close()
    assert resolved == ["li", "vg"]
    assert requests == [("libgen.li", 0), ("libgen.vg", 0)]
    assert Path(result).read_bytes() == body


@pytest.mark.asyncio
async def test_repeated_short_drop_reports_partial_transfer_after_recovery(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return reply(start, 5000)

    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError) as error:
            await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert error.value.reason == "partial_transfer"
    assert "bytes_received=15000" in error.value.detail
    assert offsets == [0, 5000, 10000]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_guard_reserves_the_next_retry_wait(tmp_path, monkeypatch, clock):
    # After 1 s, 1.5 * 0.75 s = 1.125 s fits the 1 s remainder but
    # not its required 250 ms retry wait.
    body = BODY[: 128 * 1024]
    digest = hashlib.md5(body).hexdigest()
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=1.75, preflight_enabled=False)
    )
    calls = []

    class Drop(httpx.AsyncByteStream):
        async def __aiter__(self):
            await clock.advance(1)
            yield body[:65536]
            raise httpx.RemoteProtocolError("synthetic drop")

    def handler(request):
        calls.append((request.url.host, request.headers.get("Range")))
        if request.url.host == "libgen.li" and len(calls) == 1:
            return httpx.Response(
                200, headers={"Content-Length": str(len(body))}, stream=Drop()
            )
        if request.headers.get("Range"):
            return httpx.Response(
                206,
                headers={"Content-Range": f"bytes 65536-{len(body) - 1}/{len(body)}"},
                content=body[65536:],
            )
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(**kw, transport=httpx.MockTransport(handler)),
    )
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": digest}, str(tmp_path)
        )
    finally:
        await router.close()
    assert resolved == ["li", "vg"]
    assert calls == [("libgen.li", None), ("libgen.vg", None)]
    assert Path(result).read_bytes() == body
