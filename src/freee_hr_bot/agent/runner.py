import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

from freee_hr_bot.agent.tools import PROPOSAL_KEY

APP_NAME = "freee_hr_bot"


@dataclass
class TurnResult:
    text: str
    proposal_ids: list[str] = field(default_factory=list)


class AgentRunner:
    """One ADK session per Slack DM thread; turns within a thread are serialized."""

    def __init__(self, agent: LlmAgent, sessions: BaseSessionService) -> None:
        self.sessions = sessions
        self.runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run_turn(
        self, *, user_id: str, channel_id: str, thread_ts: str, text: str
    ) -> TurnResult:
        session_id = f"{channel_id}:{thread_ts}"
        async with self._locks[session_id]:
            session = await self.sessions.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
            if session is None:
                await self.sessions.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_id,
                    state={"channel_id": channel_id, "thread_ts": thread_ts},
                )
            message = types.Content(role="user", parts=[types.Part(text=text)])
            result = TurnResult(text="")
            async for event in self.runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                for fr in event.get_function_responses():
                    pid = (fr.response or {}).get(PROPOSAL_KEY)
                    if pid:
                        result.proposal_ids.append(pid)
                if event.is_final_response() and event.content and event.content.parts:
                    result.text = "".join(p.text or "" for p in event.content.parts).strip()
            return result
