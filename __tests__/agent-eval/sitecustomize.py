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
    if response.headers.get("X-Eval-Outcome") == "read-timeout":
        await response.aclose()
        raise httpx.ReadTimeout(
            "Synthetic one-shot Anna search timeout", request=request
        )
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

# E13 real-timeout variant. Replace only the external transfer callback with a
# loopback transfer that remains pending after real link issuance and GET. The
# production acquire() coroutine owns wait_for(), commit inspection and cancel.
# A short test budget avoids waiting production's 2400 seconds. No exception is
# injected: asyncio's actual timer enters production's except TimeoutError arm.
if os.environ.get("EVAL_REAL_ACQUISITION_TIMEOUT") == "1":
    import asyncio
    import hashlib
    import json
    import linecache
    import sys
    import lib.acquisition as _acquisition

    _real_acquire = _acquisition.acquire
    _acquisition._long_bridge_budget = lambda: 20.0
    _timeout_record = {
        "kind": "real asyncio acquisition timeout with controlled loopback transfer",
        "timeout_branch": False,
        "cancelled": False,
        "source_sha256": hashlib.sha256(
            Path(_acquisition.__file__).read_bytes()
        ).hexdigest(),
    }

    def _save_timeout():
        Path(os.environ["EVAL_TIMEOUT_RECEIPT"]).write_text(json.dumps(_timeout_record))

    def _acquisition_trace(frame, event, arg):
        if frame.f_code.co_filename != _acquisition.__file__:
            return None
        if (
            event == "line"
            and frame.f_code.co_name == "_acquire"
            and "if task.done():"
            in linecache.getline(frame.f_code.co_filename, frame.f_lineno)
        ):
            scope = frame.f_locals["scope"]
            _timeout_record.update(
                timeout_branch=True,
                issued=scope["issued"],
                published=scope["published"],
                source_line=frame.f_lineno,
                task_done=frame.f_locals["task"].done(),
            )
            _save_timeout()
        return _acquisition_trace

    async def _pending_loopback_transfer(book, output_dir, **kwargs):
        async with httpx.AsyncClient(
            base_url="https://" + os.environ["ZLIBRARY_EAPI_DOMAIN"],
            verify=os.environ["SSL_CERT_FILE"],
            timeout=None,
        ) as client:
            response = await client.post(
                "/eapi/user/login",
                data={
                    "email": os.environ["ZLIBRARY_EMAIL"],
                    "password": os.environ["ZLIBRARY_PASSWORD"],
                },
            )
            user = response.json()["user"]
            client.cookies.set("remix_userid", user["id"])
            client.cookies.set("remix_userkey", user["remix_userkey"])
            issued = await client.get(
                f"/eapi/book/{book['id']}/{book.get('hash') or book['book_hash']}/file"
            )
            _timeout_record["link_status"] = issued.status_code
            url = issued.json()["file"]["downloadLink"]
            await client.get(url)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                _timeout_record["cancelled"] = True
                _save_timeout()
                raise

    async def _real_timeout_acquire(*args, **kwargs):
        kwargs["transfer"] = _pending_loopback_transfer
        sys.settrace(_acquisition_trace)
        try:
            return await _real_acquire(*args, **kwargs)
        finally:
            sys.settrace(None)
            _save_timeout()

    _acquisition.acquire = _real_timeout_acquire
