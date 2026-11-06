"""Resident-worker fixture: every provider request stays inside MockTransport."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from lib.annas_member_worker import serve
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig

posts = 0
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"


async def site(request):
    global posts
    if request.method == "POST":
        posts += 1
        await asyncio.sleep(0.01)
        if mode == "reject":
            return httpx.Response(200, text="<form>Sign in</form>")
        return httpx.Response(
            302,
            headers={
                "location": "/",
                "set-cookie": "aa_account_id2=worker-cookie-canary; Path=/",
            },
        )
    query = request.url.params.get("q")
    if query == "wait":
        await asyncio.sleep(10)
    if query == "check":
        return httpx.Response(200, text="<title>DDoS-Guard</title>")
    return httpx.Response(
        200,
        text=f'<a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Sign-ins {posts}</a>',
    )


adapter = AnnasArchiveAdapter(
    SourceConfig(annas_secret_key="worker-key-canary", preflight_enabled=False)
)
adapter._client = httpx.AsyncClient(
    transport=httpx.MockTransport(site), follow_redirects=True
)
asyncio.run(serve(adapter))
