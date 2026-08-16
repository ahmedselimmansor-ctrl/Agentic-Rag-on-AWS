"""Health checks.

/health/live  — process is up (ECS/ALB liveness; never touches the database)
/health/ready — dependencies are usable (ALB target-group health check)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession
from app.config import settings
from app.schemas.chat import HealthOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])

VERSION = "1.0.0"


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", response_model=HealthOut)
async def ready(session: DbSession) -> HealthOut:
    checks: dict[str, str] = {}
    db_ok = False
    pgvector_ok = False

    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"[:200]

    if db_ok:
        try:
            row = (
                await session.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
            pgvector_ok = row is not None
            checks["pgvector"] = f"ok (v{row})" if row else "missing"
        except Exception as exc:  # noqa: BLE001
            checks["pgvector"] = f"error: {exc}"[:200]

    checks["openai_key"] = "set" if settings.openai_api_key else "MISSING"
    checks["dashscope_key"] = "set" if settings.dashscope_api_key else "MISSING"
    checks["web_search"] = settings.web_search_provider
    checks["generation_model"] = settings.generation_model
    checks["embedding_model"] = f"{settings.embedding_model} (dim={settings.embedding_dim})"
    checks["rerank_model"] = settings.rerank_model

    healthy = db_ok and pgvector_ok and settings.openai_api_key and settings.dashscope_api_key

    return HealthOut(
        status="ok" if healthy else "degraded",
        version=VERSION,
        environment=settings.environment,
        database=db_ok,
        pgvector=pgvector_ok,
        checks=checks,
    )
