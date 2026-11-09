"""Command-line entry point. Subcommands are added as the pipeline is built."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

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
