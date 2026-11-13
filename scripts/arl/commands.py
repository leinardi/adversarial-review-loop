"""The engine's run commands: each takes parsed arguments and answers one JSON object.

Every command runs under the state lock (:func:`arl.state.locked_state`) and saves only when it
changed something. A refusal is :class:`arl.state.RunError`, which :mod:`arl.cli` prints as
``{"ok": false, "code": ..., "error": ...}``; the mod branches on ``code``, never on the prose.
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

import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from arl import config, review, workspace
from arl.atomic import ensure_private_dir, write_private_atomic
from arl.errors import StateLoadError
from arl.state import RunError, RunPaths, RunState, locked_run_dir, locked_state, new_document, next_action

__all__ = ["COMMANDS", "plan_named_paths"]

#: Path-looking tokens in a plan: absolute, ``~/``, ``./`` or ``../``, up to whitespace, a quote,
#: a bracket or a backtick. Trailing sentence punctuation is stripped after the match.
_PLAN_PATH_RE: Final = re.compile(r"(?<![\w/.~])(?:~|\.{1,2})?/[^\s`'\"()<>\[\]{},;|]+")

#: What stays in ``.arl/`` across runs: the lock, the archives and the self-ignore.
_RUN_DIR_FIXTURES: Final = frozenset({"lock", "runs", ".gitignore"})

Command = Callable[[RunPaths, Mapping[str, Any]], dict[str, Any]]


def _require(run: RunState | None) -> RunState:
    if run is None:
        raise RunError("no-run", "there is no run here; start one with /implement")
    return run


def plan_named_paths(text: str, launch_dir: Path, home: Path) -> list[Path]:
    """Existing paths the plan names that can be roots, in the order they appear.

    Only paths under ``home`` or beside the launch directory (under its parent), with no hidden
    component below that base, count -- so ``~/.claude/plans/x.md`` and ``/usr/bin/python3`` are
    never taken for workspace, while a sibling ``../other-repo`` is. Only a path inside a git
    worktree counts, and the worktree is then the root: a plan naming ``~/Workspace`` or a
    document in ``~/Downloads`` must not turn a whole folder tree into a root to snapshot. A
    plain folder joins through ``--root``, the ``roots`` config key or ``root-add``.
    """
    found: list[Path] = []
    # The filesystem root is never a base: it would make every path on the machine a candidate.
    bases = [base for base in (home, launch_dir.parent) if base.parent != base]
    for match in _PLAN_PATH_RE.finditer(text):
        token = match.group(0).rstrip(".:!?")
        path = Path(token).expanduser()
        if not path.is_absolute():
            path = launch_dir / path
        try:
            path = path.resolve()
        except OSError:
            continue
        base = next((base for base in bases if base in path.parents), None)
        if base is None or not path.exists():
            continue
        if any(part.startswith(".") for part in path.relative_to(base).parts):
            continue
        if workspace.resolve(path)[1] != "git":
            continue
        if path not in found:
            found.append(path)
    return found


def _archive(paths: RunPaths, name: str) -> str:
    """Move the old run under ``runs/<name>/``, keeping the lock and earlier archives in place."""
    dest = paths.runs_dir / name
    ensure_private_dir(dest, root=paths.run_dir)
    for entry in paths.run_dir.iterdir():
        if entry.name in _RUN_DIR_FIXTURES:
            continue
        entry.rename(dest / entry.name)
    return name


def cmd_init(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """Start a run: copy the plan, register and baseline every root it can find.

    Refuses while a run is active or paused unless ``new``, which archives it; a stopped or
    complete run is archived without asking.
    """
    plan_arg = Path(str(args["plan"])).expanduser()
    plan_source = (plan_arg if plan_arg.is_absolute() else paths.launch_dir / plan_arg).resolve()
    try:
        plan_text = plan_source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RunError("no-plan", f"cannot read the plan {plan_source}: {exc}") from exc
    cfg = config.load(str(paths.launch_dir))
    with locked_run_dir(paths):
        archived = ""
        try:
            existing = RunState.load(paths)
        except StateLoadError:
            if not args.get("new"):
                raise
            # An unreadable run cannot be resumed; `--new` is the way out, and keeps the bytes.
            existing = None
            archived = _archive(paths, f"corrupt-{time.time_ns()}")
        if existing is not None:
            if existing.status in ("active", "paused") and not args.get("new"):
                raise RunError("run-exists", f"run {existing.run_id} is {existing.status}: use /resume, or /implement --new <plan>")
            archived = _archive(paths, existing.run_id)
        try:
            run = _start(paths, plan_source, plan_text, args, cfg)
        except BaseException:
            # Nothing half-built may replace the run that was there: put it back.
            _discard_new_run(paths)
            if archived:
                _unarchive(paths, archived)
            raise
        return {"ok": True, "run": run.summary(), "archived": archived}


def _start(paths: RunPaths, plan_source: Path, plan_text: str, args: Mapping[str, Any], cfg: config.Config) -> RunState:
    run = RunState(paths, new_document(plan_source=str(plan_source), final_review=cfg.as_bool("final_review")))
    write_private_atomic(paths.plan_file, plan_text, root=paths.run_dir)
    home = Path(os.environ.get("HOME", str(Path.home()))).resolve()
    candidates = [
        paths.launch_dir,
        *(Path(str(root)).expanduser() for root in args.get("roots") or []),
        *(Path(str(root)).expanduser() for root in cfg.as_list("roots")),
        *plan_named_paths(plan_text, paths.launch_dir, home),
    ]
    for candidate in candidates:
        workspace.register(run, candidate if candidate.is_absolute() else paths.launch_dir / candidate, late=False)
    run.save()
    return run


def _discard_new_run(paths: RunPaths) -> None:
    """Remove what a failed ``init`` wrote: everything in ``.arl/`` but its fixtures."""
    for entry in paths.run_dir.iterdir():
        if entry.name in _RUN_DIR_FIXTURES:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _unarchive(paths: RunPaths, run_id: str) -> None:
    source = paths.runs_dir / run_id
    for entry in source.iterdir():
        entry.rename(paths.run_dir / entry.name)
    source.rmdir()


def cmd_resume(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    with locked_state(paths) as loaded:
        run = _require(loaded)
        outcome = run.resume(session=str(args.get("session") or ""))
        run.save()
        return {"ok": True, "run": run.summary(), **outcome}


def cmd_set_phases(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    phases = args.get("phases")
    if not isinstance(phases, list) or not all(isinstance(phase, str) for phase in phases):
        raise RunError("bad-phases", "set-phases reads a JSON array of strings on stdin")
    with locked_state(paths) as loaded:
        run = _require(loaded)
        run.set_phases(phases, replan=bool(args.get("replan")))
        run.save()
        return {"ok": True, "run": run.summary()}


def cmd_root_add(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    with locked_state(paths) as loaded:
        run = _require(loaded)
        run.require_status("active", "paused")
        target = Path(str(args["path"])).expanduser()
        outcome = workspace.register(run, target if target.is_absolute() else paths.launch_dir / target, late=True)
        run.save()
        return {"ok": True, **outcome}


def cmd_root_baseline(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    with locked_state(paths) as loaded:
        run = _require(loaded)
        run.require_status("active", "paused")
        tree = workspace.set_baseline(run, str(args["alias"]), str(args["choice"]), commit=str(args.get("commit") or ""))
        # The user's choice is what the loop was waiting on: let the next turn end continue it,
        # even though the choice moves nothing the continuation token counts.
        run.data["last_continuation"] = ""
        run.save()
        return {"ok": True, "alias": args["alias"], "tree": tree, "run": run.summary()}


def cmd_status(paths: RunPaths, _args: Mapping[str, Any]) -> dict[str, Any]:
    with locked_state(paths) as run:
        return {"ok": True, "run": run.summary() if run is not None else None}


def _transition(name: str) -> Command:
    def command(paths: RunPaths, _args: Mapping[str, Any]) -> dict[str, Any]:
        with locked_state(paths) as loaded:
            run = _require(loaded)
            getattr(run, name)()
            run.save()
            return {"ok": True, "run": run.summary()}

    return command


def cmd_accept(paths: RunPaths, _args: Mapping[str, Any]) -> dict[str, Any]:
    """``/accept``: the user approves the current phase, or the final review, as it stands now."""
    with locked_state(paths) as loaded:
        run = _require(loaded)
        run.require_status("active", "paused")
        # A repository created inside a root since the last look must become a root before
        # anything is snapshotted: unregistered, it breaks its container's snapshot (no commit)
        # or rides into it as an unreviewed gitlink. Found, its unknown baseline refuses below.
        if workspace.discover(run, late=True):
            run.save()
        try:
            trees = workspace.snapshot_all(run)
        except workspace.Oversized as exc:
            # An approval moves every root to a snapshot, and this one cannot be taken.
            raise RunError(
                "oversized", f"{exc}\nAccepting cannot get past this: remove the file(s), or ignore them in the root's .gitignore."
            ) from exc
        run.approve(trees, by="user")
        run.save()
        return {"ok": True, "run": run.summary()}


def cmd_next_action(paths: RunPaths, _args: Mapping[str, Any]) -> dict[str, Any]:
    with locked_state(paths) as run:
        answer = next_action(run)
        if run is not None and answer["action"] == "continue":
            run.save()
        return {"ok": True, **answer}


COMMANDS: Final[Mapping[str, Command]] = {
    "init": cmd_init,
    "resume": cmd_resume,
    "set-phases": cmd_set_phases,
    "root-add": cmd_root_add,
    "root-baseline": cmd_root_baseline,
    "status": cmd_status,
    "pause": _transition("pause"),
    "stop": _transition("stop"),
    "finish": _transition("request_final"),
    "accept": cmd_accept,
    "next-action": cmd_next_action,
    "review-prepare": review.prepare,
    "review-record": review.record,
    "review-run-external": review.run_external,
    "review-abandon": review.abandon,
    "clarify-prepare": review.clarify_prepare,
    "clarify-record": review.clarify_record,
    "clarify-run-external": review.clarify_run_external,
}
