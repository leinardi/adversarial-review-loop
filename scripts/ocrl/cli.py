"""Subcommand dispatch, reached only through ``scripts/ocrl-bootstrap.py``.

The subcommand table is filled in during phase 5 of the port; until the entrypoint is
flipped, ``scripts/ocrl.sh`` remains the live gate and this dispatcher only answers for
``help``.
"""

from __future__ import annotations

import sys

__all__ = ["USAGE", "main"]

USAGE = """usage: ocrl.sh <subcommand> [args]

  arm --session <id> --plan <path> [--allow-dirty]
  set-phases --phase "…" [--phase "…" …]
  pretool | confirm-commit | posttool-failure | gate-stop   (hook entrypoints)
  status | report [n] | defer --reason "…" | finish | deactivate
  dry-run | selftest
"""


def main(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    if sub in ("-h", "--help", "help", ""):
        sys.stdout.write(USAGE)
        return 0
    sys.stderr.write(USAGE)
    return 2
