import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from freee_hr_bot.db.models import Base, Proposal, UserLink, UserPreference


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    # SQLite drops tzinfo; values are always stored as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Repository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "Repository":
        return cls(create_async_engine(url))

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # --- user links -------------------------------------------------------
    async def get_link(self, slack_user_id: str) -> UserLink | None:
        async with self._sessions() as s:
            link = await s.get(UserLink, slack_user_id)
            if link:
                link.expires_at = _aware(link.expires_at)
            return link

    async def save_link(self, link: UserLink) -> None:
        async with self._sessions() as s, s.begin():
            await s.merge(link)

    async def delete_link(self, slack_user_id: str) -> bool:
        async with self._sessions() as s, s.begin():
            link = await s.get(UserLink, slack_user_id)
            if not link:
                return False
            await s.delete(link)
            return True

    # --- proposals --------------------------------------------------------
    async def create_proposal(
        self,
        *,
        slack_user_id: str,
        kind: str,
        payload: dict[str, Any],
        channel_id: str,
        thread_ts: str,
        ttl: timedelta,
    ) -> Proposal:
        now = utcnow()
        p = Proposal(
            id=str(uuid.uuid4()),
            slack_user_id=slack_user_id,
            kind=kind,
            payload=payload,
            status="pending",
            channel_id=channel_id,
            thread_ts=thread_ts,
            created_at=now,
            expires_at=now + ttl,
        )
        async with self._sessions() as s, s.begin():
            s.add(p)
        return p

    async def get_proposal(self, proposal_id: str) -> Proposal | None:
        async with self._sessions() as s:
            p = await s.get(Proposal, proposal_id)
            if p:
                p.expires_at = _aware(p.expires_at)
                p.created_at = _aware(p.created_at)
            return p

    async def update_proposal(self, proposal_id: str, **values: Any) -> None:
        async with self._sessions() as s, s.begin():
            await s.execute(update(Proposal).where(Proposal.id == proposal_id).values(**values))

    async def transition(self, proposal_id: str, from_status: str, to_status: str) -> bool:
        """Atomically move a proposal between states; False if someone else got there first."""
        async with self._sessions() as s, s.begin():
            res: CursorResult[Any] = await s.execute(  # type: ignore[assignment]
                update(Proposal)
                .where(Proposal.id == proposal_id, Proposal.status == from_status)
                .values(status=to_status)
            )
            return res.rowcount == 1

    # --- preferences ------------------------------------------------------
    async def get_pref(self, slack_user_id: str, key: str) -> str | None:
        async with self._sessions() as s:
            pref = await s.get(UserPreference, (slack_user_id, key))
            return pref.value if pref else None

    async def set_pref(self, slack_user_id: str, key: str, value: str) -> None:
        async with self._sessions() as s, s.begin():
            await s.merge(UserPreference(slack_user_id=slack_user_id, key=key, value=value))
