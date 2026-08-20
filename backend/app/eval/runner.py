"""Executes a golden dataset against the real retrieval and agent paths.

Retrieval and generation are graded separately and deliberately. Retrieval is
the ceiling: if the passage never came back, no amount of prompt work recovers
it. Grading them together hides which half regressed.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from app.agent.graph import get_graph
from app.agent.state import initial_state
from app.db.models import User
from app.db.session import session_scope
from app.eval import judge as judge_service
from app.eval.dataset import Dataset, GoldenCase
from app.eval.metrics import RetrievalScores, aggregate, score_retrieval
from app.services.retrieval import RetrievedChunk, retrieve

logger = logging.getLogger(__name__)

# Marks a retrieved chunk that satisfies no expectation. Unique per rank so it
# never collides with a real expectation key.
_MISS = "__miss__"


@dataclass(slots=True)
class CaseResult:
    id: str
    question: str
    unanswerable: bool
    retrieval: dict | None = None
    generation: dict | None = None
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    structural_problems: list[str] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None


@dataclass(slots=True)
class EvalReport:
    dataset: str
    k: int
    cases: list[CaseResult]
    retrieval_summary: dict = field(default_factory=dict)
    generation_summary: dict = field(default_factory=dict)
    generated_at: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "k": self.k,
            "generated_at": self.generated_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "retrieval": self.retrieval_summary,
            "generation": self.generation_summary,
            "cases": [asdict(c) for c in self.cases],
        }


def _expectation_keys(case: GoldenCase) -> list[str]:
    return [f"doc:{d}" for d in case.expect_documents] + [
        f"snippet:{s}" for s in case.expect_snippets
    ]


def _match(chunk: RetrievedChunk, case: GoldenCase) -> str | None:
    """Which expectation this chunk satisfies, if any.

    Expectations are filenames and text snippets rather than chunk ids, so the
    dataset survives a chunker change — which is exactly when you most need it.
    """
    for document in case.expect_documents:
        if document.lower() in chunk.filename.lower():
            return f"doc:{document}"

    haystack = f"{chunk.context_header or ''} {chunk.content}".lower()
    for snippet in case.expect_snippets:
        if snippet.lower() in haystack:
            return f"snippet:{snippet}"
    return None


def grade_retrieval(chunks: list[RetrievedChunk], case: GoldenCase, k: int) -> tuple[
    RetrievalScores, list[str], list[str]
]:
    """Map each rank to the expectation it satisfies, then score that ranked list.

    A chunk satisfying no expectation gets a rank-unique sentinel so it counts
    against precision without ever counting as a hit.
    """
    expectations = _expectation_keys(case)
    ranked: list[str] = []
    for index, chunk in enumerate(chunks):
        ranked.append(_match(chunk, case) or f"{_MISS}{index}")

    scores = score_retrieval(ranked, expectations, k=k)
    matched = [key for key in expectations if key in set(ranked[:k])]
    missed = [key for key in expectations if key not in set(ranked[:k])]
    return scores, matched, missed


async def _resolve_user(email: str) -> uuid.UUID:
    async with session_scope() as session:
        user_id = (
            await session.execute(select(User.id).where(User.email == email.lower()))
        ).scalar_one_or_none()
    if user_id is None:
        raise RuntimeError(
            f"No user {email!r}. Evaluation runs against a real corpus — register "
            "the account and upload the documents the dataset expects first."
        )
    return user_id


async def run_case(
    case: GoldenCase,
    *,
    user_id: uuid.UUID,
    k: int,
    generate: bool,
    judge: bool,
    judge_model: str | None,
) -> CaseResult:
    started = time.perf_counter()
    result = CaseResult(id=case.id, question=case.question, unanswerable=case.unanswerable)

    try:
        async with session_scope() as session:
            chunks = await retrieve(session, case.question, user_id=user_id, top_n=max(k, 10))

        result.sources = [c.citation_label for c in chunks[:k]]

        if case.has_retrieval_expectations:
            scores, matched, missed = grade_retrieval(chunks, case, k)
            result.retrieval = scores.as_dict()
            result.matched = matched
            result.missed = missed

        if not generate:
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        # Run the real agent graph, so the eval exercises the same path a user
        # would hit rather than a simplified stand-in.
        async with session_scope() as session:
            conversation_id = uuid.uuid4()
            state = initial_state(
                user_id=user_id,
                conversation_id=conversation_id,
                question=case.question,
                web_enabled=False,
            )
            # No history/summary to load: eval questions are single-turn.
            state["history"] = []
            state["chunks"] = chunks

            final = await get_graph().ainvoke(
                state, config={"configurable": {"session": session, "queue": None}}
            )

        result.answer = (final.get("answer") or "").strip()
        source_list = final.get("sources") or []
        result.structural_problems = judge_service.check_citations_resolve(
            result.answer, len(source_list)
        )

        if judge:
            passages = [c.prompt_text for c in chunks[:k]]
            verdict = (
                await judge_service.judge_refusal(
                    question=case.question,
                    answer=result.answer,
                    passages=passages,
                    model=judge_model,
                )
                if case.unanswerable
                else await judge_service.judge_answer(
                    question=case.question,
                    answer=result.answer,
                    passages=passages,
                    expect_facts=case.expect_facts,
                    forbid=case.forbid,
                    model=judge_model,
                )
            )
            result.generation = verdict.as_dict()
            if not verdict.ok:
                result.error = f"judge unavailable: {verdict.reasoning}"

    except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
        logger.exception("case %s failed", case.id)
        result.error = str(exc)[:500]

    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


async def run_dataset(
    dataset: Dataset,
    *,
    user_email: str,
    k: int = 5,
    generate: bool = True,
    judge: bool = True,
    judge_model: str | None = None,
    concurrency: int = 4,
    tag: str | None = None,
) -> EvalReport:
    from datetime import UTC, datetime

    started = time.perf_counter()
    user_id = await _resolve_user(user_email)
    cases = dataset.filter_by_tag(tag)

    if not cases:
        raise RuntimeError(f"no cases match tag {tag!r}")

    # Bounded: the judge and generation both hit rate-limited providers.
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def guarded(case: GoldenCase) -> CaseResult:
        async with semaphore:
            return await run_case(
                case,
                user_id=user_id,
                k=k,
                generate=generate,
                judge=judge,
                judge_model=judge_model,
            )

    results = await asyncio.gather(*(guarded(c) for c in cases))

    graded = [
        RetrievalScores(**_from_dict(r.retrieval)) for r in results if r.retrieval is not None
    ]
    retrieval_summary = aggregate(graded)

    judged = [r.generation for r in results if r.generation]
    generation_summary: dict = {}
    if judged:
        n = len(judged)
        generation_summary = {
            "groundedness": round(sum(j["groundedness"] for j in judged) / n, 4),
            "citation_accuracy": round(sum(j["citation_accuracy"] for j in judged) / n, 4),
            "fact_coverage": round(sum(j["fact_coverage"] for j in judged) / n, 4),
            "violations": sum(len(j["violations"]) for j in judged),
            "answers": n,
        }
        refusals = [j for j in judged if j.get("refused_correctly") is not None]
        if refusals:
            correct = sum(1 for j in refusals if j["refused_correctly"])
            generation_summary["refusal_accuracy"] = round(correct / len(refusals), 4)

    structural = sum(len(r.structural_problems) for r in results)
    if generation_summary or structural:
        generation_summary["broken_citations"] = structural

    return EvalReport(
        dataset=dataset.name,
        k=k,
        cases=list(results),
        retrieval_summary=retrieval_summary,
        generation_summary=generation_summary,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration_seconds=time.perf_counter() - started,
    )


def _from_dict(data: dict) -> dict:
    """Rebuild RetrievalScores kwargs from its serialised form."""
    return {
        "recall_at_k": data["recall@k"],
        "precision_at_k": data["precision@k"],
        "mrr": data["mrr"],
        "ndcg_at_k": data["ndcg@k"],
        "hit": data["hit"],
        "first_relevant_rank": data["first_relevant_rank"],
        "retrieved": data["retrieved"],
        "relevant_total": data["relevant_total"],
    }
