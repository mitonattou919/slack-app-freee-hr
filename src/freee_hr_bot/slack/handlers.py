import asyncio
import json
import logging
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from freee_hr_bot.agent.runner import AgentRunner
from freee_hr_bot.db.models import Proposal, UserLink
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

# Status reactions on the thread's root message; exactly one is shown at a time.
R_WORKING = "eyes"
R_DONE = "white_check_mark"
R_FAILED = "x"
STATUS_REACTIONS = (R_WORKING, R_DONE, R_FAILED)
MAX_TRACKED_THREADS = 1000


def thread_reaction(
    proposals: Sequence[Proposal], *, busy: bool = False, failed: bool = False
) -> str:
    """Working while a turn is running or a card is open; done only if every proposal succeeded.

    A card is open only while its owner can still act on it: posted, pending and within its TTL.
    A proposal that never got a card, or that failed, was cancelled or expired, counts as failed.
    """
    now = utcnow()

    def is_open(p: Proposal) -> bool:
        if p.status == "pending":
            return p.message_ts is not None and p.expires_at >= now
        return p.status == "executing"

    if busy or any(is_open(p) for p in proposals):
        return R_WORKING
    if failed or any(p.status != "done" for p in proposals):
        return R_FAILED
    return R_DONE


async def set_reaction(client: Any, channel: str, ts: str, name: str, stale: Sequence[str]) -> bool:
    """Add `name` and remove the `stale` status reactions; False if any call failed.

    Never raises: the reaction is only a hint.
    """

    async def call(method: Any, reaction: str) -> bool:
        try:
            await method(channel=channel, timestamp=ts, name=reaction)
        except SlackApiError as e:
            if e.response.get("error") not in ("already_reacted", "no_reaction"):
                logger.warning("reaction %s failed: %s", reaction, e.response.get("error"))
                return False
        except Exception:
            logger.warning("reaction %s failed", reaction, exc_info=True)
            return False
        return True

    if not await call(client.reactions_add, name):
        return False
    return all(await asyncio.gather(*(call(client.reactions_remove, r) for r in stale)))


Sweep = Callable[..., Awaitable[None]]


def register(app: AsyncApp, *, deps: Deps, oauth: FreeeOAuth, agent: AgentRunner) -> Sweep:
    """Register the listeners. Returns the sweep that closes cards nobody will act on any more."""
    ttl_minutes = int(deps.ttl.total_seconds() // 60)
    # Reaction state per thread (channel, thread_ts). In memory only: after a restart nothing is
    # running, and an unknown `shown` just costs two extra remove calls on the next change.
    running: Counter[tuple[str, str]] = Counter()
    shown: OrderedDict[tuple[str, str], str] = OrderedDict()  # least recently set first
    reaction_locks: dict[tuple[str, str], asyncio.Lock] = {}
    lock_users: Counter[tuple[str, str]] = Counter()

    @asynccontextmanager
    async def reaction_lock(key: tuple[str, str]) -> AsyncIterator[None]:
        """Per-thread lock that is dropped once nobody holds or waits for it."""
        lock_users[key] += 1
        try:
            async with reaction_locks.setdefault(key, asyncio.Lock()):
                yield
        finally:
            lock_users[key] -= 1
            if not lock_users[key]:
                del lock_users[key]
                del reaction_locks[key]

    async def post_card(client, proposal_id: str) -> None:
        row = await deps.repo.get_proposal(proposal_id)
        if row is None:
            return
        card = B.proposal_card(row.id, load_proposal(row.payload), ttl_minutes)
        res = await client.chat_postMessage(
            channel=row.channel_id, thread_ts=row.thread_ts, blocks=card, text="確認してください"
        )
        await deps.repo.update_proposal(row.id, message_ts=res["ts"])

    async def refresh_reaction(
        client, channel: str, thread_ts: str, *, failed: bool = False, fresh: bool = False
    ) -> None:
        """Recompute the thread's status and show it. `fresh`: the root message has just arrived."""
        key = (channel, thread_ts)
        # Compute and apply under one lock, so overlapping handlers cannot apply a stale status last.
        async with reaction_lock(key):
            try:
                rows = await deps.repo.list_thread_proposals(channel, thread_ts)
            except Exception:
                logger.warning("could not load proposals for reaction", exc_info=True)
                return
            name = thread_reaction(rows, busy=running[key] > 0, failed=failed)
            previous = shown.get(key)
            if previous == name:
                return
            if previous:
                stale = [previous]
            else:
                stale = [] if fresh else [r for r in STATUS_REACTIONS if r != name]
            if await set_reaction(client, channel, thread_ts, name, stale):
                shown[key] = name
                shown.move_to_end(key)
                while len(shown) > MAX_TRACKED_THREADS:
                    shown.popitem(last=False)
            else:
                # Something may be left over: forget what is shown so the next change clears all.
                shown.pop(key, None)

    async def sweep(client, *, recover: bool = False) -> None:
        """Close expired cards. `recover` (startup only) also fails proposals left executing."""
        now = utcnow()
        stale = [
            (row, "expired", "⌛ 有効期限切れ")
            for row in await deps.repo.list_proposals_by_status("pending")
            if row.expires_at < now
        ]
        if recover:
            # A single instance runs, so nothing can still be executing when the process starts.
            stale += [
                (
                    row,
                    "failed",
                    "⚠️ 再起動で処理が中断されました。freee で登録状況を確認してください",
                )
                for row in await deps.repo.list_proposals_by_status("executing")
            ]
        for row, status, line in stale:
            if not await deps.repo.transition(row.id, row.status, status):
                continue
            if row.message_ts:
                card = B.proposal_card(row.id, load_proposal(row.payload), ttl_minutes)
                try:
                    await client.chat_update(
                        channel=row.channel_id,
                        ts=row.message_ts,
                        blocks=B.finished_card(card, [line]),
                        text=line,
                    )
                except Exception:
                    logger.warning("could not close card %s", row.id, exc_info=True)
            await refresh_reaction(client, row.channel_id, row.thread_ts)

    # --- DM conversation ---------------------------------------------------
    @app.event("message")
    async def on_message(event: dict[str, Any], client, say) -> None:
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        user = event["user"]
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        fresh = "thread_ts" not in event
        text = (event.get("text") or "").strip()

        if text in UNLINK_WORDS:
            removed = await deps.repo.delete_link(user)
            await say(
                text="freee との連携を解除しました。" if removed else "連携されていません。",
                thread_ts=thread_ts,
            )
            await refresh_reaction(client, channel, thread_ts, fresh=fresh)
            return
        if await deps.repo.get_link(user) is None:
            await say(blocks=B.link_prompt(), text="freee と連携してください", thread_ts=thread_ts)
            await refresh_reaction(client, channel, thread_ts, fresh=fresh)
            return

        key = (channel, thread_ts)
        running[key] += 1
        # Acknowledge in the background so the reaction does not hold up the agent.
        acknowledged = asyncio.create_task(
            refresh_reaction(client, channel, thread_ts, fresh=fresh)
        )
        failed = True
        try:
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
            failed = False
        finally:
            await acknowledged
            running[key] -= 1
            if not running[key]:
                del running[key]
            await refresh_reaction(client, channel, thread_ts, failed=failed)

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
                await refresh_reaction(client, row.channel_id, row.thread_ts)
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
            await refresh_reaction(client, row.channel_id, row.thread_ts)
            return
        await deps.repo.update_proposal(
            row.id, status="done" if result.ok else "failed", result={"lines": result.lines}
        )
        await _finish(client, row, result.lines, body)
        await refresh_reaction(client, row.channel_id, row.thread_ts)
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
            await refresh_reaction(client, row.channel_id, row.thread_ts)

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
        await refresh_reaction(client, row.channel_id, row.thread_ts)

    return sweep
