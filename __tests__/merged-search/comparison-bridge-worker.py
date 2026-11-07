"""Real bridge/router/LibGen with only external provider I/O substituted."""

import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace

root = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(root), str(root / "lib")]

from lib import python_bridge as bridge  # noqa: E402
from lib.sources import libgen  # noqa: E402
from lib.sources.config import SourceConfig  # noqa: E402
from lib.sources.router import SourceRouter  # noqa: E402


class Catalogue:
    def __init__(self, mirror):
        self.mirror = mirror

    def search_default(self, query):
        # Longer than the actual comparison slice, shorter than ordinary search.
        # Miswiring the switch returns the first mirror's DIFFERENT row, rather
        # than relying on a test timeout to detect the wrong branch.
        if self.mirror == "li":
            time.sleep(6)
        return [
            SimpleNamespace(
                md5=("a" if self.mirror == "li" else "b") * 32,
                title=f"{query} ({self.mirror})",
                author="Stanislaw Lem",
                year="1961",
                extension="pdf",
                size="1 MB",
                language="French",
            )
        ]


async def empty_peer(*_args, **_kwargs):
    return []


libgen.LibgenSearch = Catalogue
router = SourceRouter(
    SourceConfig(preflight_enabled=False, total_timeout=15, walk_budget=30)
)
router._libgen = libgen.LibgenAdapter(router.config)
router._libgen._mirror_candidates = lambda: ["li", "vg"]
router._annas = SimpleNamespace(search=empty_peer)


async def fixture_router():
    return router


bridge.get_source_router = fixture_router
asyncio.run(bridge.main())
