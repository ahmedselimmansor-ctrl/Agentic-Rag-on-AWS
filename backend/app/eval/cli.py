"""`python -m app.eval` — run a golden dataset and report.

    python -m app.eval run   datasets/example.yaml --user you@example.com
    python -m app.eval run   d.yaml --user you@x.com --retrieval-only
    python -m app.eval run   d.yaml --user you@x.com --baseline base.json --fail-on-regression
    python -m app.eval check datasets/example.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.core.logging import configure_logging
from app.eval import dataset as dataset_module
from app.eval import report as report_module
from app.eval.runner import run_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.eval", description="RAG evaluation harness")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a dataset against a real corpus")
    run.add_argument("dataset", help="path to the golden dataset YAML")
    run.add_argument("--user", required=True, help="email of the account owning the corpus")
    run.add_argument("-k", type=int, default=5, help="cutoff for recall@k etc (default 5)")
    run.add_argument("--retrieval-only", action="store_true", help="skip generation and judging")
    run.add_argument("--no-judge", action="store_true", help="generate but do not judge")
    run.add_argument("--judge-model", default=None, help="override the judging model")
    run.add_argument("--tag", default=None, help="only run cases carrying this tag")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--json", dest="json_out", default=None, help="write the full report here")
    run.add_argument("--baseline", default=None, help="compare against a saved report")
    run.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero when a metric drops or a case gets worse",
    )
    run.add_argument("-v", "--verbose", action="store_true", help="per-case table")

    check = sub.add_parser("check", help="validate a dataset without running it")
    check.add_argument("dataset")

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    try:
        data = dataset_module.load(args.dataset)
    except dataset_module.DatasetError as exc:
        print(f"  invalid: {exc}", file=sys.stderr)
        return 1

    tags = sorted({t for c in data.cases for t in c.tags})
    unanswerable = sum(1 for c in data.cases if c.unanswerable)
    print(f"\n  {data.name}: {len(data.cases)} cases")
    if data.description:
        print(f"  {data.description}")
    print(f"    with retrieval expectations : {sum(1 for c in data.cases if c.has_retrieval_expectations)}")
    print(f"    with required facts         : {sum(1 for c in data.cases if c.expect_facts)}")
    print(f"    unanswerable (refusal tests): {unanswerable}")
    print(f"    tags                        : {', '.join(tags) or '(none)'}\n")

    if not unanswerable:
        # Not an error, but worth saying: a suite with no unanswerable cases
        # never tests the failure mode that matters most in RAG.
        print("  note: no unanswerable cases — nothing here tests whether the")
        print("        system admits when the corpus cannot answer.\n")
    return 0


async def cmd_run(
    args: argparse.Namespace, baseline: Path | None, baseline_missing: bool
) -> int:
    try:
        data = dataset_module.load(args.dataset)
    except dataset_module.DatasetError as exc:
        print(f"  invalid dataset: {exc}", file=sys.stderr)
        return 1

    try:
        result = await run_dataset(
            data,
            user_email=args.user,
            k=args.k,
            generate=not args.retrieval_only,
            judge=not (args.retrieval_only or args.no_judge),
            judge_model=args.judge_model,
            concurrency=args.concurrency,
            tag=args.tag,
        )
    except RuntimeError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    print(report_module.render(result, verbose=args.verbose))

    if args.json_out:
        path = report_module.to_json(result, args.json_out)
        print(f"  report written to {path}\n")

    exit_code = 0
    if baseline is not None:
        if baseline_missing:
            print(f"  baseline {baseline} not found — skipping comparison\n", file=sys.stderr)
        else:
            deltas, worse = report_module.compare(baseline, result)
            print(report_module.render_comparison(deltas, worse))
            if args.fail_on_regression and report_module.has_regression(deltas, worse):
                print("  FAILED: regression against baseline\n", file=sys.stderr)
                exit_code = 1

    if any(c.error for c in result.cases):
        print(f"  note: {sum(1 for c in result.cases if c.error)} case(s) errored\n")

    return exit_code


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING")  # the report is the output; logs are noise here
    args = build_parser().parse_args(argv)

    if args.command == "check":
        return cmd_check(args)

    # Filesystem checks stay synchronous — blocking syscalls do not belong in
    # the event loop, and this one can fail fast before any provider is called.
    baseline = Path(args.baseline) if args.baseline else None
    baseline_missing = baseline is not None and not baseline.exists()

    return asyncio.run(cmd_run(args, baseline, baseline_missing))


if __name__ == "__main__":
    raise SystemExit(main())
