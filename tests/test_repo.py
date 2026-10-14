import asyncio
from datetime import timedelta

from freee_hr_bot.db.repo import utcnow
from tests.conftest import USER


async def new_proposal(repo, ttl=timedelta(minutes=15)):
    return await repo.create_proposal(
        slack_user_id=USER,
        kind="work_records",
        payload={"kind": "work_records", "entries": []},
        channel_id="D1",
        thread_ts="1.0",
        ttl=ttl,
    )


async def test_transition_only_from_expected_state(repo):
    p = await new_proposal(repo)
    assert await repo.transition(p.id, "pending", "executing")
    assert not await repo.transition(p.id, "pending", "executing")
    assert not await repo.transition(p.id, "pending", "cancelled")
    assert (await repo.get_proposal(p.id)).status == "executing"


async def test_concurrent_transition_has_single_winner(repo):
    p = await new_proposal(repo)
    wins = await asyncio.gather(*(repo.transition(p.id, "pending", "executing") for _ in range(10)))
    assert wins.count(True) == 1


async def test_datetimes_come_back_timezone_aware(repo):
    p = await new_proposal(repo)
    got = await repo.get_proposal(p.id)
    assert got.expires_at.tzinfo is not None
    assert got.expires_at > utcnow()


async def test_update_proposal_and_missing(repo):
    p = await new_proposal(repo)
    await repo.update_proposal(p.id, message_ts="9.9", payload={"kind": "x"})
    got = await repo.get_proposal(p.id)
    assert (got.message_ts, got.payload) == ("9.9", {"kind": "x"})
    assert await repo.get_proposal("nope") is None


async def test_preferences_upsert(repo):
    assert await repo.get_pref(USER, "k") is None
    await repo.set_pref(USER, "k", "1")
    await repo.set_pref(USER, "k", "2")
    assert await repo.get_pref(USER, "k") == "2"


async def test_link_delete(repo, tokens):
    from tests.conftest import link_user

    assert not await repo.delete_link(USER)
    await link_user(tokens)
    assert await repo.delete_link(USER)
    assert await repo.get_link(USER) is None


async def test_list_thread_proposals_is_scoped_to_the_thread(repo):
    mine = await new_proposal(repo)
    other = await repo.create_proposal(
        slack_user_id=USER,
        kind="work_records",
        payload={"kind": "work_records", "entries": []},
        channel_id="D1",
        thread_ts="2.0",
        ttl=timedelta(minutes=15),
    )
    rows = await repo.list_thread_proposals("D1", "1.0")
    assert [r.id for r in rows] == [mine.id]
    assert other.id not in {r.id for r in rows}
    assert rows[0].expires_at.tzinfo is not None


async def test_list_proposals_by_status(repo):
    pending = await new_proposal(repo)
    executing = await new_proposal(repo)
    await repo.transition(executing.id, "pending", "executing")
    assert [r.id for r in await repo.list_proposals_by_status("pending")] == [pending.id]
    assert [r.id for r in await repo.list_proposals_by_status("executing")] == [executing.id]


async def test_create_all_adds_thread_index_to_existing_table(repo):
    async def index_names() -> set[str]:
        async with repo.engine.connect() as conn:
            rows = await conn.exec_driver_sql("PRAGMA index_list(proposals)")
            return {r[1] for r in rows}

    async with repo.engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX ix_proposals_thread")  # a DB from before the index
    assert "ix_proposals_thread" not in await index_names()
    await repo.create_all()
    assert "ix_proposals_thread" in await index_names()
