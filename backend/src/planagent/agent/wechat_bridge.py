"""Glue between the agent and the WeChat runtime.

Builds the `on_message` handler that `wechat.runtime.run_polling_loop` (and
the multi-session `run_all_sessions`) expects. The returned callable
resolves each inbound through the orchestrator and dispatches outbound
text via a session-scoped ClawBotClient sender.

PR-F makes the bridge session-aware: one handler per BotSession. Since
ClawBot only has 1:1 conversations on personal WeChat, every send must be
addressed to the session's `wechat_user_id`, threaded on the inbound's
`context_token`, with no `group_id` on the wire — but the orchestrator
still operates against the logical group (one DB row shared by all
sessions in the same logical group).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from planagent.agent.service import AgentService
from planagent.agent.tools import WechatSend
from planagent.db.models import BotSession, GroupContext, GroupMember
from planagent.llm.deepseek import DeepSeekClient
from planagent.wechat import protocol as wxp
from planagent.wechat.client import ClawBotClient
from planagent.wechat.protocol import InboundMessage
from planagent.wechat.runtime import SessionPollSpec


def wechat_send_for(
    *,
    client: ClawBotClient,
    bot_token: str,
    to_user_id: str,
    context_token: str,
    group_id: str | None = None,
) -> WechatSend:
    """Return a wechat_send closure bound to a specific outbound destination.

    `group_id` defaults to None (personal WeChat ClawBot only has 1:1 chats),
    but legacy / non-ClawBot transports may thread a real group id through;
    the `build_handler` shim passes the inbound's group_id so replies stay
    in the original group thread.
    """

    async def _send(text: str) -> None:
        if not text or not to_user_id:
            return
        await client.send_text(
            bot_token,
            to_user_id=to_user_id,
            text=text,
            context_token=context_token,
            group_id=group_id,
        )

    return _send


async def _stamp_session_inbound(
    session_factory: async_sessionmaker,
    *,
    session_db_id: str,
    msg: InboundMessage,
) -> tuple[str | None, str | None]:
    """Update BotSession timestamps + learn wechat_user_id. Returns (user_id, wechat_group_id)."""
    now_utc = datetime.now(UTC)
    async with session_factory() as session:
        res = await session.execute(
            select(BotSession)
            .where(BotSession.id == session_db_id)
            .options(selectinload(BotSession.group))
        )
        bs = res.scalar_one_or_none()
        if bs is None:
            return None, None
        bs.last_inbound_at = now_utc
        if msg.context_token:
            bs.last_context_token = msg.context_token
        # Capture wechat_user_id + bot_user_id from inbound. Inbound is
        # ALWAYS the authoritative source — if bootstrap seeded a wrong id
        # from a stale roster / cred filename, real traffic self-heals by
        # overwriting it. We log the correction so misconfig is visible.
        from_user = wxp.sender_id(msg) or msg.from_user_id
        if from_user and bs.wechat_user_id != from_user:
            if bs.wechat_user_id:
                import logging as _log
                _log.getLogger(__name__).warning(
                    "bot_session %s: correcting wechat_user_id %s -> %s "
                    "(bootstrap seed differed from real inbound)",
                    bs.id,
                    bs.wechat_user_id,
                    from_user,
                )
            bs.wechat_user_id = from_user
            # Fold the same value into the matching GroupMember row so the
            # orchestrator's snapshot sees it immediately. PR-G preferred
            # match is by wechat_user_id (pre-filled for known humans);
            # legacy fallback is by cred.name in display_name.
            mres = await session.execute(
                select(GroupMember).where(
                    GroupMember.group_id == bs.group_id,
                    GroupMember.wechat_user_id == from_user,
                )
            )
            member = mres.scalar_one_or_none()
            if member is None:
                mres2 = await session.execute(
                    select(GroupMember).where(
                        GroupMember.group_id == bs.group_id,
                        GroupMember.display_name == bs.name,
                    )
                )
                member = mres2.scalar_one_or_none()
            if member is not None and member.wechat_user_id != from_user:
                member.wechat_user_id = from_user
        if bs.bot_user_id is None and msg.to_user_id:
            bs.bot_user_id = msg.to_user_id
        group: GroupContext | None = bs.group
        wechat_group_id = group.wechat_group_id if group is not None else None
        await session.commit()
        return bs.wechat_user_id, wechat_group_id


def build_handler_for_session(
    *,
    deepseek: DeepSeekClient,
    session_factory: async_sessionmaker,
    client: ClawBotClient,
    bot_session: BotSession | SessionPollSpec,
    bot_token: str | None = None,
    session_db_id: str | None = None,
) -> Callable[[InboundMessage], Awaitable[None]]:
    """Construct an on_message handler bound to a single BotSession.

    Accepts either a live `BotSession` ORM row or a `SessionPollSpec`. The
    latter is used by the multi-session runtime, which only carries the
    minimum needed for polling; the handler re-loads the full row from the
    DB on every inbound so it sees fresh state (wechat_user_id filled in
    previous turns, etc.).
    """
    resolved_db_id = session_db_id or getattr(bot_session, "id", None) or getattr(
        bot_session, "session_id", None
    )
    resolved_token = bot_token or getattr(bot_session, "bot_token", None)
    if not resolved_db_id or not resolved_token:
        raise ValueError("bot_session must expose id+bot_token or be supplied explicitly")

    async def _on_message(msg: InboundMessage) -> None:
        # Fill session state + learn wechat_user_id on first inbound. The
        # orchestrator then runs against the shared logical group.
        _, wechat_group_id = await _stamp_session_inbound(
            session_factory,
            session_db_id=resolved_db_id,
            msg=msg,
        )
        if wechat_group_id is None:
            return

        # ClawBot 1:1 messages don't carry group_id; synthesize one from the
        # session's logical group so the orchestrator (which keys on
        # `wechat_group_id`) can thread members into a single chat history.
        synthetic = msg.model_copy(update={"group_id": wechat_group_id})

        to_user_id = wxp.sender_id(msg) or msg.from_user_id or ""
        send = wechat_send_for(
            client=client,
            bot_token=resolved_token,
            to_user_id=to_user_id,
            context_token=msg.context_token or "",
        )
        service = AgentService(
            deepseek=deepseek,
            session_factory=session_factory,
            wechat_send=send,
        )
        await service.as_handler()(synthetic)

        # Stamp outbound if we actually said anything. (We conservatively
        # stamp on handler exit; tracking whether send was called adds
        # little over "a turn happened".)
        async with session_factory() as session:
            bs = await session.get(BotSession, resolved_db_id)
            if bs is not None:
                bs.last_outbound_at = datetime.now(UTC)
                await session.commit()

    return _on_message


# --- Back-compat shim for PR-D/PR-E callers -----------------------------------


def build_handler(
    *,
    deepseek: DeepSeekClient,
    session_factory: async_sessionmaker,
    client: ClawBotClient,
    bot_token: str,
) -> Callable[[InboundMessage], Awaitable[None]]:
    """Legacy single-session handler. Kept so existing integration surfaces
    (and tests written against PR-D) keep working. For multi-session, use
    `build_handler_for_session` directly.
    """

    async def _on_message(msg: InboundMessage) -> None:
        to_user_id = wxp.sender_id(msg) or msg.from_user_id or ""
        # Preserve the inbound's group_id on the outbound so legacy/group
        # traffic threads correctly. Personal WeChat ClawBot leaves this
        # None; enterprise WeChat / QQ adapters fill it.
        send = wechat_send_for(
            client=client,
            bot_token=bot_token,
            to_user_id=to_user_id,
            context_token=msg.context_token or "",
            group_id=wxp.group_id(msg),
        )
        service = AgentService(
            deepseek=deepseek,
            session_factory=session_factory,
            wechat_send=send,
        )
        await service.as_handler()(msg)

    return _on_message


__all__ = [
    "build_handler",
    "build_handler_for_session",
    "wechat_send_for",
]
