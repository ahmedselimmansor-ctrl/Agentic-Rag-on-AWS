"""Retrieval metrics and dataset validation.

These are the numbers a chunker or reranker change is judged by, so they need
to be exactly right — a metric that is subtly wrong is worse than none, because
it will be trusted.
"""

from __future__ import annotations

import pytest

from app.eval import dataset as ds
from app.eval.metrics import (
    aggregate,
    dcg_at_k,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    score_retrieval,
)


# ------------------------------------------------------------- recall ------
def test_recall_counts_the_relevant_set_found():
    assert recall_at_k(["a", "b", "c"], ["a", "c"], k=3) == 1.0
    assert recall_at_k(["a", "x", "y"], ["a", "c"], k=3) == 0.5
    assert recall_at_k(["x", "y", "z"], ["a", "c"], k=3) == 0.0


def test_recall_respects_the_cutoff():
    # 'c' sits at rank 3, so it is invisible at k=2.
    assert recall_at_k(["a", "x", "c"], ["a", "c"], k=2) == 0.5


def test_recall_is_zero_when_nothing_is_relevant():
    assert recall_at_k(["a"], [], k=5) == 0.0


def test_duplicate_relevant_entries_do_not_inflate_recall():
    assert recall_at_k(["a", "b"], ["a", "a", "b"], k=5) == 1.0


# ---------------------------------------------------------- precision ------
def test_precision_divides_by_what_was_returned():
    """Returning 3 results when k=5 is not a ranking failure, so the
    denominator is the result count, not k."""
    assert precision_at_k(["a", "b", "c"], ["a", "b", "c"], k=5) == 1.0
    assert precision_at_k(["a", "x"], ["a"], k=5) == 0.5


def test_precision_of_nothing_is_zero():
    assert precision_at_k([], ["a"], k=5) == 0.0


# ----------------------------------------------------------------- mrr -----
def test_reciprocal_rank_uses_the_first_hit():
    assert reciprocal_rank(["a", "b"], ["a"]) == 1.0
    assert reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert reciprocal_rank(["x", "y", "a"], ["a"]) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x"], ["a"]) == 0.0


# ---------------------------------------------------------------- ndcg -----
def test_dcg_does_not_discount_rank_one():
    assert dcg_at_k([1.0], 1) == 1.0


def test_ndcg_is_one_for_ideal_ordering():
    assert ndcg_at_k(["a", "b"], ["a", "b"], k=2) == pytest.approx(1.0)


def test_ndcg_penalises_a_worse_ordering():
    ideal = ndcg_at_k(["a", "x"], ["a"], k=2)
    worse = ndcg_at_k(["x", "a"], ["a"], k=2)
    assert worse < ideal


def test_ndcg_honours_graded_relevance():
    """With grades, putting the *more* relevant item first must score higher —
    binary relevance cannot distinguish these two orderings at all."""
    grades = {"a": 3.0, "b": 1.0}
    better = ndcg_at_k(["a", "b"], ["a", "b"], k=2, grades=grades)
    worse = ndcg_at_k(["b", "a"], ["a", "b"], k=2, grades=grades)
    assert better > worse
    assert better == pytest.approx(1.0)


# -------------------------------------------------------------- combined ---
def test_score_retrieval_reports_the_first_relevant_rank():
    scores = score_retrieval(["x", "y", "a", "b"], ["a", "b"], k=4)
    assert scores.first_relevant_rank == 3
    assert scores.hit is True
    assert scores.recall_at_k == 1.0


def test_hit_is_false_when_the_only_match_is_past_k():
    scores = score_retrieval(["x", "y", "a"], ["a"], k=2)
    assert scores.hit is False
    assert scores.first_relevant_rank == 3  # found, but not within k


def test_aggregate_macro_averages():
    """Every question counts equally: a question with 20 gold chunks must not
    dominate the headline number."""
    a = score_retrieval(["a"], ["a"], k=1)
    b = score_retrieval(["x"], ["b"], k=1)
    summary = aggregate([a, b])

    assert summary["recall@k"] == 0.5
    assert summary["hit_rate"] == 0.5
    assert summary["questions"] == 2


def test_aggregate_of_nothing_is_empty():
    assert aggregate([]) == {}


# ------------------------------------------------------------- dataset -----
def write(tmp_path, body: str):
    path = tmp_path / "d.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_valid_dataset_loads(tmp_path):
    path = write(
        tmp_path,
        """
name: t
cases:
  - id: one
    question: How long is the refund window?
    expect_snippets: ["within 30 days"]
    tags: [billing]
""",
    )
    data = ds.load(path)
    assert data.name == "t"
    assert data.cases[0].expect_snippets == ["within 30 days"]
    assert data.cases[0].has_retrieval_expectations


def test_a_case_with_no_expectations_is_rejected(tmp_path):
    """It could never fail, so it would silently pad the score."""
    path = write(tmp_path, "cases:\n  - question: anything at all?\n")
    with pytest.raises(ds.DatasetError, match="measures nothing"):
        ds.load(path)


def test_unanswerable_cannot_also_expect_retrieval(tmp_path):
    path = write(
        tmp_path,
        """
cases:
  - question: what is the SLA?
    unanswerable: true
    expect_documents: [policy.md]
""",
    )
    with pytest.raises(ds.DatasetError, match="cannot also expect"):
        ds.load(path)


def test_duplicate_ids_are_rejected(tmp_path):
    path = write(
        tmp_path,
        """
cases:
  - id: same
    question: a?
    expect_facts: ["x"]
  - id: same
    question: b?
    expect_facts: ["y"]
""",
    )
    with pytest.raises(ds.DatasetError, match="duplicate case id"):
        ds.load(path)


def test_missing_file_and_empty_cases_are_rejected(tmp_path):
    with pytest.raises(ds.DatasetError, match="not found"):
        ds.load(tmp_path / "nope.yaml")

    with pytest.raises(ds.DatasetError, match="non-empty list"):
        ds.load(write(tmp_path, "name: t\ncases: []\n"))


def test_ids_are_generated_from_the_question(tmp_path):
    path = write(
        tmp_path,
        "cases:\n  - question: How long is the refund window?\n    expect_facts: ['30 days']\n",
    )
    assert ds.load(path).cases[0].id == "how-long-is-the-refund-window"


def test_tag_filter(tmp_path):
    path = write(
        tmp_path,
        """
cases:
  - id: a
    question: q1?
    expect_facts: ["x"]
    tags: [smoke]
  - id: b
    question: q2?
    expect_facts: ["y"]
""",
    )
    data = ds.load(path)
    assert [c.id for c in data.filter_by_tag("smoke")] == ["a"]
    assert len(data.filter_by_tag(None)) == 2


def test_bundled_example_dataset_is_valid():
    """The shipped example is the template people copy; a broken one teaches
    the wrong shape."""
    from pathlib import Path

    example = Path(__file__).parent.parent / "app" / "eval" / "datasets" / "example.yaml"
    data = ds.load(example)
    assert len(data.cases) >= 4
    assert any(c.unanswerable for c in data.cases), "example must show a refusal case"
