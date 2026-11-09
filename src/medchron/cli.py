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
