"""Stable per-deployment constants for the two-human logical group.

PR-G hardcodes the roster for Peng + Chenchen: we have exactly one deployment
and exactly two known humans. Putting the IDs here (one place) lets the bridge
bootstrap pre-fill GroupMember rows with real display names before the first
inbound arrives, so the agent prompt can address users by name from turn one
instead of revealing a raw wechat_user_id.

When a second deployment exists (or a third user), replace this with a DB- or
config-driven roster.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownHuman:
    wechat_user_id: str
    display_name: str
    cred_name: str  # matches BotSession.name / ~/.planagent/{name}.json


PENG = KnownHuman(
    wechat_user_id="o9cq807dznGxf81R2JoVl2pEx_T0@im.wechat",
    display_name="鹏鹏",
    cred_name="peng",
)
CHENCHEN = KnownHuman(
    wechat_user_id="o9cq80ydQIR4ZaYl6vXvDp_4KklQ@im.wechat",
    display_name="辰辰",
    cred_name="chenchen",
)

KNOWN_HUMANS: tuple[KnownHuman, ...] = (PENG, CHENCHEN)

# Map wechat_user_id → display_name for prompt rendering.
DISPLAY_NAME_BY_WECHAT_USER_ID: dict[str, str] = {
    h.wechat_user_id: h.display_name for h in KNOWN_HUMANS
}


def display_name_for(wechat_user_id: str | None) -> str | None:
    if not wechat_user_id:
        return None
    return DISPLAY_NAME_BY_WECHAT_USER_ID.get(wechat_user_id)


def peer_wechat_user_id(speaker_wechat_user_id: str | None) -> str | None:
    """Return the other known human's wechat_user_id, or None if ambiguous."""
    if not speaker_wechat_user_id:
        return None
    for h in KNOWN_HUMANS:
        if h.wechat_user_id != speaker_wechat_user_id:
            return h.wechat_user_id
    return None


__all__ = [
    "CHENCHEN",
    "DISPLAY_NAME_BY_WECHAT_USER_ID",
    "KNOWN_HUMANS",
    "PENG",
    "KnownHuman",
    "display_name_for",
    "peer_wechat_user_id",
]
