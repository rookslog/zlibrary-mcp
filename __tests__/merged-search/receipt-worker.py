"""Synthetic transfer boundary over the real acquisition/receipt implementation."""

import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lib.acquisition import acquire, AcquisitionError, destination_name  # noqa: E402

args = json.loads(sys.argv[1])
root = Path(sys.argv[2])
mode = sys.argv[3]
payload = b"%PDF-1.7\nsynthetic merged-tool receipt fixture"


async def transfer(book, directory, *, filename, publication_hook):
    with (root / "transfers").open("a") as stream:
        stream.write(book["source"] + "\n")
    if mode == "issued":
        raise AcquisitionError(
            "provider_error", "Synthetic transfer failure", issued=True
        )
    stage = Path(directory) / ".synthetic-stage"
    stage.write_bytes(payload)
    target = Path(directory) / destination_name(book, filename)
    publication_hook(stage, target)
    if mode == "checkpoint":
        raise AcquisitionError(
            "provider_error", "Synthetic publication failure", issued=True
        )
    stage.replace(target)
    return {"file_path": str(target), "publication_reused": False}


async def main():
    try:
        result = await acquire(**args, transfer=transfer)
        if mode == "lost":
            raise AcquisitionError(
                "outcome_unknown", "Synthetic lost reply", issued=True
            )
        return result
    except AcquisitionError as error:
        return {
            "synthetic_error": {"reason": error.reason, "request_issued": error.issued}
        }


print(json.dumps(asyncio.run(main())))
