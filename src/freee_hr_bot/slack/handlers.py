import json
import logging
from datetime import date
from typing import Any

from slack_bolt.async_app import AsyncApp

from freee_hr_bot.agent.runner import AgentRunner
from freee_hr_bot.db.models import UserLink
from freee_hr_bot.db.repo import utcnow
from freee_hr_bot.freee.client import FreeeAPIError, NotLinkedError
from freee_hr_bot.freee.hr import get_me
from freee_hr_bot.freee.oauth import FreeeOAuth, OAuthError
from freee_hr_bot.proposals import service
from freee_hr_bot.proposals.executor import MissingRouteError, execute
from freee_hr_bot.proposals.models import load_proposal
from freee_hr_bot.proposals.service import Deps
from freee_hr_bot.slack import blocks as B

logger = logging.getLogger(__name__)

UNLINK_WORDS = {"連携解除", "unlink"}


def register(app: AsyncApp, *, deps: Deps, oauth: FreeeOAuth, agent: AgentRunner) -> None:
    ttl_minutes = int(deps.ttl.total_seconds() // 60)

    async def post_card(client, proposal_id: str) -> None:
        row = await deps.repo.get_proposal(proposal_id)
        if row is None:
            return
        card = B.proposal_card(row.id, load_proposal(row.payload), ttl_minutes)
        res = await client.chat_postMessage(
            channel=row.channel_id, thread_ts=row.thread_ts, blocks=card, text="確認してください"
        )
        await deps.repo.update_proposal(row.id, message_ts=res["ts"])

    # --- DM conversation ---------------------------------------------------
    @app.event("message")
    async def on_message(event: dict[str, Any], client, say) -> None:
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        user = event["user"]
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        text = (event.get("text") or "").strip()

        if text in UNLINK_WORDS:
            removed = await deps.repo.delete_link(user)
            await say(
                text="freee との連携を解除しました。" if removed else "連携されていません。",
                thread_ts=thread_ts,
            )
            return
        if await deps.repo.get_link(user) is None:
            await say(blocks=B.link_prompt(), text="freee と連携してください", thread_ts=thread_ts)
            return

        try:
            turn = await agent.run_turn(
                user_id=user, channel_id=channel, thread_ts=thread_ts, text=text
            )
        except Exception:
            logger.exception("agent turn failed")
            await say(
                text="すみません、処理中にエラーが発生しました。もう一度お試しください。",
                thread_ts=thread_ts,
            )
            return
        if turn.text:
            await say(text=turn.text, thread_ts=thread_ts)
        for pid in turn.proposal_ids:
            await post_card(client, pid)

    # --- linking -----------------------------------------------------------
    @app.action(B.A_LINK_START)
    async def on_link_start(ack, body, client) -> None:
        await ack()
        await client.views_open(
            trigger_id=body["trigger_id"], view=B.link_modal(oauth.authorize_url())
        )

    @app.view(B.V_LINK_SUBMIT)
    async def on_link_submit(ack, body, view, client) -> None:
        await ack()  # close the modal right away; the token exchange may exceed 3 seconds
        user = body["user"]["id"]
        code = view["state"]["values"]["code"]["value"]["value"]
        dm = (await client.conversations_open(users=user))["channel"]["id"]
        try:
            tokens = await oauth.exchange_code(code)
            me = await get_me(deps.http, tokens.access_token)
        except (OAuthError, FreeeAPIError) as e:
            logger.warning("link failed for %s: %s", user, e)
            await client.chat_postMessage(
                channel=dm,
                blocks=B.link_prompt(),
                text="連携に失敗しました。認可コードが正しいか、有効期限(10分)内か確認してもう一度お試しください。",
            )
            return
        company = next((c for c in me.get("companies", []) if c["id"] == deps.company_id), None)
        if company is None or company.get("employee_id") is None:
            await client.chat_postMessage(
                channel=dm,
                text="対象の事業所に従業員として登録されていないため連携できませんでした。管理者に確認してください。",
            )
            return
        link = UserLink(
            slack_user_id=user,
            freee_user_id=me["id"],
            employee_id=company["employee_id"],
            access_token_enc="",
            refresh_token_enc="",
            expires_at=tokens.expires_at,
            created_at=utcnow(),
        )
        await deps.tokens.store(link, tokens)
        await client.chat_postMessage(
            channel=dm,
            text="freee と連携しました 🎉\n例:「昨日 9:00〜19:00 休憩12:00〜13:00」「来週金曜 午前休」",
        )

    # --- proposal buttons --------------------------------------------------
    async def _load_for(body: dict[str, Any], proposal_id: str, client) -> Any:
        """Return the proposal if the clicking user may act on it, else notify and return None."""
        user = body["user"]["id"]
        row = await deps.repo.get_proposal(proposal_id)
        reason = None
        if row is None or row.slack_user_id != user:
            reason = "この操作は実行できません。"
        elif row.status != "pending":
            reason = "この確認は既に処理済みです。"
        elif row.expires_at < utcnow():
            reason = "この確認は有効期限切れです。もう一度依頼してください。"
            if await deps.repo.transition(row.id, "pending", "expired"):
                await _finish(client, row, ["⌛ 有効期限切れ"], body)
        if reason:
            await client.chat_postEphemeral(channel=body["channel"]["id"], user=user, text=reason)
            return None
        return row

    async def _finish(client, row, lines: list[str], body: dict[str, Any]) -> None:
        original = body.get("message", {}).get("blocks") or []
        await client.chat_update(
            channel=row.channel_id,
            ts=row.message_ts or body["message"]["ts"],
            blocks=B.finished_card(original, lines),
            text="\n".join(lines),
        )

    @app.action(B.A_CONFIRM)
    async def on_confirm(ack, body, client) -> None:
        await ack()
        row = await _load_for(body, body["actions"][0]["value"], client)
        if row is None:
            return
        proposal = load_proposal(row.payload)
        if getattr(proposal, "route_id", 0) is None:
            await client.chat_postEphemeral(
                channel=row.channel_id,
                user=row.slack_user_id,
                text="承認経路を選択してから申請してください。",
            )
            return
        # pending -> executing is atomic, so a double click cannot write twice.
        if not await deps.repo.transition(row.id, "pending", "executing"):
            return
        try:
            result = await execute(deps, row.slack_user_id, row.payload)
        except (MissingRouteError, NotLinkedError) as e:
            await deps.repo.transition(row.id, "executing", "pending")
            await client.chat_postEphemeral(
                channel=row.channel_id,
                user=row.slack_user_id,
                text=str(e) or "freee と未連携です。",
            )
            return
        except Exception:
            logger.exception("execution failed")
            await deps.repo.update_proposal(row.id, status="failed")
            await _finish(client, row, ["❌ 予期しないエラーが発生しました"], body)
            return
        await deps.repo.update_proposal(
            row.id, status="done" if result.ok else "failed", result={"lines": result.lines}
        )
        await _finish(client, row, result.lines, body)
        if result.overtime_suggestions:
            suggestions = [
                {
                    "date": s.date.isoformat(),
                    "start": s.start,
                    "end": s.end,
                    "thread_ts": row.thread_ts,
                }
                for s in result.overtime_suggestions
            ]
            await client.chat_postMessage(
                channel=row.channel_id,
                thread_ts=row.thread_ts,
                blocks=B.overtime_suggestion_blocks(suggestions),
                text="残業申請も出しますか?",
            )

    @app.action(B.A_CANCEL)
    async def on_cancel(ack, body, client) -> None:
        await ack()
        row = await _load_for(body, body["actions"][0]["value"], client)
        if row and await deps.repo.transition(row.id, "pending", "cancelled"):
            await _finish(client, row, ["🚫 キャンセルしました"], body)

    @app.action(B.A_ROUTE)
    async def on_route(ack, body, client) -> None:
        await ack()
        block_ids = [b.get("block_id", "") for b in body["message"]["blocks"]]
        proposal_id = next(
            (b.split(":", 1)[1] for b in block_ids if b.startswith("proposal:")), None
        )
        if proposal_id is None:
            return
        row = await _load_for(body, proposal_id, client)
        if row is None:
            return
        payload = dict(row.payload)
        route_id = int(body["actions"][0]["selected_option"]["value"])
        if not any(r["id"] == route_id for r in payload.get("routes", [])):
            return
        payload["route_id"] = route_id
        await deps.repo.update_proposal(row.id, payload=payload)

    # --- overtime follow-up --------------------------------------------------
    @app.action(B.A_OVERTIME_SUGGEST)
    async def on_overtime_suggest(ack, body, client) -> None:
        await ack()
        meta = json.loads(body["actions"][0]["value"])
        meta["channel_id"] = body["channel"]["id"]
        await client.views_open(trigger_id=body["trigger_id"], view=B.overtime_reason_modal(meta))

    @app.view(B.V_OVERTIME_REASON)
    async def on_overtime_reason(ack, body, view, client) -> None:
        await ack()
        user = body["user"]["id"]
        meta = json.loads(view["private_metadata"])
        reason = view["state"]["values"]["reason"]["value"]["value"]
        try:
            row = await service.draft_overtime(
                deps,
                slack_user_id=user,
                day=date.fromisoformat(meta["date"]),
                start=meta["start"],
                end=meta["end"],
                comment=reason,
                channel_id=meta["channel_id"],
                thread_ts=meta["thread_ts"],
            )
        except Exception as e:
            logger.warning("overtime draft failed: %s", e)
            msg = " / ".join(e.messages) if isinstance(e, FreeeAPIError) else str(e)
            await client.chat_postMessage(
                channel=meta["channel_id"],
                thread_ts=meta["thread_ts"],
                text=f"残業申請の案を作れませんでした: {msg}",
            )
            return
        await post_card(client, row.id)
