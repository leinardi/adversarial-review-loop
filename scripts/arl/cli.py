"""Subcommand dispatch, reached only through ``scripts/arl-bootstrap.py``.

The engine is a command-line program the mod runs: each subcommand reads its arguments, does
one job and writes its answer to stdout. Diagnostics go to stderr through :func:`arl.util.log`.
Every subcommand module is imported inside its own branch, so a call pays only for the code it
runs.
"""

#  This file is part of adversarial-review-loop.
#
#  Copyright (c) 2026 Roberto Leinardi
#
#  adversarial-review-loop is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  adversarial-review-loop is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import sys

__all__ = ["USAGE", "main"]

USAGE = """usage: arl.sh <subcommand> [args]

  (no subcommands yet: the v4 engine commands arrive in the next phases)
"""


def main(argv: list[str]) -> int:
    sub = argv[0] if argv else ""
    if sub in ("-h", "--help", "help", ""):
        sys.stdout.write(USAGE)
        return 0
    sys.stderr.write(f"arl: unknown subcommand {sub!r}\n{USAGE}")
    return 2
