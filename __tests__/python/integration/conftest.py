"""
Pytest fixtures for integration tests.

Provides a module-scoped zlib_client fixture that authenticates
with Z-Library via EAPIClient for test methods that declare it.
"""

from functools import wraps
import os
import sys

import pytest
import pytest_asyncio

# Add project lib/ to path so zlibrary imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))

from zlibrary.eapi import EAPIClient  # noqa: E402

import python_bridge  # noqa: E402
from .ci_credentials import (  # noqa: E402
    capture_login,
    environment_credentials,
    login_with_credentials,
)


_original_login = EAPIClient.login


@wraps(_original_login)
async def _captured_login(client, *args, **kwargs):
    return await capture_login(
        client, _original_login.__get__(client, type(client)), *args, **kwargs
    )


_captured_login._captures_session_values = True
EAPIClient.login = _captured_login


def pytest_unconfigure():
    if EAPIClient.login is _captured_login:
        EAPIClient.login = _original_login


@pytest.fixture(scope="module")
def credentials():
    return environment_credentials()


@pytest.fixture(scope="module")
def eapi_domain():
    """Resolve the runtime default only when a live test requests a domain."""
    from zlibrary.eapi import DEFAULT_EAPI_DOMAINS

    return os.environ.get("ZLIBRARY_EAPI_DOMAIN") or DEFAULT_EAPI_DOMAINS[0]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def zlib_client(eapi_domain, credentials):
    """Pre-authenticated EAPIClient for integration tests.

    Reads credentials from environment variables.
    Yields the client, then closes it during teardown.

    Also installs the client as python_bridge's module-level EAPI client:
    python_bridge functions (search, get_book_metadata_complete, ...) resolve
    their client via the module-level singleton — the ``client=`` keyword is
    kept only for backward compatibility and is ignored — so without this the
    bridge-level tests would fail with "EAPI client not initialized".
    """
    client = EAPIClient(eapi_domain)
    assert await login_with_credentials(client, credentials), "Integration login failed"

    previous_global = python_bridge._eapi_client
    python_bridge._eapi_client = client

    yield client

    python_bridge._eapi_client = previous_global
    await client.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def known_book(zlib_client):
    """A live (id, hash) pair for a book that exists right now.

    Z-Library book hashes drift when files are replaced (e.g. book 1252896's
    hash changed from 882753 to ef25bc), so metadata tests resolve a book from
    a live search instead of hardcoding a pair.
    """
    result = await zlib_client.search(
        "Hegel Encyclopaedia Philosophical Sciences", limit=5
    )
    for book in result.get("books", []):
        if book.get("id") and book.get("hash"):
            return {"id": str(book["id"]), "hash": str(book["hash"])}
    result = await zlib_client.search("philosophy", limit=5)
    for book in result.get("books", []):
        if book.get("id") and book.get("hash"):
            return {"id": str(book["id"]), "hash": str(book["hash"])}
    pytest.skip("No searchable book with id+hash available to test metadata against")
