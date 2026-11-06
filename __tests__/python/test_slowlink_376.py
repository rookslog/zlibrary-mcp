"""Uneven-drop recovery on synthetic HTTP bodies, with no elapsed-time assertions."""

import asyncio
import gzip
import hashlib
from pathlib import Path
import random
import zlib

import httpx
import pytest

from lib import python_bridge as bridge
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError
from test_libgen_transfers_371 import candidates_for, loopback

BODY = b"%PDF-1.7\n" + random.Random(376).randbytes(640 * 1024 - 9)
MD5 = hashlib.md5(BODY).hexdigest()


@pytest.fixture(autouse=True)
def retry_delays(monkeypatch):
    """Record retry pacing without waiting for real time to pass."""
    delays = []
    sleep = asyncio.sleep

    async def step(seconds):
        if seconds:
            delays.append(seconds)
        await sleep(0)

    monkeypatch.setattr(asyncio, "sleep", step)
    return delays


def partial(start, count):
    return (
        206 if start else 200,
        {
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


def offset_for(headers):
    return int(headers.get("range", "bytes=0-")[6:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(6))
async def test_40kbps_uniform_1_to_5_second_drops_finish(tmp_path, seed, retry_delays):
    # Each connection's byte quota is exactly 40,000 B/s * seeded U(1,5) s.
    # Network scheduling never controls the drop or the expected offset.
    rng = random.Random(seed)
    sent = 0
    offsets = []

    def handler(_path, headers):
        nonlocal sent
        start = offset_for(headers)
        offsets.append(start)
        assert start == sent, "a dropped tail was discarded before Range resume"
        if start:
            assert next(tmp_path.glob("*.part")).read_bytes() == BODY[:start]
        count = min(int(40000 * rng.uniform(1, 5)), len(BODY) - start)
        sent += count
        assert len(offsets) < 30, "unbounded recovery"
        return partial(start, count)

    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert sent == 655360
    assert Path(result).read_bytes() == BODY
    assert retry_delays == [0.25] * (len(offsets) - 1)
    assert list(tmp_path.iterdir()) == [Path(result)]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["libgen", "annas", "zlibrary"])
async def test_first_and_later_sub_8kib_tails_survive(tmp_path, provider):
    offsets = []
    counts = iter([5003, 9001, len(BODY)])

    def handler(_path, headers):
        start = offset_for(headers)
        offsets.append(start)
        if start:
            assert next(tmp_path.glob("*.part")).read_bytes() == BODY[:start]
        return partial(start, next(counts))

    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(url, str(tmp_path), MD5, provider)
    assert offsets == [0, 5003, 14004]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_credit_accumulates_across_short_attempts(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = offset_for(headers)
        offsets.append(start)
        return partial(start, 40000 if len(offsets) <= 4 else len(BODY))

    async with loopback(handler) as url:
        result = await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert offsets == [0, 40000, 80000, 120000, 160000]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("gain, succeeds", [(65535, False), (65536, True)])
async def test_credit_threshold_and_checkpoint_advance(tmp_path, gain, succeeds):
    offsets = []
    # First gain earns credit, then two more attempts total gain bytes.
    # Advancing credited is essential: old credited=0 would refund the zero.
    counts = iter([65536, 0, gain, 0, len(BODY)])

    def handler(_path, headers):
        start = offset_for(headers)
        offsets.append(start)
        return partial(start, next(counts))

    async with loopback(handler) as url:
        operation = bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
        if succeeds:
            assert Path(await operation).read_bytes() == BODY
        else:
            with pytest.raises(ProviderResponseError) as error:
                await operation
            assert error.value.reason == "partial_transfer"
            assert not list(tmp_path.iterdir())
    assert offsets == [0, 65536, 65536] + ([131072, 131072] if succeeds else [])


@pytest.mark.asyncio
async def test_gzip_partial_decoded_tail_is_observed_but_never_ranged(
    tmp_path, monkeypatch
):
    requests, observed = [], []
    compressor = zlib.compressobj(wbits=31)
    prefix = compressor.compress(BODY[:5003]) + compressor.flush(zlib.Z_SYNC_FLUSH)

    class DroppedGzip(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield prefix
            raise httpx.RemoteProtocolError("synthetic gzip drop")

    def handler(request):
        requests.append(request.headers.get("Range"))
        if len(requests) == 1:
            return httpx.Response(
                200, headers={"Content-Encoding": "gzip"}, stream=DroppedGzip()
            )
        assert next(tmp_path.glob("*.part")).read_bytes() == b""
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(BODY)
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(**kw, transport=httpx.MockTransport(handler)),
    )
    result = await bridge._download_url_to_file(
        "https://fixture.invalid/book",
        str(tmp_path),
        MD5,
        "annas",
        progress_observer=lambda received, *_: observed.append(received),
    )
    assert 5003 in observed
    assert requests == [None, None]
    assert Path(result).read_bytes() == BODY


@pytest.mark.asyncio
async def test_corrupt_resumed_tail_still_fails_md5(tmp_path):
    offsets = []

    def handler(_path, headers):
        start = offset_for(headers)
        offsets.append(start)
        status, headers, body, delay = partial(start, 5003 if not start else len(BODY))
        if start:
            body = body[:-1] + bytes([body[-1] ^ 1])
        return status, headers, body, delay

    async with loopback(handler) as url:
        with pytest.raises(ProviderResponseError) as error:
            await bridge._download_url_to_file(url, str(tmp_path), MD5, "libgen")
    assert error.value.reason == "integrity_mismatch"
    assert offsets == [0, 5003]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ignored-range", "gzip-restart", "one-byte-range"])
async def test_bad_mirror_request_and_virtual_wait_bound(
    tmp_path, monkeypatch, retry_delays, mode
):
    bad_offsets, healthy_ranges = [], []

    def bad(_path, headers):
        start = offset_for(headers)
        bad_offsets.append(start)
        # Bound mutants with a terminal response; assert the request count
        # after the real mirror chain has completed.
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
        return partial(0, 65536)

    def good(_path, headers):
        healthy_ranges.append(headers.get("range"))
        return 200, {}, BODY, 0

    async with loopback(bad) as first, loopback(good) as second:
        candidates_for(monkeypatch, SourceConfig(), first, second)
        result = await bridge._fetch_from_source(
            {"md5": MD5, "source": "libgen"}, str(tmp_path)
        )
    assert len(bad_offsets) == 3
    assert healthy_ranges == [None]
    assert retry_delays == [0.25, 0.25]
    assert Path(result).read_bytes() == BODY
