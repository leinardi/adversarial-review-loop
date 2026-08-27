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

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from ocrl import paths
from ocrl.atomic import locked, write_private_atomic
from ocrl.config import Config
from ocrl.errors import StateLoadError
from ocrl.util import log, now

__all__ = ["STATE_VERSION", "State", "new_state_document", "pointer_clear", "pointer_read", "pointer_write"]

#: Statuses that are terminal enough that the TTL no longer applies to them. ``RESUMED``
#: marks a retired activation -- it already denies everything on its own, and letting the
#: TTL turn it into ``STALE`` instead would replace a message naming the successor session
#: with a generic re-arm prompt.
_TTL_EXEMPT: Final = frozenset({"COMPLETE", "ARM_FAILED", "NEEDS_HUMAN", "RESUMED"})

#: Version 2 adds resume, pause, plan revision and the config overlay's fields. Version 3
#: adds ``round_history`` and the convergence counters. See ``State._migrate`` for the
#: upgrade from any earlier (or unversioned) document -- a version-1 document reaches the
#: current version in one pass.
STATE_VERSION: Final = 3

#: The name ``arm`` freezes the plan under, and the name revision 0 always carries.
_PLAN_FROZEN_NAME: Final = "plan.frozen.md"


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
        #: One ``{"phase": n, "commit": sha}`` per phase confirmed by ``posttool``. Evidence of
        #: phase *progression*, which ``phase`` alone cannot prove -- see
        #: ``completion.phase_progress_proven``.
        "phase_commits": [],
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
        "stop_after_phase": 0,
        "resumed_from": "",
        "resumed_into": "",
        "resume_count": 0,
        "plan_revisions": [],
        "replan_pending": False,
        "overrides": {},
        "abandoned_pending_tree": "",
        "abandoned_pending_head": "",
        "activation_generation": 0,
        "finish_requested": False,
        #: One entry per *parsed* review (``APPROVED`` / ``CHANGES_REQUIRED`` only), appended
        #: by ``reviewer.execute``: ``{seq, label, phase, generation, round, verdict, tree,
        #: base, at, findings: [line, ...], supersedes: [line, ...]}``. Evidence, not a
        #: counter -- carried across a resume, never reset, like ``manual_accepts`` and the
        #: reports. ``state.json`` is not a trust boundary: every consumer re-validates each
        #: stored finding line before rendering it, and any git command built from ``tree``
        #: runs it through ``gitsnap.checked_tree`` first.
        "round_history": [],
        #: Infra-failure retry pacing (phase 6: ``transient_failures``, ``retry_not_before``)
        #: and the clarify allowance (phase 3: ``clarifications``). Counters, so ``resume``
        #: resets all three -- see ``commands.resume._build_successor_document``.
        "transient_failures": 0,
        "retry_not_before": 0,
        "clarifications": 0,
        #: One entry per ``accept``: ``{"at", "phase", "tree", "base", "reason", "reviews",
        #: "report"}``. Never trimmed -- it is the audit trail for every phase a human approved
        #: without an approving review.
        "manual_accepts": [],
        #: The reviewer session continuity pointer, or ``{}`` when none is held. A pure
        #: optimisation hint -- see ``ocrl.reviewer``'s module docstring for why it is never
        #: trusted to authorize anything -- holding ``{"label", "id", "title", "created",
        #: "revisions", "generation", "round", "claimed_at", "claim_id"}`` once populated.
        "reviewer_session": {},
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


def _document_version(document: Mapping[str, Any]) -> int | None:
    """The document's version as a small int, or ``None`` if it cannot be trusted.

    A genuinely **absent** key is the one legacy case: documents ``arm`` wrote before this
    field existed. Answers ``1`` for it, matching the version those documents would have
    declared had the field existed. Every other malformed value -- an explicit ``null``, a
    boolean (``bool`` is a subclass of ``int`` in Python, so it must be excluded explicitly),
    a non-numeric string, a float -- answers ``None`` rather than being folded into "legacy":
    collapsing anything the document might contain into a trusted version 1 is how malformed
    state would get migrated and trusted instead of refused.
    """
    if "version" not in document:
        return 1
    value = document["version"]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _usable_version(document: Mapping[str, Any]) -> bool:
    """Whether ``document["version"]`` is safe for this build to interpret at all.

    Used by :meth:`State.load` itself, so that a document from a build newer than this one
    -- or one carrying a malformed version -- is treated exactly like unparseable JSON:
    unusable, on every path, not merely on the write path ``_migrate`` guards. A hook that
    only checked ``status`` would otherwise read a future schema's status value at face
    value and gate on it as if it understood what that value meant.
    """
    version = _document_version(document)
    return version is not None and 1 <= version <= STATE_VERSION


def _is_future_version(document: Mapping[str, Any]) -> bool:
    """Whether ``version`` is a real, positive integer this build merely does not go up to.

    Deliberately **not** the negation of :func:`_usable_version`: that also covers ``0``,
    negative integers, and every malformed non-integer value (``null``, a boolean, a float,
    a string) -- none of which any build of this gate has ever written or ever will, so
    there is nothing plausibly "newer" about them. Only an integer greater than
    :data:`STATE_VERSION` is the shape an actual future build's document would have.
    :meth:`State.load` uses exactly this to decide whether a document is worth preserving
    untouched (see :attr:`State.version_conflict`) or is ordinary corrupt state -- conflating
    the two would let a single stray ``null`` permanently refuse every escalation and every
    re-arm over what is, in truth, no different from unparseable JSON.
    """
    version = _document_version(document)
    return version is not None and version > STATE_VERSION


class State:
    """One activation's state document, bound to a (worktree, session) pair."""

    def __init__(self, worktree: str, session: str) -> None:
        self.worktree = worktree
        self.session = session
        self.act_dir: Path = paths.activation_dir(worktree, session)
        self.state_file: Path = self.act_dir / "state.json"
        self.lock_file: Path = self.act_dir / "lock"
        self.data: dict[str, Any] = {}
        #: Set by :meth:`load` when the document is well-formed JSON -- a real, readable
        #: object -- and declares a ``version`` that is a real, positive integer greater
        #: than :data:`STATE_VERSION`: the shape an actual newer build's document would
        #: have. Distinct from every other way ``load`` can fail (missing, unparseable, not
        #: an object, or a malformed non-integer ``version`` like ``null``) -- those mean
        #: there is nothing to preserve, which is what makes ``transaction(create=True)``
        #: safe to overwrite with a fresh document. A version conflict means the opposite --
        #: the document is real, and overwriting it is exactly the "cannot know what its
        #: fields mean" case the version check exists to refuse, not a fresh start to build
        #: on. See :func:`_is_future_version` for why the boundary is drawn there and not at
        #: "any unusable version".
        self.version_conflict: bool = False
        #: The offending integer when :attr:`version_conflict` is set, for messages that
        #: want to name it. ``None`` otherwise.
        self.version_conflict_value: int | None = None

    # -- persistence -----------------------------------------------------

    def exists(self) -> bool:
        return self.state_file.is_file()

    def load(self) -> bool:
        """Read the document. False -- and an empty document -- if it is not usable.

        Callers treat False as "the gate cannot tell what has been reviewed" and deny; it is
        never an opt-out. A document whose ``version`` this build cannot trust -- newer than
        :data:`STATE_VERSION`, or malformed -- is unusable for exactly the same reason
        unparseable JSON is: every other field, ``status`` included, might mean something
        this build does not know about. Only the genuinely-newer case also sets
        :attr:`version_conflict`; see there for why.
        """
        self.version_conflict = False
        self.version_conflict_value = None
        try:
            document = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            self.data = {}
            return False
        if not isinstance(document, dict):
            self.data = {}
            return False
        if not _usable_version(document):
            self.data = {}
            if _is_future_version(document):
                self.version_conflict = True
                self.version_conflict_value = _document_version(document)
            return False
        self.data = document
        return True

    def save(self) -> None:
        write_private_atomic(self.state_file, self.serialise(), root=paths.state_root())

    def serialise(self) -> str:
        return json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"

    def new(self) -> None:
        self.data = new_state_document()

    def _migrate(self) -> None:
        """Upgrade a document written by an older build, lock already held.

        Applied as ordered arms so a version-1 (or unversioned) document reaches the current
        version in a single pass:

        * **1 -> 2** synthesizes ``plan_revisions`` revision 0 from ``plan.frozen.md`` as
          found *now* -- no earlier hash was ever recorded to check it against -- and
          defaults the resume / config-overlay fields. Later code indexes ``plan_revisions``'
          last entry, and an empty list turns that into an ``IndexError`` raised from inside
          a hook, which the fail-closed guard in ``hookio.Hook.run`` converts into a denial
          that says nothing useful. A missing, unreadable or symlinked ``plan.frozen.md``
          fails closed as ``ARM_FAILED`` -- inventing a plan is worse than refusing.
        * **2 -> 3** adds ``round_history`` and the three convergence counters. No evidence
          recovery is needed: every one of those fields degrades correctly through the typed
          accessors (``get_int`` answers ``0``, ``get_array_of_dicts`` answers ``[]``), so a
          plain ``setdefault`` from :func:`new_state_document` is the whole arm.

        Most other fields this build added degrade correctly on their own too; the explicit
        arms exist for the ones that do not, and to make each upgrade a single recorded step
        rather than an accident of accessor defaults.

        Must not call back into :meth:`transaction` or :meth:`_escalate` on any path: both
        take the activation lock, and ``flock`` does not nest across two descriptors opened
        by the same process on the same file -- a second acquisition here would block
        forever on the first. Failure is instead written straight into ``self.data`` and
        saved directly, and a :class:`StateLoadError` raised so the caller's own mutation is
        abandoned exactly as it would be for any other unusable document.

        ``version`` is re-validated here even though :meth:`load` already refused anything
        it could not trust -- belt and braces, in case a future caller ever reaches this
        method with data that did not come through ``load``.
        """
        version = _document_version(self.data)
        if version is None or not (1 <= version <= STATE_VERSION):
            # Either newer than this build understands, or a value that is not a version this
            # build has ever written. Neither is safe to interpret, so nothing is written --
            # the previous, unparsed document is left exactly as it was found.
            raise StateLoadError(f"{self.state_file} declares version {self.data.get('version')!r}, which this build does not know how to read")
        if version == STATE_VERSION:
            return

        defaults = new_state_document()
        if version < 2:
            self._migrate_1_to_2(defaults)
        if version < 3:
            for key in ("round_history", "transient_failures", "retry_not_before", "clarifications"):
                self.data.setdefault(key, defaults[key])
            self.data["version"] = 3

    def _migrate_1_to_2(self, defaults: dict[str, Any]) -> None:
        """The 1 -> 2 arm of :meth:`_migrate`: synthesize revision 0, default the resume fields."""
        # `os.path.realpath` on both the activation directory and the plan file, then a
        # containment check by exact parent-directory equality -- the same shape
        # ``pretool._guard_state_root`` uses -- so a symlink planted at either level is
        # refused rather than followed: reading and hashing whatever it points at would
        # publish that content as revision 0 of a plan nobody ever froze.
        plan_path = self.act_dir / _PLAN_FROZEN_NAME
        act_dir_real = os.path.realpath(self.act_dir)
        plan_real = os.path.realpath(plan_path)
        usable = os.path.dirname(plan_real) == act_dir_real and os.path.isfile(plan_real)
        plan_bytes = b""
        if usable:
            try:
                plan_bytes = Path(plan_real).read_bytes()
            except OSError:
                usable = False
        if not usable:
            # A migration that invents a plan is worse than a refusal. This is honest about
            # what is and is not known: the document is real, but the evidence every review
            # was run against is gone, so nothing more can be verified against it.
            self.data["status"] = "ARM_FAILED"
            self.data["reason"] = (
                f"legacy activation predates resume and {_PLAN_FROZEN_NAME} is missing, unreadable, or not a "
                "plain file inside the activation directory; the frozen plan can no longer be verified"
            )
            self.save()
            raise StateLoadError(self.data["reason"]) from None

        for key in (
            "stop_after_phase",
            "resumed_from",
            "resumed_into",
            "resume_count",
            "replan_pending",
            "overrides",
            "abandoned_pending_tree",
            "abandoned_pending_head",
            "activation_generation",
            "finish_requested",
        ):
            self.data.setdefault(key, defaults[key])

        # Revision 0 is synthesized from the plan as found *at migration time*: no earlier
        # hash was ever recorded to check it against, so this attests only that the file
        # existed with this content when the document was upgraded, not that it is unchanged
        # since arming. Every later revision is verified normally, against a hash recorded
        # when it was written.
        self.data["plan_revisions"] = [
            {
                "at": self.data.get("armed_at") or 0,
                "phase": 1,
                "sha256": hashlib.sha256(plan_bytes).hexdigest(),
                "file": _PLAN_FROZEN_NAME,
            }
        ]
        self.data["version"] = 2

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

        **``create=True`` does not cover a version conflict.** ``self.new()`` is a *fresh*
        document -- the right answer when there was truly nothing to preserve, but wrong
        for a document this build can read fine yet refuses to interpret: it is real, and
        overwriting it with a blank one destroys whatever a newer build recorded, in the
        one case the version check exists to say "refuse" about, not "start over". So this
        still raises even when ``create`` is set; the two escalations that rely on
        ``create=True`` (``needs_human``, ``arm_failed``) then raise too, and that
        propagates to the fail-closed guard in ``hookio.Hook.run`` -- which denies or blocks
        without ever calling :meth:`save`, exactly what "refuse" means here.

        **The save happens only if the block completes**, so raising out of it abandons the
        mutation with the previous document intact. That is the supported way to decline:
        any decision a caller makes about *whether* to write must be taken inside the block,
        against the document reloaded here, because anything it read before queueing for the
        lock may have been overwritten while it waited.
        """
        with locked(self.lock_file, root=paths.state_root()):
            if not self.load():
                if self.version_conflict:
                    raise StateLoadError(f"{self.state_file} declares a version this build does not trust; refusing to touch it")
                if not create:
                    raise StateLoadError(f"no usable activation state at {self.state_file}")
                self.new()
            else:
                self._migrate()
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

    def get_array_of_dicts(self, key: str) -> list[dict[str, Any]]:
        """List-of-objects value, with anything that is not an object dropped.

        Separate from :meth:`get_array`, which stringifies: the callers of this one need the
        objects back as objects. Non-object members are dropped rather than coerced, so a
        malformed document degrades to a *shorter* list -- which every caller then fails a
        length check against, rather than reading a placeholder as a real entry.
        """
        value = self.data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
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

    def phases_match_frozen(self) -> bool:
        """True when this document's ``phases`` still equals the frozen evidence on disk.

        ``phases.frozen`` is written once by ``set-phases`` (and rewritten wholesale by a
        granted replan) and is the copy handed to every review as evidence. ``phases`` in the
        document is the working copy every other check reads. They are written together and
        must agree; where they do not, the document has been edited by something that was not
        ``set-phases``, and no count derived from it means anything.

        This exists because ``phase == phase_count() + 1`` -- "every phase was committed" --
        is only as trustworthy as ``phases`` itself, and AGENTS.md is explicit that
        ``state.json`` is not a trust boundary. Truncating ``phases`` from two entries to one
        after the first phase lands satisfies that equality with the second phase never
        implemented; comparing against the frozen file is what catches it.

        Compared as the **exact bytes** ``set-phases`` writes, rather than by splitting the file
        back into lines: ``_validate`` rejects only empty and whitespace-only phase
        descriptions, so a description legitimately containing a newline is accepted and frozen,
        and parsing lines back out would never reconstruct it. That activation could complete
        every phase and still be refused here, permanently.

        Read with :meth:`Path.read_bytes`, not :meth:`Path.read_text`, for the same reason at
        one remove: text mode applies universal-newline translation on the way in, turning a
        frozen ``\\r\\n`` or ``\\r`` into ``\\n`` and failing the comparison against a
        description that legitimately contains one. Bytes are what was written (``0600``, UTF-8,
        ``errors="strict"`` -- matching the ``encode`` below), so bytes are what is compared.

        Fails closed on an unreadable file: a phase list that cannot be checked is not one to
        disarm on.
        """
        try:
            frozen = (self.act_dir / "phases.frozen").read_bytes()
        except OSError:
            return False
        return frozen == "".join(f"{phase}\n" for phase in self.get_array("phases")).encode("utf-8")

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
