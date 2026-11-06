"""Offline real-pipe/lease child; never contacts a provider."""

import asyncio
import builtins
import json
import os
from pathlib import Path
import signal
import sys
import time

if len(sys.argv) > 3:
    time.sleep(float(sys.argv[3]) / 1000)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from acquisition import AcquisitionError, DestinationLease, acquire
import acquisition
from eapi_session_protocol import SessionProtocolError, request_session, _emit

DEMAND_BODY = b'{"type":"session_needed","terminal":true}'
DEMAND = b"ZLAD\x02" + len(DEMAND_BODY).to_bytes(2, "big") + DEMAND_BODY
mode, directory = sys.argv[1:3]
extra_fd = os.dup(4) if mode in {"postgrant-extra", "stdout-late-extra"} else None
for descriptor in (3,) if mode == "control" else (3, 4):
    os.set_inheritable(descriptor, False)
root = Path(directory)
root.mkdir(parents=True, exist_ok=True)
(root / "pid").write_text(str(os.getpid()))

diagnostic_enabled = os.environ.get("LAZY_PROTOCOL_DIAGNOSTICS") == "1"
diagnostic_sequence = 0
diagnostic_started = time.monotonic()


def trace(event):
    global diagnostic_sequence
    if not diagnostic_enabled or diagnostic_sequence >= 256:
        return
    diagnostic_sequence += 1
    event = {
        **event,
        "sequence": diagnostic_sequence,
        "elapsedMs": max(0, round((time.monotonic() - diagnostic_started) * 1000)),
    }
    with (root / "protocol-diagnostic.jsonl").open("a", encoding="utf8") as stream:
        stream.write(json.dumps(event) + "\n")


def session_trace(event):
    trace(event)
    if mode == "probe-reply" and event == {"phase": "input-connect", "state": "done"}:
        print('{"reader_connected":true}', flush=True)


async def main():
    if mode == "control":
        json.loads(sys.stdin.readline(8193))
        (root / "control-ready").write_text("yes")
        try:
            await asyncio.sleep(30)
        finally:
            (root / "control-cleaned").write_text("yes")
        return
    if mode == "early-update":
        os.write(3, b'{"remix_userkey":"early-private-canary"}\n')
        os.close(4)
        await asyncio.sleep(30)
        return
    if mode in {
        "request-hold",
        "request-delayed-extra",
        "request-same-extra",
        "request-partial-open",
    }:
        payload = DEMAND[:-1] if mode == "request-partial-open" else DEMAND
        if mode == "request-same-extra":
            payload += b"x"
        os.write(4, payload)
        if mode == "request-hold":
            deadline = asyncio.get_running_loop().time() + 2
            while not (root / "parent-observed-demand").exists():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("No live demand")
                await asyncio.sleep(0.005)
            print("{}")
            return
        if mode == "request-delayed-extra":
            await asyncio.sleep(0.05)
            (root / "extra").write_text("yes")
            os.write(4, b"x")
        await asyncio.sleep(30)
        return
    if mode.startswith("request-"):
        payload = {
            "fragmented": DEMAND,
            "duplicate": DEMAND * 2,
            "oversize": b"x" * 136,
            "malformed": b"ZLAD\x02"
            + len(DEMAND_BODY).to_bytes(2, "big")
            + b"x" * len(DEMAND_BODY),
            "version": DEMAND[:4] + b"\x01" + DEMAND[5:],
            "partial": DEMAND[:-1],
        }[mode.removeprefix("request-")]
        if mode == "request-duplicate":
            # Coalesced duplicate must be rejected before commit; delayed extras
            # have their own pre/post-grant controls under the settled v2 rules.
            os.write(4, payload)
        else:
            for byte in payload:
                os.write(4, bytes([byte]))
                await asyncio.sleep(0.001)
        _emit(trace, "request-close", "start")
        os.close(4)
        _emit(trace, "request-close", "done")
        if mode == "request-partial":
            # Exit the selected launcher too, so partial+EOF is observable.
            return
        if mode.endswith("fragmented"):
            # The demand must be observed while this child is still alive,
            # not only when process exit finally closes every inherited handle.
            deadline = asyncio.get_running_loop().time() + 2
            while not (root / "parent-observed-demand").exists():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Demand was not observed before child exit")
                await asyncio.sleep(0.01)
            print("{}")
        else:
            await asyncio.sleep(30)
        return
    if mode in {"reply", "probe-reply", "unanswered-reply"}:
        try:
            await request_session(
                timeout={"reply": 0.3, "probe-reply": 1, "unanswered-reply": 30}[mode],
                diagnostic=session_trace,
            )
            print(json.dumps({"ok": True}))
        except SessionProtocolError as error:
            print(json.dumps({"error": str(error)}))
        return

    async def transfer(book, directory, *, filename, publication_hook):
        target = root / "synthetic.epub"
        assert (
            json.loads(Path(str(target) + ".acq.json").read_text())["state"] == "intent"
        )
        try:
            with DestinationLease(target, 0):
                raise AssertionError("lease was not held")
        except AcquisitionError as error:
            assert error.reason == "breaker_open"
        (root / "admitted").write_text("intent+lease")
        private = await request_session(timeout=10, diagnostic=session_trace)
        with (root / "transfers").open("ab") as stream:
            stream.write(b"1\n")
        (root / "granted").write_text("yes")
        if mode == "postgrant-extra":
            os.write(extra_fd, b"x")
            os.close(extra_fd)
            await asyncio.sleep(30)
        if mode == "stall":
            await asyncio.sleep(30)
        update = {k: v for k, v in private.items() if k != "action"}
        update["validate_next"] = True
        if mode == "leaked-error":
            update["remix_userkey"] = "recovered-private-canary"
        payload = (json.dumps(update) + "\n").encode()
        for part in (payload[:7], payload[7:]):
            os.write(3, part)
            await asyncio.sleep(0.005)
        if mode == "leaked-error":
            print(private["remix_userkey"], update["remix_userkey"], file=sys.stderr)
            raise ValueError("Synthetic failure")
        if mode == "duplicate-update":
            os.write(3, payload)
        stage = root / "stage"
        stage.write_bytes(b"offline-synthetic-document")
        publication_hook(stage, target)
        os.replace(stage, target)
        return {"file_path": str(target)}

    if mode == "refuse-admission":
        # Leave startup to the Node owner, but reserve all except 0.5s of the
        # child's acquisition allowance: below the 1s useful-transfer minimum.
        deadline = acquisition.WalkDeadline
        acquisition.WalkDeadline = lambda budget: deadline(min(budget, 0.5))

    result = await acquire(
        {
            "source": "zlibrary",
            "id": "1",
            "hash": "hash1",
            "title": "Synthetic",
            "extension": "epub",
        },
        str(root),
        "synthetic.epub",
        transfer=transfer,
    )
    print(json.dumps(result), flush=True)
    if mode == "stdout-late-extra":
        await asyncio.sleep(0.05)
        os.write(extra_fd, b"x")
        os.close(extra_fd)
        await asyncio.sleep(30)


async def run():
    task = asyncio.current_task()
    if diagnostic_enabled:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: _emit(
                trace, "loop", "error", error=context.get("exception")
            )
        )
    if os.name == "posix":
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await main()
    except asyncio.CancelledError:
        pass
    except Exception as error:
        if mode == "refuse-admission" and isinstance(error, AcquisitionError):
            (root / "refusal.json").write_text(json.dumps(error.to_dict()))
        _emit(trace, "primary", "error", error=error)
        try:
            # Fixed names/codes only: never serialize exception text or paths.
            names = {
                name
                for name, value in vars(builtins).items()
                if isinstance(value, type) and issubclass(value, Exception)
            } | {"AcquisitionError", "SessionProtocolError"}
            name = type(error).__name__
            reason = getattr(error, "reason", None)
            trace(
                {
                    "phase": "primary-exception",
                    "state": "error",
                    "exception_type": name if name in names else "OtherError",
                    "reason": reason
                    if type(reason) is str
                    and reason
                    in {
                        "breaker_open",
                        "walk_budget_exhausted",
                        "invalid_argument",
                        "output_refused",
                        "internal",
                        "integrity_mismatch",
                    }
                    else None
                    if reason is None
                    else "other",
                }
            )
        except Exception:
            pass  # Diagnostics must preserve the original exit behaviour.
        print("Synthetic private worker failed", file=sys.stderr)
        sys.exit(1)


asyncio.run(run())
