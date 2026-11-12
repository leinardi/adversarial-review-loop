"""Subcommand dispatch: parse the argv, run the command, print its one JSON answer.

Rule 3: stdout carries only the answer. A refusal is still an answer -- ``{"ok": false,
"code": ..., "error": ...}`` with exit status 1 -- so the mod reads every outcome the same way;
a usage error goes to stderr with status 2.
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

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from arl.commands import COMMANDS
from arl.errors import OcrlError, StateLoadError
from arl.state import RunError, run_paths
from arl.util import log, log_exception

__all__ = ["USAGE", "main"]

USAGE = """usage: arl.sh [--dir LAUNCH_DIR] <subcommand> [args]

  init --plan FILE [--root DIR]... [--new]   start a run (--new archives the current one)
  resume [--session ID]                      continue the run in this session
  set-phases [--replan]                      read a JSON array of phase descriptions on stdin
  root-add PATH                              register a root before changing it
  root-baseline ALIAS (--commit REV | --empty | --current)
  status [--json]                            the run summary
  pause | stop | finish | accept             user decisions
  next-action                                whether the loop continues after a turn
  review prepare [--session ID]              prepare the current stage's review; optional JSON
                                             {"verification": [...]} on stdin
  review record ID (--answer FILE | --failed transient|operational [--error TEXT])
  review run-external ID                     run the opencode reviewer and record its answer
  clarify prepare                            one question (stdin) about the latest round
  clarify record SEQ N --answer FILE [--run-id ID]
  clarify run-external                       ask the opencode reviewer (question on stdin)

Every subcommand prints one JSON object on stdout.
"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        sys.stderr.write(f"arl: {message}\n{USAGE}")
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="arl.sh", add_help=False)
    parser.add_argument("--dir", default=".")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    init = sub.add_parser("init", add_help=False)
    init.add_argument("--plan", required=True)
    init.add_argument("--root", action="append", dest="roots", default=[])
    init.add_argument("--new", action="store_true")
    phases = sub.add_parser("set-phases", add_help=False)
    phases.add_argument("--replan", action="store_true")
    root_add = sub.add_parser("root-add", add_help=False)
    root_add.add_argument("path")
    baseline = sub.add_parser("root-baseline", add_help=False)
    baseline.add_argument("alias")
    choice = baseline.add_mutually_exclusive_group(required=True)
    choice.add_argument("--commit", default=None)
    choice.add_argument("--empty", action="store_const", const="empty", dest="choice")
    choice.add_argument("--current", action="store_const", const="current", dest="choice")
    status = sub.add_parser("status", add_help=False)
    # Accepted and ignored: every answer is JSON already, and the plan's spelling is `status --json`.
    status.add_argument("--json", action="store_true", dest="_json")
    resume = sub.add_parser("resume", add_help=False)
    resume.add_argument("--session", default="")
    for name in ("pause", "stop", "finish", "accept", "next-action"):
        sub.add_parser(name, add_help=False)
    review = sub.add_parser("review", add_help=False).add_subparsers(dest="action", parser_class=_Parser)
    prepare = review.add_parser("prepare", add_help=False)
    prepare.add_argument("--session", default="")
    record = review.add_parser("record", add_help=False)
    record.add_argument("review_id")
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--answer")
    outcome.add_argument("--failed", choices=("transient", "operational"))
    record.add_argument("--error", default="")
    review.add_parser("run-external", add_help=False).add_argument("review_id")
    clarify = sub.add_parser("clarify", add_help=False).add_subparsers(dest="action", parser_class=_Parser)
    clarify.add_parser("prepare", add_help=False)
    clarify.add_parser("run-external", add_help=False)
    answer = clarify.add_parser("record", add_help=False)
    answer.add_argument("round_seq", type=int)
    answer.add_argument("question", type=int)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--run-id", dest="run_id", default="")
    return parser


#: The first word of every subcommand: ``review prepare`` is the ``review-prepare`` command.
_TOP_LEVEL: frozenset[str] = frozenset(name.split("-", 1)[0] if name.startswith(("review-", "clarify-")) else name for name in COMMANDS)

#: Subcommands whose stdin is read, and the key it lands under.
_STDIN: dict[str, str] = {"set-phases": "phases", "review-prepare": "verification", "clarify-prepare": "question", "clarify-run-external": "question"}


def _read_stdin(name: str) -> Any:
    """What a command reads on stdin: JSON, except a clarify question, which is text.

    Unreadable input becomes ``None``, which the command refuses with its own code.
    """
    try:
        raw = sys.stdin.read()
    except ValueError:  # UnicodeDecodeError included
        return None
    if name.startswith("clarify-"):
        return raw
    if name == "review-prepare" and not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        # Verification that cannot be read is refused, not dropped: the reviewer would be told
        # nothing was reported.
        return "malformed" if name == "review-prepare" else None
    if name == "review-prepare":
        return value.get("verification") if isinstance(value, dict) else "malformed"
    return value


def _answer(payload: dict[str, Any], status: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return status


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if argv[0] not in _TOP_LEVEL and not argv[0].startswith("-"):
        sys.stderr.write(f"arl: unknown subcommand {argv[0]!r}\n{USAGE}")
        return 2
    parsed = vars(_parser().parse_args(argv))
    name = parsed.pop("command", None)
    parsed.pop("_json", None)
    action = parsed.pop("action", None)
    if name in ("review", "clarify"):
        if action is None:
            sys.stderr.write(USAGE)
            return 2
        name = f"{name}-{action}"
    if name is None:
        sys.stderr.write(USAGE)
        return 2
    if name == "root-baseline" and parsed.get("commit") is not None:
        parsed["choice"] = "commit"
    if name in _STDIN:
        parsed[_STDIN[name]] = _read_stdin(name)
    return _run(name, parsed)


def _run(name: str, parsed: dict[str, Any]) -> int:
    """Run one command and print its answer, a refusal or failure included."""
    try:
        return _answer(COMMANDS[name](run_paths(Path(parsed.pop("dir"))), parsed), 0)
    except RunError as exc:
        return _answer({"ok": False, "code": exc.code, "error": str(exc)}, 1)
    except StateLoadError as exc:
        log(f"{name}: {exc}")
        return _answer({"ok": False, "code": "state-corrupt", "error": f"{exc}; start over with /implement --new <plan>"}, 1)
    except OcrlError as exc:
        log(f"{name}: {exc}")
        return _answer({"ok": False, "code": str(getattr(exc, "code", "error")), "error": str(exc)}, 1)
    except Exception as exc:  # noqa: BLE001 - the mod must get an answer, never a bare traceback
        log_exception()
        return _answer({"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {exc}"}, 1)
