"""Refusal-driven renewal for saved logins, without an environment fallback."""

from lib.login_store import StoreError, load
from zlibrary.eapi import EAPIClient, EAPIAuthenticationError


class SavedLoginClient(EAPIClient):
    def __init__(self, login, domain=None):
        super().__init__(
            domain or login.domain,
            remix_userid=login.user_id.reveal(),
            remix_userkey=login.user_key.reveal(),
        )
        self.login_record = login
        self.pinned_domain = domain
        self._renewed = False

    async def _request_json(self, method, path, **kwargs):
        try:
            return await super()._request_json(method, path, **kwargs)
        except EAPIAuthenticationError:
            try:
                fresh = load()
            except StoreError:
                fresh = None  # A rejected saved session never switches to a password.
            if (
                self._renewed
                or fresh is None
                or fresh.generation <= self.login_record.generation
                or fresh.identity != self.login_record.identity
            ):
                raise EAPIAuthenticationError(
                    "Saved login was refused; run zlibrary login"
                ) from None
        self._renewed = True
        await self.close()
        self.login_record = fresh
        self.domain = self.pinned_domain or fresh.domain
        self.remix_userid = fresh.user_id.reveal()
        self.remix_userkey = fresh.user_key.reveal()
        try:
            return await super()._request_json(method, path, **kwargs)
        except EAPIAuthenticationError:
            raise EAPIAuthenticationError(
                "Saved login was refused; run zlibrary login"
            ) from None
