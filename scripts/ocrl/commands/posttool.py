"""``confirm-commit`` and ``posttool-failure`` -- what happens after a Bash call has run.

Ports ``cmd_confirm_commit``, ``ocrl_enter_reconcile`` and ``cmd_posttool_failure``.

Neither of these can deny anything: the tool call has already happened. What
``confirm-commit`` does instead is **independently verify** that what landed is what was
approved -- ``HEAD^{tree} == pending_approved_tree``, the parent is the commit HEAD was at
when the review passed, and the worktree is clean afterwards. That is the second half of the
defence AGENTS.md describes: a command-shape bypass yields a *detected, recoverable* bad
commit that enters ``RECONCILE``, rather than a silent unreviewed one.

Both entrypoints answer "not ours" by emitting **nothing** and exiting 0. That is a real
decision, not a fallthrough: the pending approval is consumed only by the exact command that
was approved, so anything else must leave it exactly where it is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, NoReturn

from ocrl import commands
from ocrl import config as config_module
from ocrl.commands import hooks
from ocrl.config import Config
from ocrl.hookio import Hook, HookInput, read_hook_input
from ocrl.state import State, pointer_read

__all__ = ["confirm_commit", "posttool_failure"]

RECONCILE_CONTEXT: Final = """\
**opencode-review-loop: the commit that landed is not the tree that was reviewed.**

{detail}

The phase has NOT advanced. Recover explicitly, in this order:

1. `git reset --soft {parent}`   (permitted only during this reconcile)
2. rebuild the intended complete tree for this phase
3. commit again with `git add -A && git commit -m "…"` — it goes through the
   normal review gate

Do not commit forward on top of the diverging commit: the per-commit invariant
is recorded as broken and the final cumulative review will still cover it.
"""

ACTIVATION_MOVED: Final = (
    "**opencode-review-loop: {change} while this commit was being confirmed.**\n\n"
    "Nothing was written: whatever moved it owns the activation now, and it is {now}."
)

UNREVIEWED_HEAD: Final = (
    "HEAD is now {head}, whose tree {head_tree} no review ever approved. The commit gate was never consulted: "
    "either the command that created it was not recognised as a commit, or HEAD was moved by something other than a commit."
)

HEAD_UNVERIFIABLE: Final = """\
**opencode-review-loop: this repository could not be read, so nothing about it was checked.**

{error}

The mode is {status}, and the check that reports a commit no review approved could not run.
That is not a clean result. Tell the user, and have them look at the history themselves.
"""

UNREVIEWED_HEAD_REPORT: Final = """\
**opencode-review-loop: work was committed here that no review approved.**

{detail}

The mode is {status}, so the gate has nothing left to enforce and has changed nothing. Tell
the user: this commit is in their history and was never reviewed.
"""

HEAD_DID_NOT_MOVE: Final = "The command reported success but HEAD did not move, so no commit was created for the approved tree {pending}."

WRONG_PARENT: Final = (
    "The new commit {head} has parent {parent}, but HEAD was {pending_head} when the review was approved. "
    "That is an amend or a rewrite, not a commit on top of the reviewed state."
)

WRONG_TREE: Final = (
    "The commit {head} has tree {head_tree}, but the approved tree was {pending}. Something other than the reviewed snapshot "
    "was committed (a partial stage, a pathspec, or a change made between the gate and the commit)."
)

DIRTY_AFTERWARDS: Final = (
    "The commit {head} matches the approved tree, but the worktree is not clean afterwards: content appeared between the gate "
    "and now. Every phase must commit all of its work."
)

VERIFIED_HEADER: Final = """\
**opencode-review-loop: phase {phase} of {total} committed and verified.**

The commit's tree is exactly the tree OpenCode approved, and the worktree is clean.
"""

ALL_PHASES_DONE: Final = (
    VERIFIED_HEADER
    + """
All {total} phases are now committed. End your turn — the Stop gate runs the final
cumulative review over the whole activation (baseline to HEAD) and will come back
with findings if the phases do not hold together.
"""
)

NEXT_PHASE: Final = (
    VERIFIED_HEADER
    + """
Continue straight into phase {next_phase} of {total} without ending your turn:

    {description}

Implement it, then commit it the same way.
"""
)

PAUSE_TARGET_REACHED: Final = (
    VERIFIED_HEADER
    + """
The pause target (phase {target} of {total}) has been reached. End your turn now and report to
the user rather than continuing into phase {next_phase} -- this is not an approval of the
remaining phases, only of the ones just committed.
"""
)


# --------------------------------------------------------------------------
# Shared binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Check:
    """One confirmation in progress: where to answer, and what it is confirming.

    ``expected`` is captured before the git processes run and re-checked inside whichever
    transaction the confirmation ends in. Verifying against one document and then writing to
    whatever the lock later hands back is how a phase gets advanced twice by two overlapping
    hooks, and how a concurrent ``deactivate`` gets turned back into ``ACTIVE``.
    """

    hook: Hook
    state: State
    config: Config
    repo: str
    expected: hooks.Activation


def _bind(hook: Hook, payload: HookInput) -> tuple[State, Config, str]:
    """Resolve the payload onto a loaded activation, or emit nothing and exit 0.

    Every "not ours" answer goes through ``hook.pass_()``: zero bytes, exit 0. A post-hook
    has nothing to decide, and emitting a ``PreToolUse`` field here would be protocol
    corruption rather than a denial.
    """
    worktree = pointer_read(payload.session_id)
    if not worktree:
        hook.pass_()

    cwd = payload.cwd or os.getcwd()
    # No fallback to cwd, unlike `pretool`: a payload from outside any repository is simply
    # not about the armed worktree.
    if hooks.resolve_repo(cwd, worktree) != worktree:
        hook.pass_()

    state = State(worktree, payload.session_id)
    if not state.load():
        hook.pass_()
    # The shell loaded the configuration here and then read nothing out of it. It is read
    # now, for `ttl_hours`: an activation that expired between the gate and the commit must
    # not have a phase advanced on it.
    config = config_module.load(worktree, overrides=state.data.get("overrides"))
    return state, config, worktree


# --------------------------------------------------------------------------
# confirm-commit
# --------------------------------------------------------------------------


def confirm_commit(argv: list[str]) -> int:
    """Entrypoint for the ``PostToolUse`` hook on Bash."""
    del argv
    hook = Hook()
    # The shell armed nothing here, so a crash emitted nothing at all. This says
    # `additionalContext` instead -- the one shape this event is already known to accept on
    # the pinned version -- because a post-commit check that crashed has *not* verified the
    # commit, and silence reads exactly like a verification that passed.
    hook.arm_failclosed("posttool")
    return hook.run(lambda: _confirm_commit(hook))


#: Statuses in which an unapproved HEAD is a divergence this hook may record itself.
_RECONCILABLE: Final = frozenset({"ACTIVE", "ARMED"})

#: Statuses in which an unapproved HEAD is reported but **not** acted on. ``DISARMED`` and
#: ``COMPLETE`` are the interesting pair: the user may legitimately have ended the mode with
#: work still uncommitted, and reverting that would take an exit away from them (Rule 4). The
#: two escalations are here because writing ``RECONCILE`` over either downgrades a stronger
#: denial. ``RECONCILE`` itself is absent -- it already records a divergence -- and so is
#: ``ARM_FAILED``, which froze no baseline, so *every* tree is unapproved under it. ``RESUMED``
#: must never become ``RECONCILE``: that would resurrect a retired activation into one that
#: still has something to recover, so an unapproved HEAD under it is reported, not acted on.
_REPORT_ONLY: Final = frozenset({"DISARMED", "COMPLETE", "NEEDS_HUMAN", "STALE", "RESUMED"})


def _confirm_commit(hook: Hook) -> None:
    payload = read_hook_input()
    if payload.tool_name != "Bash":
        hook.pass_()
    state, config, repo = _bind(hook, payload)
    check = _Check(hook=hook, state=state, config=config, repo=repo, expected=hooks.activation(state, config))

    pending = state.get("pending_approved_tree")
    if not pending:
        _guard_unreviewed_head(check)
        hook.pass_()
    # Only the very command the gate approved may consume the approval.
    if payload.command != state.get("pending_command"):
        _guard_unreviewed_head(check)
        hook.pass_()

    _verify(check, pending=pending)


def _guard_unreviewed_head(check: _Check) -> None:
    """Notice a commit that never reached the gate at all.

    ``pretool`` decides from a *string*, and there is no textual rule that catches every way
    of running git: ``$(printf git) commit``, ``eval``, ``xargs``, a shell function, a
    Makefile target. The real parser does not close that either -- it can only report that a
    command name is a substitution node.

    This asks git instead, after the fact, and it is the check that makes the string-matching
    half defence in depth rather than the whole defence. HEAD's tree is compared against the
    set of trees a review actually approved; anything else means a commit landed -- or HEAD
    was moved -- without the gate ever being consulted. That is recorded as a divergence, not
    a denial: the call has already run, and ``RECONCILE`` is what says so and how to recover.

    Costs one ``git rev-parse`` per Bash call in the armed worktree. That is the post-hook,
    not the ``PreToolUse`` hot path.
    """
    from ocrl import gitsnap  # noqa: PLC0415 - only this branch needs git

    status = check.expected.effective_status
    if status not in _RECONCILABLE and status not in _REPORT_ONLY:
        return
    try:
        head_tree = gitsnap.head_tree_checked(check.repo)
    except gitsnap.GitUnavailable as exc:
        # **Not silence.** `head_tree` answers "" for a repository with no commits *and* for
        # one git cannot read, so a wrapper could commit, disarm, and then make `.git`
        # unreadable -- suppressing the only report this hole has. "The gate cannot see the
        # history" is not "the history is fine" (Rule 1).
        check.hook.posttool_context(HEAD_UNVERIFIABLE.format(status=status, error=exc).rstrip("\n"))
    # Empty means a repository with no commits yet, which is the state `arm` froze.
    if not head_tree or check.state.tree_approved(head_tree):
        return

    repo = check.repo
    head = gitsnap.head_commit(repo)
    detail = UNREVIEWED_HEAD.format(head=head, head_tree=head_tree)
    if status in _RECONCILABLE:
        parent = gitsnap.rev_parse(repo, "HEAD^") or check.state.get("activation_commit")
        _reconcile(check, detail=detail, bad=head, parent=parent)

    # Nothing to transition to, so the answer is to say so. **Silence here is what makes the
    # wrapper escape work**: a Bash command that commits and then runs `ocrl.sh deactivate`
    # leaves an unreviewed commit behind a mode that looks deliberately stopped, and skipping
    # this branch meant no one was ever told. Saying it here reaches the model; the Stop gate
    # says it again through `systemMessage`, which reaches the user.
    check.hook.posttool_context(UNREVIEWED_HEAD_REPORT.format(status=status, detail=detail).rstrip("\n"))


def _verify(check: _Check, *, pending: str) -> NoReturn:
    """Compare what landed against what was approved. Ends in a ``PostToolUse`` message."""
    from ocrl import gitsnap  # noqa: PLC0415 - only this branch needs git

    repo = check.repo
    pending_head = check.expected.pending_head
    head = gitsnap.head_commit(repo)
    parent = gitsnap.rev_parse(repo, "HEAD^")
    head_tree = gitsnap.head_tree(repo)

    if not head or head == pending_head:
        _reconcile(
            check,
            detail=HEAD_DID_NOT_MOVE.format(pending=pending),
            bad=head or "<none>",
            parent=pending_head or check.state.get("activation_commit"),
        )
    if parent != pending_head:
        _reconcile(
            check,
            detail=WRONG_PARENT.format(head=head, parent=parent or "<none>", pending_head=pending_head or "<none>"),
            bad=head,
            parent=parent or check.state.get("activation_commit"),
        )
    if head_tree != pending:
        _reconcile(check, detail=WRONG_TREE.format(head=head, head_tree=head_tree, pending=pending), bad=head, parent=parent)
    if not gitsnap.worktree_clean(repo):
        _reconcile(check, detail=DIRTY_AFTERWARDS.format(head=head), bad=head, parent=parent)

    _advance(check, pending=pending)


def _still_current(check: _Check) -> None:
    """Raise ``commands.Refused`` unless the reloaded document is the one that was verified.

    Called with the activation lock held, so "unchanged" means unchanged as of the instant
    the write is about to happen -- which is the only moment at which the claim is worth
    anything.
    """
    current = hooks.activation(check.state, check.config)
    if current != check.expected:
        raise commands.Refused(ACTIVATION_MOVED.format(change=hooks.describe_move(check.expected, current), now=current.summary))


def _reconcile(check: _Check, *, detail: str, bad: str, parent: str) -> NoReturn:
    """Record that a commit diverged from the reviewed tree, and say how to recover.

    The pending approval is cleared: it belonged to a commit that did not happen the way the
    gate was told it would, so nothing may consume it afterwards.
    """
    try:
        with check.state.transaction():
            _still_current(check)
            check.state.update(
                status="RECONCILE",
                reason=detail,
                bad_commit=bad,
                bad_commit_parent=parent,
                pending_approved_tree="",
                pending_head="",
                pending_command="",
            )
    except commands.Refused as exc:
        # Whoever moved the activation owns its status now, and one of the states it can have
        # moved to is DISARMED -- writing RECONCILE over that would restart enforcement the
        # user had already ended (Rule 4). The divergence is still reported rather than lost.
        check.hook.posttool_context(f"{exc}\nThe commit that landed is not the tree that was reviewed:\n\n{detail}".rstrip("\n"))
    check.hook.posttool_context(RECONCILE_CONTEXT.format(detail=detail, parent=parent).rstrip("\n"))


def _advance(check: _Check, *, pending: str) -> NoReturn:
    """The commit is exactly what was reviewed: bank the tree and move to the next phase."""
    state = check.state
    try:
        with state.transaction():
            _still_current(check)
            updates: dict[str, object] = {
                "last_approved_tree": pending,
                "pending_approved_tree": "",
                "pending_head": "",
                "pending_command": "",
                "status": "ACTIVE",
                "reason": "",
                "phase": check.expected.phase + 1,
                "failures": 0,
                "stop_blocks": 0,
            }
            # The commit just confirmed is deterministically the one an abandoned resume
            # approval would have produced on a rerun: same parent, same tree. Clearing the
            # marker here is safe precisely because confirmation is stronger than the marker
            # -- _verify already proved the parent, the tree and a clean worktree -- and
            # without this, the most ordinary recovery there is (rerun the phase) turns into a
            # false RECONCILE one commit later, when the *next* scan matches the pair anyway.
            if state.get("abandoned_pending_tree") == pending and state.get("abandoned_pending_head") == check.expected.pending_head:
                updates["abandoned_pending_tree"] = ""
                updates["abandoned_pending_head"] = ""
            state.update(updates)
    except commands.Refused as exc:
        check.hook.posttool_context(f"{exc}\nThe phase was NOT advanced.".rstrip("\n"))

    phase = check.expected.phase
    next_phase = phase + 1
    total = state.phase_count()
    if next_phase > total:
        check.hook.posttool_context(ALL_PHASES_DONE.format(phase=phase, total=total).rstrip("\n"))
    target = state.get_int("stop_after_phase") or total
    if next_phase > target:
        check.hook.posttool_context(PAUSE_TARGET_REACHED.format(phase=phase, total=total, target=target, next_phase=next_phase).rstrip("\n"))
    check.hook.posttool_context(
        NEXT_PHASE.format(phase=phase, total=total, next_phase=next_phase, description=state.phase_desc(next_phase)).rstrip("\n")
    )


# --------------------------------------------------------------------------
# posttool-failure
# --------------------------------------------------------------------------


def posttool_failure(argv: list[str]) -> int:
    """Entrypoint for ``PostToolUseFailure``.

    Emits nothing, ever, and the fail-closed fallback is silence too. Its only job is
    clearing a pending approval after a Bash call that failed; a crash leaves that pending
    tree stale rather than granting anything, because ``confirm-commit`` still requires an
    exact ``HEAD^{tree}`` match before it will advance a phase. Inventing a protocol shape
    for this event would trade a small, inert inconsistency for a possibly-ignored message.
    """
    del argv
    hook = Hook()
    hook.arm_failclosed("none")
    return hook.run(lambda: _posttool_failure(hook))


def _posttool_failure(hook: Hook) -> None:
    payload = read_hook_input()
    # Deliberately no `tool_name` filter, matching the shell: a pending approval belongs to
    # the turn, and any failed call in the armed worktree invalidates it.
    state, _config, _repo = _bind(hook, payload)
    if not state.get("pending_approved_tree"):
        hook.pass_()
    # No activation guard here, and none is needed: clearing a pending approval can only ever
    # deny more. The value it writes is the same whatever else changed underneath it.
    with state.transaction():
        state.update(pending_approved_tree="", pending_head="", pending_command="")
    hook.pass_()
