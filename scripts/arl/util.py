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
import datetime
import sys
import time
import traceback

__all__ = ["TRUNCATION_MARKER", "decode", "encode", "format_at", "log", "log_exception", "now", "truncate"]

TRUNCATION_MARKER = "[... truncated at {limit} bytes; the full report is on disk, print it with /adversarial-review-loop:report ...]"


def log(message: str) -> None:
    """Write a diagnostic to stderr, never stdout (AGENTS.md rule 3: the engine's stdout is its answer).

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


def format_at(at: object) -> str:
    """Epoch seconds read out of ``state.json`` rendered as UTC, for a human to read.

    Every caller passes a value that came out of a state document, which is not a trust
    boundary, so this must answer for *any* object rather than raise. ``fromtimestamp``
    raises ``OverflowError`` or ``OSError`` -- not only ``TypeError``/``ValueError`` -- for a
    value the platform's C library cannot represent, and a huge or negative integer is
    exactly what an edited document supplies, so all four are caught: a report that names an
    unreadable time is still a report, while an exception raised inside a hook is a
    fail-closed denial that says nothing useful.
    """
    if isinstance(at, bool) or not isinstance(at, (int, float, str)):
        return "(unknown time)"
    try:
        seconds = int(at)
        return datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "(unknown time)"


def decode(raw: bytes) -> str:
    """Bytes from git or the reviewer as text, losslessly and without raising.

    ``surrogateescape`` round-trips: whatever is written back out with :func:`encode` is
    byte-identical to what came in.
    """
    return raw.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """The inverse of :func:`decode`."""
    return text.encode("utf-8", "surrogateescape")


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
