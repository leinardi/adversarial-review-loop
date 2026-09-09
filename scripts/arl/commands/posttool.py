"""``confirm-commit`` and ``posttool-failure`` -- what happens after a Bash call has run.

Ports ``cmd_confirm_commit``, ``arl_enter_reconcile`` and ``cmd_posttool_failure``.

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
from dataclasses import dataclass
from typing import Final, NoReturn

from arl import commands
from arl import config as config_module
from arl.commands import hooks
from arl.config import Config
from arl.errors import RepoResolutionError
from arl.hookio import Hook, HookInput, read_hook_input
from arl.state import State, pointer_read
from arl.util import format_at

__all__ = ["confirm_commit", "posttool_failure"]

RECONCILE_CONTEXT: Final = """\
**adversarial-review-loop: the commit that landed is not the tree that was reviewed.**

{detail}

The phase has NOT advanced. Recover explicitly, in this order:

1. `git reset --soft {parent}`   (permitted only during this reconcile)
2. rebuild the intended complete tree for this phase
3. commit again with `git add -A && git commit -m "…"` — it goes through the
   normal review gate

Do not commit forward on top of the diverging commit: the per-commit invariant
is recorded as broken, and while it is, the Stop gate will not complete this
activation at all.
"""

#: The recovery for a diverging **root** commit. ``git reset --soft`` cannot be spelled here:
#: the commit has no parent, so there is no target that exists, and the reconcile printed
#: ``git reset --soft `` with an empty target -- advice the gate itself then refused, leaving
#: the activation with no exit but abandoning it. Deleting the branch ref removes that one
#: commit and leaves the index and worktree untouched, which is what ``--soft`` does everywhere
#: else. ``pretool._gate_root_undo`` re-checks every claim in this message before allowing it.
RECONCILE_CONTEXT_ROOT: Final = """\
**adversarial-review-loop: the commit that landed is not the tree that was reviewed.**

{detail}

That commit is this repository's root commit, so there is no parent to reset to. The phase has
NOT advanced. Recover explicitly, in this order:

1. `git update-ref -d HEAD`   (permitted only during this reconcile, and only while HEAD is
   still that root commit — it drops the commit and leaves the index and working tree exactly
   as they are)
2. rebuild the intended complete tree for this phase
3. commit again with `git add -A && git commit -m "…"` — it goes through the
   normal review gate

Do not commit forward on top of the diverging commit: the per-commit invariant
is recorded as broken, and while it is, the Stop gate will not complete this
activation at all.
"""

#: The recovery when no commit landed at all and there is no earlier commit to reset to -- an
#: activation armed in an empty repository whose ``git commit`` reported success without
#: creating anything. There is nothing to undo, so the recovery is only to build the tree and
#: commit it through the gate.
RECONCILE_CONTEXT_NOTHING_LANDED: Final = """\
**adversarial-review-loop: the approved tree was never committed.**

{detail}

Nothing landed and there is no earlier commit to reset to, so there is nothing to undo. The
phase has NOT advanced. Rebuild the intended complete tree for this phase and commit again
with `git add -A && git commit -m "…"` — it goes through the normal review gate.
"""

#: What ``_verify`` records as the diverging commit when no commit was created at all.
NO_COMMIT: Final = "<none>"

ACTIVATION_MOVED: Final = (
    "**adversarial-review-loop: {change} while this commit was being confirmed.**\n\n"
    "Nothing was written: whatever moved it owns the activation now, and it is {now}."
)

UNREVIEWED_HEAD: Final = (
    "HEAD is now {head}, whose tree {head_tree} no review ever approved. The commit gate was never consulted: "
    "either the command that created it was not recognised as a commit, or HEAD was moved by something other than a commit."
)

#: An unborn HEAD is the *armed* state of a repository with no commits, and for one of those
#: it is reported as nothing at all. It is only evidence of destroyed history when the
#: activation was armed at a commit -- and it is the one HEAD move a tree comparison cannot
#: notice, since there is no tree left to compare. The branch ref can be removed without a
#: commit ever running (``git update-ref -d HEAD``, the dashed ``git-update-ref``, an unlink
#: inside ``.git``), so the check that catches it has to be the *absence* of HEAD, not the
#: shape of a command.
UNBORN_HEAD: Final = (
    "HEAD no longer exists, but this activation was armed at commit {activation}. The branch ref was deleted or the history "
    "was rewound, which no commit does -- so every commit this activation made, reviewed or not, is unreachable."
)

HEAD_UNVERIFIABLE: Final = """\
**adversarial-review-loop: this repository could not be read, so nothing about it was checked.**

{error}

The mode is {status}, and the check that reports a commit no review approved could not run.
That is not a clean result. Tell the user, and have them look at the history themselves.
"""

UNREVIEWED_HEAD_REPORT: Final = """\
**adversarial-review-loop: work was committed here that no review approved.**

{detail}

The mode is {status}, so the gate has nothing left to enforce and has changed nothing. Tell
the user: this commit is in their history and was never reviewed.
"""

#: The terminal path's own detail lines. ``UNREVIEWED_HEAD`` is deliberately **not** reworded
#: for them: it also supplies ``{detail}`` on the ``_RECONCILABLE`` path, where "HEAD is now
#: ..." is correct and the divergence is acted on.
ENDED_UNREVIEWED_HEAD: Final = (
    "When enforcement stopped ({at}), HEAD was {head}, whose tree {head_tree} no review ever approved. "
    "Either the command that created it was not recognised as a commit, or HEAD was moved by something other than a commit."
)

ENDED_UNBORN_HEAD: Final = (
    "When enforcement stopped ({at}), HEAD no longer existed, though this activation was armed at commit {activation}. "
    "The branch ref was deleted or the history was rewound, which no commit does -- so every commit this activation made, "
    "reviewed or not, is unreachable."
)

ENDED_HEAD_UNVERIFIABLE: Final = (
    "When enforcement stopped ({at}), this repository could not be read, so the gate never saw the history it was gating. That is not a clean result."
)

ENDED_EVIDENCE_MALFORMED: Final = (
    "This activation's record of how it ended is not one this gate could have written, so state.json was edited by "
    "something other than this gate. What the gate could see when enforcement stopped cannot be recovered."
)

#: The terminal path's own report, for the **one** branch that has actually established
#: unreviewed work: a recorded tree absent from ``approved_trees``.
#:
#: The old wording's last line -- "this commit is in their history" -- is what made the model
#: believe its *own* just-run command was the ungated one, and relay that. The replacement must
#: not overcorrect into the opposite false claim either: in the ``git commit && arl.sh
#: deactivate`` escape the evidence really is captured during the very Bash call being reported
#: on, and the document carries no command identity. So it says what is true and no more.
ENDED_HEAD_REPORT: Final = """\
**adversarial-review-loop: when this worktree's review gate stopped, it was holding work no review approved.**

{detail}

The mode is {status}, so the gate has nothing left to enforce and has changed nothing. This
describes the state recorded at the moment enforcement stopped; it cannot tell which command
produced that state, and commits made after that moment are ungated by design. Tell the user
about the recorded commit, not about whatever this Bash call just did.
"""

#: The other three branches. **None of them establishes that unreviewed work exists** --
#: unreadable and malformed say the gate could not see, and unborn says the history is gone --
#: so they must not borrow the categorical headline above. Reporting "it was holding work no
#: review approved" for a repository that merely could not be read is the same class of false
#: claim as the line this change removed, pointed the other way.
ENDED_UNCERTAIN_REPORT: Final = """\
**adversarial-review-loop: this worktree's review gate could not verify how it stopped.**

{detail}

The mode is {status}, so the gate has nothing left to enforce and has changed nothing. This is
not a clean result and it is not a finding of unreviewed work either -- it is the gate saying
it cannot tell. Commits made after the mode ended are ungated by design and are not what this
is about. Tell the user, and have them look at the history themselves.
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
**adversarial-review-loop: phase {phase} of {total} committed and verified.**

The commit's tree is exactly the tree the reviewer approved, and the worktree is clean.
"""

ALL_PHASES_DONE: Final = (
    VERIFIED_HEADER
    + """
All {total} phases are now committed. End your turn — the Stop gate runs the final
cumulative review over the whole activation (baseline to HEAD) and will come back
with findings if the phases do not hold together.
"""
)

ALL_PHASES_DONE_NO_FINAL_REVIEW: Final = (
    VERIFIED_HEADER
    + """
All {total} phases are now committed. End your turn -- `final_review` is disabled, so the
Stop gate completes the activation without a cumulative review. Every phase passed the
per-commit gate before it landed -- which does not always mean a model read it, since an
unchanged, already-approved or ignore_globs-matched tree passes without a call -- and the
turn-end sweep still covers anything left uncommitted. What does not run is the cross-phase
pass, and it is only available *before* the activation completes:
`/adversarial-review-loop:finish` runs one regardless of the key, and once the activation is
COMPLETE nothing can.
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


UNRESOLVABLE_CONTEXT: Final = (
    "adversarial-review-loop: the gate could not tell which repository this command ran in ({detail}), so the commit was NOT "
    "confirmed against the approved tree -- this is not a verification. git must be runnable from the hook's PATH and the "
    "working directory must exist. Run /adversarial-review-loop:status."
)


def _bind(hook: Hook, payload: HookInput, *, report_unresolvable: bool) -> tuple[State, Config, str]:
    """Resolve the payload onto a loaded activation, or emit nothing and exit 0.

    Every "not ours" answer goes through ``hook.pass_()``: zero bytes, exit 0. A post-hook
    has nothing to decide, and emitting a ``PreToolUse`` field here would be protocol
    corruption rather than a denial.

    "Not ours" has to be *proven*, though: when git cannot say which repository the call was
    about, ``confirm-commit`` reports that the commit was not confirmed
    (``report_unresolvable=True``), since silence there would read as a verification.
    ``posttool-failure`` stays silent -- its only job is clearing a pending approval, and
    leaving one stale denies more, never less.
    """
    worktree = pointer_read(payload.session_id)
    if not worktree:
        hook.pass_()

    cwd = payload.cwd or os.getcwd()
    # No fallback to cwd, unlike `pretool`: a payload from outside any repository is simply
    # not about the armed worktree -- once git has *said* so.
    try:
        repo = hooks.resolve_repo(cwd, worktree)
    except RepoResolutionError as exc:
        if report_unresolvable:
            hook.posttool_context(UNRESOLVABLE_CONTEXT.format(detail=exc))
        hook.pass_()
    if repo != worktree:
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

#: The subset of :data:`_REPORT_ONLY` in which enforcement has **stopped**, so the question to
#: ask is what the gate could see at that moment rather than what HEAD is now. Routed to
#: :func:`_guard_ended_head`.
#:
#: ``NEEDS_HUMAN`` and ``STALE`` deliberately stay on the current-HEAD path: the loop is still
#: live in both, every mutation is still denied, and current HEAD is exactly the right
#: question there. Disjoint from :data:`_RECONCILABLE`, so the reconcile branch is unreachable
#: from here.
_ENDED: Final = frozenset({"DISARMED", "COMPLETE", "RESUMED"})


def _confirm_commit(hook: Hook) -> None:
    payload = read_hook_input()
    if payload.tool_name != "Bash":
        hook.pass_()
    state, config, repo = _bind(hook, payload, report_unresolvable=True)
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
    from arl import gitsnap  # noqa: PLC0415 - only this branch needs git

    status = check.expected.effective_status
    if status not in _RECONCILABLE and status not in _REPORT_ONLY:
        return
    if status in _ENDED:
        # Enforcement has stopped, so "is current HEAD approved?" is not a question about this
        # gate: it fires on every ordinary commit made afterwards, forever. Everything below
        # still serves the live statuses.
        _guard_ended_head(check, status=status)
        return
    try:
        head_tree = gitsnap.head_tree_checked(check.repo)
    except gitsnap.GitUnavailable as exc:
        # **Not silence.** `head_tree` answers "" for a repository with no commits *and* for
        # one git cannot read, so a wrapper could commit, disarm, and then make `.git`
        # unreadable -- suppressing the only report this hole has. "The gate cannot see the
        # history" is not "the history is fine" (Rule 1).
        check.hook.posttool_context(HEAD_UNVERIFIABLE.format(status=status, error=exc).rstrip("\n"))
    activation = check.state.get("activation_commit")
    if not head_tree and not activation:
        # A repository with no commits yet, which is the state `arm` froze. Nothing to report.
        return
    if head_tree and check.state.tree_approved(head_tree):
        return

    repo = check.repo
    if not head_tree:
        # HEAD vanished from a repository that had commits when it was armed. Reported through
        # the same path as an unapproved HEAD, with the activation commit as the recovery
        # target -- `git reset --soft <activation>` is valid on an unborn branch and puts the
        # history back where the loop started.
        _unreviewed(check, status=status, detail=UNBORN_HEAD.format(activation=activation), bad=NO_COMMIT, parent=activation)
    head = gitsnap.head_commit(repo)
    parent = gitsnap.rev_parse(repo, "HEAD^") or activation
    _unreviewed(check, status=status, detail=UNREVIEWED_HEAD.format(head=head, head_tree=head_tree), bad=head, parent=parent)


def _guard_ended_head(check: _Check, *, status: str) -> None:
    """The :data:`_ENDED` half of :func:`_guard_unreviewed_head`: report from the record.

    Same branch table as ``stop._ended``, ending in ``posttool_context`` rather than a
    ``systemMessage``. It never routes through :func:`_unreviewed`: :data:`_ENDED` is disjoint
    from :data:`_RECONCILABLE`, so the reconcile branch is unreachable from here and a
    retired activation can never be written back into one that still has something to recover.

    **Makes no git call**, so a Bash call in an ended worktree stops paying one ``git
    rev-parse`` -- the saving the live path cannot have, since it genuinely must ask about
    HEAD now.

    Detection is exactly "the recorded tree is absent from ``approved_trees``", which is
    neither proof of review nor proof of commit identity; see ``docs/security.md``.
    """
    end = hooks.end_state(check.state)
    if end.malformed:
        check.hook.posttool_context(ENDED_UNCERTAIN_REPORT.format(status=status, detail=ENDED_EVIDENCE_MALFORMED).rstrip("\n"))
    if not end.recorded:
        # Ended before the record existed. Current HEAD answers a different question, and
        # asking it here is what made every post-stop commit look like an escape. `pretool`
        # upgrades a stale document on the first tool call it gates, so this is only reached
        # for an activation that had *already* ended before the record existed.
        return
    at = format_at(end.at)
    if end.capture == "unreadable":
        check.hook.posttool_context(ENDED_UNCERTAIN_REPORT.format(status=status, detail=ENDED_HEAD_UNVERIFIABLE.format(at=at)).rstrip("\n"))
    if end.capture == "unborn":
        activation = check.state.get("activation_commit")
        if activation:
            # An unborn HEAD is also the armed state of a repository with no commits, so only
            # a non-empty anchor makes this destroyed history rather than nothing to report.
            detail = ENDED_UNBORN_HEAD.format(at=at, activation=activation)
            check.hook.posttool_context(ENDED_UNCERTAIN_REPORT.format(status=status, detail=detail).rstrip("\n"))
        return
    if not check.state.tree_approved(end.tree):
        # The only branch that has established unreviewed work, and the only one that may say
        # so categorically.
        detail = ENDED_UNREVIEWED_HEAD.format(at=at, head=end.head, head_tree=end.tree)
        check.hook.posttool_context(ENDED_HEAD_REPORT.format(status=status, detail=detail).rstrip("\n"))


def _unreviewed(check: _Check, *, status: str, detail: str, bad: str, parent: str) -> NoReturn:
    """Record or report a HEAD no review approved, according to what the status allows."""
    if status in _RECONCILABLE:
        _reconcile(check, detail=detail, bad=bad, parent=parent)

    # Nothing to transition to, so the answer is to say so. **Silence here is what makes the
    # wrapper escape work**: a Bash command that commits and then runs `arl.sh deactivate`
    # leaves an unreviewed commit behind a mode that looks deliberately stopped, and skipping
    # this branch meant no one was ever told. Saying it here reaches the model; the Stop gate
    # says it again through `systemMessage`, which reaches the user.
    check.hook.posttool_context(UNREVIEWED_HEAD_REPORT.format(status=status, detail=detail).rstrip("\n"))


def _verify(check: _Check, *, pending: str) -> NoReturn:
    """Compare what landed against what was approved. Ends in a ``PostToolUse`` message."""
    from arl import gitsnap  # noqa: PLC0415 - only this branch needs git

    repo = check.repo
    pending_head = check.expected.pending_head
    head = gitsnap.head_commit(repo)
    parent = gitsnap.rev_parse(repo, "HEAD^")
    head_tree = gitsnap.head_tree(repo)

    if not head or head == pending_head:
        _reconcile(
            check,
            detail=HEAD_DID_NOT_MOVE.format(pending=pending),
            bad=head or NO_COMMIT,
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

    _advance(check, pending=pending, head=head)


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
    check.hook.posttool_context(_recovery(detail=detail, bad=bad, parent=parent).rstrip("\n"))


def _recovery(*, detail: str, bad: str, parent: str) -> str:
    """The recovery instructions that match what actually landed.

    Three cases, and only the first has a commit to reset to. An empty ``parent`` used to be
    interpolated into ``git reset --soft {parent}`` regardless, printing a command with no
    target -- which ``cmdshape.reset_target`` refuses and ``_gate_reset`` could never match
    against an empty ``bad_commit_parent`` anyway. The reconcile was therefore unrecoverable in
    exactly the case an activation armed in an empty repository always hits.
    """
    if parent:
        return RECONCILE_CONTEXT.format(detail=detail, parent=parent)
    if bad and bad != NO_COMMIT:
        return RECONCILE_CONTEXT_ROOT.format(detail=detail)
    return RECONCILE_CONTEXT_NOTHING_LANDED.format(detail=detail)


def _advance(check: _Check, *, pending: str, head: str) -> NoReturn:
    """The commit is exactly what was reviewed: bank the tree and move to the next phase.

    ``head`` is the commit ``_verify`` just proved: its parent is the approved ``pending_head``,
    its tree is the approved tree, and the worktree was clean afterwards. Recording it against
    the phase number builds ``phase_commits``, the only evidence that a phase was *done* rather
    than merely counted -- ``phase`` is a single integer in a document AGENTS.md is explicit is
    not a trust boundary, so incrementing it past the work is indistinguishable from doing the
    work unless something outside that document says otherwise. These SHAs are checked against
    git history before the no-review completion path may disarm; see
    ``completion.phase_progress_proven``.
    """
    state = check.state
    try:
        with state.transaction():
            _still_current(check)
            # Appended to the reloaded list, never to one read before the lock: two confirmations
            # racing here would otherwise each write a list missing the other's entry.
            recorded = [entry for entry in state.get_array_of_dicts("phase_commits") if entry.get("phase") != check.expected.phase]
            recorded.append({"phase": check.expected.phase, "commit": head})
            updates: dict[str, object] = {
                "last_approved_tree": pending,
                "pending_approved_tree": "",
                "pending_head": "",
                "pending_command": "",
                "status": "ACTIVE",
                "reason": "",
                "phase": check.expected.phase + 1,
                "phase_commits": recorded,
                "failures": 0,
                # Phase 6: a transient failure/backoff belongs to the phase whose review hit
                # it. `approve()` already clears both the moment its own review approves, but
                # a busy-slot loser can still record one afterwards, in its own later
                # transaction (see `_review_failed`'s fingerprint guard for why that write is
                # now refused when it is stale -- this reset is what covers the ordinary,
                # non-racy case: a transient failure earlier in *this* phase, retried
                # successfully, must not carry its counters into the next one).
                "transient_failures": 0,
                "retry_not_before": 0,
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
        # Same `_Check.config` branch as the PAUSE_TARGET_REACHED / NEXT_PHASE choice below:
        # what the model is told to expect from ending its turn has to match what the Stop
        # gate will actually do, and with `final_review` off there is no cumulative review.
        done = ALL_PHASES_DONE if check.config.as_bool("final_review") else ALL_PHASES_DONE_NO_FINAL_REVIEW
        check.hook.posttool_context(done.format(phase=phase, total=total).rstrip("\n"))
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
    state, _config, _repo = _bind(hook, payload, report_unresolvable=False)
    if not state.get("pending_approved_tree"):
        hook.pass_()
    # No activation guard here, and none is needed: clearing a pending approval can only ever
    # deny more. The value it writes is the same whatever else changed underneath it.
    with state.transaction():
        state.update(pending_approved_tree="", pending_head="", pending_command="")
    hook.pass_()
