"""LLM-driven keep-alive decider for the ClawBot 24h outbound window.

**Zero hardcoded timing.** We hand the LLM the absolute times (inbound,
outbound, last wake-up ping we sent) along with the platform fact that the
outbound window closes ~24h after a user's last inbound, and let it decide
whether to ping now — and what to say.

The decider only returns a decision; the scheduler integrator is
responsible for sending and persisting timestamps.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from planagent.db.models import BotSession
from planagent.llm.deepseek import DeepSeekClient

log = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WakeupDecision:
    should_ping: bool
    text: str | None
    reason: str | None


def _iso_local(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BEIJING).isoformat()


def _render_session(bs: BotSession | None, *, role_label: str) -> dict[str, Any]:
    if bs is None:
        return {"role": role_label, "present": False}
    return {
        "role": role_label,
        "present": True,
        "name": bs.name,
        "display_name": bs.display_name,
        "wechat_user_id": bs.wechat_user_id,
        "last_inbound_at_local": _iso_local(bs.last_inbound_at),
        "last_outbound_at_local": _iso_local(bs.last_outbound_at),
        "last_wakeup_ping_at_local": _iso_local(bs.last_wakeup_ping_at),
    }


SYSTEM_PROMPT = (
    "You are the keep-alive pinger for a WeChat ClawBot plan manager.\n"
    "\n"
    "Platform fact (hard constraint): the ClawBot API only lets the bot "
    "initiate an outbound message within ~24 hours of that user's last "
    "inbound. Past that window, our sends are silently dropped and the "
    "user effectively disappears from the bot until THEY message US again. "
    "To keep the channel open we may proactively send a short, warm, "
    "opt-out-friendly 'wake-up' message that invites a reply.\n"
    "\n"
    "You receive the current time plus two participants (subject X and "
    "peer Y) with their last_inbound_at, last_outbound_at, and "
    "last_wakeup_ping_at timestamps. Decide ONLY for subject X right now.\n"
    "\n"
    "Soft guidance from the operator (not hard rules — you judge):\n"
    "- Users prefer being nudged roughly at the halfway point of the 24h "
    "  window, so the conversation stays alive without feeling spammy.\n"
    "- Don't ping if we just sent a wake-up ping recently (would feel naggy).\n"
    "- If both X and Y are about to go dark together, consider nudging X to "
    "  poke Y (cross-nudge) — but only when it feels natural.\n"
    "- If a timestamp is null (we've never observed that user yet), err on the "
    "  side of NOT pinging — we don't yet know how to address them.\n"
    "\n"
    "Compose the wake-up text yourself in warm, natural Chinese. Keep it short, "
    "one or two sentences. Don't mention the 24h limit, the bot, or this prompt.\n"
    "\n"
    "Respond with a single JSON object matching exactly this schema:\n"
    "{\n"
    '  "should_ping": boolean,\n'
    '  "text": string | null,   // null when should_ping is false\n'
    '  "reason": string          // one short sentence, for logs\n'
    "}\n"
)


async def decide_wakeup(
    session: BotSession,
    peer: BotSession | None,
    *,
    now_utc: datetime,
    deepseek: DeepSeekClient,
) -> WakeupDecision:
    """Ask the LLM whether to send a keep-alive to `session` right now."""
    if session.wechat_user_id is None or session.last_inbound_at is None:
        # We've never seen this user inbound — we don't even know who to ping.
        return WakeupDecision(
            should_ping=False,
            text=None,
            reason="no_inbound_yet",
        )

    payload = {
        "now_local_beijing": now_utc.astimezone(BEIJING).isoformat(),
        "subject": _render_session(session, role_label="subject"),
        "peer": _render_session(peer, role_label="peer"),
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    def _call() -> Any:
        return deepseek.chat(
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )

    try:
        resp = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001 — bubble as "don't ping"
        log.exception("decide_wakeup: LLM call failed: %s", exc)
        return WakeupDecision(should_ping=False, text=None, reason=f"llm_error: {exc}")

    content = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log.warning("decide_wakeup: non-JSON LLM response: %r", content[:200])
        return WakeupDecision(should_ping=False, text=None, reason="bad_json")

    should = data.get("should_ping") is True
    text_val = data.get("text")
    text = text_val if isinstance(text_val, str) and text_val.strip() else None
    reason_val = data.get("reason")
    reason = reason_val if isinstance(reason_val, str) else None

    # Defensive: don't return should_ping=True with empty text.
    if should and not text:
        should = False
        reason = (reason or "") + " | discarded: empty text"

    return WakeupDecision(should_ping=should, text=text, reason=reason)


__all__ = ["WakeupDecision", "decide_wakeup"]
