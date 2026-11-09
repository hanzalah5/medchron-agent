"""Command-line entry point. Subcommands are added as the pipeline is built."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from medchron import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medchron",
        description="Grounded medical record review agents (synthetic data only).",
    )
    parser.add_argument("--version", action="version", version=f"medchron {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
