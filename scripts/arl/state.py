"""The run state: one ``state.json`` under ``<launch dir>/.arl/``, and every transition on it.

Layout::

    <launch dir>/.arl/
        state.json          the document below
        lock                held across every read-modify-write
        plan.md             the plan as it was handed to ``init``
        shadow/<alias>.git  the snapshot repository of each non-git root
        runs/<run id>/      an earlier run, archived by ``init --new``

The document holds the run id, the plan, the phases and their revision, the roots, the
approvals, the round counter of the current stage, the pending review ticket and whatever the
run is waiting on the user for. It is the one source :func:`next_action` answers from, so the
mod never decides on its own whether the loop goes on.

**Stages.** A run with no phases is in ``plan``; with an unapproved phase, in ``phase``; after the
last phase approval it is in ``final`` when a final review is required (``final_review`` frozen
at ``init``, or ``/arl-finish``), and ``done`` otherwise. ``status`` turns ``complete`` only on the
approval that leaves nothing in front of the run, so a required final review is never skipped.

v4 enforces nothing, and this file is not a trust boundary: an agent can edit it. What the
document must get right is that no transition reports an approval nobody gave (AGENTS.md rule 1).
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

import json
import secrets
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from arl.atomic import locked, write_private_atomic
from arl.errors import OcrlError, StateLoadError

__all__ = [
    "RUN_DIR_NAME",
    "STATE_VERSION",
    "STATUSES",
    "WAIT_KINDS",
    "RunError",
    "RunPaths",
    "RunState",
    "locked_run_dir",
    "locked_state",
    "new_document",
    "next_action",
    "run_paths",
    "start_prompt",
]

RUN_DIR_NAME: Final = ".arl"
STATE_VERSION: Final = 4

#: ``active`` runs; ``paused`` waits for ``resume``; ``stopped`` and ``complete`` are final.
STATUSES: Final = ("active", "paused", "stopped", "complete")

#: The decisions a run can be waiting on the user for, besides a root's baseline (which is read
#: off the roots themselves, so it cannot go stale).
WAIT_KINDS: Final = ("stall", "failures", "oversized", "needs-human")

#: The waits only the user ends; the others clear once their condition is gone.
_DECISIONS: Final = frozenset({"stall", "needs-human"})

Stage = Literal["plan", "phase", "final", "done"]


class RunError(OcrlError):
    """A command refused on the state it found. ``code`` is the stable token the mod branches on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunPaths:
    """Where one launch directory keeps its run."""

    launch_dir: Path
    run_dir: Path

    @property
    def state_file(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.run_dir / "lock"

    @property
    def plan_file(self) -> Path:
        return self.run_dir / "plan.md"

    @property
    def shadow_dir(self) -> Path:
        return self.run_dir / "shadow"

    @property
    def runs_dir(self) -> Path:
        return self.run_dir / "runs"


def run_paths(launch_dir: Path) -> RunPaths:
    """The run layout under ``launch_dir``, which is resolved so every stored path is absolute."""
    resolved = launch_dir.resolve()
    return RunPaths(launch_dir=resolved, run_dir=resolved / RUN_DIR_NAME)


def new_document(*, plan_source: str, final_review: bool) -> dict[str, Any]:
    """A fresh run: no phases, no roots yet (``init`` adds them), ``active``."""
    return {
        "version": STATE_VERSION,
        "run_id": secrets.token_hex(6),
        "created_at_ns": time.time_ns(),
        "status": "active",
        "plan": {"file": "plan.md", "source": plan_source},
        "phases": [],
        "phases_revision": 0,
        "roots": [],
        "approvals": [],
        "rounds": 0,
        "round_history": [],
        "clarify_history": [],
        "review_seq": 0,
        "stall_floor": 0,
        "failures": 0,
        "transient_failures": 0,
        "final_review": final_review,
        "final_requested": False,
        "final_approved": False,
        "pending_ticket": None,
        "last_ticket": None,
        "waiting": None,
        "last_continuation": "",
        # Aliases of removed roots: never given again, since round history and stall anchors
        # still name them.
        "retired_aliases": [],
    }


#: Every key and the type it must hold. A document missing one, or holding the wrong type, is
#: refused as a whole rather than half-read: a run whose approvals cannot be read must not
#: continue as if it had none.
_SCHEMA: Final[Mapping[str, type | tuple[type, ...]]] = {
    "version": int,
    "run_id": str,
    "created_at_ns": int,
    "status": str,
    "plan": dict,
    "phases": list,
    "phases_revision": int,
    "roots": list,
    "approvals": list,
    "rounds": int,
    "round_history": list,
    "clarify_history": list,
    "review_seq": int,
    "stall_floor": int,
    "failures": int,
    "transient_failures": int,
    "final_review": bool,
    "final_requested": bool,
    "final_approved": bool,
    "pending_ticket": (dict, type(None)),
    "last_ticket": (dict, type(None)),
    "waiting": (dict, type(None)),
    "last_continuation": str,
}


def _validated(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StateLoadError("state.json does not hold an object")
    if document.get("version") != STATE_VERSION:
        raise StateLoadError(f"state.json is version {document.get('version')!r}, this engine reads {STATE_VERSION}")
    for key, kind in _SCHEMA.items():
        if not isinstance(document.get(key), kind) or (kind is int and isinstance(document.get(key), bool)):
            raise StateLoadError(f"state.json field {key!r} is missing or malformed")
    if document["status"] not in STATUSES:
        raise StateLoadError(f"state.json has an unknown status {document['status']!r}")
    if not all(isinstance(phase, str) for phase in document["phases"]):
        raise StateLoadError("state.json phases are not all text")
    for root in document["roots"]:
        if (
            not isinstance(root, dict)
            or not all(isinstance(root.get(key), str) for key in ("alias", "path", "kind", "baseline", "start_tree", "approved_tree"))
            or root["kind"] not in ("git", "dir")
            or root["baseline"] not in ("set", "unknown")
            or (root["baseline"] == "set" and not (root["start_tree"] and root["approved_tree"]))
        ):
            raise StateLoadError("state.json holds a malformed root")
    return document


class RunState:
    """The loaded document, with the transitions every command applies to it.

    Never constructed directly by a command: :func:`locked_state` loads it under the lock, and
    only a command holding that lock may call :meth:`save`.
    """

    def __init__(self, paths: RunPaths, data: dict[str, Any]) -> None:
        self.paths = paths
        self.data = data

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, paths: RunPaths) -> RunState | None:
        """The run under ``paths``, ``None`` when there is none, or :class:`StateLoadError`."""
        try:
            raw = paths.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateLoadError(f"cannot read {paths.state_file}: {exc}") from exc
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise StateLoadError(f"{paths.state_file} is not JSON: {exc}") from exc
        return cls(paths, _validated(document))

    def save(self) -> None:
        text = json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        write_private_atomic(self.paths.state_file, text, root=self.paths.run_dir)

    # -- reading ------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return str(self.data["run_id"])

    @property
    def status(self) -> str:
        return str(self.data["status"])

    @property
    def phases(self) -> list[str]:
        return list(self.data["phases"])

    @property
    def roots(self) -> list[dict[str, Any]]:
        return list(self.data["roots"])

    @property
    def approved_phases(self) -> int:
        return sum(1 for entry in self.data["approvals"] if isinstance(entry, dict) and entry.get("stage") == "phase")

    @property
    def final_required(self) -> bool:
        return bool(self.data["final_review"] or self.data["final_requested"])

    def stage(self) -> Stage:
        if not self.data["phases"]:
            return "plan"
        if self.approved_phases < len(self.data["phases"]):
            return "phase"
        if self.final_required and not self.data["final_approved"]:
            return "final"
        return "done"

    def current_phase(self) -> int:
        """The 1-based phase under work, or ``0`` outside the ``phase`` stage."""
        return self.approved_phases + 1 if self.stage() == "phase" else 0

    def label(self) -> str:
        """The name the current stage's reviews are recorded under: ``phase<N>`` or ``final``."""
        return "final" if self.stage() == "final" else f"phase{self.current_phase()}"

    def rounds_of(self, label: str) -> list[dict[str, Any]]:
        """``round_history`` entries of ``label`` under the current phase list.

        A replan renumbers what ``phase<N>`` means, and reopens the work a final review judged,
        so earlier rounds count only when they were recorded against this ``phases_revision``.
        """
        revision = self.data["phases_revision"]
        return [
            entry
            for entry in self.data["round_history"]
            if isinstance(entry, dict) and entry.get("label") == label and entry.get("revision") == revision
        ]

    def next_review_seq(self) -> int:
        self.data["review_seq"] += 1
        return int(self.data["review_seq"])

    def unknown_baselines(self) -> list[dict[str, Any]]:
        return [root for root in self.data["roots"] if root.get("baseline") == "unknown"]

    def root(self, alias: str) -> dict[str, Any] | None:
        return next((root for root in self.data["roots"] if root.get("alias") == alias), None)

    # -- transitions --------------------------------------------------------

    def require_status(self, *allowed: str) -> None:
        if self.status not in allowed:
            raise RunError(f"run-{self.status}", f"the run is {self.status}")

    def set_phases(self, phases: Sequence[str], *, replan: bool) -> None:
        """Replace the phase list.

        Before any approval the whole list is replaced. After one, only ``replan`` may change
        it, and then ``phases`` is the **remaining** list: the approved phases are kept as they
        were reviewed, and the new entries follow them. Either way the revision moves and any
        pending ticket is invalidated, since a review prepared against the old list would be
        recorded against a phase that no longer exists.
        """
        self.require_status("active", "paused")
        cleaned = [phase.strip() for phase in phases]
        if not cleaned or any(not phase for phase in cleaned):
            raise RunError("bad-phases", "set_phases needs a non-empty list of non-empty phase descriptions")
        approved = self.approved_phases
        if approved and not replan:
            raise RunError("phases-approved", f"{approved} phase(s) are already approved; changing the list needs --replan")
        self.data["phases"] = [*self.data["phases"][:approved], *cleaned]
        self.data["phases_revision"] += 1
        self.data["rounds"] = 0
        self.data["compact_due"] = False
        self.invalidate_ticket("replanned")

    def approve(self, trees: Mapping[str, str], *, by: Literal["review", "user", "ignored"]) -> None:
        """Approve the current phase, or the final review, at ``trees`` (alias to snapshot tree).

        Every root's ``approved_tree`` moves to the tree given for it, so the next phase is
        diffed from exactly what was approved. A root without a tree in ``trees`` is refused: an
        approval that leaves one root's baseline behind would put this phase's changes in front
        of the next phase's reviewer as if they were new.
        """
        stage = self.stage()
        if stage not in ("phase", "final"):
            raise RunError("nothing-to-approve", f"the run has nothing to approve in stage {stage!r}")
        missing = [root["alias"] for root in self.data["roots"] if not trees.get(root["alias"])]
        if missing:
            raise RunError("missing-tree", f"no snapshot for root(s): {', '.join(missing)}")
        for root in self.data["roots"]:
            root["approved_tree"] = trees[root["alias"]]
        self.data["approvals"].append(
            {"stage": stage, "phase": self.current_phase(), "by": by, "at": int(time.time()), "rounds": self.data["rounds"]},
        )
        if stage == "final":
            self.data["final_approved"] = True
        self.data["rounds"] = 0
        self.data["waiting"] = None
        self.data["failures"] = self.data["transient_failures"] = 0
        self.invalidate_ticket("approved")
        # The next stage may start on a compacted conversation (`auto_compact`).
        self.data["compact_due"] = True
        if self.stage() == "done":
            self.data["status"] = "complete"

    def record_changes_required(self) -> None:
        """One more round of the current stage ended with findings to fix."""
        self.data["rounds"] += 1

    def wait_for_user(self, kind: str, detail: str) -> None:
        """Wait for the user on ``kind``. A stall or a reviewer's call for the user is a decision
        only the user ends, so a later oversized file or failure budget never replaces it: those
        clear themselves, and would take the decision with them."""
        if kind not in WAIT_KINDS:
            raise ValueError(f"unknown wait kind {kind!r}")
        current = self.data["waiting"]
        if isinstance(current, dict) and current.get("kind") in _DECISIONS and kind not in _DECISIONS:
            return
        self.data["waiting"] = {"kind": kind, "detail": detail}

    def pending_decision(self) -> dict[str, Any] | None:
        """The wait only the user ends (a stall, a reviewer's call for the user), or ``None``."""
        waiting = self.data["waiting"]
        return waiting if isinstance(waiting, dict) and waiting.get("kind") in _DECISIONS else None

    def clear_wait(self, kind: str) -> None:
        """Drop the decision the run waits on, if it is ``kind``: the condition has gone away."""
        waiting = self.data["waiting"]
        if isinstance(waiting, dict) and waiting.get("kind") == kind:
            self.data["waiting"] = None

    def request_final(self) -> None:
        """``/arl-finish``: require the cumulative review, before or after the last phase approval.

        On a run that completed without one, this reopens it into the ``final`` stage: the
        cumulative review diffs every root from its ``start_tree``, so nothing is lost by asking
        for it late. A run whose final review is already approved has nothing left to finish.
        """
        self.require_status("active", "paused", "complete")
        if self.data["final_approved"]:
            raise RunError("final-done", "the final review is already approved")
        self.data["final_requested"] = True
        if self.status == "complete":
            self.data["status"] = "active"

    def issue_ticket(self, ticket: Mapping[str, Any]) -> None:
        """Make ``ticket`` the pending review; a ticket still pending is superseded by it.

        ``ticket["session"]``, when present, is the session that runs the review; :meth:`resume`
        keeps a ticket from its own session and abandons any other.
        """
        self.invalidate_ticket("superseded")
        self.data["pending_ticket"] = dict(ticket)

    def invalidate_ticket(self, why: str) -> None:
        """Retire the pending ticket, if any, so a result prepared for it can no longer land."""
        ticket = self.data["pending_ticket"]
        if ticket is not None:
            self.data["last_ticket"] = {"review_id": ticket.get("review_id", ""), "status": why}
            self.data["pending_ticket"] = None

    def pause(self) -> None:
        self.require_status("active")
        self.data["status"] = "paused"

    def resume(self, *, session: str = "") -> dict[str, Any]:
        """Continue in this session: unpause, abandon a dead review, clear a waiting decision.

        A pending ticket issued by another session (or by no recorded session, or when the caller
        names none) belongs to a review nothing will finish, so it is abandoned and Claude is told
        to ask for the review again. A ticket this same ``session`` issued is kept: the mod's "keep
        going" answer passes its session while its own review tool call is the one asking.
        ``/arl-resume`` names no session (a command runs between turns, so no review of its session
        is in flight) and always abandons it. A decision the run was waiting on (a stall, the failure budget) is
        cleared: ``resume`` is the user's "keep going". The continuation token is reset, so this
        session is prompted even if the state is otherwise unchanged.
        """
        self.require_status("active", "paused")
        if self.pending_decision() is not None and self.data["waiting"]["kind"] == "stall":
            # "Keep going" past a stall: the rounds that stalled no longer count towards the
            # next one, or the very next round would stall again on the same window.
            self.data["stall_floor"] = self.data["review_seq"]
        ticket = self.data["pending_ticket"]
        abandoned = ticket is not None and not (session and ticket.get("session") == session)
        if abandoned:
            self.invalidate_ticket("abandoned")
        self.data["status"] = "active"
        self.data["waiting"] = None
        self.data["failures"] = self.data["transient_failures"] = 0
        self.data["last_continuation"] = ""
        return {"abandoned_review": abandoned, "pending_review": self.data["pending_ticket"] is not None}

    def stop(self) -> None:
        self.require_status("active", "paused")
        self.invalidate_ticket("stopped")
        self.data["status"] = "stopped"

    # -- reporting ----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """What ``status`` prints and the mod draws: no tree ids, no history."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage(),
            "phase": self.current_phase(),
            "phase_count": len(self.data["phases"]),
            "phases": self.phases,
            "phases_revision": self.data["phases_revision"],
            "approved_phases": self.approved_phases,
            "rounds": self.data["rounds"],
            "final_required": self.final_required,
            "pending_review": self.data["pending_ticket"] is not None,
            "waiting": self.data["waiting"],
            "roots": [{key: root.get(key, "") for key in ("alias", "path", "kind", "baseline", "proposed")} for root in self.data["roots"]],
        }


@contextmanager
def locked_state(paths: RunPaths) -> Iterator[RunState | None]:
    """Hold the run lock and yield the loaded run (``None`` when there is none).

    The caller saves explicitly; leaving the block without :meth:`RunState.save` changes nothing
    on disk. Only ``init`` makes ``.arl/`` (through :func:`locked_run_dir`); every other command
    finds no ``.arl/`` and answers "no run" without writing anything, so a project the plugin
    merely looks at never gains an untracked directory. The launch directory itself is never
    touched.
    """
    if not paths.run_dir.is_dir():
        yield None
        return
    with locked_run_dir(paths):
        yield RunState.load(paths)


@contextmanager
def locked_run_dir(paths: RunPaths) -> Iterator[None]:
    """Make ``.arl/`` and hold its lock, loading nothing: ``init`` decides what to do with a
    document that does not load."""
    # `.arl/` is made here, by hand, and every private write is rooted *at* it: the atomic
    # helpers tighten their root to 0700, and rooting them at the launch directory would chmod
    # the user's own project directory.
    paths.run_dir.mkdir(mode=0o700, exist_ok=True)
    # `.arl/` ignores itself, so a repository created around it later -- a run started in an
    # empty folder whose first phase is `git init` -- never stages the run's state. The
    # `info/exclude` entry covers only repositories that already exist when a root registers.
    ignore = paths.run_dir / ".gitignore"
    if not ignore.exists():
        write_private_atomic(ignore, "*\n", root=paths.run_dir)
    with locked(paths.lock_file, root=paths.run_dir):
        yield


def _token(run: RunState) -> str:
    """Changes whenever the run moves: a new phase list, an approval, a finished round."""
    return ":".join(str(part) for part in (run.run_id, run.stage(), run.data["phases_revision"], len(run.data["approvals"]), run.data["rounds"]))


def _prompt(run: RunState) -> str:
    stage = run.stage()
    if stage == "plan":
        return "The plan has no phases yet. Call set_phases with the plan's phases, then implement phase 1."
    if stage == "final":
        if run.data["rounds"]:
            return "The final review asked for changes. Fix the cumulative findings, run the gates, then call review again."
        return (
            "Every phase is approved. Commit the approved phase by each repository's conventions, "
            "then run the final review: call review for the whole run."
        )
    phase = run.current_phase()
    count = len(run.data["phases"])
    description = run.data["phases"][phase - 1]
    if run.data["rounds"]:
        return f"The review of phase {phase} of {count} asked for changes. Fix the findings, run the gates, then call review again."
    lead = "Commit the approved phase by each repository's conventions, then implement" if run.approved_phases else "Implement"
    return f"{lead} phase {phase} of {count}: {description}\nRun the project's gates, then call review."


def start_prompt(run: RunState, verify_cmd: str) -> str:
    """What ``/arl-implement`` submits to start Claude on a new run: the loop, once."""
    roots = "; ".join(f"{root['alias']} = {root['path']}" for root in run.roots)
    checks = f"run the project's checks (`{verify_cmd}`)" if verify_cmd else "run the project's checks (tests, linters, type checks)"
    return (
        f"Implement the plan {run.data['plan']['source']} under adversarial-review-loop (run {run.run_id}); "
        f"a copy is at {run.paths.plan_file}.\n\n"
        "1. Read the plan, then call set_phases with its phases in order, one short title each, before changing any file.\n"
        "2. Implement one phase at a time. Edit and Write register the folders they write to; before you change "
        f"anything through Bash outside the workspace roots ({roots}), call add_root for that directory.\n"
        f"3. When a phase is implemented, {checks}, then call review with what they reported.\n"
        "4. Fix what the review finds and call review again. On approval, commit each changed git repository by "
        "its own conventions and go on with the next phase.\n\n"
        "The loop prompts you after each turn while work remains. When a review hands a decision to the user, stop and tell them."
    )


def _blocker(run: RunState) -> dict[str, Any] | None:
    """Why the run must yield to the user whatever the token says, or ``None``."""
    if run.status != "active":
        return {"reason": run.status}
    if run.data["pending_ticket"] is not None:
        return {"reason": "review-pending"}
    if run.data["waiting"] is not None:
        return {"reason": "waiting", "waiting": run.data["waiting"]}
    unknown = run.unknown_baselines()
    if unknown:
        return {"reason": "baseline-unknown", "roots": [root["alias"] for root in unknown]}
    if run.stage() == "done":
        return {"reason": "complete"}
    return None


def next_action(run: RunState | None) -> dict[str, Any]:
    """Whether the loop goes on after a main-loop turn, and the prompt that continues it.

    ``continue`` only when the run is active, no review is pending, no decision is waiting on the
    user (a stall, the failure budget, an oversized diff, a root's baseline), work remains, and
    the run has moved since the last continuation. The token is recorded on ``continue`` so the
    same state is never prompted twice: a turn that ends without changing anything yields.
    Every other answer is ``yield``, and the caller saves the run only when it continued.
    """
    if run is None:
        return {"action": "yield", "reason": "no-run"}
    blocked = _blocker(run)
    if blocked is not None:
        return {"action": "yield", **blocked}
    token = _token(run)
    if token == run.data["last_continuation"]:
        return {"action": "yield", "reason": "unchanged", "token": token}
    run.data["last_continuation"] = token
    return {"action": "continue", "stage": run.stage(), "phase": run.current_phase(), "prompt": _prompt(run), "token": token}
