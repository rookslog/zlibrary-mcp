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


# Review of 6ad5cfb, F3 and F5: detection is by the challenge's structure, and
# also covers HTML-looking bodies with a missing or generic content type.
RESULT_PAGE = (
    "<html><head><title>checking your browser - Anna's Archive</title></head><body>"
    '<a href="/md5/abc123">Checking Your Browser: A Guide</a></body></html>'
)


def respond(status, headers, body):
    return adapter_with(lambda r: httpx.Response(status, headers=headers, text=body))


@pytest.mark.asyncio
async def test_result_title_is_not_a_browser_check():
    # The review's red assertion, ported (F3), plus a page title that echoes it.
    for body in (
        '<html><a href="/md5/abc123">Checking Your Browser: A Guide</a></html>',
        RESULT_PAGE,
    ):
        adapter = respond(200, {"content-type": "text/html"}, body)
        assert [r.md5 for r in await adapter.search("checking your browser")] == [
            "abc123"
        ]
        await adapter.close()


@pytest.mark.asyncio
async def test_empty_anna_result_page_quoting_the_phrase_is_not_a_challenge():
    body = "<html><title>checking your browser - Anna's Archive</title><h1>Checking your browser</h1></html>"
    adapter = respond(200, {"content-type": "text/html"}, body)
    assert await adapter.search("checking your browser") == []
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"content-type": "text/plain"}, {"content-type": "application/octet-stream"}],
)
async def test_challenge_without_content_type_is_not_empty_success(headers):
    # The review's red assertion, ported (F5), for missing and generic types.
    adapter = respond(200, headers, CHALLENGE)
    with pytest.raises(ProviderResponseError, match="browser check") as caught:
        await adapter.search("grid systems")
    assert caught.value.reason == "browser_check"
    await adapter.close()


@pytest.mark.asyncio
async def test_challenge_heading_without_title_is_detected():
    body = "<!DOCTYPE html><html><body><h1>Checking your browser before accessing</h1></body></html>"
    adapter = respond(200, {}, body)
    with pytest.raises(ProviderResponseError) as caught:
        await adapter.search("grid systems")
    assert caught.value.reason == "browser_check"
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"content-type": "text/plain"}])
async def test_real_results_without_content_type_parse(headers):
    adapter = respond(200, headers, RESULT_PAGE)
    assert [r.md5 for r in await adapter.search("grid systems")] == ["abc123"]
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"content-type": "application/json"}, {}])
async def test_json_download_response_is_not_inspected_as_a_challenge(headers):
    body = '{"download_url": "https://example.invalid/file.pdf", "message": "checking your browser"}'
    adapter = respond(200, headers, body)
    result = await adapter.get_download_url("0" * 32)
    assert result.url == "https://example.invalid/file.pdf"
    await adapter.close()
