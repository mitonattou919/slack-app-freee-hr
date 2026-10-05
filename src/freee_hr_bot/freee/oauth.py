from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from freee_hr_bot.db.repo import utcnow

AUTHORIZE_URL = "https://accounts.secure.freee.co.jp/public_api/authorize"
TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
# Out-of-band: freee shows the authorization code on screen instead of redirecting,
# so the bot needs no public endpoint (it runs in Socket Mode).
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime


class FreeeOAuth:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http

    def authorize_url(self) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": OOB_REDIRECT_URI,
            "response_type": "code",
            "prompt": "select_company",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def exchange_code(self, code: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "redirect_uri": OOB_REDIRECT_URI,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        # freee rotates refresh tokens: the old one is invalid once this succeeds.
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, data: dict[str, Any]) -> TokenSet:
        data = {**data, "client_id": self.client_id, "client_secret": self.client_secret}
        res = await self.http.post(TOKEN_URL, data=data)
        if res.status_code != 200:
            raise OAuthError(f"token endpoint returned {res.status_code}: {res.text[:200]}")
        body = res.json()
        # Refresh a little early to avoid racing the expiry.
        expires_in = int(body.get("expires_in", 21600)) - 300
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=utcnow() + timedelta(seconds=expires_in),
        )
