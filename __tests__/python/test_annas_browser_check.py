"""Anna's Archive behind a DDoS-Guard browser check: the error names the cause.

Shape measured 2026-10-23 on annas-archive.gl, .pk and .gd: /search answers
302 from `server: ddos-guard` to `?check=1`, then 403 "Checking your browser".
The connector reports it; it does not try to pass the challenge.
"""

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import ProviderResponseError

CHALLENGE = "<html><title>DDoS-Guard</title><body>Checking your browser before accessing</body></html>"


def adapter_with(handler):
    adapter = AnnasArchiveAdapter(
        SourceConfig(
            annas_secret_key="test-secret-key",
            annas_base_url="https://annas-archive.gl",
        )
    )
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    return adapter


def guarded(request):
    if "check" not in request.url.params:
        return httpx.Response(
            302,
            headers={
                "server": "ddos-guard",
                "location": str(request.url.copy_add_param("check", "1")),
            },
        )
    return httpx.Response(
        403,
        headers={"server": "ddos-guard", "content-type": "text/html"},
        text=CHALLENGE,
    )


@pytest.mark.asyncio
async def test_search_challenge_reports_browser_check_with_status():
    adapter = adapter_with(guarded)
    with pytest.raises(ProviderResponseError) as caught:
        await adapter.search("grid systems")
    error = caught.value
    assert error.reason == "browser_check"
    assert error.status_code == 403
    assert error.to_dict()["status_code"] == 403
    assert "browser check" in str(error)
    await adapter.close()


@pytest.mark.asyncio
async def test_download_challenge_403_is_not_reported_as_a_rejected_key():
    adapter = adapter_with(guarded)
    with pytest.raises(ProviderResponseError) as caught:
        await adapter.get_download_url("0" * 32)
    assert caught.value.reason == "browser_check"
    assert "test-secret-key" not in repr(caught.value.to_dict())
    await adapter.close()


@pytest.mark.asyncio
async def test_challenge_page_with_200_is_not_an_empty_result():
    adapter = adapter_with(
        lambda r: httpx.Response(
            200, headers={"content-type": "text/html"}, text=CHALLENGE
        )
    )
    with pytest.raises(ProviderResponseError) as caught:
        await adapter.search("grid systems")
    assert caught.value.reason == "browser_check"
    assert caught.value.status_code == 200
    await adapter.close()


@pytest.mark.asyncio
async def test_plain_403_and_guarded_success_keep_their_meaning():
    adapter = adapter_with(lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(ProviderResponseError) as caught:
        await adapter.search("grid systems")
    assert caught.value.reason == "http_error"
    await adapter.close()
    page = '<a href="/md5/abc123">A title</a>'
    adapter = adapter_with(
        lambda r: httpx.Response(
            200,
            headers={"server": "ddos-guard", "content-type": "text/html"},
            text=page,
        )
    )
    assert [r.md5 for r in await adapter.search("grid systems")] == ["abc123"]
    await adapter.close()
