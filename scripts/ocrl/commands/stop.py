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

import dataclasses
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from ocrl import commands, planrev
from ocrl import config as config_module
from ocrl.commands import completion, hooks
from ocrl.hookio import Hook, read_hook_input
from ocrl.state import State, pointer_read

if TYPE_CHECKING:  # pragma: no cover
    from ocrl import reviewer
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
    #: The deferred-findings paragraph an approving unreviewed-work sweep left behind
    #: (``report.deferred_text``), or "". The sweep itself emits nothing on approval -- the
    #: turn goes on to the outstanding-phase, pause or completion response -- so whichever of
    #: those speaks next carries it, as the first paragraph of the same ``reason`` /
    #: ``systemMessage``: one channel, not a new field. See :func:`_say`.
    deferred: str = ""


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
    "Run /opencode-review-loop:accept to approve the current tree without another review and continue, "
    "or /opencode-review-loop:stop to leave the mode."
)

ARM_FAILED: Final = """\
opencode-review-loop: arming failed, so nothing has been reviewed and nothing may be implemented.

Reason: {reason}

Tell the user. They can re-run /opencode-review-loop:implement <plan.md> or /opencode-review-loop:stop. Do not attempt to implement the plan.
"""

STALE: Final = (
    "opencode-review-loop: this activation is past ttl_hours ({ttl_hours}) and blocks rather than silently disarming. "
    "Tell the user to continue with /opencode-review-loop:resume, which keeps the baseline and every approval -- that is "
    "usually the right recovery. Re-arm with /opencode-review-loop:implement <plan.md> only to start over from scratch, "
    "or leave the mode with /opencode-review-loop:stop.\n"
)

NOT_FROZEN: Final = """\
opencode-review-loop: the phase list has not been frozen, so no work can start and no review has run.

Read the frozen plan ({act_dir}/{plan_file}) and run exactly:

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

ABANDONED_MARKER_UNVERIFIABLE: Final = (
    "opencode-review-loop: whether a commit resume --abandon-pending gave up on already landed could not be checked ({error}). "
    "An unreadable history is not the same as nothing having landed, so the turn cannot end on it."
)

SWEEP_CHANGES: Final = "opencode-review-loop: the turn is ending with uncommitted work that OpenCode requires changes to."

SWEEP_ACTIVATION_MOVED: Final = (
    "opencode-review-loop: the activation changed while the unreviewed-work sweep was running (a same-session resume "
    "may have changed the model, the plan or the phase list), so its approval is discarded rather than trusted. "
    "The tree stays unapproved; the next turn end will review it again, against the activation as it now stands."
)

SWEEP_SUPERSEDED: Final = (
    "opencode-review-loop: a newer review of this phase finished while the unreviewed-work sweep's approval was "
    "being written, so the approving verdict it rests on is no longer the current one. The tree stays unapproved; "
    "the next turn end will act on the newest verdict, which may require changes first."
)

SWEEP_ESCALATED: Final = (
    "opencode-review-loop: escalated to NEEDS_HUMAN — {error}. This is NOT an approval. "
    "/opencode-review-loop:accept approves the current tree without another review and continues; "
    "/opencode-review-loop:stop leaves the mode."
)

SWEEP_FAILED: Final = (
    "opencode-review-loop: the review of the uncommitted work failed ({error}), so the turn cannot end as reviewed. "
    "A failed review is never an approval."
)

PHASES_OUTSTANDING: Final = """\
opencode-review-loop: phases {phase}..{total} are still outstanding, so the activation cannot be completed.

Next up, phase {phase} of {total}:

    {description}

Implement it and commit it. If the remaining phases should be abandoned, say so and let the user run /opencode-review-loop:finish.
"""

NOT_CLEAN: Final = """\
opencode-review-loop: the worktree is not clean, so the last of the work is not in any reviewed commit. Commit it (`git add -A && git commit -m "…"`) before the activation can be completed.

{summary}
"""

PAUSED: Final = """\
opencode-review-loop: paused -- the pause target (phase {target} of {total}) has been reached.

This is NOT an approval of the whole plan, only of the phases committed so far, and the \
activation is still ARMED. Next up, phase {phase} of {total}:

    {description}

Continue with /opencode-review-loop:resume --until M, or finish the whole plan now with \
/opencode-review-loop:finish.
"""

COMPLETE: Final = """\
opencode-review-loop: COMPLETE. The final cumulative review of the whole activation (baseline {base} -> {head}) passed, across {total} phases. The mode has disarmed itself; further commits are ungated.

Full report: {report}
"""

COMPLETE_UNREVIEWED: Final = """\
opencode-review-loop: COMPLETE. Every one of the {total} phases landed through the per-commit gate, and git still vouches for the commit each one produced: {total} distinct commits, in phase order, each moving the tree. That is not the same as a model having read every line -- an already-approved or ignore_globs-matched tree passes the gate without a call. A commit made outside the gate does not become a phase; it enters RECONCILE, and end-state work the unreviewed-work sweep caught was reviewed on its own terms, not as a phase. What did not run is the final cumulative review across the whole activation (final_review is disabled). The mode has disarmed itself; further commits are ungated.

This activation is now closed, so it cannot be reviewed cumulatively after the fact -- there is no remedy for this run. Set final_review=true (`config final_review true`, or OCRL_FINAL_REVIEW=true for one run) before the next /opencode-review-loop:implement to get one.
"""

SKIP_PATH_STATE_INVALID: Final = (
    "opencode-review-loop: escalated to NEEDS_HUMAN -- the no-review completion path was reached with unexpected "
    "state (status={status!r}, phase={phase}, total={total}). State is not a trust boundary, so this refuses to "
    "complete on data that does not describe a genuinely finished activation, rather than risk disarming on it. "
    "This is NOT an approval."
)

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

    state = State(worktree, payload.session_id)
    if not state.load():
        _no_state(hook, state)
    # The configuration is loaded against the *armed worktree*, not against cwd: the Stop
    # hook fires wherever the turn happened to end, and the activation's own repo config is
    # what the gate is enforcing. The shell also resolved cwd to a repository here and then
    # never read the result; that dead resolution is a git process per turn end, and it is
    # not reproduced.
    config = config_module.load(worktree, overrides=state.data.get("overrides"))

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


class _Terminal(Exception):
    """Raised inside a ``state.transaction()`` to abandon it **without saving**.

    ``State.transaction``'s own docstring is explicit that raising out of the block leaves the
    previous document exactly as it was -- the established escape hatch ``Completion.commit``
    already uses for its own refusals. Used here for the identical reason: a locked reload that
    finds the activation already terminal (``COMPLETE``/``DISARMED``/``RESUMED``) must not
    resave it, not even with an unchanged ``self.data`` (``transaction()``'s exit calls
    ``save()`` unconditionally, regardless of whether anything called ``update()``), because
    that document may belong to a *retired* activation AGENTS.md forbids mutating again.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


def _terminal_status_or_none(state: State, config: Config) -> str:
    """A plain, unlocked read of the current status, if it is already terminal.

    Decides whether entering ``state.transaction()`` at all is worth it, for the reason
    :class:`_Terminal` documents: its exit always saves, even to just *observe* status, which
    would rewrite a retired activation's ``state.json``. Empty means "not terminal, or
    unreadable" either way; the caller's own locked reload -- which still checks fresh, and
    still raises :class:`_Terminal` rather than saving if it finds the same thing -- is the
    correctness backstop for a transition landing in the instant after this read, not this.
    """
    if not state.load():
        return ""
    status = state.effective_status(config)
    return status if status in ("COMPLETE", "DISARMED", "RESUMED") else ""


def _say(gate: _Gate, text: str) -> str:
    """``text``, prefixed by the sweep's deferred-findings paragraph when there is one."""
    return f"{gate.deferred.rstrip(chr(10))}\n\n{text}" if gate.deferred else text


def _block_counted(gate: _Gate, reason: str, *, after_completion_refusal: bool = False) -> NoReturn:
    """Block the turn, but account for whether anything moved since the last block.

    Only genuine stalls count toward ``max_stop_blocks``: the marker is the tuple of things
    that change when the loop makes progress, so a block that follows a new approved tree, a
    new phase or a status transition starts the count again.

    The count is taken **inside** the transaction, against the document it reloads, for the
    reason ``defer`` documents: two overlapping Stop hooks reading the same starting value
    would both write the same one, and the limit would never be reached.

    **A terminal activation is never counted, regardless of caller** -- ``CHANGES_REQUIRED``, a
    snapshot failure, a dirty worktree, outstanding phases, a sweep failure included. If a
    cross-session ``resume`` retired this activation (``RESUMED``), or the user left the mode
    (``DISARMED``), or it already completed (``COMPLETE``) by the time this runs, writing
    ``stop_blocks``/``stop_marker`` into that document is exactly the mutation AGENTS.md
    forbids once an activation is no longer live -- so this never calls ``update()`` on one,
    via :func:`_terminal_status_or_none` first and, if that read was stale, via
    :class:`_Terminal` aborting the locked reload below **without saving** either.

    ``after_completion_refusal`` decides only what a *concurrent* ``COMPLETE`` means here, not
    whether it is safe to write: narrowed to exactly one caller, ``_commit_or_yield_to_terminal``,
    when ``pending.commit()`` was refused because the fingerprint moved. That refusal's cause is
    ambiguous -- most causes genuinely need attention, but one is harmless: a concurrent
    ``finish``, or another Stop turn, already completed the activation while this one was still
    working, and ``commit()`` refuses on the fingerprint mismatch either way; for exactly that
    one caller, finding ``COMPLETE`` ends the turn quietly through ``_ended`` instead of
    reporting a block. Every other caller still reports its own block reason on a concurrent
    ``COMPLETE``: ``CHANGES_REQUIRED`` is this turn's own genuine finding, made about a tree a
    *different* concurrent completion does not retroactively un-review, and silently swallowing
    it would be the failure-into-approval Rule 1 forbids.

    ``DISARMED`` and ``RESUMED`` are not scoped the same way, for every caller alike: unlike a
    concurrent success, a retirement or a user-initiated stop means *this session's own
    continued involvement* is moot, not merely that one particular finding might be stale --
    the same reasoning ``_by_status`` already applies for a status known from the very start of
    the turn, just discovered here mid-turn instead. Rule 4 (a failing review must not undo a
    stop the user ran while it was running) is satisfied by routing through ``_ended`` rather
    than by ``_escalate``'s comparison against ``gate.expected``, which existed only to
    reconstruct, indirectly, the same fact a fresh reload now answers directly.
    """
    reason = _say(gate, reason)
    state = gate.state
    peeked = _terminal_status_or_none(state, gate.config)
    if peeked:
        if peeked != "COMPLETE" or after_completion_refusal:
            _ended(gate, peeked)
        gate.hook.stop_block(reason)

    blocks = 0
    try:
        with state.transaction():
            status = state.effective_status(gate.config)
            if status in ("COMPLETE", "DISARMED", "RESUMED"):
                raise _Terminal(status)
            marker = f"{state.get('last_approved_tree')}:{state.get('phase')}:{state.get('status')}"
            blocks = state.get_int("stop_blocks") + 1 if marker == state.get("stop_marker") else 1
            state.update(stop_blocks=blocks, stop_marker=marker)
    except _Terminal as exc:
        if exc.status != "COMPLETE" or after_completion_refusal:
            _ended(gate, exc.status)
        gate.hook.stop_block(reason)

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


def _named_plan_file(gate: _Gate) -> str:
    """The active plan revision's file name for a message, or escalate and end the turn.

    Mirrors ``pretool._named_plan_file``: ``planrev.active_filename`` raises when a non-empty
    ``plan_revisions`` names an unsafe or malformed file, which is not a message this can
    still print with a placeholder -- see there for why. Ends the turn either way, through
    ``_escalate``'s existing NEEDS_HUMAN path.
    """
    try:
        return planrev.active_filename(gate.state.data.get("plan_revisions") or [])
    except planrev.EvidenceCorrupted as exc:
        _escalate(gate, str(exc))
        gate.hook.stop_ok(SWEEP_ESCALATED.format(error=str(exc)))


def _escalate(gate: _Gate, reason: str) -> None:
    """Escalate, or end the turn saying the activation moved and nothing was written.

    Guarded because the user can run ``/opencode-review-loop:stop`` while a review runs, and
    an escalation landing afterwards turns their ``DISARMED`` back into a state that denies
    every mutation -- the gate re-enabling itself after they left the mode (Rule 4).

    Ending the turn is the right answer when it *has* moved: if the move was the user leaving,
    blocking would refuse them their exit, and the message says plainly that nothing here is
    an approval.

    Reading the moved-to activation uses a plain, unlocked ``load()`` rather than a
    ``state.transaction()``: this read exists only to name the new state in a message, and
    ``transaction()``'s exit always ``save()``s -- which, when what moved the activation was a
    cross-session ``resume``, would rewrite a document that is now retired and must not be
    touched again. ``load()`` reads the whole file in one go against an atomically-renamed
    writer, so the snapshot is internally consistent even without the lock; a stale-by-
    microseconds label in a message is not worth a write to somebody else's activation.
    """
    if hooks.escalate(gate.state, gate.config, gate.expected, reason):
        return
    gate.state.load()
    current = hooks.activation(gate.state, gate.config)
    gate.hook.stop_ok(ACTIVATION_MOVED.format(change=hooks.describe_move(gate.expected, current), now=current.summary))


def _by_status(gate: _Gate) -> None:
    """Answer from the effective status alone, where the status is enough to answer."""
    state, config, hook = gate.state, gate.config, gate.hook
    status = state.effective_status(config)

    if status in ("COMPLETE", "DISARMED", "RESUMED"):
        # A retirement is as terminal to this session as DISARMED: the turn may end, and an
        # unapproved HEAD is still worth telling the user about through systemMessage.
        _ended(gate, status)
    if status == "NEEDS_HUMAN":
        hook.stop_ok(STILL_NEEDS_HUMAN.format(reason=state.get("reason")))
    if status == "ARM_FAILED":
        _block_counted(gate, ARM_FAILED.format(reason=state.get("reason")).rstrip("\n"))
    if status == "STALE":
        _block_counted(gate, STALE.format(ttl_hours=config.as_int("ttl_hours")).rstrip("\n"))
    if status == "ARMED":
        plan_file = _named_plan_file(gate)
        _block_counted(gate, NOT_FROZEN.format(act_dir=state.act_dir, plugin_root=commands.plugin_root(), plan_file=plan_file).rstrip("\n"))
    if status == "RECONCILE":
        _block_counted(gate, RECONCILE.format(reason=state.get("reason"), parent=state.get("bad_commit_parent")).rstrip("\n"))

    # A deliberate pause to ask the user something: allowed once, and logged.
    if state.get("defer_pending") == "true":
        with state.transaction():
            state.update(defer_pending=False)
        hook.stop_ok(DEFERRED)


def _finish_requested_after_sweep(gate: _Gate) -> bool:
    """Read ``finish_requested`` fresh, under lock, once, after the sweep.

    The sweep may have just spent minutes in the reviewer; a ``finish`` invoked concurrently
    during that window takes the same lock to record ``finish_requested=True`` before its own
    review even starts, so this reflects it. ``_review`` shares this one read across every
    check that follows rather than each answering from its own, differently-stale snapshot --
    deciding the outstanding-phase or pause checks from a value captured before the sweep,
    while only the skip-path decision re-read afterward, let a ``finish`` that landed during
    the sweep still be blocked or paused on the plan it was explicitly asked to finish.

    A plain, unlocked peek is tried first: if the sweep's own reviewer call ran long enough for
    a cross-session ``resume`` to retire this activation, entering ``state.transaction()`` just
    to read ``finish_requested`` would still resave a document AGENTS.md forbids mutating again
    -- its exit always calls ``save()``, even when nothing inside it called ``update()``. The
    locked reload is still the correctness backstop for a transition landing in the instant
    after the peek, and it raises :class:`_Terminal` rather than saving if it finds one too.
    """
    state = gate.state
    peeked = _terminal_status_or_none(state, gate.config)
    if peeked:
        _ended(gate, peeked)
    try:
        with state.transaction():
            status = state.effective_status(gate.config)
            if status in ("COMPLETE", "DISARMED", "RESUMED"):
                raise _Terminal(status)
            return state.get("finish_requested") == "true"
    except _Terminal as exc:
        _ended(gate, exc.status)


def _review(gate: _Gate) -> NoReturn:
    """Sweep the unreviewed work, insist on the outstanding phases, then review the whole."""
    from ocrl import gitsnap  # noqa: PLC0415 - not needed to answer from the status alone

    state, worktree = gate.state, gate.worktree

    # The same check pretool runs before approving a commit, run here too: a turn must not
    # end -- and the unreviewed-work sweep must not run -- while a commit resume
    # --abandon-pending gave up on turns out to have landed after all.
    try:
        bad = hooks.resolve_abandoned_marker(state, repo=worktree)
    except gitsnap.GitUnavailable as exc:
        _block_counted(gate, ABANDONED_MARKER_UNVERIFIABLE.format(error=exc))
    if bad:
        reason = f"a commit abandoned by resume ({bad}) landed after all"
        _block_counted(gate, RECONCILE.format(reason=reason, parent=state.get("bad_commit_parent")).rstrip("\n"))

    try:
        snap = gitsnap.snapshot(worktree)
    except gitsnap.SnapshotError as exc:
        _block_counted(gate, SNAPSHOT_FAILED.format(error=exc))

    tree = snap.tree
    phase = state.get_int("phase")
    total = state.phase_count()
    target = state.get_int("stop_after_phase") or total

    # Captured now, before the sweep -- which is itself a minutes-long reviewer call -- so
    # that whatever it fingerprints is what was true when this turn started, not whatever the
    # sweep's own `state.transaction()` reload happens to leave behind. A re-arm, a resume, a
    # transition or a concurrent `finish` landing during the sweep must still be caught by
    # `pending.commit()` below, on both the reviewed and the skip-without-review path -- so
    # both share this one `Completion` rather than each starting their own late. See
    # `ocrl.commands.completion`.
    pending = completion.start(state, config=gate.config, repo=worktree)

    # Unreviewed work sweep: anything not yet approved gets reviewed now. An approving sweep
    # returns the deferred-findings paragraph (or ""), which every response below carries as
    # its first paragraph -- the sweep has no response of its own to put it in.
    if tree != state.get("last_approved_tree") and not state.tree_approved(tree):
        gate = dataclasses.replace(gate, deferred=_sweep(gate, snap=snap, phase=phase))

    finish_requested = _finish_requested_after_sweep(gate)

    if phase <= target and not finish_requested:
        _block_counted(gate, PHASES_OUTSTANDING.format(phase=phase, total=total, description=state.phase_desc(phase)).rstrip("\n"))

    if not gitsnap.worktree_clean(worktree):
        _block_counted(gate, NOT_CLEAN.format(summary=gitsnap.dirty_summary(worktree)).rstrip("\n"))

    # The target was reached but the plan is not fully implemented, and the user has not
    # asked to finish early: pause here. Status, baseline_tree and approved_trees are
    # untouched, and `_final` never runs -- a pause must never reach COMPLETE, which disarms.
    if phase <= total and not finish_requested:
        gate.hook.stop_ok(_say(gate, PAUSED.format(phase=phase, total=total, target=target, description=state.phase_desc(phase)).rstrip("\n")))

    # This exact tree already passed a final review, so there is nothing left to say.
    if state.get("final_done_tree") == tree:
        gate.hook.stop_ok(_say(gate, ""))

    if not gate.config.as_bool("final_review") and not finish_requested:
        # State is not a trust boundary: everything above (the outstanding-phase and pause
        # checks) trusts `phase`/`total` at face value, and a malformed or tampered document
        # -- an empty `phases` list, a `phase` that does not describe "every phase committed"
        # -- could otherwise slip past both and reach a completion with *no* reviewer involved
        # at all, unlike `_final`, where a real review still has to approve whatever it is
        # given. Required explicitly, right before the one call that disarms with no review:
        # the stored status is genuinely `ACTIVE`, the phase list is non-empty, and `phase` is
        # exactly one past the last phase -- the only shape "every phase was committed" can
        # take.
        if (
            state.get("status") == "ACTIVE"
            and total > 0
            and phase == total + 1
            and state.phases_match_frozen()
            and completion.phase_progress_proven(state, worktree)
        ):
            _complete_without_review(gate, pending, snap=snap, total=total)
        _escalate(
            gate,
            f"the no-review completion path was reached with unexpected state (status={state.get('status')!r}, phase={phase}, total={total})",
        )
        gate.hook.stop_ok(_say(gate, SKIP_PATH_STATE_INVALID.format(status=state.get("status"), phase=phase, total=total).rstrip("\n")))
    _final(gate, pending, snap=snap, total=total)


def _sweep(gate: _Gate, *, snap: Snapshot, phase: int) -> str:
    """Review whatever is in the worktree but not yet approved, before the turn may end.

    Returns only on approval, with the deferred-findings paragraph the approval carries
    (``report.deferred_text``, "" when nothing was deferred); every other outcome blocks or
    ends the turn from here. The caller stores it on the gate so the response that ends this
    turn shows it -- an approval that silently dropped a deferred finding would be the one
    place the late-round rule became invisible.
    """
    from ocrl import report, reviewer  # noqa: PLC0415 - only a sweep needs the reviewer

    state, config = gate.state, gate.config
    # Captured before the (possibly minutes-long) reviewer call, so a same-session `resume`
    # that changes the model, the plan or the phase list underneath it -- bumping
    # `activation_generation` -- is caught before its approval is trusted. Reuses
    # `completion.fingerprint`, the same mechanism the final-completion guard already relies on
    # for the identical class of race: an approval landing on a document it is no longer true
    # of. This is not itself a completion, so the mismatch is reported on its own terms below
    # rather than through `completion.describe_change`'s completion-specific wording.
    before = completion.fingerprint(state, config)
    target = reviewer.Target(repo=gate.worktree, base=state.get("last_approved_tree"), head=snap.tree, scope="phase", phase=phase)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        # A plain, unlocked reload first, for the same reason `_block_counted` tries one before
        # its own locked reload: entering `state.transaction()` just to observe status still
        # rewrites the document on exit, and this may be a *retired* activation (a
        # cross-session `resume` mid-sweep leaves it `RESUMED`) that must never be mutated
        # again, not even by a content-identical resave.
        peeked = _terminal_status_or_none(state, gate.config)
        if peeked:
            _ended(gate, peeked)

        moved = False
        superseded = False
        try:
            with state.transaction():
                status = state.effective_status(gate.config)
                if status in ("COMPLETE", "DISARMED", "RESUMED"):
                    raise _Terminal(status)
                now = completion.fingerprint(state, config_module.load(gate.worktree, overrides=state.data.get("overrides")))
                if now != before:
                    moved = True
                # The sweep and the commit gate genuinely overlap, and `reviewer.execute` has
                # already released its active-review claim by the time this transaction opens.
                # `completion.fingerprint` no more covers `round_history` than
                # `hooks.Activation` does, so a newer review of this same phase finishing
                # CHANGES_REQUIRED in that window moves nothing either check compares -- and
                # this approval would land on top of it. Same question, same lock, as
                # `pretool._gate_commit`'s own approval.
                elif not reviewer.approval_is_current(state, target.label, review):
                    superseded = True
                else:
                    state.mark_tree_approved(snap.tree)
        except _Terminal as exc:
            _ended(gate, exc.status)
        if moved:
            _block_counted(gate, SWEEP_ACTIVATION_MOVED)
        if superseded:
            _block_counted(gate, SWEEP_SUPERSEDED)
        return report.deferred_text(review, what="turn end")
    if review.verdict == "CHANGES_REQUIRED":
        # The sweep reviews the *phase* scope, so `clarify` has a round to target here -- unlike
        # `_final`, whose cumulative review leaves no `round_history` entry for this phase.
        headline = report.with_clarify_hint(SWEEP_CHANGES, state=state, config=config)
        _block_counted(gate, report.reason(review, headline, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(gate, review.error)
        gate.hook.stop_ok(SWEEP_ESCALATED.format(error=review.error))
    _block_counted(gate, SWEEP_FAILED.format(error=review.error))


def _commit_or_yield_to_terminal(  # noqa: PLR0913 - each arg is an independent knob of the completion, matching `Completion.commit`
    gate: _Gate,
    pending: completion.Completion,
    *,
    reviewed: str,
    reason: str,
    refuse_if_review_now_required: bool = False,
    review: reviewer.Review | None = None,
) -> None:
    """Commit the pending completion, or count a block if it was refused.

    A ``Refused`` here has two shapes, and only one of them is a problem. Most causes --
    RECONCILE, an escalation, a re-arm, a stale baseline -- mean the activation genuinely needs
    attention. But a concurrent ``finish`` (or another Stop turn) can *itself* complete the
    activation while this one is still reviewing, and there ``pending.commit`` refuses too, for
    the same "the fingerprint moved" reason, even though nothing is wrong. Telling the two
    apart is ``_block_counted``'s own job, not this function's: it re-checks status on the
    locked reload it already takes for its accounting, which is the only reload guaranteed to
    happen no earlier than the moment it decides to block -- see its docstring for why a check
    placed here instead would only move the race, not close it.
    """
    try:
        pending.commit(reviewed=reviewed, reason=reason, refuse_if_review_now_required=refuse_if_review_now_required, review=review)
    except commands.Refused as exc:
        _block_counted(gate, str(exc).rstrip("\n"), after_completion_refusal=True)


def _complete_without_review(gate: _Gate, pending: completion.Completion, *, snap: Snapshot, total: int) -> NoReturn:
    """Disarm without a cumulative review -- ``final_review`` is off and ``finish`` was not asked for.

    ``pending`` is started by the caller, before the sweep: see the comment in ``_review``.
    Completing goes through :mod:`completion`, not a direct ``status`` write, so every disarm
    site shares one guard against the worktree, the activation or the status having moved
    during the (still nonzero, lock-taking) window this fingerprint spans. It deliberately does
    not import ``reviewer``/``report`` -- there is no review here to execute or report, matching
    the lazy-import discipline the reviewed paths use.
    """
    _commit_or_yield_to_terminal(
        gate,
        pending,
        reviewed=snap.tree,
        reason="completed without a final cumulative review (final_review is disabled)",
        refuse_if_review_now_required=True,
    )
    gate.hook.stop_ok(_say(gate, COMPLETE_UNREVIEWED.format(total=total).rstrip("\n")))


def _final(gate: _Gate, pending: completion.Completion, *, snap: Snapshot, total: int) -> NoReturn:
    """The cumulative review of the whole activation, and the one transition that disarms."""
    from ocrl import report, reviewer  # noqa: PLC0415 - only the final review needs these

    state, config = gate.state, gate.config
    base = state.get("baseline_tree")

    target = reviewer.Target(repo=gate.worktree, base=base, head=snap.tree, scope="final", phase=total)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        _commit_or_yield_to_terminal(gate, pending, reviewed=snap.tree, reason="final cumulative review approved", review=review)
        gate.hook.stop_ok(_say(gate, COMPLETE.format(base=base, head=snap.tree, total=total, report=review.report).rstrip("\n")))
    if review.verdict == "CHANGES_REQUIRED":
        _block_counted(gate, report.reason(review, FINAL_CHANGES, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(gate, review.error)
        gate.hook.stop_ok(_say(gate, FINAL_ESCALATED.format(error=review.error)))
    _block_counted(gate, FINAL_FAILED.format(error=review.error))
