import asyncio
from datetime import timedelta

import httpx
import pytest
import respx

from freee_hr_bot.freee.client import (
    MAX_CONCURRENT_REQUESTS,
    FreeeAPIError,
    FreeeClient,
    NotLinkedError,
)
from freee_hr_bot.freee.oauth import TOKEN_URL
from tests.conftest import API, USER, link_user


def token_response(n: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": f"access-{n}", "refresh_token": f"refresh-{n}", "expires_in": 21600},
    )


@respx.mock
async def test_valid_token_used_without_refresh(tokens):
    await link_user(tokens)
    refresh = respx.post(TOKEN_URL)
    assert await tokens.access_token(USER) == "access-1"
    assert not refresh.called


@respx.mock
async def test_expired_token_refreshed_and_rotated(tokens, repo, cipher):
    await link_user(tokens, expires_in=timedelta(seconds=-1))
    route = respx.post(TOKEN_URL).mock(return_value=token_response(2))
    assert await tokens.access_token(USER) == "access-2"
    assert b"refresh_token=refresh-1" in route.calls[0].request.content
    link = await repo.get_link(USER)
    assert cipher.decrypt(link.refresh_token_enc) == "refresh-2"


@respx.mock
async def test_concurrent_refresh_happens_once(tokens):
    # refresh tokens are single-use; a second refresh would fail and lock the user out
    await link_user(tokens, expires_in=timedelta(seconds=-1))
    route = respx.post(TOKEN_URL).mock(return_value=token_response(2))
    results = await asyncio.gather(*(tokens.access_token(USER) for _ in range(5)))
    assert set(results) == {"access-2"}
    assert route.call_count == 1


@respx.mock
async def test_401_triggers_refresh_and_retry(tokens, http):
    await link_user(tokens)
    respx.post(TOKEN_URL).mock(return_value=token_response(2))
    api = respx.get(f"{API}/users/me").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    client = FreeeClient(http, tokens, USER)
    assert await client.request("GET", "/api/v1/users/me") == {"id": 1}
    assert api.calls[1].request.headers["Authorization"] == "Bearer access-2"


@respx.mock
async def test_api_error_messages_are_extracted(tokens, http):
    await link_user(tokens)
    respx.get(f"{API}/users/me").mock(
        return_value=httpx.Response(
            400, json={"errors": [{"type": "validation", "messages": ["締め済みです"]}]}
        )
    )
    with pytest.raises(FreeeAPIError) as ei:
        await FreeeClient(http, tokens, USER).request("GET", "/api/v1/users/me")
    assert ei.value.messages == ["締め済みです"]


async def test_unlinked_user(tokens):
    with pytest.raises(NotLinkedError):
        await tokens.access_token("U_UNKNOWN")


@respx.mock
async def test_concurrent_requests_are_bounded(tokens, http):
    # freee times out when a month of days is fetched all at once
    await link_user(tokens)
    in_flight = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={})

    respx.get(f"{API}/users/me").mock(side_effect=slow)
    client = FreeeClient(http, tokens, USER)
    await asyncio.gather(*(client.request("GET", "/api/v1/users/me") for _ in range(31)))
    assert peak <= MAX_CONCURRENT_REQUESTS
