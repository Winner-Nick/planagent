"""Multi-session credentials loader + DB bootstrap.

Scans `~/.planagent/*.json` for one-file-per-bot credentials and upserts a
`BotSession` row for each. The legacy single-user `credentials.json` (PR-B)
and non-credential junk (MEMORY.md, README, etc.) are ignored.

Each credential file looks like::

    {
      "bot_token": "aa55777501ab@im.bot:06000036d16ea6bdae75ab36455570853fbb8f",
      "baseurl": "https://ilinkai.weixin.qq.com"
    }

The user's `wechat_user_id` is NOT in the file — ClawBot only reveals it on
the first inbound message. Bootstrap therefore inserts rows with a NULL
wechat_user_id; runtime fills the column once a message comes in.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from planagent.db.models import BotSession, GroupContext, GroupMember

log = logging.getLogger(__name__)

CRED_DIR = Path.home() / ".planagent"
# Stable logical-group id used across all sessions until we support more than
# one fake group. Keeping it hard-coded as a sentinel lets bootstrap be
# idempotent on re-run.
LOGICAL_GROUP_ID = "logical_group_v1"

# Filenames to skip — not bot credentials.
_IGNORED_STEMS = {"credentials", "MEMORY", "README"}


@dataclass(frozen=True)
class SessionCredential:
    name: str  # filename stem, e.g. "peng"
    bot_token: str
    baseurl: str | None


def _parse_cred_file(path: Path) -> SessionCredential | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("skipping cred file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("bot_token")
    if not isinstance(token, str) or not token:
        return None
    baseurl = data.get("baseurl")
    if not isinstance(baseurl, str) or not baseurl:
        baseurl = None
    return SessionCredential(name=path.stem, bot_token=token, baseurl=baseurl)


def load_all_sessions(cred_dir: Path | None = None) -> list[SessionCredential]:
    """Return every `{name}.json` credential file in `~/.planagent/`.

    Sorted by name for deterministic iteration order (matters for tests that
    observe which session is "first" in a scheduler tick).
    """
    d = cred_dir or CRED_DIR
    if not d.is_dir():
        return []
    out: list[SessionCredential] = []
    for path in sorted(d.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        if path.stem in _IGNORED_STEMS:
            continue
        cred = _parse_cred_file(path)
        if cred is not None:
            out.append(cred)
    return out


async def sync_sessions_to_db(
    session_factory: async_sessionmaker,
    creds: list[SessionCredential],
    *,
    logical_group_id: str = LOGICAL_GROUP_ID,
    group_name: str = "planagent logical group",
) -> list[str]:
    """Create the logical GroupContext + one BotSession per credential.

    Returns the list of BotSession.id values in order.

    Idempotent: existing sessions (matched by `name`) have their `bot_token`
    and `baseurl` refreshed from disk but other fields (display_name,
    wechat_user_id, timestamps) are preserved.
    """
    async with session_factory() as session:
        res = await session.execute(
            select(GroupContext).where(GroupContext.wechat_group_id == logical_group_id)
        )
        group = res.scalar_one_or_none()
        if group is None:
            group = GroupContext(wechat_group_id=logical_group_id, name=group_name)
            session.add(group)
            await session.flush()

        out_ids: list[str] = []
        for cred in creds:
            bres = await session.execute(
                select(BotSession).where(BotSession.name == cred.name)
            )
            bs = bres.scalar_one_or_none()
            if bs is None:
                bs = BotSession(
                    group_id=group.id,
                    name=cred.name,
                    bot_token=cred.bot_token,
                    baseurl=cred.baseurl,
                )
                session.add(bs)
                await session.flush()
            else:
                bs.bot_token = cred.bot_token
                if cred.baseurl is not None:
                    bs.baseurl = cred.baseurl
                # Keep existing group linkage; don't reparent.
            out_ids.append(bs.id)

            # Ensure a GroupMember row exists for this session's user. We key
            # by BotSession.name in the display_name slot so we can locate it
            # even before wechat_user_id is known.
            mres = await session.execute(
                select(GroupMember).where(
                    GroupMember.group_id == group.id,
                    GroupMember.display_name == cred.name,
                )
            )
            member = mres.scalar_one_or_none()
            if member is None:
                session.add(
                    GroupMember(
                        group_id=group.id,
                        wechat_user_id=bs.wechat_user_id,
                        display_name=cred.name,
                    )
                )
            elif bs.wechat_user_id and member.wechat_user_id != bs.wechat_user_id:
                member.wechat_user_id = bs.wechat_user_id
        await session.commit()
        return out_ids


class BootstrapService:
    """Thin wrapper so main.py / bridge.py can call a single verb."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sm = session_factory

    async def sync_sessions_to_db(
        self, cred_dir: Path | None = None
    ) -> list[SessionCredential]:
        creds = load_all_sessions(cred_dir=cred_dir)
        await sync_sessions_to_db(self._sm, creds)
        return creds


__all__ = [
    "CRED_DIR",
    "LOGICAL_GROUP_ID",
    "BootstrapService",
    "SessionCredential",
    "load_all_sessions",
    "sync_sessions_to_db",
]
