"""Probing OpenCode's model list, shared by ``arm``, ``resume`` and ``config``.

Only the subprocess call and its parsing live here. Whether an unreachable reviewer is a
hard refusal or a soft warning is a per-caller decision -- arming refuses outright, because
an activation whose every commit fails review for an operational reason produces denials
that look like findings; the ``config`` command only warns, because an unreachable
``opencode`` at config time is not the same failure as an unreachable one at arm time, and
arming will refuse anyway if it is still unreachable then.
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

import subprocess
from typing import Final

__all__ = ["MODELS_PROBE_TIMEOUT", "ProbeFailed", "list_models"]

MODELS_PROBE_TIMEOUT: Final = 60


class ProbeFailed(Exception):
    """``opencode models`` did not complete and answer normally.

    Covers a process that could not start, one that timed out (whatever it had printed
    before the deadline is not a confirmed list -- a model can be one line into printing the
    rest when the clock runs out, and treating that partial output as the whole answer would
    make a slow-but-working reviewer look like it does not support a model it does), one
    that exited non-zero, and one that exited zero but printed nothing. None of these confirm
    what the reviewer actually supports, so no caller may read a model list out of them.
    """


def list_models(timeout: float = MODELS_PROBE_TIMEOUT) -> list[str]:
    """The models ``opencode models`` reports. Raises ``ProbeFailed`` otherwise.

    Assumes the caller has already checked ``opencode`` is on ``PATH`` -- that check carries
    a distinct message at every call site, so it is not folded in here.
    """
    try:
        proc = subprocess.run(["opencode", "models"], capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeFailed(f"`opencode models` did not answer within {timeout:g}s") from exc
    except OSError as exc:
        raise ProbeFailed(str(exc)) from exc
    if proc.returncode != 0:
        raise ProbeFailed(f"`opencode models` exited {proc.returncode}")
    probe = proc.stdout.rstrip("\n")
    if not probe:
        raise ProbeFailed("`opencode models` printed nothing")
    return probe.split("\n")
