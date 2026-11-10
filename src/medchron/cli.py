"""Command-line entry point. Subcommands are added as the pipeline is built."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from medchron import __version__


def _cmd_synth(args: argparse.Namespace) -> int:
    from medchron.synth import generate_dataset
    from medchron.synth.generate import SPLITS

    seeds_range = SPLITS[args.split]
    start = args.start_seed if args.start_seed is not None else seeds_range.start
    seeds = list(range(start, start + args.cases))
    outside = [s for s in seeds if s not in seeds_range]
    if outside:
        raise SystemExit(
            f"seeds {outside[0]}..{outside[-1]} are outside the {args.split} split "
            f"({seeds_range.start}-{seeds_range.stop - 1}); dev and test seeds never overlap"
        )
    out = Path(args.out or f"data/generated/{args.split}")
    cases = generate_dataset(seeds, out)
    for case in cases:
        g = case.gold
        prods = f", {g.productions} productions" if g.productions > 1 else ""
        print(
            f"{g.case_id}: {g.scenario}, {len(g.pages)} pages{prods}, {len(g.events)} events, "
            f"{len(g.issues)} planted issues"
        )
    print(f"Wrote {len(cases)} {args.split} cases to {out}")
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

    if getattr(args, "extract_mode", None):
        os.environ["MEDCHRON_EXTRACT_MODE"] = args.extract_mode
    provider: Any
    if args.provider == "oracle":
        from medchron.evals.oracle import OracleProvider
        from medchron.synth import CaseGold

        if not args.gold:
            raise SystemExit("--provider oracle needs --gold <case.gold.json>")
        gold = CaseGold.model_validate_json(Path(args.gold).read_text(encoding="utf-8"))
        print("NOTE: oracle provider answers from gold labels (test double, not a model).")
        provider = OracleProvider(gold, noise=args.oracle_noise)
    elif args.provider == "replay":
        return ReplayProvider(Path(args.fixtures))
    else:
        from medchron.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider()
    return RecordingProvider(provider, Path(args.fixtures)) if args.record else provider


def _cmd_run(args: argparse.Namespace) -> int:
    from medchron.orchestrator.pipeline import RunConfig, run_case

    out = Path(args.out) / args.matter
    result = run_case(
        [Path(p) for p in args.pdf],
        args.matter,
        make_provider(args),
        RunConfig(out_dir=out, budget_usd=args.budget, concurrency=args.concurrency),
        progress=print,
    )
    if result.diff is not None:
        d = result.diff
        print(
            f"Version {result.version}: {len(d.added)} events added, {len(d.changed)} changed, "
            f"{len(d.removed)} removed since version {d.from_version}"
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
    if args.extract_mode:
        os.environ["MEDCHRON_EXTRACT_MODE"] = args.extract_mode
    data = Path(args.data or f"data/synthetic/{args.split}")
    result = run_eval(
        data,
        provider_for,
        label=args.label,
        provider_name=args.provider,
        work_dir=Path(args.work) / args.label,
        budget_per_case=args.budget_per_case,
        case_ids=args.cases.split(",") if args.cases else None,
        split=args.split,
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


def _cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    root = Path(args.root)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, "head")
    print("database migrated to head")
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


def _cmd_calib(args: argparse.Namespace) -> int:
    from medchron.evals import calibration as cal

    if args.calib_command == "sample":
        records = cal.sample_claims(Path(args.runs), args.n, seed=args.seed)
        cal.write_jsonl(records, Path(args.out))
        print(f"Wrote {len(records)} claims with blank labels to {args.out}")
        return 0
    if args.calib_command == "seed":
        records = cal.gold_seed_claims(Path(args.data), seed=args.seed)
        cal.write_jsonl(records, Path(args.out))
        print(
            f"Wrote {len(records)} gold-derived claims to {args.out} "
            "(labels by construction, not human judgement)"
        )
        return 0
    if args.calib_command == "judge":
        if args.provider == "oracle":
            from medchron.evals.oracle import OracleJudge

            print("NOTE: oracle judge always answers 'supported' (tooling self-check only).")
            provider: Any = OracleJudge()
        else:
            provider = make_provider(args)
        records = cal.judge_records(cal.read_jsonl(Path(args.file)), provider)
        cal.write_jsonl(records, Path(args.out or args.file))
        print(f"Judged {len(records)} claims -> {args.out or args.file}")
        return 0
    records = [r for f in args.files for r in cal.read_jsonl(Path(f))]
    report = cal.score(records)
    judges = {r.judge.provider for r in records if r.judge}
    text = cal.render(report, judges=judges)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from medchron.db.store import make_engine
    from medchron.mcp_server import build_server, fixed_principal, http_principal
    from medchron.security.auth import Principal, _roles, auth_mode, verify_token

    engine = make_engine()
    if args.transport == "http":
        server = build_server(engine, http_principal)
        server.run("streamable-http", host=args.host, port=args.port)
        return 0
    # stdio serves one local user: a verified token in OIDC mode, or a dev identity.
    if auth_mode() == "oidc":
        token = os.environ.get("MEDCHRON_MCP_TOKEN")
        if not token:
            raise SystemExit("set MEDCHRON_MCP_TOKEN to a bearer token for the MCP user")
        principal = verify_token(token)
    else:
        user = os.environ.get("MEDCHRON_MCP_USER")
        if not user:
            raise SystemExit("dev mode: set MEDCHRON_MCP_USER (and MEDCHRON_MCP_ROLES)")
        principal = Principal(user_id=user, roles=_roles(os.environ.get("MEDCHRON_MCP_ROLES", "")))
    build_server(engine, fixed_principal(principal)).run("stdio")
    return 0


def _cmd_db_users(args: argparse.Namespace) -> int:
    """Create or update the API and worker login roles (run with the owner's DATABASE_URL)."""
    from medchron.db.roles import ensure_login_roles, login_roles_from_env, url_for
    from medchron.db.store import DEFAULT_URL, make_engine

    roles = login_roles_from_env()
    ensure_login_roles(make_engine(), roles)
    owner = os.environ.get("DATABASE_URL", DEFAULT_URL)
    if args.shell:  # for scripts: eval "$(medchron db-users --shell)"
        api, worker = roles
        print(f"export MEDCHRON_API_DATABASE_URL='{url_for(owner, api)}'")
        print(f"export MEDCHRON_WORKER_DATABASE_URL='{url_for(owner, worker)}'")
    else:
        print("login roles ready: " + ", ".join(r.name for r in roles))
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
    p.add_argument("--record", action="store_true", help="record calls as replay fixtures")
    p.add_argument(
        "--extract-mode",
        choices=["sync", "batch"],
        help="batch = send extraction through the Message Batches API (half price, async)",
    )
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
    synth.add_argument(
        "--split", choices=["dev", "test"], default="dev", help="seed range: dev 1-999, test 1000+"
    )
    synth.add_argument(
        "--start-seed", type=int, default=None, help="first seed (default: split start)"
    )
    synth.add_argument("--out", help="output directory (default data/generated/<split>)")
    synth.set_defaults(func=_cmd_synth)

    ingest = sub.add_parser("ingest", help="OCR, de-duplicate and chunk a PDF production")
    ingest.add_argument("pdf", help="path to the PDF")
    ingest.add_argument("--matter", required=True, help="matter / case ID")
    ingest.add_argument("--no-ocr", action="store_true", help="skip OCR of image-only pages")
    ingest.add_argument("--save", action="store_true", help="persist to DATABASE_URL")
    ingest.set_defaults(func=_cmd_ingest)

    run = sub.add_parser("run", help="Run the agent pipeline on a PDF and write a chronology")
    run.add_argument("pdf", nargs="+", help="PDF production(s), in the order they were received")
    run.add_argument("--matter", required=True)
    run.add_argument("--out", default="runs")
    run.add_argument("--budget", type=float, default=None, help="max LLM spend in USD")
    run.add_argument("--concurrency", type=int, default=4)
    _add_provider_args(run)
    run.set_defaults(func=_cmd_run)

    ev = sub.add_parser("eval", help="Evaluate the pipeline against gold labels")
    ev.add_argument(
        "--split",
        choices=["dev", "test"],
        default="dev",
        help="tune prompts on dev only; test is held out for reporting",
    )
    ev.add_argument(
        "--data", help="dataset dir with manifest.json (default data/synthetic/<split>)"
    )
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

    calib = sub.add_parser("calib", help="Verifier calibration: sample, seed, judge, score")
    csub = calib.add_subparsers(dest="calib_command", required=True)
    c_sample = csub.add_parser("sample", help="Sample judged claims from runs for hand labelling")
    c_sample.add_argument("--runs", required=True, help="e.g. runs/evals/<label>")
    c_sample.add_argument("--n", type=int, default=100)
    c_sample.add_argument("--seed", type=int, default=0)
    c_sample.add_argument("--out", required=True, help="JSONL labelling file")
    c_seed = csub.add_parser("seed", help="Build gold-derived claims from synthetic gold")
    c_seed.add_argument("--data", default="data/synthetic")
    c_seed.add_argument("--seed", type=int, default=0)
    c_seed.add_argument("--out", required=True)
    c_judge = csub.add_parser("judge", help="Run the verifier over a labelling file")
    c_judge.add_argument("file")
    c_judge.add_argument("--out", help="write here instead of updating the file in place")
    _add_provider_args(c_judge)
    c_score = csub.add_parser("score", help="Judge vs label agreement")
    c_score.add_argument("files", nargs="+")
    c_score.add_argument("--out", help="also write the Markdown report here")
    calib.set_defaults(func=_cmd_calib)

    mcp = sub.add_parser("mcp", help="Serve the read-only case tools over MCP")
    mcp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    mcp.add_argument("--host", default="127.0.0.1")
    mcp.add_argument("--port", type=int, default=8811)
    mcp.set_defaults(func=_cmd_mcp)

    users = sub.add_parser("db-users", help="Create the API and worker database login roles")
    users.add_argument("--shell", action="store_true", help="print export lines for the URLs")
    users.set_defaults(func=_cmd_db_users)

    mig = sub.add_parser("migrate", help="Apply database migrations (DATABASE_URL)")
    mig.add_argument("--root", default=".", help="directory containing alembic.ini")
    mig.set_defaults(func=_cmd_migrate)
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
