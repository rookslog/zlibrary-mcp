"""Opaque integration credentials; raw values cross only the login boundary."""

import os


class Secret:
    __slots__ = ("__value",)

    def __init__(self, value):
        self.__value = value

    def __repr__(self):
        return "***"

    def __str__(self):
        return "***"

    def reveal(self):
        return self.__value


def environment_credentials():
    return {
        "email": Secret(os.environ.get("ZLIBRARY_EMAIL", "")),
        "password": Secret(os.environ.get("ZLIBRARY_PASSWORD", "")),
    }


async def login_with_credentials(client, credentials):
    """Return a boolean so assertions cannot display a complete login reply."""
    try:
        return (
            await client.login(
                credentials["email"].reveal(), credentials["password"].reveal()
            )
        ).get("success") == 1
    except Exception:
        raise RuntimeError("Integration login failed") from None
