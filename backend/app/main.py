"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import chat, conversations, documents, health, memories
from app.config import settings
from app.core.logging import configure_logging, log_extra, new_request_id, request_id_ctx
from app.db.session import dispose_engine
from app.services.http import close_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    configure_logging(settings.log_level)
    logger.info(
        "starting %s",
        settings.app_name,
        extra=log_extra(
            environment=settings.environment,
            generation_model=settings.generation_model,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            rerank_model=settings.rerank_model,
        ),
    )
    yield
    await close_client()
    await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "prod" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)
# SSE frames must not be buffered by gzip; 1000 bytes keeps small JSON responses
# compressed while streaming responses (which set no Content-Length) pass through.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def request_context(request: Request, call_next):  # noqa: ANN001, ANN201
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    # Also on request.state: the 500 handler lives in ServerErrorMiddleware,
    # which is outside this middleware, so the context var is already reset by
    # the time it runs.
    request.state.request_id = request_id
    token = request_id_ctx.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-Id"] = request_id
    if not request.url.path.startswith("/api/health"):
        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
            extra=log_extra(duration_ms=duration_ms, status=response.status_code),
        )
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or request_id_ctx.get()
    logger.exception(
        "unhandled error on %s %s",
        request.method,
        request.url.path,
        extra=log_extra(request_id=request_id),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )


app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(conversations.router, prefix=settings.api_prefix)
app.include_router(documents.router, prefix=settings.api_prefix)
app.include_router(memories.router, prefix=settings.api_prefix)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs", "health": f"{settings.api_prefix}/health/ready"}
