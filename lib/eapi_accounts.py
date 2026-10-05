"""Account configuration and quota-based selection for EAPI downloads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os

from lib.eapi_session import _locked, _private_directory


@dataclass(frozen=True)
class Account:
    email: str = field(repr=False)
    password: str = field(repr=False)


def configured_accounts() -> list[Account]:
    raw = os.environ.get("ZLIBRARY_ACCOUNT_CREDENTIALS", "").strip()
    if not raw:
        email = os.environ.get("ZLIBRARY_EMAIL")
        password = os.environ.get("ZLIBRARY_PASSWORD")
        if not email or not password:
            raise ValueError(
                "ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD environment variables required, or configure ZLIBRARY_ACCOUNT_CREDENTIALS"
            )
        return [Account(email, password)]
    message = "ZLIBRARY_ACCOUNT_CREDENTIALS must be a non-empty JSON array of unique email/password objects"
    try:
        values = json.loads(raw)
    except ValueError:
        raise ValueError(message) from None
    if not isinstance(values, list) or not values:
        raise ValueError(message)
    accounts = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or any(
            not isinstance(value.get(k), str) or not value[k].strip()
            for k in ("email", "password")
        ):
            raise ValueError(message)
        email = value["email"].strip()
        if email.lower() in seen:
            raise ValueError(message)
        seen.add(email.lower())
        accounts.append(Account(email, value["password"]))
    return accounts


@asynccontextmanager
async def download_client(accounts, initialize):
    # Hold selection and download together: concurrent bridge processes cannot
    # both spend the last observed slot. Never replay an uncertain download.
    async with _locked(_private_directory() / "downloads.lock"):
        for index, account in enumerate(accounts, 1):
            client = await initialize(account)
            try:
                profile = await client.get_profile()
                quota = _quota(profile, index)
                if quota["daily_remaining"] > 0:
                    yield index, client
                    return
            finally:
                await client.close()
        raise RuntimeError(
            "All configured Z-Library accounts have exhausted their download quota"
        )


def _quota(profile: dict, index: int) -> dict:
    user = profile.get("user")
    user = user if isinstance(user, dict) else {}
    limit, used = user.get("downloads_limit"), user.get("downloads_today")
    if (
        profile.get("success") != 1
        or type(limit) is not int
        or type(used) is not int
        or min(limit, used) < 0
    ):
        raise RuntimeError(
            f"Download quota for account {index} is unknown; automatic selection stopped"
        )
    return {
        "account_index": index,
        "daily_limit": limit,
        "daily_remaining": max(0, limit - used),
        "downloads_today": used,
        "is_premium": bool(user.get("isPremium", 0)),
    }


async def pool_limits(accounts, initialize) -> dict:
    summaries = []
    for index, account in enumerate(accounts, 1):
        client = await initialize(account)
        try:
            summaries.append(_quota(await client.get_profile(), index))
        finally:
            await client.close()
    result = {
        key: sum(row[key] for row in summaries)
        for key in ("daily_limit", "daily_remaining", "downloads_today")
    }
    result["is_premium"] = any(row["is_premium"] for row in summaries)
    result["accounts"] = summaries
    return result
