"""Command-line entry point. Subcommands are added as the pipeline is built."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from medchron import __version__


def _cmd_synth(args: argparse.Namespace) -> int:
    from medchron.synth import generate_dataset

    seeds = list(range(args.start_seed, args.start_seed + args.cases))
    cases = generate_dataset(seeds, Path(args.out))
    for case in cases:
        g = case.gold
        print(
            f"{g.case_id}: {g.scenario}, {len(g.pages)} pages, {len(g.events)} events, "
            f"{len(g.issues)} planted issues"
        )
    print(f"Wrote {len(cases)} cases to {args.out}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from medchron.ingestion import ingest_pdf

    case = ingest_pdf(Path(args.pdf), args.matter, ocr=not args.no_ocr)
    for p in case.pages:
        flags = []
        if p.duplicate_of:
            flags.append(f"duplicate of p{p.duplicate_of}")
        if p.needs_review:
            flags.append("NEEDS REVIEW")
        conf = f" ocr={p.ocr_confidence}" if p.ocr_confidence is not None else ""
        print(f"p{p.page_no:>4} {p.source.value:<10}{conf} words={len(p.words)} {' '.join(flags)}")
    print(f"{len(case.pages)} pages, {len(case.spans)} spans")
    if args.save:
        from sqlalchemy.orm import Session

        from medchron.db.store import make_engine, save_ingested
        from medchron.retrieval.embeddings import get_embedder

        with Session(make_engine()) as session, session.begin():
            save_ingested(session, case, get_embedder())
        print(f"Saved matter {case.case_id} to the database")
    return 0


def make_provider(args: argparse.Namespace) -> Any:
    """Build the LLM provider for ``run`` and ``eval`` from CLI flags."""
    from medchron.llm.providers import RecordingProvider, ReplayProvider

    if args.provider == "oracle":
        from medchron.evals.oracle import OracleProvider
        from medchron.synth import CaseGold

        if not args.gold:
            raise SystemExit("--provider oracle needs --gold <case.gold.json>")
        gold = CaseGold.model_validate_json(Path(args.gold).read_text(encoding="utf-8"))
        print("NOTE: oracle provider answers from gold labels (test double, not a model).")
        return OracleProvider(gold, noise=args.oracle_noise)
    if args.provider == "replay":
        return ReplayProvider(Path(args.fixtures))
    from medchron.llm.anthropic_provider import AnthropicProvider

    live = AnthropicProvider()
    return RecordingProvider(live, Path(args.fixtures)) if args.record else live


def _cmd_run(args: argparse.Namespace) -> int:
    from medchron.orchestrator.pipeline import RunConfig, run_case

    out = Path(args.out) / args.matter
    result = run_case(
        Path(args.pdf),
        args.matter,
        make_provider(args),
        RunConfig(out_dir=out, budget_usd=args.budget, concurrency=args.concurrency),
        progress=print,
    )
    print(f"Outputs in {out}/ (chronology.md, chronology.csv, chronology.json, run.json)")
    return 0 if result.summary.status == "completed" else 2


def _add_provider_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--provider",
        choices=["anthropic", "replay", "oracle"],
        default="anthropic",
        help="anthropic = live Claude API; replay = recorded fixtures; oracle = gold-label "
        "test double (not a model)",
    )
    p.add_argument("--fixtures", default="tests/fixtures/llm", help="record/replay directory")
    p.add_argument("--record", action="store_true", help="record live calls as fixtures")
    p.add_argument("--gold", help="gold JSON (oracle provider only)")
    p.add_argument("--oracle-noise", type=float, default=0.0, help="oracle error rate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medchron",
        description="Grounded medical record review agents (synthetic data only).",
    )
    parser.add_argument("--version", action="version", version=f"medchron {__version__}")
    sub = parser.add_subparsers(dest="command")

    synth = sub.add_parser("synth", help="Generate synthetic benchmark cases (PDF + gold JSON)")
    synth.add_argument("--cases", type=int, default=5, help="number of cases")
    synth.add_argument("--start-seed", type=int, default=1, help="first seed (one seed per case)")
    synth.add_argument("--out", default="data/generated", help="output directory")
    synth.set_defaults(func=_cmd_synth)

    ingest = sub.add_parser("ingest", help="OCR, de-duplicate and chunk a PDF production")
    ingest.add_argument("pdf", help="path to the PDF")
    ingest.add_argument("--matter", required=True, help="matter / case ID")
    ingest.add_argument("--no-ocr", action="store_true", help="skip OCR of image-only pages")
    ingest.add_argument("--save", action="store_true", help="persist to DATABASE_URL")
    ingest.set_defaults(func=_cmd_ingest)

    run = sub.add_parser("run", help="Run the agent pipeline on a PDF and write a chronology")
    run.add_argument("pdf")
    run.add_argument("--matter", required=True)
    run.add_argument("--out", default="runs")
    run.add_argument("--budget", type=float, default=None, help="max LLM spend in USD")
    run.add_argument("--concurrency", type=int, default=4)
    _add_provider_args(run)
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
