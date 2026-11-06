"""Interactive account commands. Credentials are input-only, never argv or output."""

import asyncio
import getpass
import json
import logging
from pathlib import Path
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.login_store import StoreError, load, logout, resolve, save


async def authenticate(identity, password):
    from python_bridge import _login_eapi_client

    return await _login_eapi_client(identity, password)


async def validate(login):
    from lib.login_runtime import SavedLoginClient
    from zlibrary.eapi import EAPIAuthenticationError

    client = None
    try:
        if login.origin == "saved":
            import os

            client = SavedLoginClient(
                login, os.environ.get("ZLIBRARY_EAPI_DOMAIN", "").strip() or None
            )
        elif login.origin == "environment":
            client = await authenticate(login.identity, login.password.reveal())
        else:
            return "not_configured"
        profile = await client.get_profile()
        if (
            isinstance(profile, dict)
            and profile.get("success") == 1
            and isinstance(profile.get("user"), dict)
            and str(profile["user"].get("id", "")) == client.remix_userid
        ):
            return "working"
        return "unknown"
    except EAPIAuthenticationError:
        return "refused"
    except Exception:
        return "unknown"
    finally:
        if client:
            await client.close()


def _hidden(prompt):
    # getpass otherwise falls back to echoed stdin when no controlling TTY exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


async def run(args):
    json_mode = "--json" in args
    command = args[0] if args else ""
    permitted = {"--json", *(["--password-stdin"] if command == "login" else [])}
    if (
        command not in {"login", "status", "logout"}
        or len(set(args[1:])) != len(args[1:])
        or any(arg not in permitted for arg in args[1:])
    ):
        print(
            "Invalid command options; use login [--password-stdin], status, or logout [--json].",
            file=sys.stderr,
        )
        return 2
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        if command == "login":
            # Check storage before requesting a password or contacting the provider.
            load(tolerate_content=True)
            identity = _hidden("Email: ")
            password = (
                sys.stdin.readline(8193).removesuffix("\n").removesuffix("\r")
                if "--password-stdin" in args
                else _hidden("Password: ")
            )
            if (
                not identity
                or not password
                or len(identity) > 8192
                or len(password) > 8192
            ):
                raise ValueError()
            client = await authenticate(identity, password)
            try:
                saved = save(
                    identity=identity,
                    user_id=client.remix_userid,
                    user_key=client.remix_userkey,
                    domain=client.domain,
                )
            finally:
                await client.close()
            result = saved.public()
            result["message"] = "Login saved."
        elif command == "status":
            login = resolve()
            result = login.public()
            result["works"] = await validate(login)
        else:
            logout()
            result = {
                "backend": "file",
                "message": "Saved login removed. The key is not cancelled; environment credentials, if configured, still apply.",
            }
        if json_mode:
            print(json.dumps(result))
        else:
            if result.get("notice"):
                print(result["notice"], file=sys.stderr)
            print(
                result.get("message")
                or f"{result['identity'] or 'No account'}; origin: {result['origin']}; backend: file; works: {result['works']}; expiry: unknown"
            )
        return 0
    except (Exception, KeyboardInterrupt):
        message = (
            "Login store refused; check private permissions and run zlibrary login."
            if sys.exc_info()[0] is StoreError
            else "Login failed; check setup and run zlibrary login."
        )
        print(json.dumps({"error": message}) if json_mode else message, file=sys.stderr)
        return 1
    finally:
        logging.disable(previous_logging)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(sys.argv[1:])))
