"""Drives the real ADK Runner with a scripted fake LLM (no Gemini calls)."""

from collections.abc import AsyncGenerator
from datetime import date

import httpx
import respx
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types

from freee_hr_bot.agent.agent import build_agent
from freee_hr_bot.agent.runner import AgentRunner
from tests.conftest import API, EMPLOYEE_ID, USER

TODAY = date(2026, 9, 29)


class ScriptedLlm(BaseLlm):
    """First call: request a tool call. After the tool result comes back: reply with text."""

    model: str = "scripted"
    tool_name: str
    tool_args: dict
    seen_instruction: str = ""
    seen_tool_result: dict | None = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.seen_instruction = str(llm_request.config.system_instruction)
        last = llm_request.contents[-1]
        results = [p.function_response for p in last.parts if p.function_response]
        if results:
            self.seen_tool_result = results[0].response
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="確認カードを出しました")]
                )
            )
            return
        call = types.FunctionCall(name=self.tool_name, args=self.tool_args)
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(function_call=call)])
        )


def make_runner(deps, llm):
    return AgentRunner(build_agent(deps, llm, lambda: TODAY), InMemorySessionService())


@respx.mock
async def test_proposal_tool_flows_back_to_slack_layer(deps):
    respx.get(f"{API}/employees/{EMPLOYEE_ID}/work_records/2026-09-29").mock(
        return_value=httpx.Response(200, json={"work_record_segments": [], "is_editable": True})
    )
    llm = ScriptedLlm(
        tool_name="propose_work_records",
        tool_args={
            "days": [
                {
                    "date": "2026-09-29",
                    "clock_in": "09:00",
                    "clock_out": "19:00",
                    "breaks": [{"start": "12:00", "end": "13:00"}],
                }
            ]
        },
    )
    runner = make_runner(deps, llm)

    turn = await runner.run_turn(
        user_id=USER, channel_id="D1", thread_ts="1.0", text="今日 9:00〜19:00 休憩12-13"
    )

    assert turn.text == "確認カードを出しました"
    assert len(turn.proposal_ids) == 1
    row = await deps.repo.get_proposal(turn.proposal_ids[0])
    assert (row.slack_user_id, row.channel_id, row.thread_ts, row.status) == (
        USER,
        "D1",
        "1.0",
        "pending",
    )
    assert "2026-09-29(火)" in llm.seen_instruction
    # nothing was written to freee
    assert not any(c.request.method == "PUT" for c in respx.calls)


async def test_validation_error_is_returned_to_the_model(deps):
    llm = ScriptedLlm(
        tool_name="propose_work_records",
        tool_args={"days": [{"date": "2026-10-05", "clock_in": "09:00", "clock_out": "18:00"}]},
    )
    turn = await make_runner(deps, llm).run_turn(
        user_id=USER, channel_id="D1", thread_ts="2.0", text="来週月曜 9-18"
    )
    assert turn.proposal_ids == []
    assert llm.seen_tool_result["status"] == "error"
    assert "未来" in llm.seen_tool_result["message"]
