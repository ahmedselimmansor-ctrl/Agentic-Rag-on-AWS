"""Application settings, loaded from environment / AWS Secrets Manager."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Ephemeral signing key for local dev when JWT_SECRET is unset; see
# Settings.effective_jwt_secret.
_DEV_SECRET: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "Agentic RAG"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"
    api_prefix: str = "/api"
    # NoDecode: pydantic-settings would otherwise JSON-parse this before the
    # validator runs, so a plain comma-separated env value would fail to load.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:3000",
    ]

    # --------------------------------------------------------------- auth ---
    # "jwt"    — email/password with signed access + rotating refresh tokens
    # "header" — trust X-User-Email. LOCAL DEVELOPMENT ONLY; refused in prod.
    auth_mode: Literal["jwt", "header"] = "jwt"
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30
    allow_registration: bool = True
    password_min_length: int = 10

    # --------------------------------------------------------- rate limits --
    # Counted from existing rows, so no extra write path and accurate across tasks.
    max_messages_per_hour: int = 120
    max_uploads_per_hour: int = 60

    # ----------------------------------------------------------- database ---
    # asyncpg DSN, e.g. postgresql+asyncpg://user:pass@host:5432/agentic_rag
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/agentic_rag"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ------------------------------------------------------------ openai ---
    openai_api_key: str = ""
    openai_base_url: str | None = None
    # NOTE: verify this ID against the OpenAI model list for your account.
    generation_model: str = "gpt-5.6-luna"
    generation_temperature: float = 0.3
    generation_max_output_tokens: int = 4096
    # Cheap model used for query rewriting / memory extraction / titles.
    utility_model: str = "gpt-5.6-luna"

    # ------------------------------------------- alibaba cloud model studio --
    dashscope_api_key: str = ""
    # Use the -intl host outside mainland China; swap for dashscope.aliyuncs.com inside.
    dashscope_base_url: str = "https://dashscope-intl.aliyuncs.com"
    embedding_model: str = "tongyi-embedding-vision-flash"
    embedding_dim: int = 1024
    embedding_batch_size: int = 8
    rerank_model: str = "qwen3-rerank"
    rerank_top_n: int = 8

    # --------------------------------------------------------- retrieval ---
    dense_top_k: int = 30
    sparse_top_k: int = 30
    rrf_k: int = 60
    min_rerank_score: float = 0.15
    hybrid_enabled: bool = True

    # ---------------------------------------------------------- chunking ---
    chunk_target_tokens: int = 512
    chunk_overlap_tokens: int = 64
    chunk_min_tokens: int = 64

    # ---------------------------------------------------- context window ---
    model_context_window: int = 200_000
    context_reserve_for_output: int = 4_096
    max_retrieved_context_tokens: int = 12_000
    max_history_tokens: int = 8_000
    history_summary_trigger_tokens: int = 6_000
    max_long_term_memories: int = 8

    # -------------------------------------------------------------- agent --
    agent_max_steps: int = 6
    agent_recursion_limit: int = 25

    # -------------------------------------------------------- web search ---
    web_search_provider: Literal["tavily", "serper", "none"] = "tavily"
    tavily_api_key: str = ""
    serper_api_key: str = ""
    web_search_max_results: int = 6

    # ------------------------------------------------------------ uploads --
    upload_backend: Literal["local", "s3"] = "local"
    upload_dir: str = "/tmp/agentic-rag-uploads"
    s3_bucket: str = ""
    s3_prefix: str = "uploads/"
    aws_region: str = "us-east-1"
    max_upload_bytes: int = 25 * 1024 * 1024
    presign_expiry_seconds: int = 3600

    # -------------------------------------------------------------- http ---
    http_timeout_seconds: float = 60.0
    http_max_retries: int = 3

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept either a comma-separated string or a JSON array."""
        if isinstance(v, str):
            raw = v.strip()
            if raw.startswith("["):
                import json

                return json.loads(raw)
            return [o.strip() for o in raw.split(",") if o.strip()]
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _require_strong_secret(cls, v: str, info) -> str:  # noqa: ANN001
        """A signing key short enough to brute-force is worse than no auth at
        all, because it looks secure. Enforced only outside local dev."""
        environment = info.data.get("environment", "local")
        auth_mode = info.data.get("auth_mode", "jwt")
        if environment != "local" and auth_mode == "jwt" and len(v) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters outside local dev. "
                "Generate one with: openssl rand -hex 32"
            )
        return v

    @field_validator("auth_mode")
    @classmethod
    def _refuse_header_auth_in_prod(cls, v: str, info) -> str:  # noqa: ANN001
        if v == "header" and info.data.get("environment") in {"staging", "prod"}:
            raise ValueError("AUTH_MODE=header trusts a client header; never use it in staging/prod")
        return v

    @property
    def effective_jwt_secret(self) -> str:
        """Local dev gets an ephemeral key so the app runs with no setup —
        tokens simply do not survive a restart."""
        if self.jwt_secret:
            return self.jwt_secret
        import secrets

        global _DEV_SECRET
        if _DEV_SECRET is None:
            _DEV_SECRET = secrets.token_hex(32)
        return _DEV_SECRET

    @property
    def sync_database_url(self) -> str:
        """psycopg2 DSN — Alembic runs migrations synchronously."""
        return self.database_url.replace("+asyncpg", "+psycopg2")

    @property
    def input_token_budget(self) -> int:
        return self.model_context_window - self.context_reserve_for_output


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
