from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from freee_hr_bot.crypto import TokenCipher
from freee_hr_bot.db.models import UserLink
from freee_hr_bot.db.repo import Repository, utcnow
from freee_hr_bot.freee.client import TokenProvider
from freee_hr_bot.freee.oauth import FreeeOAuth, TokenSet
from freee_hr_bot.proposals.service import Deps

COMPANY_ID = 1
EMPLOYEE_ID = 42
USER = "U123"
API = "https://api.freee.co.jp/hr/api/v1"


@pytest.fixture
async def repo(tmp_path):
    r = Repository.from_url(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await r.create_all()
    yield r
    await r.engine.dispose()


@pytest.fixture
def cipher():
    return TokenCipher(Fernet.generate_key().decode())


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def oauth(http):
    return FreeeOAuth("cid", "secret", http)


@pytest.fixture
def tokens(repo, cipher, oauth):
    return TokenProvider(repo, cipher, oauth)


async def link_user(tokens: TokenProvider, *, expires_in: timedelta = timedelta(hours=1)) -> None:
    link = UserLink(
        slack_user_id=USER,
        freee_user_id=7,
        employee_id=EMPLOYEE_ID,
        access_token_enc="",
        refresh_token_enc="",
        expires_at=utcnow(),
        created_at=utcnow(),
    )
    await tokens.store(link, TokenSet("access-1", "refresh-1", utcnow() + expires_in))


@pytest.fixture
async def deps(repo, tokens, http):
    await link_user(tokens)
    return Deps(
        repo=repo,
        tokens=tokens,
        http=http,
        company_id=COMPANY_ID,
        ttl=timedelta(minutes=15),
        max_work_record_days=7,
    )
