from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planagent.api.schemas import (
    PlanCreate,
    PlanRead,
    PlanUpdate,
    ReminderRead,
)
from planagent.db import get_session
from planagent.db.models import GroupContext, Plan, PlanStatus, Reminder

router = APIRouter(prefix="/plans", tags=["plans"])


@router.get("", response_model=list[PlanRead])
async def list_plans(
    group_id: str | None = Query(None),
    status_: PlanStatus | None = Query(None, alias="status"),
    session: AsyncSession = Depends(get_session),
) -> list[Plan]:
    stmt = select(Plan).order_by(Plan.created_at.desc())
    if group_id is not None:
        stmt = stmt.where(Plan.group_id == group_id)
    if status_ is not None:
        stmt = stmt.where(Plan.status == status_)
    res = await session.execute(stmt)
    return list(res.scalars().all())


@router.get("/{plan_id}", response_model=PlanRead)
async def get_plan(plan_id: str, session: AsyncSession = Depends(get_session)) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post("", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate, session: AsyncSession = Depends(get_session)
) -> Plan:
    group = await session.get(GroupContext, payload.group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    plan = Plan(**payload.model_dump())
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.patch("/{plan_id}", response_model=PlanRead)
async def update_plan(
    plan_id: str,
    payload: PlanUpdate,
    session: AsyncSession = Depends(get_session),
) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(plan, k, v)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: str, session: AsyncSession = Depends(get_session)) -> None:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    await session.delete(plan)
    await session.commit()


@router.get("/{plan_id}/reminders", response_model=list[ReminderRead])
async def list_plan_reminders(
    plan_id: str, session: AsyncSession = Depends(get_session)
) -> list[Reminder]:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    stmt = select(Reminder).where(Reminder.plan_id == plan_id).order_by(Reminder.fire_at.asc())
    res = await session.execute(stmt)
    return list(res.scalars().all())
