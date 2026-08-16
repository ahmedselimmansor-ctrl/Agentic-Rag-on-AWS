"""Long-term memory inspection.

Memory that a user cannot see or delete is a liability. These endpoints back a
"what do you remember about me" view and give them a delete button.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.db.models import LongTermMemory
from app.schemas.chat import MemoryOut

router = APIRouter(prefix="/memories", tags=["memory"])


@router.get("", response_model=list[MemoryOut])
async def list_memories(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[LongTermMemory]:
    rows = (
        await session.execute(
            select(LongTermMemory)
            .where(
                LongTermMemory.user_id == user.id,
                LongTermMemory.superseded_by.is_(None),
            )
            .order_by(LongTermMemory.salience.desc(), LongTermMemory.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> Response:
    memory = (
        await session.execute(
            select(LongTermMemory).where(
                LongTermMemory.id == memory_id, LongTermMemory.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if memory is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
    await session.delete(memory)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_memories(session: DbSession, user: CurrentUser) -> Response:
    memories = (
        await session.execute(
            select(LongTermMemory).where(LongTermMemory.user_id == user.id)
        )
    ).scalars().all()
    for memory in memories:
        await session.delete(memory)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
