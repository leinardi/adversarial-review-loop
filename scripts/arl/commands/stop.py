"""``gate-stop`` -- the Stop gate, and the backstop for everything the per-commit gate can miss:
uncommitted work nobody reviewed, phases nobody implemented, a reconcile nobody finished.

**The cumulative review of the whole activation is not part of that list unconditionally.** It
runs only when ``final_review`` is on -- it is **off by default** -- or when the user asks for
it through ``finish``, which ignores that key. On a default install this gate completes through
``_complete_without_review``, and the protection standing behind that completion is the
per-commit gate plus ``confirm-commit``, not a cumulative read of the end state. Never write
"the Stop gate will catch it" without saying which configuration is meant.

Two shapes of answer. ``stop_block`` sends the turn back to Claude and is **counted**, because
a gate that blocks forever with no progress is a wedged session rather than an enforcement --
``max_stop_blocks`` consecutive no-progress blocks escalate to ``NEEDS_HUMAN``. ``stop_ok``
lets the turn end, including for every terminal state and both escalations: **letting a turn
end is not an approval**, and the messages say so wherever it could be misread.

A block is only worth sending when Claude could act on it. ``STALE`` is the one non-terminal
status where it cannot -- only the user's ``resume`` refreshes ``armed_at`` -- so it ends the
turn too.
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

import dataclasses
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

from arl import commands, planrev
from arl import config as config_module
from arl.commands import completion, hooks
from arl.errors import RepoResolutionError
from arl.hookio import Hook, read_hook_input
from arl.state import State, pointer_read
from arl.util import format_at

if TYPE_CHECKING:  # pragma: no cover
    from arl import reviewer
    from arl.config import Config
    from arl.gitsnap import Snapshot

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
    "adversarial-review-loop: the Stop payload carried no session id, so the gate cannot identify this activation. "
    "This is an integration fault, not an approval — nothing was reviewed."
)

UNSTARTED_ARM: Final = (
    "adversarial-review-loop: arming never ran, so nothing was frozen and nothing has been reviewed. "
    "/adversarial-review-loop:implement or :resume was submitted but arl.sh arm did not execute at prompt-expansion time. "
    "Tell the user what the slash command reported; they can fix the cause and re-run /adversarial-review-loop:implement <plan.md>, "
    "or leave the mode with /adversarial-review-loop:stop. Do not implement the plan."
)

UNRESOLVABLE: Final = (
    "adversarial-review-loop: the gate could not tell which repository this turn was about ({detail}), so it cannot tell "
    "whether an armed worktree guards it. Every mutation was denied on the same grounds; nothing was reviewed and this is "
    "not an approval. git must be runnable from the hook's PATH and the working directory must exist."
)

UNSCOPABLE_INTENT: Final = (
    "adversarial-review-loop: this session asked for enforcement but its request marker {detail}, so the gate cannot tell "
    "which repository it was for. Every mutation was denied and nothing was reviewed; this is not an approval. "
    "Run /adversarial-review-loop:stop to discard the request, then arm again from the intended repository."
)

UNRESOLVABLE_BLOCK: Final = (
    "adversarial-review-loop: this session asked for enforcement and the gate could not tell which repository this turn was "
    "about ({detail}), so it cannot tell whether that request was answered. Nothing was reviewed. git must be runnable from "
    "the hook's PATH and the working directory must exist; tell the user."
)

UNBOUND: Final = (
    "adversarial-review-loop: this worktree is armed (activation {session}, status {status}) but this session is not bound to it, "
    "so nothing in this turn was reviewed and every mutation was denied. Run /adversarial-review-loop:resume to continue the "
    "plan in this session, or /adversarial-review-loop:stop to leave the mode."
)

MISSING_STATE_REASON: Final = "the activation state for this session could not be read, so the gate cannot tell what has been reviewed"

MISSING_STATE: Final = """\
adversarial-review-loop: the activation state for this session could not be read, so nothing can be shown to have been reviewed and the turn is not approved.

This is an enforcement failure, not a review finding: the session pointer says this worktree was armed, but its state.json is missing or unreadable. It has escalated to NEEDS_HUMAN, so every mutation stays denied.

Tell the user. They can re-arm with /adversarial-review-loop:implement <plan.md>, or leave the mode with /adversarial-review-loop:stop.
"""

STILL_NEEDS_HUMAN: Final = (
    "adversarial-review-loop: still in NEEDS_HUMAN ({reason}). The work was not reviewed to completion. "
    "Run /adversarial-review-loop:accept to approve the current tree without another review and continue, "
    "or /adversarial-review-loop:stop to leave the mode."
)

ARM_FAILED: Final = """\
adversarial-review-loop: arming failed, so nothing has been reviewed and nothing may be implemented.

Reason: {reason}

Tell the user. They can re-run /adversarial-review-loop:implement <plan.md> or /adversarial-review-loop:stop. Do not attempt to implement the plan.
"""

#: Addressed to the **user**, not to Claude: this one goes out as a ``systemMessage`` rather
#: than as a block reason, because Claude has nothing to do about it.
STALE: Final = (
    "adversarial-review-loop: this activation is past ttl_hours ({ttl_hours}), so it is STALE and blocks rather than "
    "silently disarming: every mutation is denied, and nothing in this turn was reviewed -- this is NOT an approval. "
    "Continue with /adversarial-review-loop:resume, which refreshes the activation and keeps the baseline and every "
    "approval -- that is usually the right recovery. Re-arm with /adversarial-review-loop:implement <plan.md> only to "
    "start over from scratch, or leave the mode with /adversarial-review-loop:stop."
)

#: The same recovery, for a turn that was still ``ACTIVE`` when it started. ``reason`` carries
#: whatever this turn had already found -- often a real review's findings -- because unlike the
#: constant above, this one cannot claim nothing was reviewed.
STALE_MIDTURN: Final = """\
{reason}

adversarial-review-loop: the activation passed ttl_hours ({ttl_hours}) while this turn was running, so it is now STALE: every mutation is denied and nothing above was approved. The turn ends rather than being sent back, and it was not counted against max_stop_blocks -- only you can clear a STALE activation. Continue with /adversarial-review-loop:resume, which refreshes the activation and keeps the baseline and every approval -- that is usually the right recovery. Re-arm with /adversarial-review-loop:implement <plan.md> only to start over from scratch, or leave the mode with /adversarial-review-loop:stop.
"""

#: What an escalation refused by an expiry says before :data:`STALE_MIDTURN`. The escalation
#: genuinely did not stick, so this says so rather than implying the loop is now NEEDS_HUMAN --
#: and it keeps ``reason``, which is the only place the reviewer's own finding survives.
STALE_ESCALATION: Final = (
    "adversarial-review-loop: this turn escalated to NEEDS_HUMAN ({reason}), but the escalation was NOT recorded: the "
    "activation expired first, and an expired activation is not written to. This is NOT an approval -- the work was not "
    "reviewed to completion, and the same review runs again once you resume."
)

NOT_FROZEN: Final = """\
adversarial-review-loop: the phase list has not been frozen, so no work can start and no review has run.

Read the frozen plan ({act_dir}/{plan_file}) and run exactly:

    {plugin_root}/scripts/arl.sh set-phases --phase "…" --phase "…"
"""

RECONCILE: Final = """\
adversarial-review-loop: a commit diverged from the reviewed tree and the reconcile is unfinished.

{reason}

Recover with `{recovery}`, rebuild the phase, and commit again.
"""

ENDED_UNVERIFIABLE: Final = """\
adversarial-review-loop: the mode is {status}, and when it ended ({at}) this repository could not be read.

The check that reports work committed without passing the review gate could not see the history at that moment, so nothing here says whether it was reviewed. Look at it yourself.

Commits made after the mode ended are ungated by design and are not what this reports.
"""

ENDED_UNBORN: Final = """\
adversarial-review-loop: the mode is {status}, and when it ended ({at}) this repository had no HEAD at all, though it was armed against commit {activation_commit}.

The history this activation was gating is gone. Nothing here says what was in it.

Commits made after the mode ended are ungated by design and are not what this reports.
"""

ENDED_EVIDENCE_MALFORMED: Final = """\
adversarial-review-loop: the mode is {status}, and its record of how it ended is not one this gate could have written.

state.json was edited by something other than this gate, so what the gate could see when enforcement stopped cannot be recovered. Look at the history yourself.
"""

UNREVIEWED_AT_EXIT: Final = """\
adversarial-review-loop: the mode is {status}, and when it ended ({at}) HEAD was {head}, whose tree {head_tree} no review ever approved.

Work was committed in this worktree without passing the review gate. If you did not stop the mode yourself, it was ended from inside a Bash command — the gate cannot tell those apart, so it reports rather than acts.

This describes the state recorded at the moment enforcement stopped, and nothing after it: commits made since then are ungated by design and are not what this reports.

Review commit {head} yourself, or re-arm with /adversarial-review-loop:implement <plan.md>.
"""

ACTIVATION_MOVED: Final = (
    "adversarial-review-loop: {change} while this turn was being reviewed, so nothing was written — whatever moved it owns the "
    "activation now, and it is {now}. This is NOT an approval: the work was not reviewed to completion."
)

DEFERRED: Final = "adversarial-review-loop: turn end deferred once at your request. The gate is still armed."

STALLED: Final = """\
adversarial-review-loop: STALLED — {blocks} no-progress Stop blocks in a row (limit {limit}), so the loop escalated to NEEDS_HUMAN. This is NOT an approval and the work was NOT reviewed to completion.

Last reason: {reason}

Every mutation stays denied until you run /adversarial-review-loop:accept, which clears the escalation and approves the current tree without another review, or /adversarial-review-loop:stop, which leaves the mode.
"""

SNAPSHOT_FAILED: Final = "adversarial-review-loop: the working state could not be snapshotted ({error}), so the turn cannot be approved."

ABANDONED_MARKER_UNVERIFIABLE: Final = (
    "adversarial-review-loop: whether a commit resume --abandon-pending gave up on already landed could not be checked ({error}). "
    "An unreadable history is not the same as nothing having landed, so the turn cannot end on it."
)

SWEEP_CHANGES: Final = "adversarial-review-loop: the turn is ending with uncommitted work that the reviewer requires changes to."

SWEEP_ACTIVATION_MOVED: Final = (
    "adversarial-review-loop: the activation changed while the unreviewed-work sweep was running (a same-session resume "
    "may have changed the model, the plan or the phase list), so its approval is discarded rather than trusted. "
    "The tree stays unapproved; the next turn end will review it again, against the activation as it now stands."
)

SWEEP_SUPERSEDED: Final = (
    "adversarial-review-loop: a newer review of this phase finished while the unreviewed-work sweep's approval was "
    "being written, so the approving verdict it rests on is no longer the current one. The tree stays unapproved; "
    "the next turn end will act on the newest verdict, which may require changes first."
)

SWEEP_ESCALATED: Final = (
    "adversarial-review-loop: escalated to NEEDS_HUMAN — {error}. This is NOT an approval. "
    "/adversarial-review-loop:accept approves the current tree without another review and continues; "
    "/adversarial-review-loop:stop leaves the mode."
)

SWEEP_FAILED: Final = (
    "adversarial-review-loop: the review of the uncommitted work failed ({error}), so the turn cannot end as reviewed. "
    "A failed review is never an approval."
)

PHASES_OUTSTANDING: Final = """\
adversarial-review-loop: phases {phase}..{total} are still outstanding, so the activation cannot be completed.

Next up, phase {phase} of {total}:

    {description}

Implement it and commit it. If the remaining phases should be abandoned, say so and let the user run /adversarial-review-loop:finish.
"""

NOT_CLEAN: Final = """\
adversarial-review-loop: the worktree is not clean, so the last of the work is not in any reviewed commit. Commit it (`git add -A && git commit -m "…"`) before the activation can be completed.

{summary}
"""

#: The one ignore file the gate cannot see past and cannot review. Every tree this gate builds
#: comes out of ``git add -A``, which obeys ``info/exclude`` -- and that file lives outside the
#: worktree, so no commit carries it and no review ever sees it. One line written there makes a
#: file that is really sitting in the worktree read as a clean worktree, which defeats the dirty
#: check and this sweep at once. ``.gitignore`` needs no equivalent: it is inside the
#: repository, so a change to it is itself reviewed.
EXCLUDE_MOVED: Final = """\
adversarial-review-loop: {path} changed while this activation was live, so the worktree cannot be shown to be clean.

That file decides what `git add -A` ignores, and every tree this gate reviews is built with \
`git add -A`. It is outside the repository, so no commit carries it and no review has seen \
this change -- which means anything newly listed in it is in the worktree but invisible to the \
unreviewed-work sweep, and "clean" can no longer be proven.

Restore it to what it was when the activation was armed and end the turn again. If the change \
was deliberate and should stand, the user can end the mode with /adversarial-review-loop:stop, \
or re-arm with /adversarial-review-loop:implement <plan.md> to take the new contents as the \
baseline. This is not a finding about the code.
"""

#: The baseline is present but empty, which no ``arm`` writes -- it refuses rather than record
#: one it could not establish. So this is an edited document, and the honest claim is that the
#: comparison cannot be made, **not** that the file changed: nothing here observed a change.
EXCLUDE_TAMPERED: Final = """\
adversarial-review-loop: this activation's {path} baseline is empty, which no arming writes, so the check that file guards cannot run.

`arm` refuses rather than record a baseline it could not establish, so an empty one means \
state.json was edited or written by another tool. Nothing here observed a change to the file \
itself -- what is missing is anything to compare it against, and that file decides what \
`git add -A` ignores, so "the worktree is clean" cannot be proven without it.

Tell the user. Re-arm with /adversarial-review-loop:implement <plan.md> to take a fresh \
baseline, or leave the mode with /adversarial-review-loop:stop. This is not a finding about \
the code.
"""

#: git answered for the snapshot moments earlier and will not answer for this, so "could not
#: tell" here is an anomaly rather than an environment -- a ``rev-parse`` that timed out
#: (``git_run`` reports an expiry as status 124, a denial everywhere else) or a file that
#: cannot be read. Passing on it would let one transient failure complete an activation under
#: ignore rules nothing compared.
EXCLUDE_UNREADABLE: Final = """\
adversarial-review-loop: {path} could not be read, so the worktree cannot be shown to be clean.

That file decides what `git add -A` ignores, and every tree this gate reviews is built with \
`git add -A`, so without reading it there is no way to tell whether anything is being hidden \
from the unreviewed-work sweep. git answered for the snapshot a moment ago, so this is not a \
repository the gate cannot see -- something about this one file or this one call failed.

This is not a finding about the code, and it is not a claim that anything was hidden. Retry \
the turn; if it persists, tell the user -- they can leave the mode with \
/adversarial-review-loop:stop.
"""

PAUSED: Final = """\
adversarial-review-loop: paused -- the pause target (phase {target} of {total}) has been reached.

This is NOT an approval of the whole plan, only of the phases committed so far, and the \
activation is still ARMED. Next up, phase {phase} of {total}:

    {description}

The target stays set, and it has now been passed -- so every turn end pauses here until you \
name a new one. Continue with /adversarial-review-loop:resume --until 0 to run to the end of \
the plan, or --until M to stop again at phase M. Or finish the whole plan now with \
/adversarial-review-loop:finish.
"""

COMPLETE: Final = """\
adversarial-review-loop: COMPLETE. The final cumulative review of the whole activation (baseline {base} -> {head}) passed, across {total} phases. The mode has disarmed itself; further commits are ungated.

Full report: {report}
"""

COMPLETE_UNREVIEWED: Final = """\
adversarial-review-loop: COMPLETE. Every one of the {total} phases landed through the per-commit gate, and git still vouches for the commit each one produced: {total} distinct commits, in phase order, each moving the tree. That is not the same as a model having read every line -- an already-approved or ignore_globs-matched tree passes the gate without a call. A commit made outside the gate does not become a phase; it enters RECONCILE, and end-state work the unreviewed-work sweep caught was reviewed on its own terms, not as a phase. What did not run is the final cumulative review across the whole activation (final_review is disabled). The mode has disarmed itself; further commits are ungated.

This activation is now closed, so it cannot be reviewed cumulatively after the fact -- there is no remedy for this run. Set final_review=true (`config final_review true`, or ARL_FINAL_REVIEW=true for one run) before the next /adversarial-review-loop:implement to get one.
"""

SKIP_PATH_UNPROVEN: Final = (
    "adversarial-review-loop: escalated to NEEDS_HUMAN -- the no-review completion path was reached, but the phase "
    "chain could not be proven against git history: {detail}. State is not a trust boundary, so this refuses to "
    "complete on evidence that does not describe genuinely finished work, rather than risk disarming on it. "
    "This is NOT an approval."
)

#: Addressed to Claude *and* through it to the user, and deliberately not an escalation. The
#: activation stays ACTIVE with every phase committed; what it cannot do is prove that from the
#: document alone, because it was armed before the repository had a first commit. Both exits it
#: names really work from ACTIVE -- which is the whole point of not escalating here.
UNANCHORED_COMPLETION: Final = """\
adversarial-review-loop: all {total} phases are committed and every one of them passed the per-commit gate, but this activation cannot complete itself.

It was armed on a repository with no commits, so it has no activation commit for the phase chain to be anchored to, and the no-review completion path will not disarm on a chain it cannot check against git history. Nothing is wrong with the work or with the state; this activation simply cannot use that path.

Two ways to end it, both of which work right now:

- /adversarial-review-loop:finish — runs the cumulative review across the whole activation and completes the mode if it approves. This is the one that ends with a review.
- /adversarial-review-loop:stop — leaves the mode without that review. The per-phase reviews already happened and their commits stand.

The mode stays armed until you pick one: commits here are still gated, and nothing was approved or disarmed by this message. Tell the user; do not pick for them.
"""

SKIP_PATH_STATE_INVALID: Final = (
    "adversarial-review-loop: escalated to NEEDS_HUMAN -- the no-review completion path was reached with unexpected "
    "state (status={status!r}, phase={phase}, total={total}). State is not a trust boundary, so this refuses to "
    "complete on data that does not describe a genuinely finished activation, rather than risk disarming on it. "
    "This is NOT an approval."
)

FINAL_CHANGES: Final = "adversarial-review-loop: the final cumulative review found problems across the whole activation."

FINAL_ESCALATED: Final = "adversarial-review-loop: the final review escalated to NEEDS_HUMAN — {error}. This is NOT an approval."

FINAL_FAILED: Final = (
    "adversarial-review-loop: the final cumulative review failed ({error}). A failed review is never an approval; end your turn again to retry."
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

    # Same precedence as ``pretool``: an unanswered arming request for this worktree outranks
    # a pointer the session held from before it.
    try:
        intent = hooks.pending_intent(payload.session_id, cwd)
    except RepoResolutionError as exc:
        hook.stop_block(UNRESOLVABLE_BLOCK.format(detail=exc))
    if intent.unscopable:
        # Ends the turn rather than blocking: nothing was mutated (pretool denied everything),
        # there is deliberately no document to count a block in, and only the user's
        # /adversarial-review-loop:stop can resolve it.
        hook.stop_ok(UNSCOPABLE_INTENT.format(detail=intent.unscopable))
    if intent.pending:
        _unstarted_arm(hook, session=payload.session_id, cwd=cwd)

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
    mutation stays denied until the user runs ``/adversarial-review-loop:stop``.
    """
    state.needs_human(MISSING_STATE_REASON)
    hook.stop_block(MISSING_STATE.rstrip("\n"))


def _unstarted_arm(hook: Hook, *, session: str, cwd: str) -> NoReturn:
    """The session asked for enforcement of this worktree and its ``arm`` never ran (**Rule 0**).

    Recorded as ``ARM_FAILED`` and blocked -- counted, so the same message does not repeat
    until the host's own cap intervenes.
    """
    recorded = hooks.record_unstarted_arm(session, cwd)
    if recorded is None:
        hook.stop_ok(NO_SESSION)
    state, config = recorded
    gate = _Gate(hook=hook, state=state, config=config, worktree=state.worktree, expected=hooks.activation(state, config))
    _block_counted(gate, UNSTARTED_ARM)


def _no_pointer(hook: Hook, *, session: str, cwd: str) -> NoReturn:
    """Same reasoning as ``pretool``: no pointer means this session never bound (**Rule 0**).

    An unbound session ends its turn: it mutated nothing,
    because ``pretool`` denied every attempt, so there is no unreviewed work to hold the turn
    open for, and the one thing that can fix it, a slash command, is the user's to run, not
    Claude's. Nothing is written for those: the only document belongs to another session.
    """
    if not session:
        hook.stop_ok(NO_SESSION)
    unbound = hooks.unbound_activation(cwd)
    if unbound is None:
        hook.stop_ok()
    if not unbound.session:
        hook.stop_ok(UNRESOLVABLE.format(detail=unbound.status))
    hook.stop_ok(UNBOUND.format(session=unbound.session, status=unbound.status))


class _Terminal(Exception):
    """Raised inside a ``state.transaction()`` to abandon it **without saving**.

    ``transaction()``'s exit calls ``save()`` unconditionally, whether or not anything called
    ``update()``, so a locked reload that finds the activation already terminal
    (``COMPLETE``/``DISARMED``/``RESUMED``) must raise rather than return: that document may belong
    to a *retired* activation, which must never be mutated again.

    ``STALE`` is raised through here too, on a different ground -- nothing forbids writing to a
    stale document, but the counters must not be written into one (see
    :func:`_uncountable_status_or_none`). The caller sorts the two apart by ``status``.
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


def _uncountable_status_or_none(state: State, config: Config) -> str:
    """A plain, unlocked read of the current status, if it is one no block may be counted in.

    :func:`_terminal_status_or_none`'s three, **plus ``STALE``**. Stale is not terminal but is
    equally not Claude's to fix, and the TTL is a wall clock: ``_by_status`` reads the status once
    at the top of the hook, so a turn that began ``ACTIVE`` can arrive here stale after a review
    that took minutes. Counting that block escalated to ``NEEDS_HUMAN`` -- which only ``accept``
    clears -- and left a ``stop_marker`` the next genuine block resumed counting from.

    Kept separate rather than widening :func:`_terminal_status_or_none`, whose callers route into
    :func:`_ended`, the wrong answer for a status that has disarmed nothing.
    """
    if not state.load():
        return ""
    status = state.effective_status(config)
    return status if status in ("COMPLETE", "DISARMED", "RESUMED", "STALE") else ""


def _stale_end(gate: _Gate, reason: str) -> NoReturn:
    """End the turn on an activation that expired *during* it, carrying ``reason`` with it.

    A separate message from ``_by_status``'s, which can honestly say nothing was reviewed. By
    the time this runs a review may well have run and found something, and ``reason`` is what
    it found -- reported rather than dropped, since ending the turn is not an approval and
    those findings are the user's to read before they decide to resume.
    """
    gate.hook.stop_ok(STALE_MIDTURN.format(reason=reason, ttl_hours=gate.config.as_int("ttl_hours")).rstrip("\n"))


def _say(gate: _Gate, text: str) -> str:
    """``text``, prefixed by the sweep's deferred-findings paragraph when there is one."""
    return f"{gate.deferred.rstrip(chr(10))}\n\n{text}" if gate.deferred else text


def _block_counted(gate: _Gate, reason: str, *, after_completion_refusal: bool = False) -> NoReturn:
    """Block the turn, but account for whether anything moved since the last block.

    Only genuine stalls count toward ``max_stop_blocks``: the marker is the tuple of things that
    change when the loop makes progress. The count is taken **inside** the transaction, against
    the document it reloads, or two overlapping Stop hooks both write the same value and the limit
    is never reached.

    **A terminal activation is never counted, whatever the caller.** Writing
    ``stop_blocks``/``stop_marker`` into a ``RESUMED``, ``DISARMED`` or ``COMPLETE`` document is
    the mutation forbidden once an activation is no longer live, so this never calls ``update()``
    on one -- via :func:`_terminal_status_or_none` first and, if that read was stale, via
    :class:`_Terminal` aborting the locked reload without saving.

    ``after_completion_refusal`` decides only what a *concurrent* ``COMPLETE`` means, not whether
    writing is safe, and is narrowed to one caller: ``_commit_or_yield_to_terminal``, when
    ``pending.commit()`` was refused on a moved fingerprint. There, a concurrent ``finish`` or
    another Stop turn completing the activation ends the turn quietly. Every other caller still
    reports its own block reason -- a ``CHANGES_REQUIRED`` is this turn's genuine finding about a
    tree another completion does not retroactively un-review, and swallowing it is the
    failure-into-approval Rule 1 forbids.

    ``DISARMED`` and ``RESUMED`` are not scoped that way for any caller: a retirement or a
    user-initiated stop makes this session's continued involvement moot, not merely one finding
    stale. Rule 4 is satisfied by routing through ``_ended``.
    """
    reason = _say(gate, reason)
    state = gate.state
    peeked = _uncountable_status_or_none(state, gate.config)
    if peeked == "STALE":
        _stale_end(gate, reason)
    if peeked:
        if peeked != "COMPLETE" or after_completion_refusal:
            _ended(gate, peeked)
        gate.hook.stop_block(reason)

    blocks = 0
    try:
        with state.transaction():
            status = state.effective_status(gate.config)
            if status in ("COMPLETE", "DISARMED", "RESUMED", "STALE"):
                raise _Terminal(status)
            marker = f"{state.get('last_approved_tree')}:{state.get('phase')}:{state.get('status')}"
            blocks = state.get_int("stop_blocks") + 1 if marker == state.get("stop_marker") else 1
            state.update(stop_blocks=blocks, stop_marker=marker)
    except _Terminal as exc:
        if exc.status == "STALE":
            _stale_end(gate, reason)
        if exc.status != "COMPLETE" or after_completion_refusal:
            _ended(gate, exc.status)
        gate.hook.stop_block(reason)

    limit = gate.config.as_int("max_stop_blocks")
    if blocks > limit:
        _escalate(gate, f"the Stop gate blocked {blocks} times with no progress in between: {reason}")
        gate.hook.stop_ok(STALLED.format(blocks=blocks, limit=limit, reason=reason).rstrip("\n"))
    gate.hook.stop_block(reason)


def _ended(gate: _Gate, status: str) -> NoReturn:
    """The mode is off. Let the turn end -- but not silently if work went unreviewed *then*.

    ``systemMessage`` rather than a block, and that choice is the point: it reaches the **user**
    instead of the model, so relaying it is not the model's decision.

    This is the only place a Rule 4 escape becomes visible. A Bash command that commits and then
    runs ``arl.sh deactivate`` leaves exactly this shape, and the gate cannot tell it from a user
    who stopped the mode with work outstanding -- so it reports rather than acts, because
    reverting would take an exit away from the user.

    **What changed is the question, not the choice.** Asking "is current HEAD approved?" is not a
    question about this gate: an ordinary commit made hours after a terminal transition was
    indistinguishable from the escape and fired the same alarm on every turn end, forever. It now
    reads the record ``hooks.end_state`` validates.

    **Detection is exactly "the recorded tree is absent from ``approved_trees``"** -- not proof of
    review, not proof of commit identity. An empty commit, a rewrite onto a tree already in the
    set, or an escape ordered ``deactivate && commit`` is silent. See
    ``docs/design/end-state-record.md``.

    Makes **no git call on any path**, so an ended session stops paying one ``git rev-parse`` per
    turn end.
    """
    end = hooks.end_state(gate.state)
    if end.malformed:
        gate.hook.stop_ok(ENDED_EVIDENCE_MALFORMED.format(status=status).rstrip("\n"))
    if not end.recorded:
        # A document written before the end-state record existed. There is no evidence to
        # report from, and current HEAD answers a different question -- so this says nothing.
        # `/adversarial-review-loop:status` offers that other comparison explicitly, on
        # request, where it cannot become noise.
        gate.hook.stop_ok()
    at = format_at(end.at)
    if end.capture == "unreadable":
        # Recorded, so it no longer decays if git becomes readable again -- and breaking
        # `.git` after the stop can no longer suppress a report already on disk.
        gate.hook.stop_ok(ENDED_UNVERIFIABLE.format(status=status, at=at).rstrip("\n"))
    if end.capture == "unborn":
        activation_commit = gate.state.get("activation_commit")
        if activation_commit:
            # An unborn HEAD is also the armed state of a repository with no commits, so only
            # a non-empty anchor makes this "the history was destroyed" rather than "nothing
            # was ever committed here".
            gate.hook.stop_ok(ENDED_UNBORN.format(status=status, at=at, activation_commit=activation_commit).rstrip("\n"))
        gate.hook.stop_ok()
    if not gate.state.tree_approved(end.tree):
        gate.hook.stop_ok(UNREVIEWED_AT_EXIT.format(status=status, at=at, head=end.head, head_tree=end.tree).rstrip("\n"))
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

    Guarded because the user can run ``/adversarial-review-loop:stop`` while a review runs, and an
    escalation landing afterwards turns their ``DISARMED`` back into a state that denies every
    mutation -- the gate re-enabling itself after they left (Rule 4). When it has moved, ending
    the turn is right: blocking would refuse them their exit, and the message says plainly that
    nothing here is an approval.

    Reading the moved-to activation uses a plain ``load()``, not a transaction: the read exists
    only to name the new state in a message, and ``transaction()``'s exit always saves -- which,
    when a cross-session ``resume`` moved it, rewrites a retired document. ``load()`` reads the
    file in one go against an atomically-renamed writer, so the snapshot is consistent without the
    lock.

    **An expiry is reported as an expiry.** A TTL crossed during a long review refuses the
    escalation exactly as a genuine move does, but ``ACTIVATION_MOVED`` would blame "whatever
    moved it", drop ``reason`` -- the reviewer's own finding, carried nowhere else in this
    response -- and name no way out. Narrowed to the case where the TTL is the only difference.
    """
    if hooks.escalate(gate.state, gate.config, gate.expected, reason):
        return
    gate.state.load()
    current = hooks.activation(gate.state, gate.config)
    if current.effective_status == "STALE" and dataclasses.replace(current, effective_status=current.status) == gate.expected:
        _stale_end(gate, STALE_ESCALATION.format(reason=reason))
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
        # Ends the turn rather than blocking, for the reasons an unscopable intent already
        # does in `_gate_stop`: every mutation was denied by `pretool._gate_terminal_status`,
        # so nothing went unreviewed, and **nothing Claude can do resolves it** -- only the
        # user's `resume` refreshes `armed_at`. `session.reorient` already classifies STALE
        # with NEEDS_HUMAN on exactly that ground ("needs the user, not another attempt").
        #
        # Counting it was worse than useless: the marker below is built from the *stored*
        # status, which a TTL expiry never changes, so every turn end counted and the
        # activation escalated to NEEDS_HUMAN within one user turn -- an escalation `resume`
        # refuses by design, so reaching it took away the very recovery this message names.
        hook.stop_ok(STALE.format(ttl_hours=config.as_int("ttl_hours")))
    if status == "ARMED":
        plan_file = _named_plan_file(gate)
        _block_counted(gate, NOT_FROZEN.format(act_dir=state.act_dir, plugin_root=commands.plugin_root(), plan_file=plan_file).rstrip("\n"))
    if status == "RECONCILE":
        _block_counted(gate, RECONCILE.format(reason=state.get("reason"), recovery=hooks.reconcile_recovery(state)).rstrip("\n"))

    # A deliberate pause to ask the user something: allowed once, and logged.
    if state.get("defer_pending") == "true":
        with state.transaction():
            state.update(defer_pending=False)
        hook.stop_ok(DEFERRED)


def _finish_requested_after_sweep(gate: _Gate) -> bool:
    """Read ``finish_requested`` fresh, under lock, once, after the sweep.

    The sweep may have just spent minutes in the reviewer, and a concurrent ``finish`` records
    ``finish_requested=True`` under the same lock before its own review starts. ``_review`` shares
    this one read across every check that follows: deciding the outstanding-phase or pause checks
    from a value captured *before* the sweep let a ``finish`` that landed during it still be
    blocked on the plan it was asked to finish.

    A plain, unlocked peek runs first, because entering a transaction just to read this would
    resave a document a concurrent retirement may have made retired. The locked reload is the
    backstop for a transition landing in the instant after the peek, and raises :class:`_Terminal`
    rather than saving if it finds one.
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
    from arl import gitsnap  # noqa: PLC0415 - not needed to answer from the status alone

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
        _block_counted(gate, RECONCILE.format(reason=reason, recovery=hooks.reconcile_recovery(state)).rstrip("\n"))

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
    # `arl.commands.completion`.
    pending = completion.start(state, config=gate.config, repo=worktree)

    # Before the sweep, because the sweep is what this protects: every tree below it comes out
    # of `git add -A`, which obeys `.git/info/exclude`, so a changed exclude file means the
    # snapshot may be hiding work and "clean" can no longer be proven. Rule 0's direction --
    # a gate that cannot prove it is running denies -- applies to a gate that cannot prove
    # what it is looking at.
    _guard_exclude(gate, worktree)

    # Unreviewed work sweep: anything not yet approved gets reviewed now. An approving sweep
    # returns the deferred-findings paragraph (or ""), which every response below carries as
    # its first paragraph -- the sweep has no response of its own to put it in.
    if tree != state.get("last_approved_tree") and not state.tree_approved(tree):
        gate = dataclasses.replace(gate, deferred=_sweep(gate, snap=snap, phase=phase))

    # Again, because the sweep above is a reviewer call and the worktree kept moving through
    # it. An exclude edit landing mid-sweep hides whatever was created beside it, and the
    # cleanliness check a few lines down would then read the worktree as clean.
    _guard_exclude(gate, worktree)

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
        if state.get("status") == "ACTIVE" and total > 0 and phase == total + 1 and state.phases_match_frozen():
            gap = completion.phase_progress_gap(state, worktree)
            if not gap.code:
                _complete_without_review(gate, pending, snap=snap, total=total)
            if gap.code == completion.UNANCHORED:
                # **Refuse to complete, but do not escalate.** What this document describes was
                # genuinely done -- every phase committed, every commit gate-verified -- and the
                # one thing missing is an anchor `arm` itself left empty because the repository
                # had no commits yet. Escalating on that wedges the activation rather than ending
                # it: `NEEDS_HUMAN` is neither finishable nor resumable, and `accept` refuses too
                # because `phase` is past the last phase, so all three remedies the escalation
                # names are themselves refused and only `stop` is left. Measured on a real
                # 12-phase activation. Staying `ACTIVE` keeps `finish` reachable -- a cumulative
                # review is exactly where the evidence this cannot have does come from -- and
                # costs nothing: the mode stays armed, nothing disarms, no tree is approved, and
                # the turn ends rather than blocking, so no no-progress counter runs either.
                gate.hook.stop_ok(_say(gate, UNANCHORED_COMPLETION.format(total=total).rstrip("\n")))
            _escalate(gate, f"the no-review completion path could not be proven: {gap.message}")
            gate.hook.stop_ok(_say(gate, SKIP_PATH_UNPROVEN.format(detail=gap.message).rstrip("\n")))
        _escalate(
            gate,
            f"the no-review completion path was reached with unexpected state (status={state.get('status')!r}, phase={phase}, total={total})",
        )
        gate.hook.stop_ok(_say(gate, SKIP_PATH_STATE_INVALID.format(status=state.get("status"), phase=phase, total=total).rstrip("\n")))
    _final(gate, pending, snap=snap, total=total)


#: Distinguishes "the document has no ``exclude_digest``" from "it has an empty one". Mirrors
#: ``hooks._ABSENT``, for the identical reason: ``.get(key, "")`` collapses the two, and the
#: two mean opposite things -- legacy silence versus a baseline that should exist and does not.
_ABSENT: Final = object()


def _guard_exclude(gate: _Gate, worktree: str) -> None:
    """Block the turn end when ``info/exclude`` has moved since arming, else return.

    **Called before the sweep and again after every reviewer call**, because a review is a
    minutes-long window in which the worktree keeps moving: an exclude file edited during the
    sweep hides a file created alongside it, and the cleanliness check that follows -- built on
    ``git add -A``, which obeys the new rules -- then reports the worktree clean and lets the
    activation complete. The check is one ``rev-parse`` and one file read, and what it guards is
    the gate's central claim.

    Kept off ``_review``'s body so the decision and its reason live together, and so the lazy
    ``gitsnap`` import stays out of a path that may never need git.
    """
    verdict = _exclude_verdict(gate.state, worktree)
    if verdict == _EXCLUDE_OK:
        return
    from arl import gitsnap  # noqa: PLC0415 - module-scope git is off this module's hot path

    path = gitsnap.exclude_path(worktree) or ".git/info/exclude"
    template = {
        _EXCLUDE_CHANGED: EXCLUDE_MOVED,
        _EXCLUDE_TAMPERED: EXCLUDE_TAMPERED,
        _EXCLUDE_UNREADABLE: EXCLUDE_UNREADABLE,
    }[verdict]
    _block_counted(gate, template.format(path=path).rstrip("\n"))


#: :func:`_exclude_verdict`'s answers. Deliberately three failures rather than one boolean:
#: "it changed", "the baseline is not one an arm wrote" and "the current state could not be
#: read" are three different claims, and only the first is evidence about the worktree.
_EXCLUDE_OK: Final = ""
_EXCLUDE_CHANGED: Final = "changed"
_EXCLUDE_TAMPERED: Final = "tampered"
_EXCLUDE_UNREADABLE: Final = "unreadable"


def _exclude_verdict(state: State, worktree: str) -> str:
    """Has ``info/exclude`` changed since this activation was armed, and can that be told?

    **An absent field and a present-empty one are not the same thing, and conflating them turns
    this check off.** Absent means a document written before the field existed; calling that a
    change would fire on every activation already on disk, and it is the only case here that
    passes silently. Present-and-empty cannot be produced by any current ``arm``, which refuses
    rather than store a baseline it could not establish, so it means an edited document.

    **An unreadable current reading blocks too**, and calling it "unchanged" was a real hole:
    ``gitsnap.exclude_digest`` answers ``""`` for a ``rev-parse`` that failed or timed out --
    ``git_run`` reports an expiry as status 124, a denial everywhere else -- so passing lets one
    transient failure complete an activation under ignore rules nothing compared. The sandbox
    argument that once justified passing does not survive the order things run in: ``_review``
    blocks on ``SnapshotError`` before this is reached.

    Directional in neither sense: a file appearing and one being emptied are both changes. What
    matters is that the set of paths ``git add -A`` skips is no longer the set the baseline was
    taken under. See ``docs/design/resume-and-retirement.md``.
    """
    baseline = state.data.get("exclude_digest", _ABSENT)
    if baseline is _ABSENT:
        return _EXCLUDE_OK
    if not isinstance(baseline, str) or not baseline:
        return _EXCLUDE_TAMPERED
    from arl import gitsnap  # noqa: PLC0415 - module-scope git is off this module's hot path

    current = gitsnap.exclude_digest(worktree)
    if not current:
        return _EXCLUDE_UNREADABLE
    return _EXCLUDE_OK if current == baseline else _EXCLUDE_CHANGED


def _sweep(gate: _Gate, *, snap: Snapshot, phase: int) -> str:
    """Review whatever is in the worktree but not yet approved, before the turn may end.

    Returns only on approval, with the deferred-findings paragraph the approval carries
    (``report.deferred_text``, "" when nothing was deferred); every other outcome blocks or
    ends the turn from here. The caller stores it on the gate so the response that ends this
    turn shows it -- an approval that silently dropped a deferred finding would be the one
    place the late-round rule became invisible.
    """
    from arl import report, reviewer  # noqa: PLC0415 - only a sweep needs the reviewer

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
    from arl import report, reviewer  # noqa: PLC0415 - only the final review needs these

    state, config = gate.state, gate.config
    base = state.get("baseline_tree")

    target = reviewer.Target(repo=gate.worktree, base=base, head=snap.tree, scope="final", phase=total)
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict == "APPROVED":
        # The final review is the longest window of all, and what follows it is the one
        # transition that disarms. An approval decided under one set of ignore rules must not
        # complete an activation under another.
        _guard_exclude(gate, gate.worktree)
        _commit_or_yield_to_terminal(gate, pending, reviewed=snap.tree, reason="final cumulative review approved", review=review)
        gate.hook.stop_ok(_say(gate, COMPLETE.format(base=base, head=snap.tree, total=total, report=review.report).rstrip("\n")))
    if review.verdict == "CHANGES_REQUIRED":
        _block_counted(gate, report.reason(review, FINAL_CHANGES, config=config).rstrip("\n"))
    if review.verdict == "NEEDS_HUMAN":
        _escalate(gate, review.error)
        gate.hook.stop_ok(_say(gate, FINAL_ESCALATED.format(error=review.error)))
    _block_counted(gate, FINAL_FAILED.format(error=review.error))
