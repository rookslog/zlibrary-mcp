"""Evaluation-only URL seam; retain the rehearsal's strict loopback fence."""

import os
from pathlib import Path
import runpy
from urllib.parse import urlsplit

runpy.run_path(str(Path(__file__).resolve().parents[1] / "rehearsal/sitecustomize.py"))
# Rehearsal redirects requests (LibGen search); downloads use httpx instead.
import httpx

_original_send = httpx.AsyncClient.send


async def _send(self, request, *args, **kwargs):
    parsed = urlsplit(str(request.url))
    if parsed.hostname in {"libgen.li", "libgen.vg", "libgen.la"}:
        request.url = httpx.URL(
            os.environ["REHEARSAL_LIBGEN_ORIGIN"]
            + "/libgen"
            + parsed.path
            + ("?" + parsed.query if parsed.query else "")
        )
        request.headers["host"] = request.url.netloc.decode()
    response = await _original_send(self, request, *args, **kwargs)
    # Controlled E13 injection AFTER the real fake has issued a link and observed
    # a transfer request. Model a spent acquisition deadline, without a long sleep.
    if (
        parsed.path.startswith("/files/")
        and response.headers.get("X-Eval-Outcome") == "issued-unknown"
    ):
        from lib.acquisition import AcquisitionError

        await response.aclose()
        raise AcquisitionError(
            "walk_budget_exhausted",
            "Synthetic post-issuance unknown outcome",
            issued=True,
        )
    return response


httpx.AsyncClient.send = _send
