"""Resident-worker fixture: every provider request stays inside MockTransport."""

import asyncio
import socket
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from lib.annas_member_worker import serve
from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import SourceConfig
from lib.sources.errors import SourceError

posts = 0
dns_calls = 0
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"


async def site(request):
    global posts
    if request.method == "POST":
        posts += 1
        if mode in ("reject-slow", "slow-signin", "reject-large"):
            print("SIGNIN_POST", file=sys.stderr, flush=True)
        if mode == "slow-signin":
            await asyncio.sleep(10)
        if mode == "refresh-slow" and posts > 1:
            print("SIGNIN_STARTED", file=sys.stderr, flush=True)
            await asyncio.sleep(10)
        await asyncio.sleep(0.01)
        if mode in ("reject", "reject-slow", "reject-large"):
            return httpx.Response(200, text="<form>Sign in</form>")
        return httpx.Response(
            302,
            headers={
                "location": "/",
                "set-cookie": "aa_account_id2=worker-cookie-canary; Path=/",
            },
        )
    query = request.url.params.get("q")
    if mode == "reject-slow" and query == "wait":
        print("GET_STARTED", file=sys.stderr, flush=True)
    if mode == "reject-large" and query == "oversize":
        title = "x" * 90000
        return httpx.Response(
            200,
            text="".join(f'<a href="/md5/{i:032x}">{title}</a>' for i in range(50)),
        )
    if query and query.startswith("http-status-"):
        return httpx.Response(
            int(query.rsplit("-", 1)[1]),
            text="worker-key-canary worker-cookie-canary",
        )
    if query == "generic-canary":
        raise RuntimeError("worker-key-canary worker-cookie-canary")
    if query == "dns-hang":
        try:
            await asyncio.get_running_loop().getaddrinfo("synthetic.invalid", 443)
        except socket.gaierror as error:
            # Match the real httpx transport's DNS exception boundary.
            raise httpx.ConnectError(str(error), request=request) from error
    if query == "wait":
        await asyncio.sleep(10)
    if query == "check" or (query == "refresh" and posts == 1):
        return httpx.Response(200, text="<title>DDoS-Guard</title>")
    return httpx.Response(
        200,
        text=f'<a href="/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">Sign-ins {posts}'
        + (f" DNS {dns_calls}" if mode in ("preflight", "preflight-tcp") else "")
        + (f" Query {query}" if mode == "unicode" else "")
        + "</a>",
    )


adapter = AnnasArchiveAdapter(
    SourceConfig(annas_secret_key="worker-key-canary", preflight_enabled=False)
)
adapter._client = httpx.AsyncClient(
    transport=httpx.MockTransport(site), follow_redirects=True
)


async def main():
    original = socket.getaddrinfo
    server = None
    if mode in ("preflight", "preflight-tcp"):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        if mode == "preflight":
            listener.listen()
            server = await asyncio.start_server(lambda r, w: w.close(), sock=listener)
        adapter.config.preflight_enabled = True
        adapter.port = listener.getsockname()[1]

        def resolve(host, port, *args, **kwargs):
            global dns_calls
            dns_calls += 1
            if mode == "preflight" and dns_calls == 1:
                raise socket.gaierror(socket.EAI_AGAIN, "synthetic transient DNS")
            return original("127.0.0.1", port, *args, **kwargs)

        socket.getaddrinfo = resolve
        if mode == "preflight-tcp":
            preflight = adapter._preflight

            async def recover(*args, **kwargs):
                nonlocal server
                try:
                    await preflight(*args, **kwargs)
                except SourceError:
                    if server is None:
                        listener.listen()
                        server = await asyncio.start_server(
                            lambda r, w: w.close(), sock=listener
                        )
                    raise

            adapter._preflight = recover
    if mode == "dns-hang":
        adapter.config.connect_timeout = 0.05
        adapter.config.total_timeout = 0.4

        def resolve(*args, **kwargs):
            time.sleep(2)
            raise socket.gaierror(socket.EAI_AGAIN, "synthetic hung DNS")

        socket.getaddrinfo = resolve
    if mode == "generic":
        original_search = adapter.search

        async def search(query, **kwargs):
            if query == "generic-canary":
                raise RuntimeError("worker-key-canary worker-cookie-canary")
            return await original_search(query, **kwargs)

        adapter.search = search
    try:
        await serve(adapter)
    finally:
        if server:
            server.close()
            await server.wait_closed()


asyncio.run(main())
