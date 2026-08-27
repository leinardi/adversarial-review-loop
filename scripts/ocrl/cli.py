"""Subcommand dispatch, reached only through ``scripts/ocrl-bootstrap.py``.

Every subcommand module is imported **inside** its own branch. ``pretool`` runs on every
tool call, so importing ``arm``, ``dryrun`` and the reviewer stack to answer a ``Read`` would
be import cost multiplied by thousands of calls for code that never runs.

The four hook entrypoints return the process exit status their :class:`ocrl.hookio.Hook`
reports, and that status is the shim's only discriminator: ``0`` means a response was
written in full -- including a legitimately empty one -- and anything else means the shim
must discard what it captured and emit that event's own fail-closed response. Nothing here
may turn a non-zero into a zero. ``scripts/ocrl.sh`` is the live gate, a guarded shim over
this package -- see "Interpreter invocation" in ``AGENTS.md``.
"""

from __future__ import annotations

import sys

__all__ = ["USAGE", "main"]

USAGE = """usage: ocrl.sh <subcommand> [args]

  arm --session <id> --plan <path> [--allow-dirty] [--until N] [--model X] [--variant V]
  resume --session <id> [--until N] [--plan <path>] [--allow-dirty] [--abandon-pending] [--model X] [--variant V]
  set-phases --phase "…" [--phase "…" …]
  pretool | confirm-commit | posttool-failure | gate-stop   (hook entrypoints)
  status | report [n] | defer --reason "…" | finish | deactivate
  clarify --question "…" [--session <id>]
  accept [--reason "…"] [--session <id>]
  config [<key> <value> [--repo] [--force] | <key> --unset [--repo]]
  dry-run | selftest
"""


def _selftest(argv: list[str]) -> int:
    """Hand over to the acceptance suite, which stays Bash and language-agnostic."""
    import os  # noqa: PLC0415 - only this branch needs it

    import ocrl  # noqa: PLC0415

    script = str(ocrl.PLUGIN_ROOT / "tests" / "selftest.sh")
    os.execv(script, [script, *argv])


def main(argv: list[str]) -> int:  # noqa: PLR0911, PLR0912 - one return per subcommand, which is the table
    sub = argv[0] if argv else ""
    rest = argv[1:]

    if sub in ("-h", "--help", "help", ""):
        sys.stdout.write(USAGE)
        return 0

    # The hot path first: `pretool` runs on every single tool call, so it is matched before
    # the table a user's typing reaches.
    if sub == "pretool":
        from ocrl.commands import pretool  # noqa: PLC0415

        return pretool.run(rest)
    if sub == "confirm-commit":
        from ocrl.commands import posttool  # noqa: PLC0415

        return posttool.confirm_commit(rest)
    if sub == "posttool-failure":
        from ocrl.commands import posttool  # noqa: PLC0415

        return posttool.posttool_failure(rest)
    if sub == "gate-stop":
        from ocrl.commands import stop  # noqa: PLC0415

        return stop.run(rest)

    if sub == "arm":
        from ocrl.commands import arm  # noqa: PLC0415

        return arm.run(rest)
    if sub == "resume":
        from ocrl.commands import resume  # noqa: PLC0415

        return resume.run(rest)
    if sub == "set-phases":
        from ocrl.commands import phases  # noqa: PLC0415

        return phases.run(rest)
    if sub in ("defer", "status", "report", "finish", "deactivate"):
        from ocrl.commands import session  # noqa: PLC0415

        handler = {
            "defer": session.defer,
            "status": session.status,
            "report": session.report_cmd,
            "finish": session.finish,
            "deactivate": session.deactivate,
        }[sub]
        return handler(rest)
    if sub == "clarify":
        from ocrl.commands import clarify  # noqa: PLC0415

        return clarify.run(rest)
    if sub == "accept":
        from ocrl.commands import accept  # noqa: PLC0415

        return accept.run(rest)
    if sub == "config":
        from ocrl.commands import configcmd  # noqa: PLC0415

        return configcmd.run(rest)
    if sub == "dry-run":
        from ocrl.commands import dryrun  # noqa: PLC0415

        return dryrun.run(rest)
    if sub == "selftest":
        return _selftest(rest)

    sys.stderr.write(USAGE)
    return 2
