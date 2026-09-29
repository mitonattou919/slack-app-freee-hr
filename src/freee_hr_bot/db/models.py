from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserLink(Base):
    """Slack user ⇔ freee employee link with encrypted OAuth tokens."""

    __tablename__ = "user_links"

    slack_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    freee_user_id: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[int] = mapped_column(Integer)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Proposal(Base):
    """A write action drafted by the agent, executed only after the owner confirms in Slack."""

    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    # pending -> executing -> done | failed ; pending -> cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending")
    channel_id: Mapped[str] = mapped_column(String(32))
    thread_ts: Mapped[str] = mapped_column(String(32))
    message_ts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserPreference(Base):
    __tablename__ = "user_preferences"

    slack_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
