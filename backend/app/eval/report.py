"""Rendering and regression comparison.

A single run tells you a number. What you actually need is whether the number
moved, and which cases moved it — so comparison against a saved baseline is the
primary output, not an afterthought.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.eval.runner import EvalReport

# Below this, a metric change is noise rather than signal.
SIGNIFICANT = 0.02


def to_json(report: EvalReport, path: str | Path) -> Path:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return file


def _bar(value: float, width: int = 20) -> str:
    filled = int(round(value * width))
    return "█" * filled + "·" * (width - filled)


def render(report: EvalReport, *, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"\n  {report.dataset}  —  k={report.k}  —  {report.duration_seconds:.1f}s")
    lines.append("  " + "─" * 62)

    if report.retrieval_summary:
        s = report.retrieval_summary
        lines.append(f"\n  RETRIEVAL  ({s['questions']} questions)")
        for key in ("recall@k", "precision@k", "mrr", "ndcg@k", "hit_rate"):
            if key in s:
                lines.append(f"    {key:<14} {s[key]:.3f}  {_bar(s[key])}")

    if report.generation_summary:
        g = report.generation_summary
        answers = g.get("answers", 0)
        lines.append(f"\n  GENERATION  ({answers} answers judged)")
        for key in ("groundedness", "citation_accuracy", "fact_coverage", "refusal_accuracy"):
            if key in g:
                lines.append(f"    {key:<18} {g[key]:.3f}  {_bar(g[key])}")
        if g.get("violations"):
            lines.append(f"    {'violations':<18} {g['violations']}")
        if g.get("broken_citations"):
            lines.append(f"    {'broken citations':<18} {g['broken_citations']}")

    failures = [c for c in report.cases if c.error]
    weak = [
        c
        for c in report.cases
        if c.retrieval and not c.retrieval["hit"] and not c.error
    ]
    ungrounded = [
        c
        for c in report.cases
        if c.generation and c.generation.get("groundedness", 1.0) < 0.5 and not c.error
    ]

    if weak:
        lines.append(f"\n  MISSED RETRIEVAL  ({len(weak)})")
        for case in weak[:10]:
            lines.append(f"    · {case.id}")
            lines.append(f"        {case.question[:70]}")
            if case.missed:
                lines.append(f"        wanted: {', '.join(case.missed[:3])}")

    if ungrounded:
        lines.append(f"\n  UNGROUNDED ANSWERS  ({len(ungrounded)})")
        for case in ungrounded[:10]:
            lines.append(f"    · {case.id}  groundedness={case.generation['groundedness']:.2f}")
            for violation in case.generation.get("violations", [])[:2]:
                lines.append(f"        {violation[:80]}")

    if failures:
        lines.append(f"\n  ERRORS  ({len(failures)})")
        for case in failures[:10]:
            lines.append(f"    · {case.id}: {case.error[:80]}")

    if verbose:
        lines.append("\n  PER CASE")
        for case in report.cases:
            bits = []
            if case.retrieval:
                bits.append(f"r@k={case.retrieval['recall@k']:.2f}")
                bits.append(f"mrr={case.retrieval['mrr']:.2f}")
            if case.generation:
                bits.append(f"grnd={case.generation['groundedness']:.2f}")
            mark = "!" if case.error else ("x" if case.retrieval and not case.retrieval["hit"] else " ")
            lines.append(f"   {mark} {case.id:<34} {'  '.join(bits)}  {case.latency_ms}ms")

    lines.append("")
    return "\n".join(lines)


@dataclass(slots=True)
class Delta:
    metric: str
    before: float
    after: float

    @property
    def change(self) -> float:
        return self.after - self.before

    @property
    def regressed(self) -> bool:
        return self.change < -SIGNIFICANT


def compare(baseline_path: str | Path, report: EvalReport) -> tuple[list[Delta], list[str]]:
    """Returns (metric deltas, case ids that got worse)."""
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))

    deltas: list[Delta] = []
    for section in ("retrieval", "generation"):
        old = baseline.get(section) or {}
        new = (report.retrieval_summary if section == "retrieval" else report.generation_summary)
        for metric, before in old.items():
            if not isinstance(before, (int, float)) or metric in {"questions", "answers"}:
                continue
            after = new.get(metric)
            if isinstance(after, (int, float)):
                deltas.append(Delta(f"{section}.{metric}", float(before), float(after)))

    old_cases = {c["id"]: c for c in baseline.get("cases", [])}
    worse: list[str] = []
    for case in report.cases:
        old = old_cases.get(case.id)
        if not old:
            continue
        old_hit = (old.get("retrieval") or {}).get("hit")
        new_hit = (case.retrieval or {}).get("hit")
        if old_hit and not new_hit:
            worse.append(f"{case.id}: retrieval hit -> miss")
            continue
        old_g = (old.get("generation") or {}).get("groundedness")
        new_g = (case.generation or {}).get("groundedness")
        if (
            isinstance(old_g, (int, float))
            and isinstance(new_g, (int, float))
            and new_g < old_g - 0.2
        ):
            worse.append(f"{case.id}: groundedness {old_g:.2f} -> {new_g:.2f}")

    return deltas, worse


def render_comparison(deltas: list[Delta], worse: list[str]) -> str:
    lines = ["\n  VS BASELINE", "  " + "─" * 62]
    for delta in deltas:
        arrow = "▲" if delta.change > SIGNIFICANT else ("▼" if delta.regressed else "=")
        lines.append(
            f"    {arrow} {delta.metric:<28} {delta.before:.3f} -> {delta.after:.3f}"
            f"  ({delta.change:+.3f})"
        )
    if worse:
        lines.append(f"\n  CASES THAT GOT WORSE  ({len(worse)})")
        lines.extend(f"    · {w}" for w in worse[:15])
    lines.append("")
    return "\n".join(lines)


def has_regression(deltas: list[Delta], worse: list[str]) -> bool:
    return any(d.regressed for d in deltas) or bool(worse)
