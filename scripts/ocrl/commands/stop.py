"""``gate-stop`` -- the Stop gate. Ports ``cmd_gate_stop`` and ``ocrl_stop_block_counted``.

This is the backstop. Everything the per-commit gate can miss ends up here: uncommitted work
nobody reviewed, phases nobody implemented, a reconcile nobody finished, and finally the
cumulative review of the whole activation from the frozen baseline to HEAD.

Two shapes of answer, and the difference matters:

- ``stop_block`` sends the turn back to Claude with a reason. It is **counted**, because a
  gate that blocks forever with no progress is a wedged session rather than an enforcement.
  ``max_stop_blocks`` consecutive no-progress blocks escalate to ``NEEDS_HUMAN``.
- ``stop_ok`` lets the turn end. It is used for every terminal state -- including the
  escalations, which end the turn while leaving every mutation denied. **Letting a turn end
  is not an approval**, and the messages say so wherever it could be misread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from ocrl import commands
from ocrl import config as config_module
from ocrl.commands import completion, hooks
from ocrl.hookio import Hook, read_hook_input
from ocrl.state import State, pointer_read

if TYPE_CHECKING:  # pragma: no cover
    from ocrl.config import Config
    from ocrl.gitsnap import Snapshot

__all__ = ["run"]


@dataclass(frozen=True)
class _Gate:
    """What every branch below needs to answer: where to write, and what it is enforcing.

    One value rather than four parallel parameters, because they travel together through
    every helper here and a transposed pair of strings would type-check silently.
    """

    hook: Hook
    state: State
    config: Config
    worktree: str
    #: What the activation was when this turn end started. Every escalation below is guarded
    #: with it, because a review takes minutes and the user can leave the mode during them.
    expected: hooks.Activation


NO_SESSION: Final = (
    "opencode-review-loop: the Stop payload carried no session id, so the gate cannot identify this activation. "
    "This is an integration fault, not an approval — nothing was reviewed."
)

UNSTARTED_ARM: Final = (
    "opencode-review-loop: arming never ran, so nothing was frozen and nothing has been reviewed. The hooks registered but "
    "ocrl.sh arm did not execute at prompt-expansion time. Tell the user what the slash command reported; they can fix the "
    "cause and re-run /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop. "
    "Do not implement the plan."
)

MISSING_STATE_REASON: Final = "the activation state for this session could not be read, so the gate cannot tell what has been reviewed"

MISSING_STATE: Final = """\
opencode-review-loop: the activation state for this session could not be read, so nothing can be shown to have been reviewed and the turn is not approved.

This is an enforcement failure, not a review finding: the session pointer says this worktree was armed, but its state.json is missing or unreadable. It has escalated to NEEDS_HUMAN, so every mutation stays denied.

Tell the user. They can re-arm with /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop.
"""

STILL_NEEDS_HUMAN: Final = (
    "opencode-review-loop: still in NEEDS_HUMAN ({reason}). The work was not reviewed to completion. "
    "Run /opencode-review-loop:stop to leave the mode."
)

ARM_FAILED: Final = """\
opencode-review-loop: arming failed, so nothing has been reviewed and nothing may be implemented.

Reason: {reason}

Tell the user. They can re-run /opencode-review-loop:implement <plan.md> or /opencode-review-loop:stop. Do not attempt to implement the plan.
"""

STALE: Final = (
    "opencode-review-loop: this activation is past ttl_hours ({ttl_hours}) and blocks rather than silently disarming. "
    "Tell the user to re-arm with /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop.\n"
)

NOT_FROZEN: Final = """\
opencode-review-loop: the phase list has not been frozen, so no work can start and no review has run.

Read the frozen plan ({act_dir}/plan.frozen.md) and run exactly:

    {plugin_root}/scripts/ocrl.sh set-phases --phase "…" --phase "…"
"""

RECONCILE: Final = """\
opencode-review-loop: a commit diverged from the reviewed tree and the reconcile is unfinished.

{reason}

Recover with `git reset --soft {parent}`, rebuild the phase, and commit again.
"""

UNVERIFIABLE_AT_EXIT: Final = """\
opencode-review-loop: the mode is {status}, and this repository could not be read.

{error}

The check that reports work committed without passing the review gate could not run, so this turn ending says nothing about whether the history was reviewed. Look at it yourself.
"""

UNREVIEWED_AT_EXIT: Final = """\
opencode-review-loop: the mode is {status}, but HEAD is {head}, whose tree {head_tree} no review ever approved.

Work was committed in this worktree without passing the review gate. If you did not stop the mode yourself, it was ended from inside a Bash command — the gate cannot tell those apart, so it reports rather than acts.

Review that commit yourself, or re-arm with /opencode-review-loop:implement <plan.md>.
"""

ACTIVATION_MOVED: Final = (
    "opencode-review-loop: {change} while this turn was being reviewed, so nothing was written — whatever moved it owns the "
    "activation now, and it is {now}. This is NOT an approval: the work was not reviewed to completion."
)

DEFERRED: Final = "opencode-review-loop: turn end deferred once at your request. The gate is still armed."

STALLED: Final = """\
opencode-review-loop: STALLED — {blocks} no-progress Stop blocks in a row (limit {limit}), so the loop escalated to NEEDS_HUMAN. This is NOT an approval and the work was NOT reviewed to completion.

Last reason: {reason}

Every mutation stays denied until you run /opencode-review-loop:stop.
"""

SNAPSHOT_FAILED: Final = "opencode-review-loop: the working state could not be snapshotted ({error}), so the turn cannot be approved."

SWEEP_CHANGES: Final = "opencode-review-loop: the turn is ending with uncommitted work that OpenCode requires changes to."

SWEEP_ESCALATED: Final = "opencode-review-loop: escalated to NEEDS_HUMAN — {error}. This is NOT an approval."

SWEEP_FAILED: Final = (
    "opencode-review-loop: the review of the uncommitted work failed ({error}), so the turn cannot end as reviewed. "
    "A failed review is never an approval."
)

PHASES_OUTSTANDING: Final = """\
opencode-review-loop: phases {phase}..{total} are still outstanding, so the final cumulative review has not run.

Next up, phase {phase} of {total}:

    {description}

Implement it and commit it. If the remaining phases should be abandoned, say so and let the user run /opencode-review-loop:finish.
"""

NOT_CLEAN: Final = """\
opencode-review-loop: the worktree is not clean, so the last of the work is not in any reviewed commit. Commit it (`git add -A && git commit -m "…"`) before the final review runs.

{summary}
"""

COMPLETE: Final = """\
opencode-review-loop: COMPLETE. The final cumulative review of the whole activation (baseline {base} -> {head}) passed, across {total} phases. The mode has disarmed itself; further commits are ungated.

Full report: {report}
"""

FINAL_CHANGES: Final = "opencode-review-loop: the final cumulative review found problems across the whole activation."

FINAL_ESCALATED: Final = "opencode-review-loop: the final review escalated to NEEDS_HUMAN — {error}. This is NOT an approval."

FINAL_FAILED: Final = (
    "opencode-review-loop: the final cumulative review failed ({error}). A failed review is never an approval; end your turn again to retry."
)


def run(argv: list[str]) -> int:
    """Entrypoint for the ``Stop`` hook."""
    del argv
    hook = Hook()
    hook.arm_failclosed("stop")
    return hook.run(lambda: _gate_stop(hook))


def _gate_stop(hook: Hook) -> None:
    payload = read_hook_input()
    cwd = payload.cwd or os.getcwd()

    worktree = pointer_read(payload.session_id)
    if not worktree:
        _no_pointer(hook, session=payload.session_id, cwd=cwd)

    # The configuration is loaded against the *armed worktree*, not against cwd: the Stop
    # hook fires wherever the turn happened to end, and the activation's own repo config is
    # what the gate is enforcing. The shell also resolved cwd to a repository here and then
    # never read the result; that dead resolution is a git process per turn end, and it is
    # not reproduced.
    config = config_module.load(worktree)
    state = State(worktree, payload.session_id)
    if not state.load():
        _no_state(hook, state)

    gate = _Gate(hook=hook, state=state, config=config, worktree=worktree, expected=hooks.activation(state, config))
    _by_status(gate)
    _review(gate)


def _no_state(hook: Hook, state: State) -> NoReturn:
    """A live pointer with no readable state. The shell ended the turn here; this blocks.

    The pointer says this session armed, so unreadable state is the fail-open case -- exactly
    the one ``pretool`` denies every mutation for. Ending the turn silently on it reports a
    completed piece of work as reviewed when nothing was.

    It escalates rather than merely blocking because a block has to be **counted** to be
    bounded, and counting needs a document to count in -- which is the thing that is missing.
    ``needs_human`` writes one whose only effect is to deny, so this blocks exactly once and
    every later turn end takes the ``NEEDS_HUMAN`` branch above: the turn may end, and every
    mutation stays denied until the user runs ``/opencode-review-loop:stop``.
    """
    state.needs_human(MISSING_STATE_REASON)
    hook.stop_block(MISSING_STATE.rstrip("\n"))


def _no_pointer(hook: Hook, *, session: str, cwd: str) -> NoReturn:
    """Same reasoning as ``pretool``: no pointer means arming never ran (**Rule 0**)."""
    recorded = hooks.record_unstarted_arm(session, cwd)
    if recorded is None:
        hook.stop_ok(NO_SESSION)
    state, config = recorded
    # Counted, not unconditional: without this the same message repeats on every turn end
    # until the host's own block cap intervenes.
    gate = _Gate(hook=hook, state=state, config=config, worktree=state.worktree, expected=hooks.activation(state, config))
    _block_counted(gate, UNSTARTED_ARM)


def _block_counted(gate: _Gate, reason: str) -> NoReturn:
    """Block the turn, but account for whether anything moved since the last block.

    Only genuine stalls count toward ``max_stop_blocks``: the marker is the tuple of things
    that change when the loop makes progress, so a block that follows a new approved tree, a
    new phase or a status transition starts the count again.

    The count is taken **inside** the transaction, against the document it reloads, for the
    reason ``defer`` documents: two overlapping Stop hooks reading the same starting value
    would both write the same one, and the limit would never be reached.
    """
    state = gate.state
    with state.transaction():
        marker = f"{state.get('last_approved_tree')}:{state.get('phase')}:{state.get('status')}"
        blocks = state.get_int("stop_blocks") + 1 if marker == state.get("stop_marker") else 1
        state.update(stop_blocks=blocks, stop_marker=marker)

    limit = gate.config.as_int("max_stop_blocks")
    if blocks > limit:
        _escalate(gate, f"the Stop gate blocked {blocks} times with no progress in between: {reason}")
        gate.hook.stop_ok(STALLED.format(blocks=blocks, limit=limit, reason=reason).rstrip("\n"))
    gate.hook.stop_block(reason)


def _ended(gate: _Gate, status: str) -> NoReturn:
    """The mode is off. Let the turn end -- but not silently if work went unreviewed.

    ``systemMessage`` rather than a block, and that choice is the point: it reaches the
    **user** instead of the model, and the model does not get to decide whether to relay it.

    This is the only place a Rule 4 escape becomes visible. A Bash command that commits and
    then runs ``ocrl.sh deactivate`` leaves exactly this shape behind -- an unapproved HEAD
    under a mode that looks deliberately stopped -- and the gate cannot tell it apart from a
    user who stopped the mode with work outstanding. So it reports rather than acts: reverting
    would take an exit away from the user, which is the same rule in the other direction.
    """
    from ocrl import gitsnap  # noqa: PLC0415 - a disarmed session pays one git process here

    try:
        head_tree = gitsnap.head_tree_checked(gate.worktree)
    except gitsnap.GitUnavailable as exc:
        # The same hole the post-hook guards: `head_tree` cannot tell an unreadable `.git`
        # from a repository with no commits, and reading the empty string as "nothing to see"
        # is what lets breaking `.git` suppress this warning entirely.
        gate.hook.stop_ok(UNVERIFIABLE_AT_EXIT.format(status=status, error=exc).rstrip("\n"))
    if head_tree and not gate.state.tree_approved(head_tree):
        head = gitsnap.head_commit(gate.worktree)
        gate.hook.stop_ok(UNREVIEWED_AT_EXIT.format(status=status, head=head, head_tree=head_tree).rstrip("\n"))
    gate.hook.stop_ok()


def _escalate(gate: _Gate, reason: str) -> None:
    """Escalate, or end the turn saying the activation moved and nothing was written.

    Guarded because the user can run ``/opencode-review-loop:stop`` while a review runs, and
    an escalation landing afterwards turns their ``DISARMED`` back into a state that denies
    every mutation -- the gate re-enabling itself after they left the mode (Rule 4).

    Ending the turn is the right answer when it *has* moved: if the move was the user leaving,
    blocking would refuse them their exit, and the message says plainly that nothing here is
    an approval.
    """
    if hooks.escalate(gate.state, gate.config, gate.expected, reason):
        return
    with gate.state.transaction():
        current = hooks.activation(gate.state, gate.config)
    gate.hook.stop_ok(ACTIVATION_MOVED.format(change=hooks.describe_move(gate.expected, current), now=current.summary))


def _by_status(gate: _Gate) -> None:
    """Answer from the effective status alone, where the status is enough to answer."""
    state, config, hook = gate.state, gate.config, gate.hook
    status = state.effective_status(config)

    if status in ("COMPLETE", "DISARMED"):
        _ended(gate, status)
    if status == "NEEDS_HUMAN":
        hook.stop_ok(STILL_NEEDS_HUMAN.format(reason=state.get("reason")))
    if status == "ARM_FAILED":
        _block_counted(gate, ARM_FAILED.format(reason=state.get("reason")).rstrip("\n"))
    if status == "STALE":
        _block_counted(gate, STALE.format(ttl_hours=config.as_int("ttl_hours")).rstrip("\n"))
    if status == "ARMED":
        _block_counted(gate, NOT_FROZEN.format(act_dir=state.act_dir, plugin_root=commands.plugin_root()).rstrip("\n"))
    if status == "RECONCILE":
        _block_counted(gate, RECONCILE.format(reason=state.get("reason"), parent=state.get("bad_commit_parent")).rstrip("\n"))

    # A deliberate pause to ask the user something: allowed once, and logged.
    if state.get("defer_pending") == "true":
        with state.transaction():
            state.update(defer_pending=False)
        hook.stop_ok(DEFERRED)


def _review(gate: _Gate) -> NoReturn:
    """Sweep the unreviewed work, insist on the outstanding phases, then review the whole."""
    from ocrl import gitsnap  # noqa: PLC0415 - not needed to answer from the status alone

    state, worktree = gate.state, gate.worktree
    try:
        snap = gitsnap.snapshot(worktree)
    except gitsnap.SnapshotError as exc:
        _block_counted(gate, SNAPSHOT_FAILED.format(error=exc))

    tree = snap.tree
    phase = state.get_int("phase")
    total = state.phase_count()

    # Unreviewed work sweep: anything not yet approved gets reviewed now.
    if tree != state.get("last_approved_tree") and not state.tree_approved(tree):
        _sweep(gate, snap=snap, phase=phase)

    if phase <= total and state.get("finish_requested") != "true":
        _block_counted(gate, PHASES_OUTSTANDING.format(phase=phase, total=total, description=state.phase_desc(phase)).rstrip("\n"))

    if not gitsnap.worktree_clean(worktree):
        _block_counted(gate, NOT_CLEAN.format(summary=gitsnap.dirty_summary(worktree)).rstrip("\n"))

    # This exact tree already passed a final review, so there is nothing left to say.
    if state.get("final_done_tree") == tree:
        gate.hook.stop_ok()

    _final(gate, snap=snap, total=total)


def _sweep(gate: _Gate, *, snap: Snapshot, phase: int) -> None:
    """Review whatever is in the worktree but not yet approved, before the turn may end."""
    from ocrl import report, reviewer  # noqa: PLC0415 - only a sweep needs the reviewer

    state, config = gate.state, gate.config
    target = reviewer.Target(repo=gate.worktree, base=state.get("last_approved_tree"), head=snap.tree, scope="phase", phase=phase)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        with state.transaction():
            state.mark_tree_approved(snap.tree)
        return
    if review.verdict == "CHANGES_REQUIRED":
        _block_counted(gate, report.reason(review, SWEEP_CHANGES, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(gate, review.error)
        gate.hook.stop_ok(SWEEP_ESCALATED.format(error=review.error))
    _block_counted(gate, SWEEP_FAILED.format(error=review.error))


def _final(gate: _Gate, *, snap: Snapshot, total: int) -> NoReturn:
    """The cumulative review of the whole activation, and the one transition that disarms."""
    from ocrl import report, reviewer  # noqa: PLC0415 - only the final review needs these

    state, config = gate.state, gate.config
    base = state.get("baseline_tree")
    # Captured before the review, which takes minutes, and checked again before COMPLETE is
    # written. See `ocrl.commands.completion`.
    pending = completion.start(state, config=config, repo=gate.worktree)

    target = reviewer.Target(repo=gate.worktree, base=base, head=snap.tree, scope="final", phase=total)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        try:
            pending.commit(reviewed=snap.tree, reason="final cumulative review approved")
        except commands.Refused as exc:
            # Counted rather than terminal: whatever moved the activation may itself be
            # resolvable, and a counted block still escalates once nothing is progressing.
            _block_counted(gate, str(exc).rstrip("\n"))
        gate.hook.stop_ok(COMPLETE.format(base=base, head=snap.tree, total=total, report=review.report).rstrip("\n"))
    if review.verdict == "CHANGES_REQUIRED":
        _block_counted(gate, report.reason(review, FINAL_CHANGES, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(gate, review.error)
        gate.hook.stop_ok(FINAL_ESCALATED.format(error=review.error))
    _block_counted(gate, FINAL_FAILED.format(error=review.error))
