"""Plumbing shared by the four hook entrypoints.

What lives here is what more than one of ``pretool``, ``confirm-commit``,
``posttool-failure`` and ``gate-stop`` needs: the read-only tool table, the denial preamble,
resolving the payload's ``cwd`` onto the armed worktree, and **Rule 0** -- finding the
activation that guards a worktree when the calling session never bound to one.

Imports are kept to what the ``PreToolUse`` hot path already pays for. ``pretool`` runs on
every single tool call, so ``gitsnap``, ``cmdshape``, ``reviewer`` and ``report`` are
imported inside the functions that need them rather than at module scope.
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

import contextlib
from dataclasses import dataclass
from typing import Final, NoReturn

from arl import commands, paths
from arl import config as config_module
from arl.config import Config
from arl.errors import RepoResolutionError, UnsafePathError
from arl.hookio import Hook
from arl.state import State, intent_read, pointer_ack, pointer_read, pointer_write
from arl.util import log, now

__all__ = [
    "DENY_PREAMBLE",
    "READONLY_TOOLS",
    "UNBOUND_PASS_STATUSES",
    "UNSTARTED_ARM_REASON",
    "Activation",
    "IntentCheck",
    "Unbound",
    "activation",
    "deny",
    "describe_move",
    "escalate",
    "find_abandoned_marker_commit",
    "pending_intent",
    "record_unstarted_arm",
    "resolve_abandoned_marker",
    "resolve_repo",
    "tool_is_readonly",
    "unbound_activation",
]

#: Tools that cannot change the repository. Everything else is treated as a mutation, so an
#: unknown or newly added tool fails closed rather than open.
READONLY_TOOLS: Final = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "LS",
        "NotebookRead",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "AskUserQuestion",
        "Skill",
        "SlashCommand",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
        "TaskOutput",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "ReadMcpResourceDirTool",
    }
)

DENY_PREAMBLE: Final = "adversarial-review-loop is enforcing an external review gate in this worktree.\n\n"

UNSTARTED_ARM_REASON: Final = (
    "arming never executed: enforcement was requested (/adversarial-review-loop:implement or :resume), "
    "but arl.sh arm did not run at prompt-expansion time. Nothing was frozen and nothing has been reviewed."
)

#: The statuses under which a worktree's most recent activation guards nothing: the loop
#: ended there, so a session that never bound to it has nothing to be denied for.
UNBOUND_PASS_STATUSES: Final = frozenset({"COMPLETE", "DISARMED"})


def tool_is_readonly(tool: str) -> bool:
    return tool in READONLY_TOOLS


# --------------------------------------------------------------------------
# "Is this still the activation I decided about?"
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Activation:
    """Everything that must be unchanged for a decision taken earlier to still apply.

    Both hooks that write a decision -- ``pretool`` approving a commit, ``confirm-commit``
    advancing a phase -- do slow work first: a review that takes minutes, or a handful of git
    processes. Taking the activation lock and *reloading* is not enough on its own, because
    the reloaded document is then written to regardless of what it says. What that allows is
    the one direction Rule 1 forbids: an escalation, a reconcile, an expiry or a user's
    ``stop`` recorded while the slow work ran, and then quietly replaced by an approval.

    So each of those callers captures this before, compares it inside the transaction, and
    refuses on any difference. Deliberately an equality check over everything rather than a
    list of statuses that may not be overwritten -- a deny-list of statuses fails open the
    day one is added, which is how ``RECONCILE`` was missed once already.
    """

    armed_at: str
    baseline_tree: str
    session_id: str
    status: str
    effective_status: str
    phase: int
    last_approved_tree: str
    pending_approved_tree: str
    pending_head: str
    pending_command: str
    activation_generation: int

    @property
    def identity(self) -> tuple[str, str, str]:
        """Which activation this is. ``arm`` writes a fresh document, so a re-arm changes it."""
        return (self.armed_at, self.baseline_tree, self.session_id)

    @property
    def summary(self) -> str:
        return f"{self.effective_status}, phase {self.phase}"


def activation(state: State, config: Config) -> Activation:
    """Capture the loaded document's activation record. Reads only; takes no lock."""
    return Activation(
        armed_at=state.get("armed_at"),
        baseline_tree=state.get("baseline_tree"),
        session_id=state.get("session_id"),
        status=state.get("status"),
        effective_status=state.effective_status(config),
        phase=state.get_int("phase"),
        last_approved_tree=state.get("last_approved_tree"),
        pending_approved_tree=state.get("pending_approved_tree"),
        pending_head=state.get("pending_head"),
        pending_command=state.get("pending_command"),
        activation_generation=state.get_int("activation_generation"),
    )


def escalate(state: State, config: Config, expected: Activation, reason: str) -> bool:
    """Record ``NEEDS_HUMAN``, unless something else already moved the activation.

    ``State.needs_human`` writes unconditionally, which is right for arming but wrong at the
    end of a review: a review takes minutes, and the user can run ``/adversarial-review-loop:stop``
    during them. An escalation landing afterwards turns their ``DISARMED`` back into a state
    that denies every mutation -- the gate re-enabling itself after the user left the mode,
    which Rule 4 says only they may decide.

    Answers whether the escalation stuck. ``False`` means the caller's whole decision is void
    and it must say so rather than report an escalation that was not recorded.
    """
    try:
        with state.transaction(create=True):
            if activation(state, config) != expected:
                raise commands.Refused(describe_move(expected, activation(state, config)))
            state.update(status="NEEDS_HUMAN", reason=reason)
    except commands.Refused:
        log(f"not escalating: the activation moved first ({reason})")
        return False
    return True


def describe_move(before: Activation, now: Activation) -> str:
    """Name what changed, most significant first, for the message the caller prints."""
    if before.identity != now.identity:
        return "this activation was re-armed"
    if before.effective_status != now.effective_status:
        return f"the activation moved from {before.effective_status} to {now.effective_status}"
    if before.phase != now.phase:
        return f"the phase moved from {before.phase} to {now.phase}"
    if before.last_approved_tree != now.last_approved_tree:
        return "another call approved a different tree"
    if before.activation_generation != now.activation_generation:
        return "a resume or an accept changed the activation while this was in progress"
    return "the pending approval changed"


def find_abandoned_marker_commit(repo: str, *, activation_commit: str, marker_head: str, marker_tree: str) -> str:
    """The abandoned commit's id, if it landed in ``activation_commit..HEAD``; else ``""``.

    ``resume --abandon-pending`` clears a stale pending approval whose approving session is
    gone, but records the one commit it would have produced -- parent ``marker_head``, tree
    ``marker_tree`` -- because the killed session's Bash call may still complete *after* the
    marker is recorded. The pair is unambiguous: matching on the tree alone would also match
    an ordinary, already-reviewed commit whose diff happened to be empty (its tree equals
    ``last_approved_tree``, which the abandoned tree can legitimately equal too).

    One ``git rev-list`` plus a ``rev-parse`` pair per commit in range -- paid only when a
    marker is actually set, which is a rare, user-chosen state.

    Raises ``gitsnap.GitUnavailable`` when ``git rev-list`` itself fails (a broken ``.git``),
    or when either per-commit ``rev-parse`` fails to answer for a commit ``rev-list`` just
    proved exists. Whether the abandoned commit landed is exactly what this function cannot
    answer if git cannot be read at all, and Rule 1 says that uncertainty must not silently
    read as "it didn't land" -- which plain ``rev_parse`` would do, since it folds a genuine
    failure into the same empty string it returns for "does not resolve".

    One range is legitimately empty rather than unreadable: an activation armed against an
    empty repository has ``activation_commit == ""``, and if nothing has landed since, ``HEAD``
    is still unborn -- ``git rev-list HEAD`` fails for that reason alone, with the same exit
    status a genuine failure would have. That is checked for explicitly, with the *checked*
    read (``rev_parse_checked``, not the lenient ``head_commit``) so a genuine git failure while
    asking the question still raises rather than being folded into the same "" an unborn HEAD
    answers with -- the same distinction the rest of this function already draws for its own
    git calls. Only a *confirmed* unborn HEAD short-circuits to "no match"; wedging every
    commit in the successor before the very first one has ever landed is exactly the failure
    this short-circuit exists to prevent, and it must not itself become a way past a genuine
    git failure.
    """
    from arl import gitsnap  # noqa: PLC0415 - not on the read-only hot path

    if not activation_commit and not gitsnap.rev_parse_checked(repo, "HEAD"):
        return ""

    if activation_commit and not gitsnap.looks_like_object_id(activation_commit):
        # state.json is not a trust boundary: a tampered `activation_commit` shaped like a
        # git option must not be interpolated into `rev-list` argv. Fail closed -- the caller
        # denies on a raised GitUnavailable, exactly as it would for any unreadable range.
        raise gitsnap.GitUnavailable(f"the recorded activation commit {activation_commit!r} is not a usable git object id")

    range_spec = f"{activation_commit}..HEAD" if activation_commit else "HEAD"
    proc = gitsnap.git_run(repo, ["rev-list", range_spec, "--"])
    if proc.returncode != 0:
        raise gitsnap.GitUnavailable(proc.stderr.decode("utf-8", "surrogateescape").strip() or f"git rev-list {range_spec} failed")
    for commit in proc.stdout.decode("utf-8", "surrogateescape").split():
        if gitsnap.rev_parse_checked(repo, f"{commit}^") == marker_head and gitsnap.rev_parse_checked(repo, f"{commit}^{{tree}}") == marker_tree:
            return commit
    return ""


def resolve_abandoned_marker(state: State, *, repo: str) -> str:
    """Enter ``RECONCILE`` if a commit ``resume --abandon-pending`` gave up on landed anyway.

    Must run **before** anything in this activation approves or reviews a commit: the
    abandoned commit's own ``PostToolUse`` ran, if at all, in the retired session against a
    document nothing here ever sees again, so this is the only place left to notice it landed.
    Returns the bad commit id on a match (already recorded as ``RECONCILE``), or ``""`` --
    including when there was no marker to check at all.

    Raises ``gitsnap.GitUnavailable`` when the scan itself could not run; callers must deny or
    block on it rather than let an unreadable history read as "nothing landed" (Rule 1).
    """
    marker_tree = state.get("abandoned_pending_tree")
    if not marker_tree:
        return ""
    marker_head = state.get("abandoned_pending_head")
    activation_commit = state.get("activation_commit")
    match = find_abandoned_marker_commit(repo, activation_commit=activation_commit, marker_head=marker_head, marker_tree=marker_tree)
    if not match:
        return ""
    with state.transaction():
        # Re-checked against the reloaded document: a concurrent hook call may already have
        # resolved (or moved past) this exact marker.
        if state.get("abandoned_pending_tree") == marker_tree and state.get("abandoned_pending_head") == marker_head:
            state.update(
                status="RECONCILE",
                reason=(
                    f"a commit abandoned by resume ({match}) landed after all, on top of the marker it was expected to "
                    f"build on ({marker_head} -> {marker_tree})."
                ),
                bad_commit=match,
                bad_commit_parent=marker_head,
                abandoned_pending_tree="",
                abandoned_pending_head="",
            )
    return match


def deny(hook: Hook, body: str) -> NoReturn:
    """Deny with the standard preamble in front of ``body``.

    The trailing newlines go because the shell built every one of these reasons inside
    ``$( … )``, which strips them, and the messages are asserted on character for character
    by ``tests/selftest.sh``.
    """
    hook.deny(f"{DENY_PREAMBLE}{body}".rstrip("\n"))


def resolve_repo(cwd: str, worktree: str) -> str:
    """Which repository the payload's ``cwd`` belongs to.

    The overwhelmingly common case is ``cwd`` already being the armed worktree root, and that
    needs no git process to establish -- which matters, because this is on the path of every
    tool call. Empty when git *answers* that ``cwd`` is not inside a repository; callers
    decide what that means for them. Raises :class:`RepoResolutionError` when git could not
    answer -- not on PATH, ``cwd`` gone, a timeout -- because every caller is about to decide
    whether this call is *about the armed worktree*, and "could not tell" read as "somewhere
    else" is a pass for a commit run from a subdirectory with git off the hook's PATH.
    """
    if cwd == worktree:
        return worktree
    return paths.repo_root_or_raise(cwd)


@dataclass(frozen=True)
class Unbound:
    """The activation a session with no pointer ran into: which one, and in what state.

    ``status`` is the activation's own status, or ``"unreadable"`` when ``latest`` names an
    activation whose document cannot be read -- which still denies, since a pointer that says
    the worktree was armed with no document to say it stopped is the fail-open case.

    ``session`` is empty when the worktree itself could not be resolved: git could not be
    run, or the directory is gone. ``status`` then carries the detail. That denies too -- a
    gate that cannot tell what worktree a call is about cannot tell whether it is guarded.
    """

    repo: str
    session: str
    status: str


def unbound_activation(cwd: str) -> Unbound | None:
    """The live activation guarding ``cwd``'s repository, for a session with no pointer (**Rule 0**).

    The hooks register at plugin load, so a hook entrypoint runs in *every* session -- the
    ones that armed a loop and the ones that never did. A session with no pointer is one that
    never ran ``implement`` or ``resume``: a fresh ``claude`` in an armed worktree, a session
    that opened it by accident, or any session in a repository nobody ever armed. The
    worktree's ``latest`` pointer is what tells those apart. It names the activation a
    shell-run ``status`` or ``resume`` would pick up; if that one is still live, this session
    must not mutate the worktree it guards until ``resume`` binds it. If there is none, or it
    ended (:data:`UNBOUND_PASS_STATUSES`), there is nothing to enforce and the call passes.

    "Nothing to enforce" has to be *proven*, not defaulted to: the repository is resolved
    with :func:`paths.repo_root_or_raise`, so a ``git`` missing from the hook's PATH or a
    vanished ``cwd`` is a denial, never a pass. Only git itself saying "not a repository" is.

    ``None`` means "nothing to enforce". Anything else is a reason to deny, and nothing is
    written: the activation belongs to another session, and its document is not this
    session's to touch.
    """
    try:
        repo = paths.repo_root_or_raise(cwd)
    except RepoResolutionError as exc:
        return Unbound(repo=cwd, session="", status=f"unresolvable ({exc})")
    if not repo:
        return None
    session = commands.latest_session(repo)
    if not session:
        return None
    status = _latest_status(repo, session)
    if status in UNBOUND_PASS_STATUSES:
        return None
    return Unbound(repo=repo, session=session, status=status)


def _latest_status(repo: str, session: str) -> str:
    """The status of the worktree's ``latest`` activation, or ``"unreadable"``."""
    try:
        state = State(repo, session)
    except UnsafePathError:
        return "unreadable"
    if not state.load():
        return "unreadable"
    return str(state.get("status") or "")


@dataclass(frozen=True)
class IntentCheck:
    """What the session's intent marker means for one call.

    ``pending``: this session asked for enforcement of the call's worktree and no ``arm`` has
    answered *and the worktree is not already being gated* -- record ``ARM_FAILED`` and deny.
    ``unscopable``: a marker exists that the gate
    cannot read or that names no worktree, so it cannot tell *which* repository was asked for
    -- deny everywhere, record nothing, consume nothing; only ``deactivate --session`` (the
    user's ``/adversarial-review-loop:stop``) discards it. Neither: nothing to do for this call.
    """

    pending: bool = False
    unscopable: str = ""


NO_INTENT: Final = IntentCheck()


def pending_intent(session: str, cwd: str) -> IntentCheck:
    """Whether this session asked for enforcement of ``cwd``'s worktree and no ``arm`` answered.

    The marker is written by the ``intent`` entrypoint (``UserPromptSubmit``) when the prompt
    *is* an ``implement`` or ``resume`` command, before the skill expands. It is answered by
    ``pointer_write`` copying its token onto the pointer -- so the check is the token, not the
    file's presence: a marker the pointer acknowledges is done (and cleaned up here, best
    effort), whatever pointer the session held *before* the request is irrelevant, and that
    is why the callers run this ahead of ``pointer_read``. A session whose earlier activation
    ended (``DISARMED``, ``COMPLETE``) still has a pointer, and a re-arm whose expansion
    failed must not hide behind it.

    Scoped to the worktree the marker names. A call from another repository leaves the
    marker untouched and takes its own path; consuming it there would record ``ARM_FAILED``
    for the wrong repository and let the one that was asked for go ungated. For the same
    reason a marker that *cannot* be scoped -- unreadable, or naming no worktree -- is never
    consumed against whatever repository the call happens to be in: it is reported as
    ``unscopable``, denied everywhere, and left for the user to discard explicitly.

    An unsafe session id has no marker, the same way it has no pointer. Propagates
    :class:`RepoResolutionError` from the scoping itself; the callers deny on it.
    """
    if not paths.is_safe_component(session) or (marker := intent_read(session)) is None:
        return NO_INTENT
    if marker.token and pointer_ack(session) == marker.token:
        # Answered. The pointer was published; the marker is leftover from a cleanup that did
        # not get to run, and this is the retry.
        with contextlib.suppress(OSError):
            paths.intent_path(session).unlink()
        return NO_INTENT
    if marker.problem:
        log(f"intent marker for {session!r} {marker.problem}; denying without consuming it")
        return IntentCheck(unscopable=marker.problem)
    if resolve_repo(cwd, marker.worktree) != marker.worktree:
        return NO_INTENT
    if _answered_by_live_activation(session, marker.worktree):
        # **The marker's write order is not guaranteed.** Measured live (2026-08-30, twice):
        # the `UserPromptSubmit` marker can land *after* the skill expansion has already run
        # -- so an arming command can succeed, leave the loop ACTIVE, and only then have its
        # own request marker appear. Treating that marker as "arming never ran" overwrote a
        # freshly resumed activation with ARM_FAILED. A marker whose worktree already has a
        # live, *gating* activation bound to this session is answered whichever side wrote
        # last: either the arm it asked for ran, or an earlier activation is still enforcing
        # -- the gate is on in both worlds, which is all the marker exists to guarantee.
        pointer_write(session, marker.worktree)
        return NO_INTENT
    return IntentCheck(pending=True)


#: The statuses under which the gate is actively enforcing: an intent marker finding one of
#: these bound to its own session and worktree is answered, not pending. Terminal and failed
#: statuses stay out -- they are exactly the pointers a failed re-arm must not hide behind.
_GATING_STATUSES: Final = frozenset({"ACTIVE", "ARMED", "RECONCILE"})


def _answered_by_live_activation(session: str, worktree: str) -> bool:
    """Whether this session is bound to a live, gating activation of ``worktree``."""
    if pointer_read(session) != worktree:
        return False
    try:
        state = State(worktree, session)
    except UnsafePathError:
        return False
    if not state.load():
        return False
    return str(state.get("status") or "") in _GATING_STATUSES


def record_unstarted_arm(session: str, cwd: str) -> tuple[State, Config] | None:
    """Persist ``ARM_FAILED`` for an activation whose ``arm`` never ran (**Rule 0**).

    Reached only when :func:`pending_intent` says this session asked for enforcement of this
    worktree and no ``arm`` answered: the ``implement``/``resume`` prompt went in, but ``arl.sh arm`` never
    executed at expansion time -- a denied sandbox, a missing interpreter, an unreadable
    script, a ``${CLAUDE_PLUGIN_ROOT}`` that did not resolve. ``arm`` persists ``ARM_FAILED``
    for every failure it can observe, but it cannot persist a failure to start; and a failed
    expansion gives Claude no turn, so nothing the skill body says ever reaches it.

    Recording it here means the very next call takes the ordinary ``ARM_FAILED`` path, with
    all of its counting and messaging, and ``/adversarial-review-loop:stop`` has a document to
    disarm. Enforcement was requested and is not running; passing would be the one outcome
    the design exists to prevent.

    ``None`` when nothing could be recorded, which the caller reports as an integration
    fault. That happens when the session id is not usable as a key: an empty one lands the
    state in the worktree directory itself and makes ``pointer_write`` target a directory,
    so the next call repeats this branch forever, and an unsafe one is refused outright by
    ``arl.paths`` rather than composing a path out of the state root.
    """
    if not paths.is_safe_component(session):
        log(f"cannot record an unstarted arm for an unusable session id: {session!r}")
        return None

    repo = paths.repo_root(cwd) or cwd
    config = config_module.load(repo)
    state = State(repo, session)
    # `create=True`: there is nothing to load, and the document being written can only deny.
    with state.transaction(create=True):
        state.update(
            status="ARM_FAILED",
            worktree=repo,
            session_id=session,
            reason=UNSTARTED_ARM_REASON,
            armed_at=now(),
        )
    pointer_write(session, repo)
    commands.write_latest(repo, session)
    return state, config
