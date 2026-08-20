"""Golden dataset loading and validation.

A dataset is a YAML file of questions with what a correct answer must contain
and which documents it should come from. Relevance is expressed against
*filenames and text snippets* rather than chunk IDs, because chunk IDs change
every time the chunker changes — which is precisely when you most want the
dataset to still be valid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class DatasetError(ValueError):
    pass


@dataclass(slots=True)
class GoldenCase:
    id: str
    question: str
    # Filenames that should appear among the retrieved sources.
    expect_documents: list[str] = field(default_factory=list)
    # Substrings that should appear in at least one retrieved passage. Survives
    # re-chunking, unlike a chunk id.
    expect_snippets: list[str] = field(default_factory=list)
    # Facts the answer must state. Checked by the judge, not by string match.
    expect_facts: list[str] = field(default_factory=list)
    # Things the answer must NOT claim — the regression guard for known
    # hallucinations.
    forbid: list[str] = field(default_factory=list)
    # True when the corpus genuinely cannot answer this. The correct behaviour
    # is to say so, and that is worth testing explicitly.
    unanswerable: bool = False
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def has_retrieval_expectations(self) -> bool:
        return bool(self.expect_documents or self.expect_snippets)


@dataclass(slots=True)
class Dataset:
    name: str
    cases: list[GoldenCase]
    description: str = ""

    def filter_by_tag(self, tag: str | None) -> list[GoldenCase]:
        if not tag:
            return self.cases
        return [c for c in self.cases if tag in c.tags]


def _as_list(value: Any, field_name: str, case_id: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise DatasetError(f"case {case_id!r}: {field_name} must be a string or list of strings")


def load(path: str | Path) -> Dataset:
    import yaml

    file = Path(path)
    if not file.exists():
        raise DatasetError(f"dataset not found: {file}")

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DatasetError(f"{file}: invalid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise DatasetError(f"{file}: top level must be a mapping")

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetError(f"{file}: 'cases' must be a non-empty list")

    cases: list[GoldenCase] = []
    seen: set[str] = set()

    for index, entry in enumerate(raw_cases):
        if not isinstance(entry, dict):
            raise DatasetError(f"{file}: case {index} must be a mapping")

        question = entry.get("question")
        if not isinstance(question, str) or not question.strip():
            raise DatasetError(f"{file}: case {index} needs a non-empty 'question'")

        case_id = str(entry.get("id") or _slug(question, index))
        if case_id in seen:
            # Duplicate ids would silently collapse in the per-case report.
            raise DatasetError(f"{file}: duplicate case id {case_id!r}")
        seen.add(case_id)

        case = GoldenCase(
            id=case_id,
            question=question.strip(),
            expect_documents=_as_list(entry.get("expect_documents"), "expect_documents", case_id),
            expect_snippets=_as_list(entry.get("expect_snippets"), "expect_snippets", case_id),
            expect_facts=_as_list(entry.get("expect_facts"), "expect_facts", case_id),
            forbid=_as_list(entry.get("forbid"), "forbid", case_id),
            unanswerable=bool(entry.get("unanswerable", False)),
            tags=_as_list(entry.get("tags"), "tags", case_id),
            notes=str(entry.get("notes") or ""),
        )

        if case.unanswerable and case.has_retrieval_expectations:
            raise DatasetError(
                f"case {case_id!r}: unanswerable cases cannot also expect retrieval hits"
            )
        if not case.unanswerable and not (case.has_retrieval_expectations or case.expect_facts):
            raise DatasetError(
                f"case {case_id!r}: needs expect_documents, expect_snippets, or expect_facts — "
                "a case with no expectations cannot fail, so it measures nothing"
            )

        cases.append(case)

    return Dataset(
        name=str(raw.get("name") or file.stem),
        description=str(raw.get("description") or ""),
        cases=cases,
    )


def _slug(question: str, index: int) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", question.lower()).split()[:6]
    return "-".join(words) or f"case-{index}"
