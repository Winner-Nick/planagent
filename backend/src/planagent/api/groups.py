from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planagent.api.schemas import ConversationTurnRead, GroupRead
from planagent.db import get_session
from planagent.db.models import ConversationTurn, GroupContext

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupRead])
async def list_groups(session: AsyncSession = Depends(get_session)) -> list[GroupContext]:
    res = await session.execute(select(GroupContext).order_by(GroupContext.created_at.asc()))
    return list(res.scalars().all())


@router.get("/{group_id}", response_model=GroupRead)
async def get_group(group_id: str, session: AsyncSession = Depends(get_session)) -> GroupContext:
    g = await session.get(GroupContext, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="group not found")
    return g


@router.get("/{group_id}/conversations", response_model=list[ConversationTurnRead])
async def list_conversation_turns(
    group_id: str,
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationTurn]:
    """Return the most recent `limit` turns for a group in chronological (oldest-first) order."""
    g = await session.get(GroupContext, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail="group not found")
    # fetch newest `limit` then reverse so response is oldest-first chronological
    stmt = (
        select(ConversationTurn)
        .where(ConversationTurn.group_id == group_id)
        .order_by(ConversationTurn.created_at.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    rows = list(res.scalars().all())
    rows.reverse()
    return rows
