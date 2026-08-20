"""Activation state: the session pointer, ``state.json`` and the derived status.

Layout under ``$XDG_STATE_HOME/opencode-review-loop``::

    sessions/<session_id>              -> worktree path of the armed activation
    worktrees/<sha256(worktree)>/<session_id>/
        state.json
        plan.frozen.md
        phases.frozen                  (one phase description per line)
        reports/NNN-*.md
        bundles/NNN/

Nothing is ever written inside the repository under review (Rule 3).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from ocrl import paths
from ocrl.atomic import locked, write_private_atomic
from ocrl.config import Config
from ocrl.errors import StateLoadError
from ocrl.util import log, now

__all__ = ["State", "new_state_document", "pointer_clear", "pointer_read", "pointer_write"]

#: Statuses that are terminal enough that the TTL no longer applies to them.
_TTL_EXEMPT: Final = frozenset({"COMPLETE", "ARM_FAILED", "NEEDS_HUMAN"})

STATE_VERSION: Final = 1


def new_state_document() -> dict[str, Any]:
    """A fresh activation record. Key order matches the shell's, for readable diffs."""
    return {
        "version": STATE_VERSION,
        "status": "ARMED",
        "reason": "",
        "session_id": "",
        "worktree": "",
        "plan_path": "",
        "baseline_tree": "",
        "activation_commit": "",
        "armed_at": 0,
        "allow_dirty": False,
        "phases": [],
        "phase": 1,
        "last_approved_tree": "",
        "approved_trees": [],
        "pending_approved_tree": "",
        "pending_head": "",
        "pending_command": "",
        "bad_commit": "",
        "bad_commit_parent": "",
        "failures": 0,
        "stop_blocks": 0,
        "stop_marker": "",
        "defers": 0,
        "defer_pending": False,
        "final_done_tree": "",
        "report_seq": 0,
    }


# --------------------------------------------------------------------------
# Session pointer
# --------------------------------------------------------------------------


def pointer_write(session: str, worktree: str) -> None:
    """Record which worktree a session armed.

    Written atomically for the same reason ``state.json`` is: the shell redirected straight
    onto the destination, so an interrupted write left an empty pointer -- which Rule 0
    then reads as "arming never executed", denying every subsequent tool call.
    """
    root = paths.state_root()
    write_private_atomic(paths.session_pointer_path(session), f"{worktree}\n", root=root)


def pointer_read(session: str) -> str | None:
    """First line of the session pointer, or ``None`` if there is no usable pointer.

    On the hot path -- every single tool call reaches this -- so it is a plain read with no
    directory creation and no permission work.
    """
    if not paths.is_safe_component(session):
        # Not an exception: this is the hot path, and a session id that cannot name a
        # pointer simply has no pointer. The caller then takes the Rule 0 branch and
        # denies. Logged, because a traversal attempt is worth seeing.
        if session:
            log(f"refusing to read a pointer for an unsafe session id: {session!r}")
        return None
    pointer = paths.sessions_dir() / session
    try:
        with pointer.open(encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    return line.rstrip("\n")


def pointer_clear(session: str) -> None:
    """Remove a session pointer.

    Validated rather than composed: this unlinks, so an absolute or traversing session id
    would be an arbitrary-file-delete primitive.
    """
    paths.session_pointer_path(session).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# state.json
# --------------------------------------------------------------------------


class State:
    """One activation's state document, bound to a (worktree, session) pair."""

    def __init__(self, worktree: str, session: str) -> None:
        self.worktree = worktree
        self.session = session
        self.act_dir: Path = paths.activation_dir(worktree, session)
        self.state_file: Path = self.act_dir / "state.json"
        self.lock_file: Path = self.act_dir / "lock"
        self.data: dict[str, Any] = {}

    # -- persistence -----------------------------------------------------

    def exists(self) -> bool:
        return self.state_file.is_file()

    def load(self) -> bool:
        """Read the document. False -- and an empty document -- if it is not usable.

        Callers treat False as "the gate cannot tell what has been reviewed" and deny; it is
        never an opt-out.
        """
        try:
            document = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            self.data = {}
            return False
        if not isinstance(document, dict):
            self.data = {}
            return False
        self.data = document
        return True

    def save(self) -> None:
        write_private_atomic(self.state_file, self.serialise(), root=paths.state_root())

    def serialise(self) -> str:
        return json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"

    def new(self) -> None:
        self.data = new_state_document()

    @contextmanager
    def transaction(self, *, create: bool = False) -> Iterator[State]:
        """Hold the activation lock across load -> mutate -> save.

        Without the lock a ``PostToolUse`` hook overlapping a user-run ``defer`` is a
        read-modify-write race: atomic rename keeps the file well-formed, but the loser's
        update is simply gone.

        **A failed load raises rather than yielding.** State that is missing or unparseable
        must not be mutated: the gate cannot tell what has been reviewed, so writing a
        document on top of it would manufacture an activation nobody armed -- a caller
        setting ``status="ACTIVE"`` would turn unreadable state into a running loop. That is
        precisely the failure-into-approval Rule 1 forbids.

        ``create=True`` is the deliberate exception, and it is only for transitions that
        can never grant anything: arming, and the two escalations. Both write a document
        whose effect is to deny.
        """
        with locked(self.lock_file, root=paths.state_root()):
            if not self.load():
                if not create:
                    raise StateLoadError(f"no usable activation state at {self.state_file}")
                self.new()
            yield self
            self.save()

    # -- accessors -------------------------------------------------------

    def get(self, key: str) -> str:
        """Value as text, empty when absent or null.

        Note the absence of a ``//`` fallback, which is what the shell needed a comment for:
        in jq ``false // ""`` yields ``""``, so the alternative operator silently blanks
        every boolean field. Presence is tested instead, so ``False`` renders as ``"false"``.
        """
        value = self.data.get(key)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, dict)):
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return str(value)

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.data.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def get_array(self, key: str) -> list[str]:
        value = self.data.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
        return []

    def update(self, values: Mapping[str, Any] | None = None, /, **kwargs: Any) -> None:
        """Set keys to native Python values.

        The shell needed two setters -- ``ocrl_set`` for strings and ``ocrl_setj`` for raw
        JSON -- because every write went through ``jq``. Here the value's own type carries
        that distinction, so a boolean cannot accidentally be stored as the string "true".
        """
        if values:
            self.data.update(values)
        if kwargs:
            self.data.update(kwargs)

    # -- derived ---------------------------------------------------------

    def effective_status(self, config: Config) -> str:
        """Stored status, with the TTL applied.

        An expired activation becomes ``STALE``, and ``STALE`` still blocks. The loop never
        silently disarms itself on a timer -- that would be a failure converted into
        permission (Rule 1).
        """
        status = self.get("status")
        if status in _TTL_EXEMPT:
            return status
        try:
            armed_at = int(self.data.get("armed_at") or 0)
            ttl_hours = int(config.values.get("ttl_hours") or 24)
        except (TypeError, ValueError):
            return status
        if armed_at > 0 and ttl_hours > 0 and (now() - armed_at) > ttl_hours * 3600:
            return "STALE"
        return status

    def phase_count(self) -> int:
        return len(self.get_array("phases"))

    def phase_desc(self, n: int) -> str:
        """Description of phase ``n``, 1-based. Empty when out of range."""
        phases = self.get_array("phases")
        index = n - 1
        if index < 0 or index >= len(phases):
            return ""
        return phases[index]

    def tree_approved(self, tree: str) -> bool:
        return tree in self.get_array("approved_trees")

    def mark_tree_approved(self, tree: str) -> None:
        # Sorted and deduplicated, matching jq's `unique`.
        self.data["approved_trees"] = sorted(set(self.get_array("approved_trees")) | {tree})

    # -- escalation ------------------------------------------------------

    def _escalate(self, status: str, reason: str) -> None:
        """Record a terminal, denying status under the lock.

        Taken under the lock and applied to a *freshly reloaded* document. Mutating a stale
        in-memory copy and saving it lets a concurrent ``defer`` or post-hook save land
        afterwards and overwrite the escalation, turning it back into ordinary operation --
        the one direction that must never happen.

        ``create=True`` because an escalation must not be dropped just because state is
        missing or unreadable; both statuses deny, so materialising them is always safe.

        Callers holding other unsaved mutations must save them first: the reload is what
        makes the escalation win, and it necessarily discards anything only held in memory.
        """
        with self.transaction(create=True):
            self.update(status=status, reason=reason)

    def needs_human(self, reason: str) -> None:
        self._escalate("NEEDS_HUMAN", reason)

    def arm_failed(self, reason: str) -> None:
        self._escalate("ARM_FAILED", reason)
