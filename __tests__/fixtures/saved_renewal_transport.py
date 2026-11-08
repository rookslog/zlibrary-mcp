"""Exercise an old saved-key rejection followed by a new-key transport failure."""

import asyncio
import json
from unittest.mock import patch

import httpx
from lib import login_store as store
from lib.auth_diagnostics import failure_diagnostics
from lib.login_runtime import SavedLoginClient


async def renewal_failure():
    def save(key):
        return store.save(
            identity="reader@example.test",
            user_id="42",
            user_key=key,
            domain="origin.example.test",
        )

    first = save("synthetic-old-key")
    requests = []

    def handle(request):
        requests.append(request.headers["cookie"])
        if "synthetic-old-key" in requests[-1]:
            save("synthetic-new-key")
            return httpx.Response(401, json={"error": "Please login"})
        raise httpx.ConnectError("Synthetic connection failed", request=request)

    original_init = httpx.AsyncClient.__init__

    def offline_client(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        original_init(self, *args, **kwargs)

    client = SavedLoginClient(first)
    try:
        with patch.object(httpx.AsyncClient, "__init__", offline_client):
            try:
                await client.get_profile()
            except httpx.ConnectError as error:
                assert len(requests) == 2
                assert "synthetic-new-key" in requests[-1]
                return error
        raise AssertionError("Expected a real new-key transport failure")
    finally:
        await client.close()


if __name__ == "__main__":
    error = asyncio.run(renewal_failure())
    print(
        json.dumps(
            {"type": type(error).__name__, "diagnostics": failure_diagnostics(error)}
        )
    )
