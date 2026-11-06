"""Synthetic pipe peer for interrupted calls and retry-deadline propagation."""

import json
import signal
import sys

mode = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    if request.get("cancel"):
        if mode == "ack":
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "status_only": True,
                        "member_session": "none",
                        "retry_at": 0,
                    }
                ),
                flush=True,
            )
            continue
        # End before the cancellation acknowledgement reaches the owner.
        break
    if mode in ("pending", "ack"):
        print("REQUEST_RECEIVED", file=sys.stderr, flush=True)
        continue
    reply = {
        "id": request["id"],
        "member_session": "none",
        "retry_at": 0,
        "result": {"received_retry_at": request["retry_at"]},
    }
    if mode == "malformed-late":

        def followup(*_args):
            print(json.dumps(reply), flush=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, followup)
        print("not-json", flush=True)
        continue
    if mode == "malformed":
        # Both frames share one write, so the valid frame follows the bad one.
        sys.stdout.write("not-json\n" + json.dumps(reply) + "\n")
    else:
        sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
