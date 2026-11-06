"""Private resident Anna search owner. Credentials/cookies never cross its pipes."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.sources.annas import AnnasArchiveAdapter
from lib.sources.config import get_source_config
from lib.sources.errors import SourceError
from lib.sources.net import WalkDeadline


async def serve(adapter):
    tasks = {}

    def send(value):
        print(json.dumps(value), flush=True)

    async def search(request):
        request_id = request["id"]
        try:
            args = request["args"]
            count = args.get("count", 10)
            if type(count) is not int or not 1 <= count <= 50:
                raise ValueError("Invalid count")
            rows = await adapter.search(
                args["query"], deadline=WalkDeadline(adapter.config.walk_budget)
            )
            books = [
                {
                    "md5": r.md5,
                    "title": r.title,
                    "author": r.author,
                    "year": r.year,
                    "extension": r.extension,
                    "size": r.size,
                    "source": r.source.value,
                    "download_url": r.download_url,
                    **r.extra,
                }
                for r in rows[:count]
            ]
            send(
                {
                    "id": request_id,
                    "result": {
                        "books": books,
                        "sources_used": ["annas_archive"] if books else [],
                    },
                    **adapter.member_status(),
                }
            )
        except asyncio.CancelledError:
            pass
        except SourceError as error:
            send(
                {
                    "id": request_id,
                    "error": {"message": str(error), "details": error.to_dict()},
                    **adapter.member_status(),
                }
            )
        except Exception:
            # No exception repr/traceback: third-party errors can carry bodies.
            send(
                {
                    "id": request_id,
                    "error": {
                        "message": "Anna member search failed",
                        "details": {"provider": "annas", "reason": "protocol_error"},
                    },
                    **adapter.member_status(),
                }
            )
        finally:
            tasks.pop(request_id, None)

    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            request = json.loads(line)
            request_id = request["id"]
            if request.get("cancel") is True:
                if request_id in tasks:
                    tasks[request_id].cancel()
            elif request_id not in tasks and len(tasks) < 64:
                tasks[request_id] = asyncio.create_task(search(request))
    finally:
        pending = list(tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await adapter.close()


if __name__ == "__main__":
    try:
        asyncio.run(serve(AnnasArchiveAdapter(get_source_config())))
    except Exception:
        # Startup failures must not dump environment, request or cookie data.
        sys.exit(1)
