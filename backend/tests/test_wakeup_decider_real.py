"""Real DeepSeek tests for the keep-alive wake-up decider — NO mocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from planagent.db.models import BotSession, GroupContext
from planagent.llm.deepseek import DeepSeekClient
from planagent.scheduler.wakeup import decide_wakeup


@pytest.fixture(scope="module")
def deepseek() -> DeepSeekClient:
    return DeepSeekClient()


def _session(
    *,
    name: str,
    wechat_user_id: str | None = "wx-user",
    last_inbound_at: datetime | None = None,
    last_wakeup_ping_at: datetime | None = None,
    display_name: str | None = None,
    group: GroupContext | None = None,
) -> BotSession:
    bs = BotSession(
        group_id=(group.id if group else "g1"),
        name=name,
        wechat_user_id=wechat_user_id,
        bot_token="tok",
        display_name=display_name,
        last_inbound_at=last_inbound_at,
        last_wakeup_ping_at=last_wakeup_ping_at,
    )
    bs.id = f"bs-{name}"
    bs.group = group
    return bs


def _looks_chinese(text: str) -> bool:
    """At least one CJK ideograph present (loose content check)."""
    return any("一" <= ch <= "鿿" for ch in text)


@pytest.mark.real_api
async def test_wakeup_when_user_silent_near_window_close(deepseek: DeepSeekClient) -> None:
    now = datetime.now(UTC)
    # Silent 20 hours — close to the 24h cutoff. LLM should typically
    # recommend pinging.
    subject = _session(
        name="peng",
        display_name="鹏鹏",
        last_inbound_at=now - timedelta(hours=20),
    )
    peer = _session(
        name="chenchen",
        display_name="辰辰",
        wechat_user_id="wx-user-chen",
        last_inbound_at=now - timedelta(minutes=30),  # freshly active
    )
    dec = await decide_wakeup(subject, peer, now_utc=now, deepseek=deepseek)

    assert isinstance(dec.should_ping, bool)
    if dec.should_ping:
        assert isinstance(dec.text, str)
        assert len(dec.text.strip()) > 5
        assert _looks_chinese(dec.text), f"expected Chinese text, got: {dec.text!r}"
    # Even if the LLM said False, reason must be present for logging.
    assert dec.reason is not None


@pytest.mark.real_api
async def test_wakeup_skips_when_user_just_spoke(deepseek: DeepSeekClient) -> None:
    now = datetime.now(UTC)
    subject = _session(
        name="peng",
        display_name="鹏鹏",
        last_inbound_at=now - timedelta(minutes=10),
    )
    peer = _session(
        name="chenchen",
        display_name="辰辰",
        wechat_user_id="wx-user-chen",
        last_inbound_at=now - timedelta(minutes=5),
    )
    dec = await decide_wakeup(subject, peer, now_utc=now, deepseek=deepseek)
    # LLM has absolute discretion; we only assert the obviously-bad case
    # doesn't happen: a just-active user should not be pinged.
    assert dec.should_ping is False, (
        f"user active 10min ago should NOT be pinged; got reason={dec.reason!r} "
        f"text={dec.text!r}"
    )


@pytest.mark.real_api
async def test_wakeup_both_silent_both_at_risk(deepseek: DeepSeekClient) -> None:
    now = datetime.now(UTC)
    subject = _session(
        name="peng",
        display_name="鹏鹏",
        last_inbound_at=now - timedelta(hours=22, minutes=30),
    )
    peer = _session(
        name="chenchen",
        display_name="辰辰",
        wechat_user_id="wx-user-chen",
        last_inbound_at=now - timedelta(hours=22),
    )
    dec = await decide_wakeup(subject, peer, now_utc=now, deepseek=deepseek)
    # Not strictly asserting True (the LLM could choose either), but when
    # True we expect a real Chinese sentence back.
    if dec.should_ping:
        assert isinstance(dec.text, str) and dec.text.strip()
        assert _looks_chinese(dec.text)


async def test_wakeup_no_inbound_yet_skips_without_calling_llm() -> None:
    """Guard: when we've never observed the user, we can't address them —
    short-circuit to should_ping=False without hitting the API."""

    class _BoomDeepSeek:
        def chat(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            raise AssertionError("LLM should not be called when wechat_user_id is None")

    subject = _session(
        name="peng",
        wechat_user_id=None,
        last_inbound_at=None,
    )
    dec = await decide_wakeup(
        subject,
        peer=None,
        now_utc=datetime.now(UTC),
        deepseek=_BoomDeepSeek(),  # type: ignore[arg-type]
    )
    assert dec.should_ping is False
    assert dec.text is None
    assert dec.reason == "no_inbound_yet"
