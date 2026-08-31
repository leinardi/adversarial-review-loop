"""Subcommand dispatch, reached only through ``scripts/arl-bootstrap.py``.

Every subcommand module is imported **inside** its own branch. ``pretool`` runs on every
tool call, so importing ``arm``, ``dryrun`` and the reviewer stack to answer a ``Read`` would
be import cost multiplied by thousands of calls for code that never runs.

The four hook entrypoints return the process exit status their :class:`arl.hookio.Hook`
reports, and that status is the shim's only discriminator: ``0`` means a response was
written in full -- including a legitimately empty one -- and anything else means the shim
must discard what it captured and emit that event's own fail-closed response. Nothing here
may turn a non-zero into a zero. ``scripts/arl.sh`` is the live gate, a guarded shim over
this package -- see "Interpreter invocation" in ``AGENTS.md``.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Final

__all__ = ["HOOK_DEADLINE_SEC", "HOOK_STARTED", "USAGE", "main"]

USAGE = """usage: arl.sh <subcommand> [args]

  arm --session <id> --plan <path> [--allow-dirty] [--until N] [--harness H] [--model X] [--variant V] [--guide <path>]
  resume --session <id> [--until N] [--plan <path>] [--guide <path>] [--allow-dirty] [--abandon-pending] [--harness H] [--model X] [--variant V]
  set-phases --phase "…" [--phase "…" …]
  pretool | confirm-commit | posttool-failure | gate-stop | reorient | intent   (hook entrypoints)
  status | report [n] | defer --reason "…" | finish | deactivate
  pause [N | 0 | all]
  clarify --question "…" [--session <id>]
  accept [--reason "…"] [--session <id>]
  config [<key> <value> [--repo] [--force] | <key> --unset [--repo]]
  dry-run | selftest
"""


#: The shim's own ``timeout`` ceiling for each hook entrypoint, in seconds -- the same
#: numbers ``scripts/arl.sh`` passes to ``timeout`` and a hard maximum here too. They are
#: duplicated rather than derived because the shim is what actually enforces them: this table
#: only has to *agree* with it, and disagreeing in the safe direction (a smaller number here)
#: costs an optional extra call, while trusting the environment for a larger one would let
#: anything that can set an env var talk the gate into starting work the shim will kill.
_HOOK_CEILINGS: Final = {"pretool": 1150, "confirm-commit": 50, "posttool-failure": 20, "gate-stop": 1750, "reorient": 25, "intent": 8}


#: When this process started, as :func:`time.monotonic` reads it. Re-stamped at the top of
#: :func:`main` so it measures the run rather than the import, and read together with
#: :data:`HOOK_DEADLINE_SEC` by ``reviewer.remaining_budget``.
HOOK_STARTED: float = time.monotonic()

#: How many seconds this process has in total before ``scripts/arl.sh`` kills it, or ``None``
#: when it is not one of the four hook entrypoints and no such deadline exists. A *whole-hook*
#: budget, deliberately, not a per-call one: by the time the reviewer is invoked the hook may
#: already have spent the bundle build, ``verify_cmd`` and a session listing, and only a
#: number measured from process entry can say what is left.
HOOK_DEADLINE_SEC: float | None = None


def _hook_deadline(sub: str) -> float | None:
    """This subcommand's whole-hook deadline: ``None`` unless it is a hook entrypoint.

    ``ARL_HOOK_DEADLINE_SEC`` is what the shim passes alongside its own ``timeout``, so a
    test that shrinks the shim timeout (``ARL_SHIM_TIMEOUT_*``) shrinks this in step and the
    two never disagree. It is read from the environment, which is not a trust boundary, so it
    is accepted only as a plain ASCII positive integer **at or below** the subcommand's own
    ceiling -- exactly the clamp ``arl_bounded_timeout`` applies on the other side. Anything
    else falls back to the ceiling, which is what the shim uses when the override is refused.
    """
    ceiling = _HOOK_CEILINGS.get(sub)
    if ceiling is None:
        return None
    raw = os.environ.get("ARL_HOOK_DEADLINE_SEC", "")
    if raw.isascii() and raw.isdigit():
        value = int(raw)
        if 0 < value <= ceiling:
            return float(value)
    return float(ceiling)


def _start_clock(sub: str) -> None:
    """Stamp :data:`HOOK_STARTED` and :data:`HOOK_DEADLINE_SEC` for this run."""
    global HOOK_STARTED, HOOK_DEADLINE_SEC  # noqa: PLW0603 - process-wide facts about this one run, read by reviewer.remaining_budget
    HOOK_STARTED = time.monotonic()
    HOOK_DEADLINE_SEC = _hook_deadline(sub)


def _selftest(argv: list[str]) -> int:
    """Hand over to the acceptance suite, which stays Bash and language-agnostic."""
    import arl  # noqa: PLC0415

    script = str(arl.PLUGIN_ROOT / "tests" / "selftest.sh")
    os.execv(script, [script, *argv])


def main(argv: list[str]) -> int:  # noqa: PLR0911, PLR0912 - one return per subcommand, which is the table
    sub = argv[0] if argv else ""
    rest = argv[1:]
    _start_clock(sub)

    if sub in ("-h", "--help", "help", ""):
        sys.stdout.write(USAGE)
        return 0

    # The hot path first: `pretool` runs on every single tool call, so it is matched before
    # the table a user's typing reaches.
    if sub == "pretool":
        from arl.commands import pretool  # noqa: PLC0415

        return pretool.run(rest)
    if sub == "confirm-commit":
        from arl.commands import posttool  # noqa: PLC0415

        return posttool.confirm_commit(rest)
    if sub == "posttool-failure":
        from arl.commands import posttool  # noqa: PLC0415

        return posttool.posttool_failure(rest)
    if sub == "gate-stop":
        from arl.commands import stop  # noqa: PLC0415

        return stop.run(rest)
    if sub == "reorient":
        from arl.commands import session  # noqa: PLC0415

        return session.reorient(rest)
    if sub == "intent":
        from arl.commands import intent  # noqa: PLC0415

        return intent.run(rest)

    if sub == "arm":
        from arl.commands import arm  # noqa: PLC0415

        return arm.run(rest)
    if sub == "resume":
        from arl.commands import resume  # noqa: PLC0415

        return resume.run(rest)
    if sub == "set-phases":
        from arl.commands import phases  # noqa: PLC0415

        return phases.run(rest)
    if sub in ("defer", "status", "report", "finish", "deactivate"):
        from arl.commands import session  # noqa: PLC0415

        handler = {
            "defer": session.defer,
            "status": session.status,
            "report": session.report_cmd,
            "finish": session.finish,
            "deactivate": session.deactivate,
        }[sub]
        return handler(rest)
    if sub == "pause":
        from arl.commands import pausecmd  # noqa: PLC0415

        return pausecmd.run(rest)
    if sub == "clarify":
        from arl.commands import clarify  # noqa: PLC0415

        return clarify.run(rest)
    if sub == "accept":
        from arl.commands import accept  # noqa: PLC0415

        return accept.run(rest)
    if sub == "config":
        from arl.commands import configcmd  # noqa: PLC0415

        return configcmd.run(rest)
    if sub == "dry-run":
        from arl.commands import dryrun  # noqa: PLC0415

        return dryrun.run(rest)
    if sub == "selftest":
        return _selftest(rest)

    sys.stderr.write(USAGE)
    return 2
