"""Automatic choice uses observed quotas, never a guessed daily allowance."""

import json
from unittest.mock import AsyncMock

import pytest

from lib import eapi_accounts


@pytest.fixture
def accounts(monkeypatch, tmp_path):
    monkeypatch.setenv("ZLIBRARY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv(
        "ZLIBRARY_ACCOUNT_CREDENTIALS",
        json.dumps(
            [
                {"email": "one@example.test", "password": "test-password-one"},
                {"email": "two@example.test", "password": "test-password-two"},
            ]
        ),
    )
    return eapi_accounts.configured_accounts()


def client(limit, used):
    value = AsyncMock()
    value.get_profile.return_value = {
        "success": 1,
        "user": {"downloads_limit": limit, "downloads_today": used},
    }
    return value


async def test_exhausted_first_account_selects_second(accounts):
    first, second = client(10, 10), client(10, 3)
    initialize = AsyncMock(side_effect=[first, second])
    async with eapi_accounts.download_client(accounts, initialize) as (index, selected):
        assert index == 2
        assert selected is second
    first.close.assert_awaited_once()
    assert initialize.await_args_list[1].args == (accounts[1],)


async def test_first_account_with_quota_is_reused_without_logging_into_others(accounts):
    first = client(20, 12)
    initialize = AsyncMock(return_value=first)
    async with eapi_accounts.download_client(accounts, initialize) as (index, selected):
        assert index == 1
        assert selected is first
    initialize.assert_awaited_once_with(accounts[0])


async def test_all_accounts_exhausted_is_explicit(accounts):
    initialize = AsyncMock(side_effect=[client(10, 10), client(10, 11)])
    with pytest.raises(RuntimeError, match="All configured.*quota"):
        async with eapi_accounts.download_client(accounts, initialize):
            pytest.fail("must not dispatch a download")


async def test_unknown_quota_does_not_guess_or_rotate(accounts):
    initialize = AsyncMock(return_value=client(None, 0))
    with pytest.raises(RuntimeError, match="quota.*unknown"):
        async with eapi_accounts.download_client(accounts, initialize):
            pytest.fail("must not dispatch")
    initialize.assert_awaited_once()


async def test_login_error_does_not_rotate(accounts):
    initialize = AsyncMock(side_effect=RuntimeError("Too many logins"))
    with pytest.raises(RuntimeError, match="Too many logins"):
        async with eapi_accounts.download_client(accounts, initialize):
            pytest.fail("must not dispatch")
    initialize.assert_awaited_once()


async def test_uncertain_download_is_not_replayed_on_another_account(accounts):
    initialize = AsyncMock(return_value=client(10, 0))
    with pytest.raises(TimeoutError):
        async with eapi_accounts.download_client(accounts, initialize):
            raise TimeoutError("download outcome unknown")
    initialize.assert_awaited_once()


def test_legacy_credentials_remain_supported(monkeypatch):
    monkeypatch.delenv("ZLIBRARY_ACCOUNT_CREDENTIALS", raising=False)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "legacy@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "test-password")
    result = eapi_accounts.configured_accounts()
    assert len(result) == 1
    assert result[0].email == "legacy@example.test"
    assert result[0].password == "test-password"
    assert "test-password" not in repr(result)


@pytest.mark.parametrize(
    "raw",
    ["not-json", "{}", "[]", '[{"email":"one"}]', '[{"email":"one","password":null}]'],
)
def test_invalid_pool_is_reported_without_leaking_values(monkeypatch, raw):
    monkeypatch.setenv("ZLIBRARY_ACCOUNT_CREDENTIALS", raw)
    with pytest.raises(ValueError, match="ZLIBRARY_ACCOUNT_CREDENTIALS") as exc:
        eapi_accounts.configured_accounts()
    assert raw not in str(exc.value)


async def test_concurrent_downloads_recheck_quota_after_previous_download(accounts):
    import asyncio

    used = [9, 0]
    selected_indices = []

    async def initialize(account):
        index = accounts.index(account)
        value = client(10, used[index])
        return value

    async def download():
        async with eapi_accounts.download_client(accounts, initialize) as (index, _):
            selected_indices.append(index)
            used[index - 1] += 1

    await asyncio.gather(download(), download())
    assert selected_indices == [1, 2]


async def test_bridge_dispatch_download_uses_selected_account(
    accounts, monkeypatch, capsys
):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
    import python_bridge

    first, second = client(10, 10), client(10, 3)
    initialize = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(python_bridge, "_initialize_account", initialize)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)
    monkeypatch.setattr(
        python_bridge,
        "_dispatch_bridge_function",
        AsyncMock(return_value={"file_path": "book.epub"}),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python_bridge.py",
            "download_book",
            '{"book_details":{"id":"123","hash":"abc"}}',
        ],
    )
    await python_bridge.main()
    payload = json.loads(capsys.readouterr().out)
    assert json.loads(payload["content"][0]["text"])["account_index"] == 2
    assert initialize.await_count == 2


async def test_limits_tool_reports_pool_total_so_exhausted_first_account_does_not_hide_capacity(
    accounts, monkeypatch
):
    import python_bridge

    first, second = client(10, 10), client(15, 3)
    monkeypatch.setattr(python_bridge, "get_eapi_client", AsyncMock(return_value=first))
    monkeypatch.setattr(
        python_bridge, "_initialize_account", AsyncMock(side_effect=[first, second])
    )
    result = await python_bridge.get_download_limits()
    assert result["daily_limit"] == 25
    assert result["daily_remaining"] == 12
    assert [a["daily_remaining"] for a in result["accounts"]] == [0, 12]
    assert "@" not in json.dumps(result)
