import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from google.adk.sessions import DatabaseSessionService
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from freee_hr_bot.agent.agent import build_agent
from freee_hr_bot.agent.runner import AgentRunner
from freee_hr_bot.config import get_settings
from freee_hr_bot.crypto import TokenCipher
from freee_hr_bot.db.repo import Repository
from freee_hr_bot.freee.client import TokenProvider
from freee_hr_bot.freee.oauth import FreeeOAuth
from freee_hr_bot.proposals.service import Deps
from freee_hr_bot.slack.handlers import register


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # Gemini via Google AI Studio (not Vertex AI)
    os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    repo = Repository.from_url(settings.db_url)
    await repo.create_all()

    async with httpx.AsyncClient(timeout=15) as http:
        oauth = FreeeOAuth(settings.freee_client_id, settings.freee_client_secret, http)
        deps = Deps(
            repo=repo,
            tokens=TokenProvider(repo, TokenCipher(settings.fernet_key), oauth),
            http=http,
            company_id=settings.freee_company_id,
            ttl=timedelta(minutes=settings.proposal_ttl_minutes),
            max_work_record_days=settings.max_work_record_days,
        )

        def today():
            return datetime.now(settings.zone).date()

        agent = AgentRunner(
            build_agent(deps, settings.gemini_model, today),
            DatabaseSessionService(db_url=settings.db_url),
        )
        app = AsyncApp(token=settings.slack_bot_token)
        register(app, deps=deps, oauth=oauth, agent=agent)
        await AsyncSocketModeHandler(app, settings.slack_app_token).start_async()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
