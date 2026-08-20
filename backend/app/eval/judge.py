"""LLM-as-judge for the generation half.

Three things are worth judging, and only the first is about correctness:

- **groundedness** — is every claim supported by the passages that were actually
  retrieved? An answer can be true and still ungrounded, which for a RAG system
  is a failure: it means the model answered from memory and the citation is
  decoration.
- **citation accuracy** — do the inline [n] markers point at passages that
  actually support the sentence they follow?
- **fact coverage** — does the answer state the facts the dataset requires?

The judge is deliberately asked to be strict and to quote its evidence, because
a judge that hands out 0.9 to everything measures nothing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.services.llm import complete

logger = logging.getLogger(__name__)

CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")


@dataclass(slots=True)
class JudgeResult:
    groundedness: float = 0.0
    citation_accuracy: float = 0.0
    fact_coverage: float = 0.0
    refused_correctly: bool | None = None
    violations: list[str] = field(default_factory=list)
    reasoning: str = ""
    ok: bool = True

    def as_dict(self) -> dict:
        return {
            "groundedness": round(self.groundedness, 3),
            "citation_accuracy": round(self.citation_accuracy, 3),
            "fact_coverage": round(self.fact_coverage, 3),
            "refused_correctly": self.refused_correctly,
            "violations": self.violations,
        }


_PROMPT = """You are grading a retrieval-augmented answer. Be strict: this
grading exists to catch failures, and a generous grade hides them.

QUESTION:
%(question)s

RETRIEVED PASSAGES (the only material the answer was allowed to use):
%(passages)s

ANSWER:
%(answer)s

REQUIRED FACTS the answer should state:
%(facts)s

CLAIMS THE ANSWER MUST NOT MAKE:
%(forbidden)s

Grade on three axes, each 0.0-1.0:

1. groundedness — is every factual claim supported by the passages above?
   An answer that is true but not supported by these passages scores LOW: it
   means the model used prior knowledge, not retrieval.
   Explicitly labelled general knowledge ("the documents don't cover X, but
   generally...") does not count against this.

2. citation_accuracy — do the inline [n] markers point at passages that
   actually support the sentence they are attached to? 1.0 if there are no
   claims needing citation. 0.0 if markers are present but point to unrelated
   passages.

3. fact_coverage — what fraction of the REQUIRED FACTS does the answer state?
   1.0 if none were required.

Also list `violations`: any forbidden claim the answer makes, and any factual
claim with no support in the passages. Quote the offending text.

Return ONLY JSON:
{"groundedness": 0.0, "citation_accuracy": 0.0, "fact_coverage": 0.0,
 "violations": ["..."], "reasoning": "one or two sentences"}"""


_REFUSAL_PROMPT = """The retrieved passages do not contain the answer to this
question. The correct behaviour is to say so plainly rather than to invent one.

QUESTION:
%(question)s

RETRIEVED PASSAGES:
%(passages)s

ANSWER:
%(answer)s

Did the answer correctly indicate that it could not answer from the available
material — or that it was answering from general knowledge rather than the
documents?

Return ONLY JSON: {"refused_correctly": true|false, "reasoning": "..."}"""


def _render_passages(passages: list[str]) -> str:
    if not passages:
        return "(none retrieved)"
    return "\n\n".join(f"[{i}] {p[:1500]}" for i, p in enumerate(passages, start=1))


def _clamp(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


async def judge_answer(
    *,
    question: str,
    answer: str,
    passages: list[str],
    expect_facts: list[str],
    forbid: list[str],
    model: str | None = None,
) -> JudgeResult:
    raw = await complete(
        [
            {
                "role": "user",
                "content": _PROMPT
                % {
                    "question": question,
                    "passages": _render_passages(passages),
                    "answer": answer or "(empty)",
                    "facts": "\n".join(f"- {f}" for f in expect_facts) or "(none)",
                    "forbidden": "\n".join(f"- {f}" for f in forbid) or "(none)",
                },
            }
        ],
        model=model,
        temperature=0.0,
        max_tokens=800,
        json_mode=True,
    )

    if not raw:
        # A judge that silently returns zeros looks like a quality collapse.
        # Flag it as not-ok so the report can separate "bad answer" from
        # "grading failed".
        return JudgeResult(ok=False, reasoning="judge call returned nothing")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("judge returned non-JSON: %r", raw[:200])
        return JudgeResult(ok=False, reasoning="judge returned non-JSON")

    violations = data.get("violations") or []
    return JudgeResult(
        groundedness=_clamp(data.get("groundedness")),
        citation_accuracy=_clamp(data.get("citation_accuracy")),
        fact_coverage=_clamp(data.get("fact_coverage")),
        violations=[str(v) for v in violations][:10],
        reasoning=str(data.get("reasoning") or "")[:500],
    )


async def judge_refusal(
    *, question: str, answer: str, passages: list[str], model: str | None = None
) -> JudgeResult:
    raw = await complete(
        [
            {
                "role": "user",
                "content": _REFUSAL_PROMPT
                % {
                    "question": question,
                    "passages": _render_passages(passages),
                    "answer": answer or "(empty)",
                },
            }
        ],
        model=model,
        temperature=0.0,
        max_tokens=300,
        json_mode=True,
    )

    if not raw:
        return JudgeResult(ok=False, reasoning="judge call returned nothing")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return JudgeResult(ok=False, reasoning="judge returned non-JSON")

    correct = bool(data.get("refused_correctly"))
    return JudgeResult(
        refused_correctly=correct,
        # A correct refusal is perfectly grounded by definition.
        groundedness=1.0 if correct else 0.0,
        citation_accuracy=1.0,
        fact_coverage=1.0,
        reasoning=str(data.get("reasoning") or "")[:500],
    )


def check_citations_resolve(answer: str, source_count: int) -> list[str]:
    """Cheap structural check that needs no model: every [n] must point at a
    source that exists. A marker past the end of the list is a fabricated
    citation, and no judge is needed to know that."""
    problems = []
    for match in CITATION_PATTERN.finditer(answer):
        index = int(match.group(1))
        if index < 1 or index > source_count:
            problems.append(f"citation [{index}] does not exist ({source_count} sources)")
    return sorted(set(problems))
