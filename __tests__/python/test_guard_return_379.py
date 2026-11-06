"""Gate-2 regressions for feasibility inputs and the final resolved-URL return."""

import asyncio
import datetime as dt
from contextlib import contextmanager
import hashlib
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from lib import acquisition
from lib import python_bridge as bridge
from lib.sources.config import SourceConfig
from lib.sources.errors import (
    AllSourcesFailedError,
    ProviderResponseError,
    ProviderUnreachableError,
)
from lib.sources.net import WalkDeadline
from test_libgen_transfers_371 import clock as _virtual_clock
from test_mirror_feasibility_379 import BODY, install_router

clock = _virtual_clock


@pytest.fixture(autouse=True)
def scripted_waits(monkeypatch, clock):
    async def sleep(seconds):
        if seconds:
            await clock.advance(seconds)
        else:
            await clock.sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)


@contextmanager
def scope(clock, seconds):
    token = acquisition._acquisition_scope.set(
        {"deadline": WalkDeadline(seconds, clock=clock.time)}
    )
    try:
        yield
    finally:
        acquisition._acquisition_scope.reset(token)


def install_http(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(**kw, transport=httpx.MockTransport(handler)),
    )


class Drop(httpx.AsyncByteStream):
    def __init__(self, clock, chunk, seconds):
        self.clock, self.chunk, self.seconds = clock, chunk, seconds

    async def __aiter__(self):
        await self.clock.advance(self.seconds)
        yield self.chunk
        raise httpx.RemoteProtocolError("synthetic drop")


@pytest.mark.asyncio
async def test_n1_guard_uses_lease_scoped_time_left(tmp_path, monkeypatch, clock):
    body = BODY * 2
    digest = hashlib.md5(body).hexdigest()
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=1500, preflight_enabled=False)
    )
    calls = []

    def handler(request):
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        calls.append((request.url.host, start))
        if request.url.host == "libgen.vg":
            return httpx.Response(200, content=body)
        return httpx.Response(
            206 if start else 200,
            headers={
                "Content-Length": str(len(body) - start),
                **(
                    {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
                    if start
                    else {}
                ),
            },
            stream=Drop(clock, body[start : start + 40000], 0.01),
        )

    install_http(monkeypatch, handler)
    began = clock.time()
    try:
        with scope(clock, 16):
            result = await bridge._fetch_from_source(
                {"source": "libgen", "md5": digest}, str(tmp_path)
            )
    finally:
        await router.close()
    assert resolved == ["li", "vg"]
    assert calls[-1] == ("libgen.vg", 0)
    assert clock.time() - began < 14
    assert Path(result).read_bytes() == body


@pytest.mark.parametrize(
    "restart", [True, False], ids=["written-not-furthest", "range-total-not-remainder"]
)
@pytest.mark.asyncio
async def test_n1_guard_keeps_current_remainder_and_range_total(
    tmp_path, monkeypatch, clock, restart
):
    cut = 3 * 512 * 1024
    config = SourceConfig(
        download_timeout=7.25 if restart else 4.95, preflight_enabled=False
    )
    router, resolved = install_router(monkeypatch, config)
    calls = []

    def handler(request):
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        calls.append((request.url.host, start))
        if request.url.host == "libgen.vg":
            return httpx.Response(200, content=BODY)
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(BODY))},
                stream=Drop(clock, BODY[:cut], 1),
            )
        if len(calls) == 2:
            origin = 0 if restart else start
            return httpx.Response(
                200 if restart else 206,
                headers={
                    "Content-Length": str(len(BODY) - origin),
                    **(
                        {"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"}
                        if not restart
                        else {}
                    ),
                },
                stream=Drop(clock, BODY[origin : origin + 65536], 3),
            )
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{len(BODY) - 1}/{len(BODY)}"},
            content=BODY[start:],
        )

    install_http(monkeypatch, handler)
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": hashlib.md5(BODY).hexdigest()}, str(tmp_path)
        )
    finally:
        await router.close()
    assert resolved == ["li", "vg"]
    assert calls == [("libgen.li", 0), ("libgen.li", cut), ("libgen.vg", 0)]
    assert Path(result).read_bytes() == BODY


class Stalled(httpx.AsyncByteStream):
    def __init__(self, clock, chunk, seconds, timeout=True):
        self.clock, self.chunk, self.seconds, self.timeout = (
            clock,
            chunk,
            seconds,
            timeout,
        )

    async def __aiter__(self):
        await self.clock.advance(self.seconds)
        yield self.chunk
        if self.timeout:
            await self.clock.advance(30)
            raise httpx.ReadTimeout("synthetic stall")
        raise httpx.RemoteProtocolError("synthetic drop")


class Completed(httpx.AsyncByteStream):
    def __init__(self, clock, data, seconds=None):
        self.clock, self.data, self.seconds = clock, data, seconds

    async def __aiter__(self):
        await self.clock.advance(
            len(self.data) / 5000000 if self.seconds is None else self.seconds
        )
        yield self.data


F1_ROWS = [
    "no-key",
    "unreachable",
    "large-prefix",
    "slow-start",
    "annas",
    "lease",
    "second-pass",
    "healthy",
    "annas-healthy",
    "two-abandoned",
    "first-return-403",
    "annas-first-return-403",
    "return-drop",
    "bad-transfers",
]


@pytest.mark.parametrize("mode", F1_ROWS)
@pytest.mark.asyncio
async def test_f1_abandoned_url_returns_once_after_candidates_exhausted(
    tmp_path, monkeypatch, clock, capsys, mode
):
    size = (
        (
            64
            if mode == "large-prefix"
            else 32
            if mode == "slow-start"
            else 2
            if mode == "lease"
            else 16
        )
        * 1024
        * 1024
    )
    body = (BODY * ((size + len(BODY) - 1) // len(BODY)))[:size]
    digest = hashlib.md5(body).hexdigest()
    keyed = mode.startswith("annas")
    config = SourceConfig(
        download_timeout=1500,
        preflight_enabled=mode in ("lease", "unreachable"),
        annas_secret_key="synthetic" if keyed else None,
    )
    router, resolved = install_router(monkeypatch, config, ("li", "vg", "la"))
    annas = router._get_annas() if keyed else None
    if annas is not None:
        monkeypatch.setattr(annas, "_preflight", AsyncMock())
    key_requests, requests, counts = [], [], {}
    first_host = "bytes.example" if keyed else "libgen.li"
    cut = (
        1024 * 1024
        if mode == "large-prefix"
        else len(body) - 150000
        if mode == "lease"
        else 200000
        if mode == "slow-start"
        else 100000
    )
    delay = (
        100
        if mode == "lease"
        else cut / 3300
        if mode == "slow-start"
        else cut / 5000000
    )
    calls_to_li = 0

    async def resolve(_client, mirror, _md5):
        nonlocal calls_to_li
        resolved.append(mirror)
        if mirror == "li":
            calls_to_li += 1
        if mode == "second-pass" and (mirror != "li" or calls_to_li == 1):
            return None, "no key"
        if mirror == "li" and (not keyed or mode == "annas-first-return-403"):
            return "synthetic", "fixture"
        if (
            mode
            in (
                "healthy",
                "annas-healthy",
                "two-abandoned",
                "first-return-403",
                "lease",
            )
            and mirror == "vg"
        ) or (mode == "bad-transfers" and mirror in ("vg", "la")):
            return "synthetic", "fixture"
        return None, "no key"

    async def preflight(mirror):
        if mode == "unreachable" and mirror != "li":
            await clock.advance(5)
            raise ProviderUnreachableError(
                "libgen",
                "libgen." + mirror,
                "synthetic unreachable",
                reason="connect_timeout",
            )

    monkeypatch.setattr(router._libgen, "_resolve_key", resolve)
    monkeypatch.setattr(router._libgen, "_preflight", preflight)

    def handler(request):
        if request.url.path.endswith("fast_download.json"):
            key_requests.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "download_url": "https://bytes.example/file?token=synthetic-secret",
                    "account_fast_download_info": {
                        "downloads_left": 5,
                        "downloads_per_day": 10,
                        "downloads_done_today": 5,
                    },
                },
            )
        host = request.url.host
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        requests.append((host, start, str(request.url)))
        counts[host] = counts.get(host, 0) + 1
        if start == 0:
            assert next(tmp_path.glob("*.part")).stat().st_size == 0
        if host == first_host and counts[host] == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Stalled(
                    clock, body[:cut], delay, mode not in ("lease", "slow-start")
                ),
            )
        if (
            mode in ("first-return-403", "annas-first-return-403")
            and host == first_host
            and counts[host] == 2
        ):
            return httpx.Response(403)
        if mode == "return-drop" and host == first_host and counts[host] == 2:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Stalled(clock, body[:cut], delay),
            )
        if mode == "bad-transfers" and host != first_host:
            return httpx.Response(400)
        second_host = "libgen.li" if keyed else "libgen.vg"
        if (
            mode in ("two-abandoned", "first-return-403", "annas-first-return-403")
            and host == second_host
        ):
            if counts[host] == 1:
                return httpx.Response(
                    200,
                    headers={"Content-Length": str(len(body))},
                    stream=Stalled(clock, body[:100000], 0.02),
                )
            if mode == "two-abandoned":
                return httpx.Response(400)
        return httpx.Response(
            206 if start else 200,
            headers={
                "Content-Length": str(len(body) - start),
                **(
                    {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
                    if start
                    else {}
                ),
            },
            stream=Completed(clock, body[start:]),
        )

    install_http(monkeypatch, handler)
    began = clock.time()
    if keyed:
        epoch = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)

        class ObservedClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return epoch + dt.timedelta(seconds=clock.time() - began)

        monkeypatch.setattr(dt, "datetime", ObservedClock)
    try:
        if mode == "lease":
            with scope(clock, 120):
                result = await bridge._fetch_from_source(
                    {"source": "libgen", "md5": digest}, str(tmp_path)
                )
        else:
            result = await bridge._fetch_from_source(
                {"source": "auto" if keyed else "libgen", "md5": digest}, str(tmp_path)
            )
    finally:
        await router.close()
    assert Path(result).read_bytes() == body
    assert list(tmp_path.iterdir()) == [Path(result)]
    assert len(key_requests) == (1 if keyed else 0)
    if mode in ("healthy", "annas-healthy"):
        assert [r[0] for r in requests] == [first_host, "libgen.vg"]
    elif mode == "large-prefix":
        # At the new 1.5x margin this row no longer abandons the original URL.
        assert [(r[0], r[1]) for r in requests] == [(first_host, 0), (first_host, cut)]
    elif mode == "return-drop":
        assert [(r[0], r[1]) for r in requests] == [
            (first_host, 0),
            (first_host, 0),
            (first_host, cut),
        ]
    elif mode == "bad-transfers":
        assert [(r[0], r[1]) for r in requests] == [
            (first_host, 0),
            ("libgen.vg", 0),
            ("libgen.la", 0),
            (first_host, 0),
        ]
    elif mode in ("first-return-403", "annas-first-return-403"):
        second_host = "libgen.li" if keyed else "libgen.vg"
        assert [(r[0], r[1]) for r in requests] == [
            (first_host, 0),
            (second_host, 0),
            (first_host, 0),
            (second_host, 0),
        ]
        assert requests[0][2] == requests[2][2]
        assert requests[1][2] == requests[3][2]
        assert resolved == ["li", "vg", "la"]
        assert result.provenance["host"] == second_host
        assert result.provenance["mirror"] == second_host
        assert result.provenance["route"] == "mirror_download"
        assert getattr(result, "quota_estimate", None) is None
    elif mode == "two-abandoned":
        assert [(r[0], r[1]) for r in requests] == [
            (first_host, 0),
            ("libgen.vg", 0),
            (first_host, 0),
        ]
    else:
        assert [(r[0], r[1]) for r in requests] == [(first_host, 0), (first_host, 0)]
    if requests[-1][0] == first_host and mode != "large-prefix":
        assert requests[0][2] == requests[-1][2], "return must reuse the resolved URL"
    if mode == "second-pass":
        assert resolved == ["li", "vg", "la", "li", "vg", "la"]
    if mode == "lease":
        assert resolved == ["li"], "under six seconds left cannot admit vg preflight"
        assert clock.time() - began < 105
    if keyed and mode not in ("annas-healthy", "annas-first-return-403"):
        assert result.quota_estimate["remaining"] == 5
        assert result.quota_estimate["as_of"] == "2000-01-01T00:00:00Z"
        assert result.provenance["route"] == "fast_download"
    captured = capsys.readouterr()
    messages = [
        line
        for line in captured.err.splitlines()
        if "recovery feasibility guard" in line
    ]
    assert len(messages) == (
        0
        if mode == "large-prefix"
        else 2
        if mode in ("two-abandoned", "first-return-403", "annas-first-return-403")
        else 1
    )
    assert all("rate_bytes_per_second=" in m and "seconds_left=" in m for m in messages)
    assert all(
        "synthetic-secret" not in m and "token=" not in m and "key=" not in m
        for m in messages
    )
    if mode == "no-key":
        rate, left = re.search(
            r"rate_bytes_per_second=([0-9.]+).*seconds_left=([0-9.]+)", messages[0]
        ).groups()
        assert 3300 < float(rate) < 3400 and 1469 < float(left) < 1470
    assert captured.out == ""


@pytest.mark.asyncio
async def test_f2_margin_keeps_link_within_one_point_five_times_left(
    tmp_path, monkeypatch, clock
):
    body = BODY[: 128 * 1024]
    digest = hashlib.md5(body).hexdigest()
    # After the drop: 1 s remainder + 0.25 s wait, against 0.85 s left.
    # The projection fits 1.5 * 0.85 but not 1.4 * 0.85.
    router, resolved = install_router(
        monkeypatch, SourceConfig(download_timeout=1.85, preflight_enabled=False)
    )
    calls = []

    def handler(request):
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        calls.append((request.url.host, start))
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Drop(clock, body[:65536], 1),
            )
        return httpx.Response(
            206 if start else 200,
            headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
            if start
            else {},
            content=body[start:],
        )

    install_http(monkeypatch, handler)
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": digest}, str(tmp_path)
        )
    finally:
        await router.close()
    assert resolved == ["li"]
    assert calls == [("libgen.li", 0), ("libgen.li", 65536)]
    assert Path(result).read_bytes() == body


@pytest.mark.parametrize(
    "mode", ["returned-failure", "never-guarded", "budget", "cancel"]
)
@pytest.mark.asyncio
async def test_return_is_bounded_and_preserves_errors_and_cancellation(
    tmp_path, monkeypatch, clock, mode
):
    body = BODY[: 1024 * 1024]
    digest = hashlib.md5(body).hexdigest()
    router, resolved = install_router(
        monkeypatch,
        SourceConfig(download_timeout=30, preflight_enabled=False),
        ("li", "vg", "la"),
    )
    calls = []

    async def resolve(_client, mirror, _md5):
        resolved.append(mirror)
        if mirror == "li":
            return "synthetic", "fixture"
        if mode == "budget":
            await clock.advance(5)
        return None, "no key"

    monkeypatch.setattr(router._libgen, "_resolve_key", resolve)

    class Cancelled(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise asyncio.CancelledError()
            yield b""  # Mark this as the real streaming interface.

    def handler(request):
        calls.append((request.url.host, request.headers.get("Range")))
        if mode == "never-guarded":
            return httpx.Response(400)
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Drop(clock, body[:100000], 5),
            )
        if mode == "returned-failure":
            return httpx.Response(400)
        return httpx.Response(
            200, stream=Cancelled() if mode == "cancel" else Completed(clock, body, 16)
        )

    install_http(monkeypatch, handler)
    try:
        with pytest.raises(
            asyncio.CancelledError if mode == "cancel" else AllSourcesFailedError
        ) as error:
            await bridge._fetch_from_source(
                {"source": "libgen", "md5": digest}, str(tmp_path)
            )
    finally:
        await router.close()
    assert resolved == ["li", "vg", "la"]
    assert calls == [("libgen.li", None)] * (1 if mode == "never-guarded" else 2)
    assert not list(tmp_path.iterdir())
    if mode in ("never-guarded", "returned-failure"):
        assert [f.host for f in error.value.failures] == [
            "libgen.li",
            "libgen.vg",
            "libgen.la",
        ] + (["libgen.li"] if mode == "returned-failure" else [])
        assert error.value.failures[-1].reason in ("http_error", "protocol_error")


@pytest.mark.asyncio
async def test_return_handles_direct_stream_error_and_closes_original(
    tmp_path, monkeypatch, clock
):
    from lib.sources.models import DownloadResult, SourceType

    body = BODY[: 1024 * 1024]
    digest = hashlib.md5(body).hexdigest()
    router, _ = install_router(
        monkeypatch, SourceConfig(download_timeout=30, preflight_enabled=False)
    )
    closed, calls = [], []

    class Candidates:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not calls:
                return DownloadResult(
                    url="https://libgen.li/file",
                    source=SourceType.LIBGEN,
                    has_more_candidates=True,
                )
            raise ProviderResponseError(
                "libgen",
                "fallback.example",
                "synthetic resolution failure",
                reason="protocol_error",
            )

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(
        router, "iter_download_candidates", lambda *_args, **_kwargs: Candidates()
    )

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Drop(clock, body[:100000], 5),
            )
        assert closed == [True], "close exhausted source before returning to cached URL"
        return httpx.Response(200, content=body)

    install_http(monkeypatch, handler)
    try:
        result = await bridge._fetch_from_source(
            {"source": "libgen", "md5": digest}, str(tmp_path)
        )
    finally:
        await router.close()
    assert calls == ["https://libgen.li/file"] * 2
    assert closed == [True]
    assert Path(result).read_bytes() == body


@pytest.mark.asyncio
async def test_quiet_guard_does_not_cache_a_later_http_failure(
    tmp_path, monkeypatch, clock, capsys
):
    # 512 KiB in one second leaves a 1.25 s estimate against 29 s remaining.
    # The guard permits a resume, which then gets 403; this is not abandonment.
    # Calling the observer on that quiet check would wrongly request li again.
    body = BODY[: 1024 * 1024]
    router, resolved = install_router(
        monkeypatch,
        SourceConfig(download_timeout=30, preflight_enabled=False),
        ("li", "vg", "la"),
    )
    calls = []

    async def resolve(_client, mirror, _md5):
        resolved.append(mirror)
        return ("synthetic", "fixture") if mirror == "li" else (None, "no key")

    monkeypatch.setattr(router._libgen, "_resolve_key", resolve)

    def handler(request):
        start = int(request.headers.get("Range", "bytes=0-")[6:-1])
        calls.append((request.url.host, start))
        if len(calls) == 1:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(body))},
                stream=Drop(clock, body[: 512 * 1024], 1),
            )
        return httpx.Response(403)

    install_http(monkeypatch, handler)
    try:
        with pytest.raises(AllSourcesFailedError) as error:
            await bridge._fetch_from_source(
                {"source": "libgen", "md5": hashlib.md5(body).hexdigest()},
                str(tmp_path),
            )
    finally:
        await router.close()
    assert calls == [("libgen.li", 0), ("libgen.li", 512 * 1024)]
    assert resolved == ["li", "vg", "la"]
    assert [f.host for f in error.value.failures] == [
        "libgen.li",
        "libgen.vg",
        "libgen.la",
    ]
    assert error.value.failures[0].reason == "http_error"
    assert not list(tmp_path.iterdir())
    captured = capsys.readouterr()
    assert "recovery feasibility guard" not in captured.err
    assert captured.out == ""
