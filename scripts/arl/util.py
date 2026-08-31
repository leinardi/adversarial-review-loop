"""Small helpers ported from ``scripts/lib/common.sh``: logging, clock, truncation."""

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

import contextlib
import sys
import time
import traceback

__all__ = ["TRUNCATION_MARKER", "log", "log_exception", "now", "truncate"]

TRUNCATION_MARKER = "[... truncated at {limit} bytes; the full report is on disk, print it with /adversarial-review-loop:report ...]"


def log(message: str) -> None:
    """Write a diagnostic to stderr, never stdout (Rule 2: hook stdout is protocol).

    **This function never raises.** Diagnostics are best-effort by design, because the
    alternative is far worse than a lost log line: a caller reporting that stdout is broken
    would have its ``OutputFailure`` displaced by an ``OSError`` from *this* function, the
    top-level guard would read that as an ordinary crash, and it would then append a
    fail-closed response to the partial one already on stdout. Two concatenated JSON
    objects do not parse, and a ``PreToolUse`` response that does not parse is not a denial.
    """
    with contextlib.suppress(OSError, ValueError):
        sys.stderr.write(f"arl: {message}\n")
        sys.stderr.flush()


def log_exception() -> None:
    """Best-effort traceback to stderr. Never raises, for the reason ``log`` documents."""
    with contextlib.suppress(OSError, ValueError):
        traceback.print_exc(file=sys.stderr)


def now() -> int:
    """Seconds since the epoch, matching the shell's ``printf '%(%s)T' -1``."""
    return int(time.time())


def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit``, appending a marker when it had to cut.

    The shell original is named ``arl_truncate_bytes`` but measures with ``${#text}`` and
    slices with ``${text:0:N}``, both of which count *characters*. That behaviour is
    preserved here; only the name is corrected, since the user-visible marker still says
    "bytes" and is matched by the test suite.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n{TRUNCATION_MARKER.format(limit=limit)}"
