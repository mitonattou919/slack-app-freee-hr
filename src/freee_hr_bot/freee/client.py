import asyncio
from collections import defaultdict
from typing import Any

import httpx

from freee_hr_bot.crypto import TokenCipher
from freee_hr_bot.db.models import UserLink
from freee_hr_bot.db.repo import Repository, utcnow
from freee_hr_bot.freee.oauth import FreeeOAuth, TokenSet

API_BASE = "https://api.freee.co.jp/hr"
# freee times out when a whole month of days is requested at once.
MAX_CONCURRENT_REQUESTS = 4


class FreeeAPIError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        super().__init__(f"freee API error {status}: {body}")

    @property
    def messages(self) -> list[str]:
        if isinstance(self.body, dict):
            msgs = self.body.get("message") or self.body.get("messages") or []
            if isinstance(msgs, str):
                return [msgs]
            errors = self.body.get("errors") or []
            for e in errors:
                msgs = [*msgs, *(e.get("messages") or [])]
            return [str(m) for m in msgs]
        return [str(self.body)[:200]]


class NotLinkedError(Exception):
    pass


async def call_api(
    http: httpx.AsyncClient, access_token: str, method: str, path: str, **kwargs: Any
) -> dict[str, Any]:
    res = await http.request(
        method, f"{API_BASE}{path}", headers={"Authorization": f"Bearer {access_token}"}, **kwargs
    )
    if res.status_code >= 400:
        error: Any
        try:
            error = res.json()
        except ValueError:
            error = res.text
        raise FreeeAPIError(res.status_code, error)
    data: dict[str, Any] = res.json() if res.content else {}
    return data


class TokenProvider:
    """Hands out a valid access token per Slack user, refreshing (serialized) when needed."""

    def __init__(self, repo: Repository, cipher: TokenCipher, oauth: FreeeOAuth) -> None:
        self.repo = repo
        self.cipher = cipher
        self.oauth = oauth
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def access_token(self, slack_user_id: str, *, rejected: str | None = None) -> str:
        """Return a usable token. Pass ``rejected`` when the API answered 401 with that token."""
        link = await self._link(slack_user_id)
        current = self.cipher.decrypt(link.access_token_enc)
        if link.expires_at > utcnow() and current != rejected:
            return current
        async with self._locks[slack_user_id]:
            # Another coroutine may have refreshed while we waited; refresh tokens are
            # single-use, so refreshing twice would lock the user out.
            link = await self._link(slack_user_id)
            current = self.cipher.decrypt(link.access_token_enc)
            if link.expires_at > utcnow() and current != rejected:
                return current
            tokens = await self.oauth.refresh(self.cipher.decrypt(link.refresh_token_enc))
            await self.store(link, tokens)
            return tokens.access_token

    async def store(self, link: UserLink, tokens: TokenSet) -> None:
        link.access_token_enc = self.cipher.encrypt(tokens.access_token)
        link.refresh_token_enc = self.cipher.encrypt(tokens.refresh_token)
        link.expires_at = tokens.expires_at
        await self.repo.save_link(link)

    async def _link(self, slack_user_id: str) -> UserLink:
        link = await self.repo.get_link(slack_user_id)
        if link is None:
            raise NotLinkedError(slack_user_id)
        return link


class FreeeClient:
    """freee API client acting on behalf of one Slack user."""

    def __init__(self, http: httpx.AsyncClient, tokens: TokenProvider, slack_user_id: str) -> None:
        self.http = http
        self.tokens = tokens
        self.slack_user_id = slack_user_id
        self._limit = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self._limit:
            return await self._request(method, path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self.tokens.access_token(self.slack_user_id)
        try:
            return await call_api(self.http, token, method, path, **kwargs)
        except FreeeAPIError as e:
            if e.status != 401:
                raise
        token = await self.tokens.access_token(self.slack_user_id, rejected=token)
        return await call_api(self.http, token, method, path, **kwargs)
