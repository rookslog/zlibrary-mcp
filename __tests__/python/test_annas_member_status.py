"""Member status evidence remains sterile and usable by the Node breaker."""

import json
import logging
import traceback

import httpx
import pytest

from lib.sources.errors import SourceError
from test_annas_member import COOKIE, KEY, FakeSite

pytest_plugins = ["test_annas_member"]


@pytest.mark.parametrize("status", [408, 429, 500, 503, 599, 404])
async def test_member_http_error_keeps_only_numeric_status(
    make_adapter, caplog, status
):
    fake = FakeSite()

    async def site(request):
        if request.url.params.get("q") == "status":
            assert f"aa_account_id2={COOKIE}" in request.headers["cookie"]
            return httpx.Response(
                status,
                text=KEY + COOKIE,
                headers={"x-untrusted": KEY + COOKIE},
            )
        return await fake(request)

    caplog.set_level(logging.DEBUG)
    adapter = make_adapter(site)
    assert (await adapter.search("books"))[0].title == "A book"
    assert adapter.member_status()["member_session"] == "active"
    with pytest.raises(SourceError) as caught:
        await adapter.search("status")
    error = caught.value
    assert error.to_dict()["reason"] == "http_error"
    assert error.to_dict()["detail"] == f"HTTP {status}"
    observed = (
        json.dumps(error.to_dict())
        + "".join(traceback.format_exception(error))
        + caplog.text
    )
    assert KEY not in observed and COOKIE not in observed
    assert error.__context__ is None
