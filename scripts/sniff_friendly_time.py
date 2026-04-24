"""Live DeepSeek sniff: does 小计 talk like a human about "下周一 10 点"?

Runs one real inbound through the orchestrator and prints:
- the outbound texts 小计 sent
- whether any ISO-like shape ("T...+08:00") leaked
- whether a colloquial form ("周一 10:00" / "下周一 10:00") appeared

Not a pytest — this is the "paste into PR body" proof. Keep it short.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

# Load .env if present (mirrors what pytest does).
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend" / "src"))

from planagent import db as db_mod  # noqa: E402
from planagent.agent.orchestrator import handle_inbound  # noqa: E402
from planagent.db.models import GroupContext, GroupMember  # noqa: E402
from planagent.llm.deepseek import DeepSeekClient  # noqa: E402
from planagent.main import run_migrations  # noqa: E402
from planagent.wechat.constants import CHENCHEN, PENG  # noqa: E402
from planagent.wechat.protocol import (  # noqa: E402
    ITEM_TYPE_TEXT,
    InboundMessage,
    Item,
    TextItemPayload,
)


async def _preseed(sm) -> str:
    async with sm() as session:
        g = GroupContext(wechat_group_id="wx-sniff", name="sniff")
        session.add(g)
        await session.flush()
        for who in (PENG, CHENCHEN):
            session.add(
                GroupMember(
                    group_id=g.id,
                    wechat_user_id=who.wechat_user_id,
                    display_name=who.display_name,
                )
            )
        await session.commit()
        return g.id


async def main() -> int:
    db_file = REPO / "_sniff.db"
    if db_file.exists():
        db_file.unlink()
    url = f"sqlite:///{db_file}"
    os.environ["PLANAGENT_DB_URL"] = url
    run_migrations(url)
    db_mod.init_engine(url)
    sm = db_mod.get_sessionmaker()
    try:
        await _preseed(sm)
        sent: list[str] = []

        async def _send(text: str) -> None:
            sent.append(text)

        msg = InboundMessage(
            from_user_id=PENG.wechat_user_id,
            to_user_id="bot",
            context_token="sniff-1",
            item_list=[
                Item(
                    type=ITEM_TYPE_TEXT,
                    text_item=TextItemPayload(text="帮我安排下周一 10 点学 Rust"),
                )
            ],
            group_id="wx-sniff",
        )
        deepseek = DeepSeekClient()
        await handle_inbound(
            msg, deepseek=deepseek, session_factory=sm, wechat_send=_send
        )

        print("--- outbound texts ---")
        for i, t in enumerate(sent):
            print(f"[{i}] {t}")
        joined = " || ".join(sent)
        iso_leak = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", joined)
        colloquial = re.search(r"(?:下?)?周[一二三四五六日]\s*\d{1,2}[:：]\d{2}", joined)
        print("--- checks ---")
        print(f"ISO leak in outbound? {bool(iso_leak)} ({iso_leak})")
        print(f"Colloquial 周X HH:MM present? {bool(colloquial)} ({colloquial})")
        return 0 if (not iso_leak and colloquial) else 1
    finally:
        await db_mod.dispose_engine()
        if db_file.exists():
            db_file.unlink()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
