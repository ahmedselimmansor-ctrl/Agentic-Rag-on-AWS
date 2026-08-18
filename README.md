# Agentic RAG on AWS

Retrieval-augmented chat with an agent loop: hybrid retrieval over your own
documents, a cross-encoder reranking pass, web search as a tool, two-tier memory,
and token-by-token streaming to a React frontend.

```
React (Vite)  ──SSE──>  FastAPI  ──>  LangGraph agent
                                        ├─ hybrid retrieval  → pgvector + Postgres FTS
                                        ├─ rerank            → qwen3-rerank
                                        ├─ tools             → web search, fetch page
                                        └─ generation        → OpenAI (streamed)
                          PostgreSQL 16 + pgvector · S3 · ECS Fargate · CloudFront
```

## The pipeline

| Stage | What happens | Where |
|---|---|---|
| **Parse** | PDF / DOCX / MD / HTML / CSV / TXT / images → structured blocks with heading trails and page numbers | [parsing.py](backend/app/services/parsing.py) |
| **Chunk** | Structure-aware packing to ~512 tokens with sentence-boundary splits and overlap; heading trail kept as a `context_header` | [chunking.py](backend/app/services/chunking.py) |
| **Embed** | `tongyi-embedding-vision-flash` — multimodal, so images embed as pictures rather than captions | [embeddings.py](backend/app/services/embeddings.py) |
| **Retrieve** | Dense (pgvector HNSW, cosine) + sparse (`tsvector`/`ts_rank_cd`) fused with Reciprocal Rank Fusion | [retrieval.py](backend/app/services/retrieval.py) |
| **Rerank** | `qwen3-rerank` scores each (query, passage) pair jointly; weak matches are dropped, not padded in | [reranker.py](backend/app/services/reranker.py) |
| **Augment** | Priority-ordered context assembly under a hard token budget | [context.py](backend/app/services/context.py) |
| **Generate** | OpenAI streaming with tool calling; deltas forwarded as SSE | [llm.py](backend/app/services/llm.py) |

### Why hybrid + rerank

Dense search finds paraphrases but misses rare literals — part numbers, error
codes, surnames. Sparse search nails those literals but misses synonyms. RRF
merges the two ranked lists without needing their scores to share a scale, and
the cross-encoder then does the precision pass over the fused candidates. Each
retrieval leg degrades independently: if the reranker is down, fusion order is
used; if embeddings fail, sparse still answers.

### Memory

**Short-term** is the conversation. The last 8 turns stay verbatim; older turns
fold into a rolling summary on the `conversations` row, so a 200-turn thread
still fits the window.

**Long-term** is cross-conversation. After each turn a cheap model extracts
durable facts and preferences, embeds them, and stores them; later turns recall
them by vector similarity. Near-duplicates reinforce an existing memory instead
of creating a new row. Users can inspect and delete everything through
`/api/memories`.

### The agent loop

```
prepare ──> retrieve ──> generate ──┬──> tools ──┐
                            ^       │            │
                            └───────┴────────────┘
                                    └──> END
```

`prepare` rewrites the question into a standalone search query (a follow-up like
*"what about the second one?"* is unsearchable on its own) and recalls memory.
`retrieve` does one grounded pass up front so a simple question never pays for a
tool round-trip. `generate` streams; if the context does not cover the question,
the model calls `search_documents`, `web_search`, or `fetch_page` and the loop
returns to `generate` with the results appended. Bounded by `AGENT_MAX_STEPS`.

## Run it locally

Requires Docker and Docker Compose.

```bash
make env    # creates .env — add OPENAI_API_KEY and DASHSCOPE_API_KEY
```

```bash
make up
```

Web on <http://localhost:8080>, API docs on <http://localhost:8000/docs>.
Check wiring at <http://localhost:8000/api/health/ready> — it reports which keys
are missing and whether pgvector is installed.

Frontend with hot reload against the containerised API:

```bash
make db && make api    # terminal 1
make web               # terminal 2 → http://localhost:5173
```

## Configuration

Every knob is an environment variable; see [.env.example](.env.example). The ones
that matter most:

| Variable | Default | Notes |
|---|---|---|
| `GENERATION_MODEL` | `gpt-5.6-luna` | **Verify this ID against your account's model list.** |
| `EMBEDDING_MODEL` | `tongyi-embedding-vision-flash` | |
| `EMBEDDING_DIM` | `1024` | Must match what the model returns. See below. |
| `RERANK_MODEL` | `qwen3-rerank` | |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com` | Use `dashscope.aliyuncs.com` inside mainland China. |
| `WEB_SEARCH_PROVIDER` | `tavily` | `tavily`, `serper`, or `none`. |
| `MODEL_CONTEXT_WINDOW` | `200000` | Drives the whole context budget. |
| `AGENT_MAX_STEPS` | `6` | Tool rounds before a final answer is forced. |

### About `EMBEDDING_DIM`

The pgvector column, the HNSW index, and the Alembic migration all read this
value. If it does not match what the model actually returns, ingestion fails
loudly at the first batch with a dimension-mismatch error rather than storing
corrupt vectors.

Changing it later means a new migration **and** re-embedding every chunk:

```bash
docker compose exec db psql -U postgres -d agentic_rag -c "TRUNCATE chunks, long_term_memories;"
```

Then `make revision m="resize embedding"`, `make migrate`, and re-upload.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Stream a turn (SSE) |
| `POST` | `/api/search` | Retrieval only — for debugging relevance |
| `GET/POST` | `/api/conversations` | List / create |
| `GET/PATCH/DELETE` | `/api/conversations/{id}` | Load, rename, delete |
| `POST` | `/api/documents` | Upload; ingestion runs in the background |
| `GET` | `/api/documents/{id}` | Poll ingestion status |
| `DELETE` | `/api/documents/{id}` | Delete document, chunks, and blob |
| `GET/DELETE` | `/api/memories` | Inspect and forget long-term memory |
| `POST` | `/api/auth/{register,login,refresh,logout,logout-all}` | Session lifecycle |
| `POST` | `/api/account/{verify-email,forgot-password,reset-password}` | Account recovery |
| `GET` | `/api/health/{live,ready}` | Liveness / readiness |

### Stream protocol

`POST /api/chat` returns `text/event-stream`:

| Event | Payload |
|---|---|
| `start` | conversation + message ids |
| `status` | human-readable step label |
| `sources` | citation list, sent before generation begins |
| `tool_call` / `tool_result` | agent trace |
| `token` | one generation delta |
| `usage` | token counts, latency, step count |
| `done` \| `error` | terminal |

`:` comment frames arrive every 15s as heartbeats so proxies do not reap an idle
connection mid-answer.

## Deploy to AWS

```bash
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
```

Fill in your keys, then:

```bash
make tf-init && make tf-apply
```

```bash
make deploy
```

`deploy.sh` builds and pushes the backend image, runs `alembic upgrade head` as a
one-off ECS task, **aborts if it fails**, then rolls the service and syncs the
frontend to S3 + CloudFront.

### What gets created

- **VPC** across 2 AZs — public subnets for the ALB, private for tasks and RDS, S3 gateway endpoint so upload traffic skips the NAT
- **RDS PostgreSQL 16** — gp3, encrypted, automated backups, Performance Insights, pgvector enabled by the first migration
- **ECS Fargate** — autoscaling on CPU *and* requests-per-target (a streaming turn holds a connection open, so CPU alone under-reports load), circuit-breaker rollback
- **ALB** — 600s idle timeout for long streams; rejects any request without the CloudFront-injected `X-Origin-Verify` header, so the origin cannot be reached directly
- **CloudFront** — static assets cached, `/api/*` pass-through with compression **off** (compression buffers SSE and destroys token-by-token delivery)
- **S3** — separate buckets for uploads and web assets, both private
- **Secrets Manager** — DSN and API keys, read by the execution role only

### Cost note

The default shape (2× Fargate 1vCPU, `db.t4g.medium`, single NAT) runs roughly
$150–200/month before model usage. The NAT gateway and RDS dominate; set
`single_nat_gateway = false` only when you need AZ-level HA.

## Tests

```bash
make test
```

```bash
cd frontend && npm test
```

Backend covers chunk-boundary and overlap behaviour, RRF ranking, context-budget
enforcement and priority order, tool-call reassembly across stream chunks, token
and password semantics, queue message handling, and email link generation.
Frontend covers the SSE parser: frames split mid-JSON across network chunks,
several frames in one chunk, heartbeats, CRLF, and malformed frames.

## Layout

```
backend/
  app/
    agent/      LangGraph nodes, graph wiring, SSE runner, prompts
    api/        routes + dependencies
    db/         SQLAlchemy models and session
    services/   parsing, chunking, embeddings, retrieval, rerank, memory,
                context, auth, email, queue, ocr
    tools/      tool schemas, dispatch, web search
    worker.py   SQS ingestion worker
  alembic/      migrations
frontend/
  src/
    api/        REST client + SSE parser
    components/ Sidebar, PromptBox, MessageList, Markdown, SourceDrawer,
                AuthScreen, PasswordFlows, LibraryPanel
    hooks/      useChat (streaming), useUploads (ingestion polling), useAuth
infra/terraform/  VPC, RDS, ECS, ALB, CloudFront, S3, Secrets
```

## Authentication

Email + password, with short-lived JWT access tokens and long-lived rotating
refresh tokens.

- **Rotation with replay detection.** Every refresh issues a new token and
  revokes the old one. If a revoked token is presented again, the whole token
  *family* is revoked — a stolen token turns into a forced re-login rather than
  silent persistent access.
- **Only hashes are stored.** Passwords use bcrypt (cost 12); refresh tokens are
  stored as SHA-256. A database leak yields no working credentials.
- **No account enumeration.** Wrong password and unknown account return the
  identical error, and the unknown-account path burns equivalent CPU so response
  timing does not distinguish them either.
- **Length over composition.** Minimum 10 characters, no symbol-class rules —
  complexity requirements push people toward reused passwords.

Endpoints: `POST /api/auth/{register,login,refresh,logout,logout-all}`,
`GET /api/auth/me`.

`AUTH_MODE=header` restores the old header-trusting behaviour for local
development only; `Settings` refuses it when `ENVIRONMENT` is staging or prod.
To use Cognito or another OIDC provider instead, replace `resolve_user` in
[deps.py](backend/app/api/deps.py) — it is the only place identity is read.

### Email verification and password reset

`POST /api/account/{verify-email,resend-verification,forgot-password,reset-password}`.

Tokens are single-use, time-bounded, and stored only as SHA-256 — a database
leak must not hand over working reset links. Reset windows are deliberately
shorter than verification windows, because a live reset link sitting in an inbox
is the more dangerous of the two.

Forgot-password answers identically for known, unknown, and malformed
addresses. Anything else turns the form into a free membership check for anyone
holding a list of emails. A successful reset also revokes every refresh token,
since a reset is the remedy for a compromised account.

Set `EMAIL_BACKEND=log` (the default) and the message — link included — goes to
the logs, so the whole flow is testable without a verified SES domain. Switch to
`ses` in production. `REQUIRE_EMAIL_VERIFICATION=true` gates the API on a
confirmed address.

### Rate limits

Per user, counted from rows that already exist, so there is no extra write path
and the limit holds across every ECS task rather than per-process:
`MAX_MESSAGES_PER_HOUR` (120) and `MAX_UPLOADS_PER_HOUR` (60).

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and PR:

| Job | Checks |
|---|---|
| backend | ruff, pytest, migrations apply against real pgvector, **migration-drift check**, downgrade to base |
| frontend | tsc, vitest, production build |
| docker | both images build with layer caching |
| terraform | `fmt -check`, `validate` |

The drift check runs `alembic revision --autogenerate` and fails if it produces
any operations — that is how a model change with no migration gets caught before
it reaches a deploy rather than after.

## Ingestion: inline or queued

`INGESTION_MODE=inline` (default) processes uploads in the API process — zero
infrastructure, right for local dev and small deployments.

`INGESTION_MODE=sqs` hands the job to a dedicated worker (`python -m app.worker`,
its own ECS service). Embedding a 200-page PDF is minutes of CPU and provider
latency; in the API process that competes with streaming turns for the event
loop, and an API deploy mid-ingest loses the job silently.

Details that matter:

- **Delivery is at-least-once**, which is safe because `ingest_document`
  *replaces* a document's chunks rather than appending. Reprocessing converges
  instead of duplicating passages.
- **The worker heartbeats** the message's visibility timeout while a long
  document is in flight, so a slow file is not redelivered to a second worker.
- **Enqueue failure degrades to inline** rather than rejecting the upload. A
  queue outage costs throughput, not documents.
- **Autoscaling tracks queue depth per worker**, not CPU — a worker blocked on
  the embedding provider is idle but very much busy.
- **Anything reaching the DLQ raises an alarm.** Those are documents a user
  uploaded and will never get an answer from.

## Monitoring

`terraform apply` creates a CloudWatch dashboard and alarms on API 5xx,
unhealthy targets, p95 latency, RDS CPU / free storage / connections, DLQ depth,
and ingestion backlog age. Set `alert_email` to subscribe to the SNS topic.

## Known gaps

- **OCR needs S3.** Textract reads from a bucket, so scanned PDFs cannot be
  OCR'd with `UPLOAD_BACKEND=local`. Ingestion says so explicitly rather than
  failing opaquely.
- **No SSO.** Email/password only. `resolve_user` in
  [deps.py](backend/app/api/deps.py) is the single seam for adding Cognito or
  another OIDC provider.
- **The worker has no local queue.** `make up` runs inline ingestion; exercising
  the SQS path locally needs ElasticMQ or a real queue.
