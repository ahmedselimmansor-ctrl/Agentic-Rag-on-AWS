"""Retrieval metrics.

Pure functions over ranked ID lists — no I/O, no models — so they can be tested
exactly and reasoned about when a number moves.

Each answers a different question:

- **recall@k**   did the relevant material make it into the top k at all?
                 This is the ceiling on answer quality: nothing the reranker or
                 the model does can recover a passage retrieval never returned.
- **precision@k** how much of the top k is worth the context budget?
- **MRR**        how far does the user (or the model) read before hitting
                 something relevant?
- **nDCG@k**     like MRR but credits every relevant hit, discounted by rank,
                 and normalises against the best achievable ordering.
- **hit rate**   the blunt one — did we get anything at all?
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class RetrievalScores:
    recall_at_k: float
    precision_at_k: float
    mrr: float
    ndcg_at_k: float
    hit: bool
    first_relevant_rank: int | None
    retrieved: int
    relevant_total: int

    def as_dict(self) -> dict[str, float | bool | int | None]:
        return {
            "recall@k": round(self.recall_at_k, 4),
            "precision@k": round(self.precision_at_k, 4),
            "mrr": round(self.mrr, 4),
            "ndcg@k": round(self.ndcg_at_k, 4),
            "hit": self.hit,
            "first_relevant_rank": self.first_relevant_rank,
            "retrieved": self.retrieved,
            "relevant_total": self.relevant_total,
        }


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top k."""
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return len(top & set(relevant)) / len(set(relevant))


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of the top k that is relevant. Denominator is how many we
    actually returned, not k — otherwise returning fewer than k is punished
    for something that is not a ranking failure."""
    top = retrieved[:k]
    if not top:
        return 0.0
    relevant_set = set(relevant)
    return sum(1 for r in top if r in relevant_set) / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """1/rank of the first relevant result, 0 if none."""
    relevant_set = set(relevant)
    for index, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return 1.0 / index
    return 0.0


def dcg_at_k(gains: Sequence[float], k: int) -> float:
    # log2(rank + 1) so rank 1 is undiscounted.
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains[:k], start=1))


def ndcg_at_k(
    retrieved: Sequence[str],
    relevant: Sequence[str],
    k: int,
    grades: dict[str, float] | None = None,
) -> float:
    """Graded relevance when `grades` is supplied, binary otherwise.

    Normalised against the ideal ordering, so 1.0 means "could not have been
    ranked better" rather than "everything relevant was returned".
    """
    if not relevant:
        return 0.0

    grade_of = grades or dict.fromkeys(relevant, 1.0)
    actual = [grade_of.get(item, 0.0) for item in retrieved[:k]]
    ideal = sorted((grade_of.get(item, 0.0) for item in relevant), reverse=True)

    best = dcg_at_k(ideal, k)
    return dcg_at_k(actual, k) / best if best > 0 else 0.0


def score_retrieval(
    retrieved: Sequence[str],
    relevant: Sequence[str],
    k: int = 5,
    grades: dict[str, float] | None = None,
) -> RetrievalScores:
    relevant_set = set(relevant)
    first_rank = next(
        (i for i, item in enumerate(retrieved, start=1) if item in relevant_set), None
    )
    return RetrievalScores(
        recall_at_k=recall_at_k(retrieved, relevant, k),
        precision_at_k=precision_at_k(retrieved, relevant, k),
        mrr=reciprocal_rank(retrieved, relevant),
        ndcg_at_k=ndcg_at_k(retrieved, relevant, k, grades),
        hit=first_rank is not None and first_rank <= k,
        first_relevant_rank=first_rank,
        retrieved=len(retrieved),
        relevant_total=len(relevant_set),
    )


def aggregate(scores: Sequence[RetrievalScores]) -> dict[str, float]:
    """Macro-average — every question counts equally regardless of how many
    relevant passages it has, so a single question with 20 gold chunks cannot
    dominate the headline number."""
    if not scores:
        return {}
    n = len(scores)
    return {
        "recall@k": round(sum(s.recall_at_k for s in scores) / n, 4),
        "precision@k": round(sum(s.precision_at_k for s in scores) / n, 4),
        "mrr": round(sum(s.mrr for s in scores) / n, 4),
        "ndcg@k": round(sum(s.ndcg_at_k for s in scores) / n, 4),
        "hit_rate": round(sum(1 for s in scores if s.hit) / n, 4),
        "questions": n,
    }
