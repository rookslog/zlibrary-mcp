"""Synthetic acquisition under the real Node bridge timer; no provider I/O."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lib import acquisition as acq
from lib.sources.net import WalkDeadline


async def main():
    now = [0.0]
    acq.WalkDeadline = lambda budget: WalkDeadline(budget, clock=lambda: now[0])
    with tempfile.TemporaryDirectory(prefix="deadline-worker-") as directory:
        target = Path(directory) / "chosen.pdf"
        issued = []
        lease = acq.DestinationLease(target, 4).__enter__()

        async def release():
            # Keep the actual OS lease and cooperative wait; its logical spend
            # is independent of how long the runner takes to schedule us.
            await asyncio.sleep(0)
            now[0] = 34.0 if sys.argv[1] == "late" else 20.0
            lease.close()

        async def transfer(*args, **kwargs):
            issued.append(True)
            await asyncio.Event().wait()
            raise AssertionError("transfer outlived its remaining allowance")

        wait_for = acq.asyncio.wait_for

        async def expire_transfer(task, timeout):
            assert abs(timeout - 10.724125) < 0.000001
            return await wait_for(task, 0)

        acq.asyncio.wait_for = expire_transfer

        holder = asyncio.create_task(release())
        try:
            try:
                await acq.acquire(
                    {"source": "libgen", "md5": "a" * 32, "extension": "pdf"},
                    directory,
                    filename="chosen.pdf",
                    transfer=transfer,
                )
            except acq.AcquisitionError as error:
                print(json.dumps({**error.to_dict(), "transfers": len(issued)}))
        finally:
            await holder
            lease.close()


asyncio.run(main())
