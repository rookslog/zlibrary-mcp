"""Bounded lazy-v1 frame and deferred bridge initialization controls."""

import asyncio
import json

import httpx
import pytest
import python_bridge
from eapi_session_protocol import MAX_FRAME, SessionProtocolError, parse_session_frame

PRIVATE = {
    "identity": "synthetic@example.invalid",
    "generation": 0,
    "action": "operation",
    "domain": "synthetic.invalid",
    "remix_userid": "7",
    "remix_userkey": "private-canary",
}


def frame(value):
    return (json.dumps(value) + "\n").encode()


def test_exact_grant():
    assert (
        parse_session_frame(frame({"v": 1, "type": "session", "input": PRIVATE}))
        == PRIVATE
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"{}",
        b"{}\n",
        b"\xff\n",
        b"x" * (MAX_FRAME + 1),
        frame({"v": 2, "type": "session", "input": PRIVATE}),
        frame({"v": True, "type": "session", "input": PRIVATE}),
        frame({"v": 1, "type": "session", "input": {**PRIVATE, "action": "login"}}),
        frame({"v": 1, "type": "session", "input": {**PRIVATE, "generation": True}}),
        frame(
            {"v": 1, "type": "session", "input": {**PRIVATE, "extra": "private-canary"}}
        ),
        frame({"v": 1, "type": "session", "input": PRIVATE}) * 2,
        frame({"v": 1, "type": "denied", "code": "auth_unavailable"}),
    ],
)
def test_invalid_or_denied_is_fixed_and_redacted(data):
    with pytest.raises(SessionProtocolError) as error:
        parse_session_frame(data)
    assert "private-canary" not in str(error.value)
    assert str(error.value) in {
        "Invalid private session reply",
        "Private session request denied",
    }


@pytest.mark.asyncio
async def test_lazy_grant_recovers_domain_and_publishes_exactly_once(monkeypatch):
    from lib import eapi_session_protocol

    monkeypatch.setenv("ZLIBRARY_EMAIL", PRIVATE["identity"])
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "synthetic-password")
    monkeypatch.delenv("ZLIBRARY_EAPI_DOMAIN", raising=False)
    monkeypatch.setattr(python_bridge, "_lazy_session", True)
    monkeypatch.setattr(python_bridge, "_session_input", None)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    monkeypatch.setattr(python_bridge, "_session_published", False)
    demands, requests, updates, exclusions = [], [], [], []

    async def demand(**kwargs):
        demands.append(kwargs)
        await asyncio.sleep(0)
        return {**PRIVATE, "domain": "old.invalid"}

    monkeypatch.setattr(eapi_session_protocol, "request_session", demand)

    async def resolve(*, exclude=()):
        exclusions.append(tuple(sorted(exclude)))
        return next(
            (host for host in ("old.invalid", "new.invalid") if host not in exclude),
            None,
        )

    monkeypatch.setattr(python_bridge, "resolve_eapi_domain", resolve)

    def handler(request):
        requests.append((request.url.host, request.url.path))
        assert "/login" not in request.url.path
        if request.url.host == "old.invalid":
            raise httpx.ConnectError("synthetic offline", request=request)
        return httpx.Response(200, json={"success": 1, "user": {"id": 7}, "books": []})

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(
        python_bridge.os,
        "write",
        lambda fd, data: updates.append((fd, json.loads(data))) or len(data),
    )
    try:
        client = await python_bridge.initialize_eapi_client()
        assert (await client.search("offline"))["books"] == []
        python_bridge._publish_eapi_session()
        python_bridge._publish_eapi_session()
        assert len(demands) == 1
        assert exclusions == [("old.invalid",)]
        assert len(updates) == 1
        assert updates[0][0] == 3
        assert updates[0][1]["domain"] == "new.invalid"
        assert updates[0][1].get("validate_next", False) is False
        assert ("new.invalid", "/eapi/user/profile") in requests
    finally:
        if python_bridge._eapi_client:
            await python_bridge._eapi_client.close()


def test_protocol_diagnostic_never_copies_untrusted_values():
    from eapi_session_protocol import _emit

    events = []
    error = OSError(5, "private-message-canary")
    error.winerror = 6
    _emit(events.append, "input-read", "error", error=error, count=999999)
    assert events == [
        {
            "phase": "input-read",
            "state": "error",
            "error_type": "OSError",
            "errno": 5,
            "winerror": 6,
            "count": 8193,
        }
    ]
    _emit(
        events.append,
        "private-phase-canary",
        "private-state-canary",
        error=type("PrivateClassCanary", (Exception,), {})("private-message-canary"),
    )
    assert events[-1] == {
        "phase": "unknown",
        "state": "unknown",
        "error_type": "OtherError",
    }
    assert "canary" not in json.dumps(events).lower()


def test_protocol_diagnostic_callback_failure_does_not_change_control_flow():
    from eapi_session_protocol import _emit

    def broken(_event):
        raise RuntimeError("private-callback-canary")

    _emit(broken, "input-read", "start")


def test_windows_input_adapter_owns_crt_duplicate_not_original(monkeypatch):
    import os
    import sys
    import types
    import weakref
    from eapi_session_protocol import _WindowsReadPipe

    closed, converted = [], []
    monkeypatch.setattr(
        os, "dup", lambda fd: 404 if fd == 0 else pytest.fail("wrong original")
    )
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        types.SimpleNamespace(get_osfhandle=lambda fd: converted.append(fd) or 9004),
    )
    pipe = _WindowsReadPipe(0)
    assert weakref.ref(pipe)() is pipe
    assert pipe.fileno() == 9004
    assert converted == [404]
    pipe.close()
    pipe.close()
    assert closed == [404]  # Never CloseHandle/close original stdin.
    with pytest.raises(ValueError, match="Closed private session pipe"):
        pipe.fileno()


def test_windows_input_adapter_detaches_even_when_close_fails(monkeypatch):
    import os
    from eapi_session_protocol import _WindowsReadPipe

    closed = []
    monkeypatch.setattr(os, "dup", lambda fd: 404)

    def fail(fd):
        closed.append(fd)
        raise OSError(5, "private-close-canary")

    monkeypatch.setattr(os, "close", fail)
    pipe = _WindowsReadPipe(0)
    with pytest.raises(OSError):
        pipe.close()
    pipe.close()
    assert closed == [404]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["connect", "primary-and-cleanup", "valid-and-cleanup"]
)
async def test_owned_pipe_cleanup_and_primary_precedence(monkeypatch, kind):
    import eapi_session_protocol as protocol

    events, closes = [], []

    class Pipe:
        def close(self):
            closes.append("pipe")

    class Transport:
        def close(self):
            closes.append("transport")
            raise OSError(5, "private-cleanup-canary")

    monkeypatch.setattr(
        protocol, "_open_input_pipe", lambda fd, diagnostic=None: Pipe()
    )
    real_write, real_close = protocol.os.write, protocol.os.close
    monkeypatch.setattr(
        protocol.os,
        "write",
        lambda fd, data: len(data) if fd == 404 else real_write(fd, data),
    )
    monkeypatch.setattr(
        protocol.os, "close", lambda fd: None if fd == 404 else real_close(fd)
    )

    async def connect(factory, pipe):
        if kind == "connect":
            raise OSError(6, "private-connect-canary")
        p = factory()
        if kind == "valid-and-cleanup":
            p.data_received(frame({"v": 1, "type": "session", "input": PRIVATE}))
        p.eof_received()
        return Transport(), p

    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    with pytest.raises(SessionProtocolError) as error:
        await protocol.request_session(request_fd=404, diagnostic=events.append)
    assert (
        str(error.value)
        == {
            "connect": "Asynchronous private session pipe unavailable",
            "primary-and-cleanup": "Invalid private session reply",
            "valid-and-cleanup": "Private session cleanup failed",
        }[kind]
    )
    assert closes == ["pipe" if kind == "connect" else "transport"]
    assert "canary" not in json.dumps(events).lower()
    if kind != "connect":
        assert any(e["phase"] == "cleanup" and e["state"] == "error" for e in events)
        assert any(
            e["phase"] == "primary"
            and e["state"] == ("done" if kind == "valid-and-cleanup" else "error")
            for e in events
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["success", "connect", "handle", "duplicate"])
async def test_windows_wrapper_contract_at_async_connect_boundary(monkeypatch, kind):
    import sys
    import types
    import weakref
    import eapi_session_protocol as protocol

    events, closed = [], []
    original_write, original_close = protocol.os.write, protocol.os.close

    def duplicate(fd):
        assert fd == 0
        if kind == "duplicate":
            raise OSError(24, "private-duplicate-canary")
        return 606

    def convert(fd):
        assert fd == 606
        if kind == "handle":
            raise OSError(6, "private-handle-canary")
        return 9004

    def close(fd):
        if fd in (404, 606):
            closed.append(fd)
        else:
            original_close(fd)

    monkeypatch.setattr(protocol.os, "dup", duplicate)
    monkeypatch.setattr(protocol.os, "close", close)
    monkeypatch.setattr(
        protocol.os,
        "write",
        lambda fd, data: len(data) if fd == 404 else original_write(fd, data),
    )
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=convert)
    )
    monkeypatch.setattr(
        protocol,
        "_open_input_pipe",
        lambda fd, diagnostic=None: protocol._WindowsReadPipe(fd, diagnostic),
    )

    async def connect(factory, pipe):
        registry = weakref.WeakSet()
        registry.add(pipe)
        assert pipe.fileno() == 9004  # IOCP receives a HANDLE, never CRT606.
        if kind == "connect":
            raise OSError(6, "private-connect-canary")
        p = factory()
        p.data_received(frame({"v": 1, "type": "session", "input": PRIVATE}))
        p.eof_received()

        class Transport:
            def close(self):
                pipe.close()
                pipe.close()
                p.connection_lost(None)

        return Transport(), p

    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    if kind == "success":
        assert (
            await protocol.request_session(request_fd=404, diagnostic=events.append)
            == PRIVATE
        )
    else:
        with pytest.raises(SessionProtocolError) as error:
            await protocol.request_session(request_fd=404, diagnostic=events.append)
        assert str(error.value) == (
            "Private session parent unavailable"
            if kind == "duplicate"
            else "Asynchronous private session pipe unavailable"
        )
    assert closed == ([404] if kind == "duplicate" else [404, 606])
    assert "canary" not in json.dumps(events).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_connection_lost_wait_failure_does_not_mask_primary(monkeypatch, valid):
    import eapi_session_protocol as protocol

    events = []

    class Pipe:
        def close(self):
            pass

    monkeypatch.setattr(
        protocol, "_open_input_pipe", lambda fd, diagnostic=None: Pipe()
    )
    real_write, real_close = protocol.os.write, protocol.os.close
    monkeypatch.setattr(
        protocol.os,
        "write",
        lambda fd, data: len(data) if fd == 404 else real_write(fd, data),
    )
    monkeypatch.setattr(
        protocol.os, "close", lambda fd: None if fd == 404 else real_close(fd)
    )

    async def connect(factory, pipe):
        p = factory()
        if valid:
            p.data_received(frame({"v": 1, "type": "session", "input": PRIVATE}))
        p.eof_received()

        class Transport:
            def close(self):
                pass  # Missing connection_lost completion is the failure seam.

        return Transport(), p

    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    wait = asyncio.wait_for

    async def bounded(awaitable, timeout):
        if timeout == 1:
            raise asyncio.TimeoutError()
        return await wait(awaitable, timeout)

    monkeypatch.setattr(protocol.asyncio, "wait_for", bounded)
    with pytest.raises(SessionProtocolError) as error:
        await protocol.request_session(request_fd=404, diagnostic=events.append)
    assert str(error.value) == (
        "Private session cleanup failed" if valid else "Invalid private session reply"
    )
    assert any(
        e["phase"] == "cleanup" and e["error_type"] == "TimeoutError"
        for e in events
        if e.get("state") == "error"
    )


@pytest.mark.asyncio
async def test_cancellation_remains_primary_when_transport_cleanup_fails(monkeypatch):
    import eapi_session_protocol as protocol

    events = []
    connected = asyncio.Event()

    class Pipe:
        def close(self):
            pass

    class Transport:
        def close(self):
            raise OSError(5, "private-cleanup-canary")

    monkeypatch.setattr(
        protocol, "_open_input_pipe", lambda fd, diagnostic=None: Pipe()
    )
    real_write, real_close = protocol.os.write, protocol.os.close
    monkeypatch.setattr(
        protocol.os,
        "write",
        lambda fd, data: len(data) if fd == 404 else real_write(fd, data),
    )
    monkeypatch.setattr(
        protocol.os, "close", lambda fd: None if fd == 404 else real_close(fd)
    )

    async def connect(factory, pipe):
        connected.set()
        return Transport(), factory()

    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    task = asyncio.create_task(
        protocol.request_session(request_fd=404, diagnostic=events.append)
    )
    await connected.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(
        event["phase"] == "primary" and event["state"] == "cancelled"
        for event in events
    )
    assert any(
        event["phase"] == "cleanup" and event["state"] == "error" for event in events
    )
    assert "canary" not in json.dumps(events)


@pytest.mark.asyncio
async def test_demand_encoder_is_exact_lazy_v2_but_reply_stays_v1(monkeypatch):
    import eapi_session_protocol as protocol

    writes = []

    class Pipe:
        def close(self):
            pass

    real_write, real_close = protocol.os.write, protocol.os.close
    monkeypatch.setattr(
        protocol.os,
        "write",
        lambda fd, data: writes.append(bytes(data)) or len(data)
        if fd == 404
        else real_write(fd, data),
    )
    monkeypatch.setattr(
        protocol.os, "close", lambda fd: None if fd == 404 else real_close(fd)
    )
    monkeypatch.setattr(
        protocol, "_open_input_pipe", lambda fd, diagnostic=None: Pipe()
    )

    async def connect(factory, pipe):
        p = factory()
        p.data_received(frame({"v": 1, "type": "session", "input": PRIVATE}))
        p.eof_received()

        class Transport:
            def close(self):
                p.connection_lost(None)

        return Transport(), p

    monkeypatch.setattr(asyncio.get_running_loop(), "connect_read_pipe", connect)
    assert await protocol.request_session(request_fd=404) == PRIVATE
    body = b'{"type":"session_needed","terminal":true}'
    assert writes == [b"ZLAD\x02" + len(body).to_bytes(2, "big") + body]


@pytest.mark.parametrize("mode", ["", "1", "lazy-v2", "lazy-v1", "lazy-v3", "unknown"])
def test_private_mode_admission_is_exact_and_never_falls_back(monkeypatch, mode):
    import io
    import os
    import sys
    from pathlib import Path

    monkeypatch.setenv("ZLIBRARY_EAPI_CHANNEL", mode)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    inherited = []
    monkeypatch.setattr(
        os, "set_inheritable", lambda fd, value: inherited.append((fd, value))
    )
    namespace = {"__name__": "__main__"}
    source = Path(python_bridge.__file__).read_text().split("import httpx")[0]
    if mode in {"lazy-v1", "lazy-v3", "unknown"}:
        with pytest.raises(SystemExit, match="Invalid private session input"):
            exec(source, namespace)
        assert not inherited
    else:
        exec(source, namespace)
        assert namespace["_lazy_session"] is (mode == "lazy-v2")
        assert namespace["_session_input"] == ({} if mode == "1" else None)
        assert inherited == (
            []
            if not mode
            else [(3, False), (4, False)]
            if mode == "lazy-v2"
            else [(3, False)]
        )
