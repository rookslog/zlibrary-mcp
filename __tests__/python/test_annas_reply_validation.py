"""Offline contracts for fast-download replies and keyed error normalization."""

import socket
import ssl
import traceback
from unittest.mock import patch

import httpx
import pytest

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
SECRET = "offline-sentinel-137"
MD5 = "a" * 32
BASE = "https://annas-archive.gl"
GOOD_URL = "https://partner.example/book.pdf"
COUNTERS = ("downloads_left", "downloads_per_day", "downloads_done_today")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Reply tests forbid DNS and network connections")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


async def resolve(handler, *, base=BASE, secret=SECRET):
    """Exercise real parsing, status checking and redirects over MockTransport."""
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    adapter = AnnasArchiveAdapter(
        SourceConfig(
            annas_secret_key=secret, annas_base_url=base, preflight_enabled=False
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(record), follow_redirects=True, trust_env=False
    ) as client:
        with patch.object(adapter, "_get_client", return_value=client):
            try:
                result = await adapter.get_download_url(MD5)
            except Exception as error:
                return None, error, traceback.format_exc(), seen
    return result, None, "", seen


def assert_protocol_error(error):
    assert isinstance(error, ProviderResponseError)
    assert error.reason == "protocol_error"
    assert error.provider == "annas"
    assert error.host == "annas-archive.gl"


@pytest.mark.parametrize(
    "body", [b"not json", b"\xff", b'{"error":"\xff"}', b'{"error":"\xed\xa0\x80"}']
)
async def test_invalid_reply_bytes_are_protocol_errors(body):
    _, error, _, seen = await resolve(lambda request: httpx.Response(200, content=body))
    assert len(seen) == 1
    assert_protocol_error(error)


async def test_utf16_bom_json_is_a_protocol_error():
    body = ('{"download_url":"' + GOOD_URL + '"}').encode("utf-16")
    _, error, _, _ = await resolve(lambda request: httpx.Response(200, content=body))
    assert_protocol_error(error)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        42,
        [GOOD_URL],
        "/book.pdf",
        "not-a-url",
        "ftp://partner.example/book.pdf",
        "https:///book.pdf",
        " https://partner.example/book.pdf",
        "https://partner.example/book.pdf ",
        "https://partner.example/book\nfile.pdf",
        "https://partner.example/book\x00.pdf",
        "https://partner.example/book\x7f.pdf",
        "https://partner.example/book\x85.pdf",
        "https://partner.example/book.pdf\u00a0",
        "\u3000https://partner.example/book.pdf",
        "https://[::1/book.pdf",
        "https://partner.example:invalid/book.pdf",
        "https://partner.example:65536/b",
        "https://partner.example:-1/b",
        "https://user:password@partner.example/b",
        "https://user@partner.example/b",
        "https://@partner.example/b",
        "https://[::1]junk/book.pdf",
        "https://[::1]junk:443/book.pdf",
    ],
    ids=[
        "empty",
        "spaces",
        "number",
        "array",
        "relative",
        "unparseable",
        "ftp",
        "missing-host",
        "leading-space",
        "trailing-space",
        "newline",
        "nul",
        "del",
        "c1-control",
        "trailing-nbsp",
        "leading-ideographic-space",
        "ipv6-bracket",
        "text-port",
        "large-port",
        "negative-port",
        "credentials",
        "username",
        "empty-credentials",
        "ipv6-suffix",
        "ipv6-suffix-with-port",
    ],
)
async def test_download_url_requires_absolute_http_url(url):
    _, error, _, _ = await resolve(
        lambda request: httpx.Response(200, json={"download_url": url})
    )
    assert_protocol_error(error)


@pytest.mark.parametrize(
    "url",
    [
        GOOD_URL,
        "http://partner.example:8080/book.pdf",
        "HTTPS://PARTNER.EXAMPLE:443/b",
        "https://partner.example:65535/b",
        "https://[2001:db8::1]:8443/b",
        "https://partner.example/book%20file.pdf?x=a%2Fb&sig=A%2BB#part",
        "https://partner.example/世界.pdf",
        "https://partner.example/book file.pdf",
        "https://partner.example/世界\u3000の.pdf",
        "https://partner.example/book\u00a0file.pdf",
        "https://partner.example/b?address=a@b",
        "https://partner.example",
        "http://127.0.0.1:8000/b",
        "https://[::1]:/b",
    ],
)
async def test_valid_download_url_text_is_preserved(url):
    result, error, _, _ = await resolve(
        lambda request: httpx.Response(200, json={"download_url": url})
    )
    assert error is None
    assert result.url == url


@pytest.mark.parametrize("field", COUNTERS)
@pytest.mark.parametrize("value", ["1", [], -1, True, False, 1.0, {}])
async def test_present_quota_counters_are_nonnegative_integers(field, value):
    _, error, _, _ = await resolve(
        lambda request: httpx.Response(
            200,
            json={
                "download_url": GOOD_URL,
                "account_fast_download_info": {field: value},
            },
        )
    )
    assert_protocol_error(error)


@pytest.mark.parametrize("field", COUNTERS)
async def test_null_quota_counters_are_unknown(field):
    info = dict.fromkeys(COUNTERS, 5)
    info[field] = None
    result, error, _, _ = await resolve(
        lambda request: httpx.Response(
            200, json={"download_url": GOOD_URL, "account_fast_download_info": info}
        )
    )
    assert error is None
    if field == "downloads_left":
        assert result.quota_info is None
    else:
        assert vars(result.quota_info) == info


@pytest.mark.parametrize(
    "info", [{}, {"downloads_done_today": 2}, {"downloads_per_day": 25}]
)
async def test_missing_remaining_quota_is_unknown(info):
    result, error, _, _ = await resolve(
        lambda request: httpx.Response(
            200, json={"download_url": GOOD_URL, "account_fast_download_info": info}
        )
    )
    assert error is None
    assert result.quota_info is None


async def test_missing_optional_quota_counters_are_unknown():
    result, error, _, _ = await resolve(
        lambda request: httpx.Response(
            200,
            json={
                "download_url": GOOD_URL,
                "account_fast_download_info": {"downloads_left": 0},
            },
        )
    )
    assert error is None
    assert result.quota_info.downloads_left == 0
    assert result.quota_info.downloads_per_day is None
    assert result.quota_info.downloads_done_today is None


@pytest.mark.parametrize("value", [0, 1, 2**63])
async def test_valid_quota_counters_remain_integers(value):
    result, error, _, _ = await resolve(
        lambda request: httpx.Response(
            200,
            json={
                "download_url": GOOD_URL,
                "account_fast_download_info": dict.fromkeys(COUNTERS, value),
            },
        )
    )
    assert error is None
    assert vars(result.quota_info) == dict.fromkeys(COUNTERS, value)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    "mutation",
    [
        "drop-query",
        "wrong-key",
        "wrong-md5",
        "drop-md5",
        "scheme",
        "port",
        "host",
        "path",
        "userinfo",
        "duplicate-key",
        "duplicate-md5",
    ],
)
async def test_auth_status_requires_complete_final_request_identity(status, mutation):
    def handler(request):
        if "changed" in request.url.params:
            return httpx.Response(status)
        url = request.url.copy_add_param("changed", "1")
        if mutation == "drop-query":
            url = url.copy_with(query=b"changed=1")
        elif mutation == "wrong-key":
            url = url.copy_set_param("key", "other-synthetic-key")
        elif mutation == "wrong-md5":
            url = url.copy_set_param("md5", "b" * 32)
        elif mutation == "drop-md5":
            url = url.copy_remove_param("md5")
        elif mutation == "scheme":
            url = url.copy_with(scheme="http")
        elif mutation == "port":
            url = url.copy_with(port=444)
        elif mutation == "host":
            url = url.copy_with(host="edge.example")
        elif mutation == "path":
            url = url.copy_with(path="/other")
        elif mutation == "userinfo":
            url = url.copy_with(username="synthetic", password="changed")
        else:
            field = mutation.removeprefix("duplicate-")
            url = url.copy_add_param(field, "different")
        return httpx.Response(302, headers={"location": str(url)})

    _, error, _, seen = await resolve(handler)
    assert len(seen) == 2
    assert isinstance(error, ProviderResponseError)
    assert error.reason == "http_error"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.parametrize(
    "base", [BASE, BASE + ":444/prefix", "https://synthetic:pass@annas-archive.gl"]
)
async def test_matching_request_auth_status_rejects_key(status, redirect, base):
    def handler(request):
        if redirect and "changed" not in request.url.params:
            return httpx.Response(
                302,
                headers={"location": str(request.url.copy_add_param("changed", "1"))},
            )
        return httpx.Response(status)

    _, error, _, seen = await resolve(handler, base=base, secret="synthetic /+? key")
    assert len(seen) == (2 if redirect else 1)
    assert isinstance(error, ProviderConfigurationError)
    assert error.reason == "configuration_error"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("port", [None, 8443])
async def test_same_host_https_upgrade_rejects_key(status, port):
    def handler(request):
        if request.url.scheme == "http":
            url = request.url.copy_with(scheme="https", port=port)
            return httpx.Response(302, headers={"location": str(url)})
        return httpx.Response(status)

    _, error, _, seen = await resolve(
        handler,
        base="http://synthetic:pass@annas-archive.gl:8080/prefix",
        secret="synthetic /+ key",
    )
    assert len(seen) == 2
    assert isinstance(error, ProviderConfigurationError)
    assert error.reason == "configuration_error"


@pytest.mark.parametrize(
    "echo",
    ["Ab/c+d e", "Ab%2fc%2bd%20e", "Ab%2fc%2bd+e", "KeY=unrelated"],
    ids=["raw", "percent", "plus", "key-marker"],
)
async def test_api_error_echo_redaction_clauses(echo):
    secret = "Ab/c+d e"
    _, error, formatted, seen = await resolve(
        lambda request: httpx.Response(200, json={"error": {"message": echo}}),
        secret=secret,
    )
    assert isinstance(error, ProviderResponseError)
    assert error.reason == "http_error"
    assert error.detail == "Anna's Archive API error: details omitted"
    assert_no_keyed_surfaces(error, formatted, seen[-1], secret)


async def test_benign_api_error_detail_is_preserved():
    _, error, _, _ = await resolve(
        lambda request: httpx.Response(200, json={"error": "synthetic failure"})
    )
    assert error.detail == "Anna's Archive API error: synthetic failure"


def assert_no_keyed_surfaces(error, formatted, request, secret):
    forbidden = (secret, "key=", str(request.url), request.url.query.decode())
    pending = [error]
    seen = set()
    renderings = [formatted]
    while pending:
        value = pending.pop()
        if value is None:
            continue
        renderings.extend((str(value), repr(value)))
        if isinstance(value, (str, bytes, int, float, bool)) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, BaseException):
            pending.extend(
                (value.args, value.__cause__, value.__context__, vars(value))
            )
            if hasattr(value, "to_dict"):
                pending.append(value.to_dict())
            if hasattr(value, "request"):
                pending.append(str(value.request.url))
            if hasattr(value, "response"):
                pending.append(str(value.response.request.url))
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
    assert not any(token in text for token in forbidden for text in renderings)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "kind,expected_class,reason",
    [
        ("read-timeout", ProviderUnreachableError, "read_timeout"),
        ("connect-error", ProviderUnreachableError, "connect_error"),
        ("dns-error", ProviderUnreachableError, "dns_failure"),
        ("tls-error", ProviderUnreachableError, "tls_error"),
        ("protocol-error", ProviderResponseError, "protocol_error"),
        ("unexpected", ProviderUnreachableError, "unknown"),
        ("attributed", ProviderTimeoutError, "search_timeout"),
        ("reject-401-direct", ProviderConfigurationError, "configuration_error"),
        ("reject-403-direct", ProviderConfigurationError, "configuration_error"),
        ("reject-401-redirect", ProviderConfigurationError, "configuration_error"),
        ("reject-403-redirect", ProviderConfigurationError, "configuration_error"),
        ("http-503", ProviderResponseError, "http_error"),
        ("api-error", ProviderResponseError, "http_error"),
    ],
)
async def test_keyed_errors_keep_classification_without_retaining_request_text(
    kind, expected_class, reason
):
    def handler(request):
        message = f"failure while requesting {request.url}"
        if kind == "read-timeout":
            raise httpx.ReadTimeout(message, request=request)
        if kind in ("connect-error", "dns-error", "tls-error"):
            error = httpx.ConnectError(message, request=request)
            if kind == "dns-error":
                raise error from socket.gaierror(message)
            if kind == "tls-error":
                raise error from ssl.SSLError(message)
            raise error
        if kind == "protocol-error":
            raise httpx.RemoteProtocolError(message, request=request)
        if kind == "unexpected":
            raise ValueError(message)
        if kind == "attributed":
            error = ProviderTimeoutError("annas", "annas-archive.gl", message)
            error.request = request
            raise error
        if kind == "api-error":
            return httpx.Response(200, json={"error": message})
        if kind.endswith("redirect") and "changed" not in request.url.params:
            return httpx.Response(
                302,
                headers={"location": str(request.url.copy_add_param("changed", "1"))},
            )
        return httpx.Response(503 if kind == "http-503" else int(kind.split("-")[1]))

    _, error, formatted, seen = await resolve(handler)
    assert type(error) is expected_class
    assert error.reason == reason
    assert error.provider == "annas"
    assert error.host == "annas-archive.gl"
    assert len(seen) == (2 if kind.endswith("redirect") else 1)
    assert_no_keyed_surfaces(error, formatted, seen[-1], SECRET)


async def test_keyed_error_clears_context_even_inside_an_active_handler():
    try:
        raise ValueError("outer " + SECRET)
    except ValueError:
        _, error, formatted, seen = await resolve(lambda request: httpx.Response(503))
    assert_no_keyed_surfaces(error, formatted, seen[-1], SECRET)


@pytest.mark.parametrize("stage", ["preflight", "client"])
async def test_setup_errors_do_not_retain_an_active_keyed_context(stage):
    from lib.sources.net import WalkDeadline

    adapter = AnnasArchiveAdapter(
        SourceConfig(
            annas_secret_key=SECRET, annas_base_url=BASE, preflight_enabled=False
        )
    )
    request = httpx.Request("GET", BASE, params={"key": SECRET})
    with patch.object(adapter, "_get_client", side_effect=ValueError(str(request.url))):
        try:
            raise httpx.ConnectError(str(request.url), request=request)
        except httpx.ConnectError:
            try:
                await adapter.get_download_url(
                    MD5, deadline=WalkDeadline(0) if stage == "preflight" else None
                )
            except Exception as error:
                expected_class = (
                    ProviderTimeoutError
                    if stage == "preflight"
                    else ProviderUnreachableError
                )
                expected_reason = (
                    "walk_budget_exhausted" if stage == "preflight" else "unknown"
                )
                assert type(error) is expected_class
                assert error.reason == expected_reason
                assert_no_keyed_surfaces(error, traceback.format_exc(), request, SECRET)
            else:
                pytest.fail("setup failure must be reported")
