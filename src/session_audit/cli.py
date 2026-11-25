"""Command-line entry point for session-audit."""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from . import __version__
from .textsafe import write_stderr

PROG = "session-audit"
DESCRIPTION = "Analyze Claude session transcripts and outcomes: retros and usage analytics."
USAGE_ERROR = 2


class _Parser(argparse.ArgumentParser):
    """argparse echoes user text in its errors; print it through the sanitizer."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        write_stderr(f"{self.prog}: error: {message}")
        raise SystemExit(USAGE_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description=DESCRIPTION,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON output")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help(sys.stderr)
        return USAGE_ERROR
    return 0
