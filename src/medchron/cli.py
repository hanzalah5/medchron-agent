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


def _cmd_eval(args: argparse.Namespace) -> int:
    import datetime as dt

    from medchron.evals.report import render
    from medchron.evals.runner import EvalResult, regressions, run_eval

    def provider_for(gold: Any) -> Any:
        if args.provider == "oracle":
            from medchron.evals.oracle import OracleProvider

            return OracleProvider(gold, noise=args.oracle_noise, seed=gold.seed)
        return make_provider(args)

    if args.provider == "oracle":
        print("NOTE: oracle provider = gold-label test double. Results are NOT model results.")
    result = run_eval(
        Path(args.data),
        provider_for,
        label=args.label,
        provider_name=args.provider,
        work_dir=Path(args.work) / args.label,
        budget_per_case=args.budget_per_case,
        case_ids=args.cases.split(",") if args.cases else None,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{dt.date.today().isoformat()}-{args.label}.json"
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    baseline = (
        EvalResult.model_validate_json(Path(args.baseline).read_text(encoding="utf-8"))
        if args.baseline
        else None
    )
    print(render(result, baseline))
    print(f"\nResults written to {path}")
    if baseline is not None:
        regressed = regressions(result, baseline, args.tolerance)
        if regressed:
            print("REGRESSION vs baseline: " + "; ".join(regressed))
            return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("medchron.api.app:create_app", factory=True, host=args.host, port=args.port)
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from medchron.db.store import make_engine
    from medchron.jobs import work

    n = work(make_engine(), once=args.once)
    print(f"processed {n} job(s)")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from medchron.evals.report import render_report
    from medchron.evals.runner import EvalResult

    results = [
        EvalResult.model_validate_json(Path(p).read_text(encoding="utf-8")) for p in args.results
    ]
    Path(args.out).write_text(render_report(results), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


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

    ev = sub.add_parser("eval", help="Evaluate the pipeline against gold labels")
    ev.add_argument("--data", default="data/synthetic", help="dataset dir with manifest.json")
    ev.add_argument("--label", required=True, help="name for this evaluation run")
    ev.add_argument("--out", default="evals/results")
    ev.add_argument("--work", default="runs/evals", help="pipeline outputs and checkpoints")
    ev.add_argument("--cases", help="comma-separated case IDs (default: all)")
    ev.add_argument("--budget-per-case", type=float, default=None)
    ev.add_argument("--baseline", help="results JSON to compare against (regression gate)")
    ev.add_argument("--tolerance", type=float, default=0.02, help="allowed metric drop")
    _add_provider_args(ev)
    ev.set_defaults(func=_cmd_eval)

    rep = sub.add_parser("eval-report", help="Render evals/REPORT.md from results files")
    rep.add_argument("results", nargs="+", help="results JSON files, newest first")
    rep.add_argument("--out", default="evals/REPORT.md")
    rep.set_defaults(func=_cmd_report)

    serve = sub.add_parser("serve", help="Run the HTTP API (localhost, synthetic data only)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=_cmd_serve)

    worker = sub.add_parser("worker", help="Process queued pipeline jobs")
    worker.add_argument("--once", action="store_true", help="drain the queue, then exit")
    worker.set_defaults(func=_cmd_worker)
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
