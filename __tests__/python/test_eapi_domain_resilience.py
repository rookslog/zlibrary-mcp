# Tests for ISSUE-API-002: resilient EAPI domain fallback and probing.
#
# The default domain z-library.sk is fronted by the DiamWall anti-bot wall
# (HTTP 307 self-redirect setting a __diamwall cookie, then 513/517 Access
# Denied), and /eapi/info/domains still advertises it first. These tests pin
# the defensive behaviour: probe-before-use fallback at login, env override
# with no silent switching, probe-guarded hydra discovery, and explicit
# DiamWall diagnosis in the health check. All HTTP is mocked via
# httpx.MockTransport — no real requests, and never a login.

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
)

import python_bridge
from python_bridge import _classify_health_error, eapi_health_check

from zlibrary.eapi import (
    DEFAULT_EAPI_DOMAINS,
    DiamWallError,
    EAPIClient,
    decode_eapi_json,
    probe_eapi_domain,
    resolve_eapi_domain,
    select_advertised_domain,
)

pytestmark = pytest.mark.unit


# --- Canned responses -------------------------------------------------------

DIAMWALL_HTML = (
    "<html><head><script src='https://cdn.diamwall.com/protect.js'></script>"
    "</head><body><h1>Access Denied</h1></body></html>"
)


def healthy_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"domains": ["z-library.ec", "z-library.sk"]})


def diamwall_redirect(request: httpx.Request) -> httpx.Response:
    """The wall's first move: a 307 self-redirect setting the __diamwall cookie."""
    return httpx.Response(
        307,
        headers={
            "location": str(request.url),
            "set-cookie": "__diamwall=abc123; path=/",
        },
    )


def diamwall_denied(request: httpx.Request) -> httpx.Response:
    """The wall's second move: 517 Access Denied with DiamWall-branded HTML."""
    return httpx.Response(517, text=DIAMWALL_HTML)


def transport_by_host(routes: dict) -> httpx.MockTransport:
    """Route requests by hostname; unrouted hosts raise a connect error."""

    def handler(request: httpx.Request) -> httpx.Response:
        fn = routes.get(request.url.host)
        if fn is None:
            raise httpx.ConnectError("no route to host", request=request)
        return fn(request)

    return httpx.MockTransport(handler)


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records which hosts were probed, in order."""

    def __init__(self, routes: dict):
        self.hosts = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.hosts.append(request.url.host)
            fn = routes.get(request.url.host)
            if fn is None:
                raise httpx.ConnectError("no route to host", request=request)
            return fn(request)

        super().__init__(handler)


@pytest.fixture
def no_domain_override(monkeypatch):
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN", raising=False)


# --- Probe discrimination ---------------------------------------------------


class TestProbeEAPIDomain:
    async def test_healthy_domain_passes(self):
        transport = transport_by_host({"z-library.ec": healthy_response})
        assert await probe_eapi_domain("z-library.ec", transport=transport) is True

    async def test_diamwall_307_redirect_fails(self):
        """Redirects are not followed: the 307 self-redirect IS the walled signal."""
        transport = transport_by_host({"z-library.sk": diamwall_redirect})
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_diamwall_denied_page_fails(self):
        transport = transport_by_host({"z-library.sk": diamwall_denied})
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_html_where_json_expected_fails(self):
        transport = transport_by_host(
            {
                "z-library.sk": lambda r: httpx.Response(
                    200, text="<html>maintenance</html>"
                )
            }
        )
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_diamwall_marker_in_200_body_fails(self):
        transport = transport_by_host(
            {"z-library.sk": lambda r: httpx.Response(200, text=DIAMWALL_HTML)}
        )
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_network_error_fails(self):
        transport = transport_by_host({})  # every host unreachable
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_json_without_domains_payload_fails(self):
        transport = transport_by_host(
            {"z-library.sk": lambda r: httpx.Response(200, json={"success": 0})}
        )
        assert await probe_eapi_domain("z-library.sk", transport=transport) is False

    async def test_probe_targets_info_domains_not_login(self):
        """Probing must never touch /eapi/user/login (rate-limited ~10/hour/IP)."""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return healthy_response(request)

        await probe_eapi_domain("z-library.ec", transport=httpx.MockTransport(handler))
        assert seen == ["/eapi/info/domains"]


# --- Fallback ordering at login --------------------------------------------


class TestResolveEAPIDomain:
    async def test_first_healthy_candidate_wins(self, no_domain_override):
        transport = RecordingTransport({"z-library.ec": healthy_response})
        assert await resolve_eapi_domain(transport=transport) == "z-library.ec"
        # Later candidates must not even be probed.
        assert transport.hosts == ["z-library.ec"]

    async def test_walled_first_candidate_falls_through_to_second(
        self, no_domain_override
    ):
        transport = RecordingTransport(
            {
                "z-library.ec": diamwall_denied,
                "z-library.sk": healthy_response,
            }
        )
        assert await resolve_eapi_domain(transport=transport) == "z-library.sk"
        assert transport.hosts == ["z-library.ec", "z-library.sk"]

    async def test_all_candidates_walled_falls_back_to_first(self, no_domain_override):
        transport = RecordingTransport(
            {
                "z-library.ec": diamwall_denied,
                "z-library.sk": diamwall_denied,
                "1lib.sk": diamwall_redirect,
            }
        )
        # Nothing passed the probe: return the first candidate so login can
        # surface the real error rather than failing with no domain at all.
        assert await resolve_eapi_domain(transport=transport) == DEFAULT_EAPI_DOMAINS[0]
        assert transport.hosts == DEFAULT_EAPI_DOMAINS

    async def test_candidate_order_matches_issue_api_002(self):
        assert DEFAULT_EAPI_DOMAINS == ["z-library.ec", "z-library.sk", "1lib.sk"]

    async def test_env_override_bypasses_probing(self, monkeypatch):
        """An explicit ZLIBRARY_EAPI_DOMAIN means no probing and no fallback."""
        monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "my-pinned.example")
        transport = RecordingTransport({})  # any request would raise/record
        assert await resolve_eapi_domain(transport=transport) == "my-pinned.example"
        assert transport.hosts == []

    async def test_env_override_wins_even_if_it_would_fail_probe(self, monkeypatch):
        """The override is honoured verbatim — even for a domain the probe
        would reject. Explicit configuration is never second-guessed."""
        monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "z-library.sk")
        transport = RecordingTransport({"z-library.sk": diamwall_denied})
        assert await resolve_eapi_domain(transport=transport) == "z-library.sk"
        assert transport.hosts == []


# --- Probe-guarded hydra discovery ------------------------------------------


class TestSelectAdvertisedDomain:
    async def test_skips_walled_advertised_domain(self):
        """/eapi/info/domains advertises the walled z-library.sk first; the
        discovery step must skip it instead of switching onto it."""
        transport = RecordingTransport(
            {
                "z-library.sk": diamwall_denied,
                "z-library.mx": healthy_response,
            }
        )
        picked = await select_advertised_domain(
            ["z-library.sk", "z-library.mx"], "z-library.ec", transport=transport
        )
        assert picked == "z-library.mx"
        assert transport.hosts == ["z-library.sk", "z-library.mx"]

    async def test_all_advertised_walled_keeps_current(self):
        transport = transport_by_host(
            {
                "z-library.sk": diamwall_denied,
                "1lib.sk": diamwall_redirect,
            }
        )
        picked = await select_advertised_domain(
            ["z-library.sk", "1lib.sk"], "z-library.ec", transport=transport
        )
        assert picked is None  # caller keeps its current working domain

    async def test_current_domain_in_list_needs_no_probe(self):
        transport = RecordingTransport({})
        picked = await select_advertised_domain(
            ["z-library.ec", "z-library.sk"], "z-library.ec", transport=transport
        )
        assert picked == "z-library.ec"
        assert transport.hosts == []

    async def test_dict_entries_are_understood(self):
        transport = transport_by_host({"z-library.mx": healthy_response})
        picked = await select_advertised_domain(
            [{"domain": "z-library.mx"}], "z-library.ec", transport=transport
        )
        assert picked == "z-library.mx"

    async def test_empty_listing_keeps_current(self):
        picked = await select_advertised_domain(
            [], "z-library.ec", transport=transport_by_host({})
        )
        assert picked is None


# --- initialize_eapi_client wiring ------------------------------------------


@pytest.fixture
def eapi_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZLIBRARY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ZLIBRARY_EMAIL", "test@example.com")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "secret")


@pytest.fixture
def reset_bridge_client(mocker):
    mocker.patch.object(python_bridge, "_eapi_client", None)


def make_client_mock():
    client = MagicMock()
    client.login = AsyncMock(
        return_value={"success": 1, "user": {"id": "1", "remix_userkey": "k"}}
    )
    client.get_domains = AsyncMock(
        return_value={"domains": ["z-library.sk", "z-library.mx"]}
    )
    client.close = AsyncMock()
    client.domain = "z-library.ec"
    client.remix_userid = "1"
    client.remix_userkey = "k"
    return client


class TestInitializeEAPIClient:
    async def test_env_override_uses_only_that_domain_and_never_switches(
        self, monkeypatch, eapi_env, reset_bridge_client, mocker
    ):
        monkeypatch.setenv("ZLIBRARY_EAPI_DOMAIN", "my-pinned.example")
        client = make_client_mock()
        client_cls = mocker.patch.object(
            python_bridge, "EAPIClient", return_value=client
        )

        result = await python_bridge.initialize_eapi_client()

        client_cls.assert_called_once_with("my-pinned.example")
        # Pinned domain: hydra discovery must not even be consulted.
        client.get_domains.assert_not_awaited()
        assert result is client

    async def test_walled_advertised_primary_is_not_adopted(
        self, no_domain_override, eapi_env, reset_bridge_client, mocker
    ):
        """Discovery advertises the walled domain first; the client must keep
        the working domain it logged in on."""
        mocker.patch.object(
            python_bridge, "resolve_eapi_domain", AsyncMock(return_value="z-library.ec")
        )
        # Every advertised domain fails the probe.
        mocker.patch.object(
            python_bridge, "select_advertised_domain", AsyncMock(return_value=None)
        )
        client = make_client_mock()
        client_cls = mocker.patch.object(
            python_bridge, "EAPIClient", return_value=client
        )

        result = await python_bridge.initialize_eapi_client()

        client_cls.assert_called_once_with("z-library.ec")
        client.close.assert_not_awaited()  # no switch happened
        assert result is client

    async def test_healthy_advertised_domain_is_adopted(
        self, no_domain_override, eapi_env, reset_bridge_client, mocker
    ):
        mocker.patch.object(
            python_bridge, "resolve_eapi_domain", AsyncMock(return_value="z-library.ec")
        )
        mocker.patch.object(
            python_bridge,
            "select_advertised_domain",
            AsyncMock(return_value="z-library.mx"),
        )
        first, second = make_client_mock(), make_client_mock()
        client_cls = mocker.patch.object(
            python_bridge, "EAPIClient", side_effect=[first, second]
        )

        result = await python_bridge.initialize_eapi_client()

        assert client_cls.call_count == 2
        assert client_cls.call_args_list[1].args == ("z-library.mx",)
        first.close.assert_awaited_once()
        assert result is second


# --- DiamWall diagnosis ------------------------------------------------------


class TestDiamWallDiagnosis:
    def test_classifier_names_diamwall(self):
        err = DiamWallError(
            "DiamWall anti-bot wall detected on z-library.sk (HTTP 517)"
        )
        assert _classify_health_error(err) == "diamwall_blocked"

    def test_classifier_still_detects_cloudflare(self):
        assert (
            _classify_health_error(Exception("Checking your browser before accessing"))
            == "cloudflare_blocked"
        )

    async def test_health_check_reports_diamwall_with_remedy(self, mocker):
        """The health check must name the wall and suggest the env override."""
        client = MagicMock()
        client.search = AsyncMock(
            side_effect=DiamWallError(
                "DiamWall anti-bot wall detected on z-library.sk (HTTP 517): "
                "the domain blocks programmatic /eapi access. Set "
                "ZLIBRARY_EAPI_DOMAIN to a working domain "
                "(e.g. export ZLIBRARY_EAPI_DOMAIN=z-library.ec)."
            )
        )
        mocker.patch.object(python_bridge, "_eapi_client", client)

        result = await eapi_health_check()

        assert result["status"] == "unhealthy"
        assert result["error_code"] == "diamwall_blocked"
        assert "DiamWall" in result["error"]
        assert "ZLIBRARY_EAPI_DOMAIN" in result["error"]

    async def test_eapi_client_raises_diamwall_error_on_denied_page(self):
        client = EAPIClient("z-library.sk")
        client._client = httpx.AsyncClient(
            transport=transport_by_host({"z-library.sk": diamwall_denied})
        )
        with pytest.raises(DiamWallError) as excinfo:
            await client.get_domains()
        msg = str(excinfo.value)
        assert "DiamWall" in msg
        assert "z-library.sk" in msg
        assert "ZLIBRARY_EAPI_DOMAIN" in msg
        await client.close()

    async def test_eapi_client_raises_diamwall_error_on_html_200(self):
        client = EAPIClient("z-library.sk")
        client._client = httpx.AsyncClient(
            transport=transport_by_host(
                {"z-library.sk": lambda r: httpx.Response(200, text=DIAMWALL_HTML)}
            )
        )
        with pytest.raises(DiamWallError):
            await client.get_domains()
        await client.close()

    def test_decode_passes_healthy_json_through(self):
        resp = httpx.Response(
            200,
            json={"domains": ["z-library.ec"]},
            request=httpx.Request("GET", "https://z-library.ec/eapi/info/domains"),
        )
        assert decode_eapi_json(resp, "z-library.ec") == {"domains": ["z-library.ec"]}

    def test_decode_preserves_plain_http_errors(self):
        """Non-walled failures keep their httpx semantics (no false DiamWall)."""
        resp = httpx.Response(
            404,
            text="not found",
            request=httpx.Request("GET", "https://z-library.ec/eapi/info/domains"),
        )
        with pytest.raises(httpx.HTTPStatusError):
            decode_eapi_json(resp, "z-library.ec")

    def test_decode_flags_non_json_without_wall_marker(self):
        resp = httpx.Response(
            200,
            text="<html>maintenance</html>",
            request=httpx.Request("GET", "https://z-library.ec/eapi/info/domains"),
        )
        with pytest.raises(RuntimeError, match="non-JSON"):
            decode_eapi_json(resp, "z-library.ec")
