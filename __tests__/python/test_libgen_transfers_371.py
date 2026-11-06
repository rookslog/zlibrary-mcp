"""Issue 371 regressions: real HTTP on loopback, synthetic books only."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from lib import python_bridge as bridge
from lib.sources.config import SourceConfig, get_source_config
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from lib.sources.libgen import LibgenAdapter
from lib.sources.models import DownloadResult, SourceType
from lib.sources.net import WalkDeadline
from lib.sources.router import SourceRouter

BODY = b"%PDF-1.7\n" + b"x" * (65536 * 9)
MD5 = hashlib.md5(BODY).hexdigest()


@asynccontextmanager
async def loopback(handler):
    """Serve explicit HTTP replies; a short body causes a real httpx read error."""
    tasks = set()

    async def serve(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            headers = dict(
                line.lower().split(": ", 1) for line in lines[1:] if ": " in line
            )
            reply = handler(lines[0].split()[1], headers)
            if asyncio.iscoroutine(reply):
                reply = await reply
            status, extra, body, delay = reply
            if delay:
                await asyncio.sleep(delay)
            reply = {"Content-Length": str(len(body)), "Connection": "close", **extra}
            writer.write(f"HTTP/1.1 {status} fixture\r\n".encode())
            writer.write(
                "".join(f"{k}: {v}\r\n" for k, v in reply.items()).encode()
                + b"\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
async def clock(monkeypatch):
    """Advance the event-loop clock at fixture boundaries, never by CPU timing."""
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    sleep = asyncio.sleep
    monkeypatch.setattr(loop, "time", lambda: now[0])

    async def advance(seconds):
        now[0] += seconds
        for _ in range(4):
            await sleep(0)

    return SimpleNamespace(time=lambda: now[0], advance=advance, sleep=sleep)


def route_to_loopback(monkeypatch, base):
    """Keep adapters/redirect logic real; replace provider destinations only."""
    client_type = httpx.AsyncClient

    class LocalOnly(httpx.AsyncBaseTransport):
        def __init__(self):
            self.transport = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            headers = dict(request.headers)
            headers["host"] = request.url.host
            local = httpx.Request(
                request.method, base + request.url.raw_path.decode(), headers=headers
            )
            return await self.transport.handle_async_request(local)

        async def aclose(self):
            await self.transport.aclose()

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(**kw, transport=LocalOnly(), trust_env=False),
    )


def dropped(start, count=65536):
    return (
        200 if not start else 206,
        {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(BODY) - start),
            **(
                {"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"}
                if start
                else {}
            ),
        },
        BODY[start : start + count],
        0,
    )


@pytest.mark.asyncio
async def test_many_progressing_drops_do_not_spend_resume_budget(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return dropped(start, 65536 if start < 65536 * 7 else len(BODY))

    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(
            url,
            str(tmp_path),
            MD5,
            "libgen",
            config=SourceConfig(download_resume_attempts=1),
        )
    assert offsets == [65536 * i for i in range(8)]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_no_progress_resumes_stop_at_limit(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return dropped(start, 0 if start else 65536)

    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError, match="partial transfer"):
            await bridge._download_url_to_file(
                url,
                str(tmp_path),
                MD5,
                "libgen",
                config=SourceConfig(download_resume_attempts=2),
            )
    assert offsets == [0, 65536, 65536]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_range_5xx_keeps_prefix_and_resumes_with_matching_md5(tmp_path, status):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        if len(offsets) == 2:
            return status, {}, b"", 0
        return dropped(start, 65536 if not start else len(BODY))

    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert offsets == [0, 65536, 65536]
    assert hashlib.md5(Path(result).read_bytes()).hexdigest() == MD5
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_mirror_resolution_gets_exactly_one_second_pass(monkeypatch, recover):
    hosts = []

    def handler(path, headers):
        assert "ads.php" in path or "get.php" in path
        if "ads.php" in path:
            hosts.append(headers["host"])
            if len(hosts) <= 3 or not recover:
                return 503, {}, b"", 0
            return (
                200,
                {},
                b'<a href="get.php?md5=' + MD5.encode() + b'&key=FIXTURE">GET</a>',
                0,
            )
        return 206, {}, BODY[:2048], 0

    config = SourceConfig(preflight_enabled=False)
    adapter = LibgenAdapter(config)
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=None))
    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        if recover:
            result = await adapter.get_download_url(MD5)
            assert result.url.startswith("https://libgen.li/")
        else:
            with pytest.raises(AllSourcesFailedError) as caught:
                await adapter.get_download_url(MD5)
            assert [f.reason for f in caught.value.failures] == ["http_error"] * 6
    assert hosts == ["libgen.li", "libgen.vg", "libgen.la"] + (
        ["libgen.li"] if recover else ["libgen.li", "libgen.vg", "libgen.la"]
    )


@pytest.mark.asyncio
async def test_second_pass_delay_cannot_outlive_resolution_deadline(monkeypatch, clock):
    hosts = []

    async def handler(_path, headers):
        hosts.append(headers["host"])
        await clock.advance(1)
        return 503, {}, b"", 0

    async def retry_delay(seconds):
        if seconds:
            await clock.advance(seconds)
            await asyncio.Event().wait()  # The existing deadline must cancel this wait.
        else:
            await clock.sleep(0)

    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=None))
    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        monkeypatch.setattr(asyncio, "sleep", retry_delay)
        with pytest.raises(AllSourcesFailedError):
            await adapter.get_download_url(
                MD5, deadline=WalkDeadline(4, clock=clock.time)
            )
    assert hosts == ["libgen.li", "libgen.vg", "libgen.la"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size, succeeds", [(None, False), ("4 MB", True)])
async def test_known_size_extends_the_actual_chain_deadline(
    tmp_path, monkeypatch, clock, size, succeeds
):
    async def handler(_path, _headers):
        await clock.advance(7)
        return 200, {"Content-Type": "application/pdf"}, BODY, 0

    config = SourceConfig(download_timeout=3, download_timeout_ceiling=25)
    async with loopback(handler) as url:
        candidates_for(monkeypatch, config, url)
        operation = bridge._fetch_from_source(
            {"md5": MD5, "source": "libgen", "size": size}, str(tmp_path)
        )
        if succeeds:
            assert Path(await operation).read_bytes() == BODY
        else:
            with pytest.raises(ProviderTimeoutError) as error:
                await operation
            assert error.value.reason == "read_timeout"


@pytest.mark.asyncio
async def test_5xx_delay_is_inside_transfer_deadline(tmp_path, monkeypatch, clock):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return (503, {}, b"", 0) if start else dropped(0)

    async def retry_delay(seconds):
        if seconds:
            assert 0 < seconds <= 0.5
            if len(offsets) == 2:
                await clock.advance(11)
                await asyncio.Event().wait()
        await clock.sleep(0)

    monkeypatch.setattr(asyncio, "sleep", retry_delay)
    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError) as error:
            await bridge._download_url_to_file(
                url,
                str(tmp_path),
                MD5,
                "libgen",
                config=SourceConfig(download_timeout=10),
            )
    assert error.value.reason == "partial_transfer"
    assert offsets == [0, 65536]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "size, expected",
    [(None, 1500), (1024, 1500), (80 * 1024 * 1024, 2048), (400 * 1024 * 1024, 3600)],
)
def test_size_budget_base_floor_and_ceiling(size, expected):
    assert SourceConfig().download_budget(size) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "bad", ""])
def test_invalid_scaling_settings_use_finite_defaults(monkeypatch, raw):
    monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_FLOOR_SPEED", raw)
    monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_TIMEOUT_CEILING", raw)
    config = get_source_config(for_download=True)
    assert config.download_budget(400 * 1024 * 1024) == 3600


def test_size_ceiling_is_clamped_to_short_node_owner(monkeypatch):
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "15000")
    config = get_source_config(for_download=True)
    assert 6 < config.download_budget(400 * 1024 * 1024) < 15


@pytest.mark.asyncio
async def test_annas_range_recovery_spends_one_keyed_resolution(tmp_path, monkeypatch):
    keyed_calls = []
    offsets = []

    def handler(path, headers):
        if "fast_download.json" in path:
            keyed_calls.append(path)
            return (
                200,
                {"Content-Type": "application/json"},
                b'{"download_url":"https://bytes.example/book","account_info":{"downloads_left":7}}',
                0,
            )
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        if len(offsets) == 2:
            return 503, {}, b"", 0
        return dropped(start, 65536 if not start else len(BODY))

    router = SourceRouter(
        SourceConfig(annas_secret_key="synthetic", preflight_enabled=False)
    )
    monkeypatch.setattr(bridge, "get_source_router", AsyncMock(return_value=router))
    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        try:
            result = await bridge._fetch_from_source(
                {"source": "annas", "md5": MD5}, str(tmp_path)
            )
        finally:
            await router.close()
    assert Path(result).read_bytes() == BODY
    assert len(keyed_calls) == 1
    assert offsets == [0, 65536, 65536]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner", [False, True], ids=["size-ceiling", "acquisition-owner"]
)
async def test_known_size_cannot_extend_ceiling_or_acquisition_owner(
    tmp_path, monkeypatch, clock, owner
):
    from lib.acquisition import _acquisition_scope

    async def handler(_path, _headers):
        # 12 is below the 15s size ceiling but above the owner's 8.749s slice.
        # Without the chain's owner clamp the owner variant succeeds and fails this test.
        await clock.advance(12 if owner else 20)
        return 200, {}, BODY, 0

    config = SourceConfig(download_timeout=4, download_timeout_ceiling=15)
    async with loopback(handler) as url:
        candidates_for(monkeypatch, config, url)
        token = (
            _acquisition_scope.set({"deadline": WalkDeadline(10, clock=clock.time)})
            if owner
            else None
        )
        try:
            with pytest.raises(ProviderTimeoutError) as error:
                await bridge._fetch_from_source(
                    {"md5": MD5, "source": "libgen", "size": "400 MB"}, str(tmp_path)
                )
            assert error.value.reason == "read_timeout"
        finally:
            if token is not None:
                _acquisition_scope.reset(token)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_no_progress_budget_resets_after_credited_progress(tmp_path, monkeypatch):
    offsets = []
    sleep = asyncio.sleep

    async def step(_seconds):
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", step)

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return dropped(
            start,
            len(BODY) if len(offsets) == 5 else 65536 if len(offsets) in (1, 3) else 0,
        )

    # A genuinely new 64 KiB earns a fresh empty-attempt allowance. Keeping
    # lifetime penalties would reject a useful transfer after scattered stalls.
    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(
            url,
            str(tmp_path),
            MD5,
            "libgen",
            config=SourceConfig(download_resume_attempts=2),
        )
    assert offsets == [0, 65536, 65536, 131072, 131072]
    assert Path(result).read_bytes() == BODY


@pytest.mark.parametrize(
    "book, expected",
    [
        ({"size_mb": 80}, 83886080),
        ({"filesize": 83886080}, 83886080),
        ({"size": "80 MB"}, 83886080),
        ({"size": "81920 KiB"}, 83886080),
        ({"size": "0.078125 GB"}, 83886080),
        ({"size": "unknown"}, None),
        ({"size": -1}, None),
        ({"size": True}, None),
        ({"size": "nan MB"}, None),
    ],
)
def test_existing_size_metadata_normalizes_without_a_provider_probe(book, expected):
    assert bridge._source_size_bytes(book) == expected


@pytest.mark.asyncio
async def test_yielded_candidate_prevents_another_mirror_resolution_pass(monkeypatch):
    hosts = []

    def handler(path, headers):
        if "ads.php" in path:
            hosts.append(headers["host"])
            if headers["host"] != "libgen.li":
                return 503, {}, b"", 0
            return (
                200,
                {},
                b'<a href="get.php?md5=' + MD5.encode() + b'&key=FIXTURE">GET</a>',
                0,
            )
        return 206, {}, BODY[:2048], 0

    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=None))
    yielded = []
    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        with pytest.raises(AllSourcesFailedError) as caught:
            async for candidate in adapter.iter_download_candidates(MD5):
                yielded.append(candidate)
    assert len(yielded) == 1
    assert hosts == ["libgen.li", "libgen.vg", "libgen.la"]
    assert [failure.reason for failure in caught.value.failures] == ["http_error"] * 2


def candidates_for(monkeypatch, config, *urls):
    async def candidates(_md5, source):
        for url in urls:
            yield DownloadResult(url=url, source=SourceType.LIBGEN)

    monkeypatch.setattr(
        bridge,
        "get_source_router",
        AsyncMock(
            return_value=SimpleNamespace(
                config=config, iter_download_candidates=candidates
            )
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ignored-range", "gzip-restart", "one-byte-range"])
async def test_useless_recovery_reaches_healthy_second_mirror(
    tmp_path, monkeypatch, mode
):
    import gzip

    bad_offsets, good_offsets = [], []

    def bad(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        bad_offsets.append(start)
        # Bound the unfixed control without relying on elapsed wall time.
        if len(bad_offsets) > 3:
            return 400, {}, b"", 0
        if mode == "gzip-restart":
            compressed = gzip.compress(BODY)
            return (
                200,
                {"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
                compressed[: len(compressed) // 2],
                0,
            )
        if mode == "one-byte-range" and start:
            return (
                206,
                {"Content-Range": f"bytes {start}-{start}/{len(BODY)}"},
                BODY[start : start + 1],
                0,
            )
        return dropped(0)

    def healthy(_path, headers):
        good_offsets.append(headers.get("range"))
        return 200, {"Content-Type": "application/pdf"}, BODY, 0

    async with loopback(bad) as first, loopback(healthy) as second:
        candidates_for(monkeypatch, SourceConfig(), first, second)
        result = await bridge._fetch_from_source(
            {"md5": MD5, "source": "libgen"}, str(tmp_path)
        )
    assert len(bad_offsets) == 3
    assert good_offsets == [None]
    assert Path(result).read_bytes() == BODY
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 501, 505])
async def test_nonretry_range_statuses_stop_immediately(tmp_path, status):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        return dropped(0) if not start else (status, {}, b"", 0)

    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError) as error:
            await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert error.value.detail == f"HTTP {status}"
    assert offsets == [0, 65536]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_5xx_after_encoded_restart_at_zero_reaches_next_mirror(
    tmp_path, monkeypatch
):
    import gzip

    requests = []

    def first(_path, headers):
        requests.append(headers.get("range"))
        if len(requests) == 1:
            compressed = gzip.compress(BODY)
            return (
                200,
                {"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
                compressed[: len(compressed) // 2],
                0,
            )
        return 503, {}, b"", 0

    async with loopback(first) as bad, loopback(lambda *_: (200, {}, BODY, 0)) as good:
        candidates_for(monkeypatch, SourceConfig(), bad, good)
        result = await bridge._fetch_from_source(
            {"md5": MD5, "source": "libgen"}, str(tmp_path)
        )
    assert requests == [None, None]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_budget_stopped_first_pass_does_not_wait_or_record_twice(monkeypatch):
    from lib.sources import libgen

    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(libgen, "_attempt_minimum", lambda *_: 3.0)
    sleep = AsyncMock()
    monkeypatch.setattr(libgen.asyncio, "sleep", sleep)
    with pytest.raises(AllSourcesFailedError) as error:
        await adapter.get_download_url(MD5, deadline=WalkDeadline(2.5))
    assert [f.reason for f in error.value.failures] == ["walk_budget_exhausted"]
    assert sleep.await_count == 0


@pytest.mark.parametrize(
    "book",
    [
        {"size_mb": "", "size": "80 MB"},
        {"size_mb": False, "filesize": 83886080},
        {"size_mb": -1, "filesize": "bad", "size": "80 MB"},
        {"size_mb": "0", "size": "80 MB"},
        {"size_mb": "9" * 5000, "size": "80 MB"},
    ],
)
def test_unusable_size_fields_fall_through(book):
    assert bridge._source_size_bytes(book) == 83886080


def test_configured_base_is_not_cut_by_default_ceiling(monkeypatch):
    monkeypatch.setenv("BOOK_SOURCE_DOWNLOAD_TIMEOUT", "5000")
    monkeypatch.setenv("PYTHON_BRIDGE_LONG_TIMEOUT", "6000000")
    config = get_source_config(for_download=True)
    from lib.sources.config import worst_case_download_seconds

    assert worst_case_download_seconds(config) == 5030
    assert config.download_budget(None) == 5000
    assert config.download_budget(400 * 1024 * 1024) == 5000


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["503", "416"])
async def test_every_admitted_retry_is_paced_with_a_short_delay(
    tmp_path, monkeypatch, response
):
    delays, offsets = [], []
    sleep = asyncio.sleep

    async def paced(seconds):
        if seconds:
            delays.append(seconds)
        await sleep(0)

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        if len(offsets) == 1:
            return dropped(0)
        if len(offsets) == 2:
            return int(response), {}, b"", 0
        return dropped(start, len(BODY))

    monkeypatch.setattr(asyncio, "sleep", paced)
    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert len(delays) == 2
    assert all(0 < delay <= 0.5 for delay in delays)
    assert offsets == [0, 65536, 0 if response == "416" else 65536]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_completed_pass_with_insufficient_time_does_not_admit_retry_delay(
    monkeypatch, clock
):
    hosts = []

    async def handler(_path, headers):
        hosts.append(headers["host"])
        await clock.advance(1)
        return 503, {}, b"", 0

    async def no_retry_delay(seconds):
        assert seconds == 0, "retry delay admitted with less than one second remaining"
        await clock.sleep(0)

    adapter = LibgenAdapter(SourceConfig(preflight_enabled=False))
    monkeypatch.setattr(adapter, "_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(asyncio, "sleep", no_retry_delay)
    async with loopback(handler) as base:
        route_to_loopback(monkeypatch, base)
        with pytest.raises(AllSourcesFailedError) as error:
            await adapter.get_download_url(
                MD5, deadline=WalkDeadline(3.5, clock=clock.time)
            )
    assert [failure.reason for failure in error.value.failures] == ["http_error"] * 3
    assert hosts == ["libgen.li", "libgen.vg", "libgen.la"]


@pytest.mark.asyncio
async def test_unsatisfied_range_spends_the_last_empty_attempt(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = int(headers.get("range", "bytes=0-")[6:-1])
        offsets.append(start)
        if len(offsets) == 1:
            return dropped(0)
        return (416 if len(offsets) == 2 else 400), {}, b"", 0

    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError) as error:
            await bridge._download_url_to_file(
                url,
                str(tmp_path),
                MD5,
                "libgen",
                config=SourceConfig(download_resume_attempts=1),
            )
    assert error.value.detail == "HTTP 416"
    assert offsets == [0, 65536]
    assert not list(tmp_path.iterdir())
