"""The five commands a user runs against a live activation, and one hook that answers a
compaction.

Ports ``cmd_defer``, ``cmd_status``, ``cmd_report``, ``cmd_finish`` and ``cmd_deactivate``.

``finish`` and ``deactivate`` are two of the three exits the user owns (Rule 4). Nothing
here is reachable by Claude: the skills carry ``disable-model-invocation: true``, and
``pretool`` denies the Bash route to both. What that means for this module is that its
output is written for a human -- it is the last thing said before the mode ends.

:func:`reorient` is the exception on both counts: it is a ``SessionStart`` hook (compaction and
resume) rather than a user command, and its reader is Claude rather than a human. It lives here
because it is a *report* on the activation, assembled from the same fields :func:`status`
prints -- and because, like everything else in this module, it decides nothing.
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

import math
import os
import sys
from typing import Any, Final, NamedTuple

from arl import commands, gitsnap, guide, harness, hookio, oscillation, paths, planrev, report, reviewer
from arl.commands import completion, hooks
from arl.commands.completion import Completion
from arl.config import Config
from arl.errors import UnsafePathError
from arl.gitsnap import SnapshotError
from arl.state import ENDED_EVIDENCE_STATUSES, State, pointer_read
from arl.util import format_at, log, now

__all__ = ["deactivate", "defer", "finish", "reorient", "report_cmd", "status"]

#: The working rules a compacted session has to get back, in the order they bite. Deliberately
#: the *rules*, not the plan: the plan is on disk at a path this names, and re-injecting a 64
#: KiB document into a context that was just compacted for being too large would undo the
#: compaction. Kept close to ``commands.posttool``'s ``NEXT_PHASE`` banner in spirit -- both
#: tell Claude how to proceed -- but not shared with it: that one is read mid-flow by a session
#: that still remembers everything, this one by a session that remembers nothing, and merging
#: them would make each carry the other's assumptions.
REORIENT_RULES: Final = """\
- Commit with `git add -A && git commit -m "..."` only. No --amend, no pathspecs, no `--only`
  or `--include`, no command substitution. Builds, tests and formatters go in their own Bash
  calls, never chained into the commit.
- A multi-paragraph message is repeated `-m`, one per paragraph (`-m "subject" -m "body"`);
  git joins them with a blank line. A real newline inside one `-m`, and `-F`/`--file`, are
  both refused.
- Every commit is intercepted and reviewed. A denial lists the blocking findings: fix all of
  them and commit again. A failed or malformed review is never an approval.
- Each phase ends with one commit and a clean worktree.
- If a finding is ambiguous or contradicts an earlier round, ask before guessing:
  `{root}/scripts/arl.sh clarify --question "..."`.
- To stop mid-phase and ask the user something, run `{root}/scripts/arl.sh defer --reason "..."`
  first, then end your turn.
- You cannot end the mode; `/adversarial-review-loop:finish` and `/adversarial-review-loop:stop` are
  the user's.
"""

NOT_ARMED: str = "adversarial-review-loop: not armed in this worktree.\n"


# --------------------------------------------------------------------------
# defer
# --------------------------------------------------------------------------


def defer(argv: list[str]) -> int:
    """Record a deliberate pause, so one turn may end without the Stop gate blocking it.

    Bounded by ``max_defers`` and counted, because "let me ask the user something" is also
    the shape of an agent that has stopped making progress.
    """
    reason = ""
    index = 0
    while index < len(argv):
        if argv[index] == "--reason":
            reason = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
            continue
        index += 1

    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stderr.write("adversarial-review-loop: nothing armed in this worktree.\n")
        return 1

    state = activation.state
    limit = activation.config.as_int("max_defers")
    # Counted *inside* the transaction, against the document it reloads. Counting on the copy
    # read before the lock lets concurrent defers each see the same starting count, all pass
    # the limit check, and all write the same value -- so the allowance is spent once and
    # granted many times, which is the limit failing open.
    try:
        with state.transaction():
            used = state.get_int("defers") + 1
            if used > limit:
                raise commands.Refused(
                    f"adversarial-review-loop: {used - 1} defers already used (limit {limit}). "
                    "The turn cannot be deferred again; finish the phase or ask the user to run /adversarial-review-loop:stop.\n"
                )
            state.update(defers=used, defer_pending=True, reason=f"deferred: {reason}")
    except commands.Refused as exc:
        sys.stderr.write(str(exc))
        return 1

    sys.stdout.write(f"adversarial-review-loop: turn end deferred ({used} of {limit}). Reason recorded: {reason}\n")
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def _spent(history: list[dict[str, Any]]) -> tuple[float, int]:
    """``(dollars, rounds)`` over the entries that recorded a readable cost.

    ``round_history`` comes out of ``state.json``, which is not a trust boundary, so every
    hop is type-checked: a non-object ``usage``, a non-numeric ``cost_usd``, a ``bool``
    (an ``int`` subclass in Python, so ``True`` would otherwise total as one dollar) and a
    non-finite float are all skipped rather than coerced. ``status`` changes nothing and
    escalates nothing -- the honest answer to an unreadable entry is to leave it out of the
    total and out of the count, which is why the count is returned alongside: it says how many
    rounds the figure actually covers.
    """
    total = 0.0
    counted = 0
    for entry in history:
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        cost = usage.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
            continue
        total += float(cost)
        counted += 1
    return total, counted


def _cost_line(history: list[dict[str, Any]], phase_history: list[dict[str, Any]]) -> str:
    """``reviewer cost: …``, or "" when no round recorded one.

    Empty rather than ``$0.00`` for an OpenCode activation or one armed before this was
    recorded: a zero would claim the reviews were free, when what is true is that their cost
    was never reported.
    """
    total, rounds = _spent(history)
    if not rounds:
        return ""
    phase_total, phase_rounds = _spent(phase_history)
    return f"reviewer cost:       ${total:.2f} over {rounds} round(s), ${phase_total:.2f} this phase ({phase_rounds})\n"


def _revision_list(state: State, key: str) -> list[dict[str, Any]] | None:
    """A revisions field as a list of objects, or ``None`` when it is not one.

    ``state.json`` is not a trust boundary (AGENTS.md), and every revisions field is read here
    only to *describe* the activation. The distinction ``None`` draws is between "nothing
    recorded", which is ordinary, and "recorded as something that is not a list of objects",
    which is corruption worth naming -- so this is not ``get_array_of_dicts``, which answers
    ``[]`` to both and would report a mangled field as an activation with no revisions.

    Nothing downstream may index or count the raw value before this runs: an object or a number
    is truthy, and ``status`` is the one command a human runs *because* something is wrong. It
    must not be the thing that crashes.
    """
    value = state.data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        return None
    return value


#: Distinguishes "no ``exclude_digest`` in the document" from "an empty one". Mirrors
#: ``hooks._ABSENT`` and ``stop._ABSENT``: ``.get(key, "")`` collapses two states that mean
#: opposite things -- a document that predates the field, and one whose baseline was removed.
_ABSENT: Final = object()


def _exclude_display(state: State, repo: str) -> str:
    """The ``info/exclude`` line: unchanged, changed, or a stated reason for neither.

    ``status`` is the compensating control for a check that otherwise only speaks at turn end,
    so it says which of the three it is rather than printing a digest nobody can compare by
    eye. It changes nothing and escalates nothing -- an activation armed before the field
    existed says so, instead of being reported as though the file had moved.
    """
    baseline = state.data.get("exclude_digest", _ABSENT)
    if baseline is _ABSENT:
        return "not recorded (this activation predates the check)"
    if not isinstance(baseline, str) or not baseline:
        # No current `arm` can store this -- it refuses rather than record a baseline it could
        # not establish -- so an empty one is an edited document, and saying "predates the
        # check" here would launder a tampered field into a reassuring sentence.
        return "recorded as empty, which no arm writes -- state.json was edited; the check cannot run"
    current = gitsnap.exclude_digest(repo)
    if not current:
        return f"recorded, but unreadable now -- compare {gitsnap.exclude_path(repo)} yourself"
    if current == baseline:
        where = "no such file, then or now" if current == gitsnap.EXCLUDE_ABSENT else "unchanged since arming"
        return where
    return f"CHANGED since arming -- {gitsnap.exclude_path(repo)} now hides paths no review has seen"


def _ended_display(state: State, effective: str) -> str:
    """How this activation ended, for :func:`status`. Reads only; makes no git call.

    **This is the compensating control for the legacy rule.** Both reporting channels stay
    silent for an activation that ended before the end-state record existed, because there is
    no evidence to report from and current HEAD answers a different question. That is the right
    default for a message nobody asked for and which would otherwise repeat on every turn end
    forever -- but it leaves a user with no way to ask. Here they can: user-invoked, writes
    nothing, and structurally incapable of becoming noise.

    The comparison is worded as **gate-approved / not in approved_trees**, never as
    "reviewed". Membership in ``approved_trees`` is not evidence a model read anything: the
    baseline tree is in it, and so is any tree the gate passed without a reviewer call --
    already approved, or ``ignore_globs``-matched. The same caveat the detection rule carries.
    """
    end = hooks.end_state(state)
    if end.malformed:
        return "record present but not one this gate could have written -- state.json was edited; look at the history yourself"
    if not end.recorded:
        # Offered explicitly as the different question it is, rather than answered here: the
        # gate cannot scope it to the mode's lifetime, so an answer would be misleading in
        # exactly the way the silent channels are avoiding.
        return (
            f"not recorded (this activation predates the check). What HEAD is *now* is a different question -- "
            f"compare `git rev-parse HEAD^{{tree}}` against approved_trees in {state.state_file} yourself."
        )
    at = format_at(end.at)
    if end.capture == "unreadable":
        return f"{at} -- the repository could not be read when enforcement stopped, so the gate never saw the history it was gating"
    if end.capture == "unborn":
        activation_commit = state.get("activation_commit")
        if activation_commit:
            return f"{at} -- HEAD did not exist when enforcement stopped, though this activation was armed at {activation_commit}"
        return f"{at} -- HEAD did not exist when enforcement stopped, and this activation was armed on an empty repository"
    verdict = "gate-approved" if state.tree_approved(end.tree) else "NOT in approved_trees"
    return f"{at} at {end.head}, tree {end.tree} ({verdict}); status {effective}"


def status(argv: list[str]) -> int:
    """Print everything the gate is currently deciding on. Never changes anything."""
    del argv
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write(NOT_ARMED)
        return 0

    state, config = activation.state, activation.config
    effective = state.effective_status(config)
    stored = state.get("status")
    status_line = effective if effective == stored else f"{effective} (stored: {stored})"

    # Both blocks are rendered without a trailing newline, because the shell interpolated
    # them with `$( )` -- which strips one -- and the surrounding template supplies it. An
    # empty list therefore leaves a blank line exactly where the shell left one.
    phases = "".join(f"  {index + 1}. {phase}\n" for index, phase in enumerate(state.get_array("phases"))).rstrip("\n")
    reports = "".join(f"  {name}\n" for name in report.list_reports(activation.act_dir)).rstrip("\n")
    pause_target = state.pause_target_display()
    manual_accepts = state.get_array_of_dicts("manual_accepts")
    accepted_phases = ", ".join(str(entry.get("phase")) for entry in manual_accepts)
    accepts_line = f"{len(manual_accepts)} (phases {accepted_phases})" if manual_accepts else "0"
    # `status` never changes anything (see the docstring above), so a corrupted revision
    # entry is reported inline rather than escalated the way `pretool`/`gate-stop` do --
    # there is no mutation here to gate, only a diagnostic to print honestly.
    #
    # The *shape* is checked before anything indexes or counts it. `state.json` is not a trust
    # boundary, and a revisions field holding an object or a number is truthy: it would reach
    # `active_filename`'s `existing[-1]` and `len()` and raise straight out of `status`, so the
    # one command a human runs to find out what went wrong would itself be what fails.
    plan_revisions = _revision_list(state, "plan_revisions")
    if plan_revisions is None:
        active_plan_file = "<corrupted: plan_revisions is not a list of objects>"
        revision_count = 1
    else:
        try:
            active_plan_file = planrev.active_filename(plan_revisions)
        except planrev.EvidenceCorrupted as exc:
            active_plan_file = f"<corrupted: {exc}>"
        revision_count = len(plan_revisions) or 1

    # The repo-supplied reviewer guide, beside the frozen plan for the same reason it is in the
    # arming banner: it is the one repository input that becomes instruction to the reviewer, so
    # "what was this review run under" has to be answerable without opening state.json. An empty
    # `guide_revisions` is "no guide" -- never a backfilled revision 0 (`arl.guide`).
    guide_revisions = _revision_list(state, "guide_revisions")
    if guide_revisions is None:
        guide_line = "<corrupted: guide_revisions is not a list of objects>"
    elif not guide_revisions:
        guide_line = "none"
    else:
        recorded = str(guide_revisions[-1].get("sha256") or "")
        # `guide_path` is repository-controlled (`review_guide`), and this is printed to a
        # terminal -- see `guide.display_path`.
        source = guide.display_path(state.get("guide_path")) if state.get("guide_path") else "<unrecorded>"
        guide_line = f"{source} (sha256 {recorded[:12] or '<unrecorded>'}, revision {len(guide_revisions) - 1}, {len(guide_revisions)} recorded)"

    # Phase 5: how many rounds this phase's own label has run at the current generation, and
    # whether any of its anchors have stopped moving. Mirrors exactly the scope
    # `reviewer._stall_review` reads -- this label, this generation -- so what a human sees
    # here is the same evidence the next commit attempt would escalate on.
    phase_label = f"phase{state.get_int('phase')}"
    generation = state.get_int("activation_generation")
    phase_history = [
        entry for entry in state.get_array_of_dicts("round_history") if entry.get("label") == phase_label and entry.get("generation") == generation
    ]
    stall_rounds = config.as_int("stall_rounds")
    persisting_points = (
        oscillation.persisting(phase_history, phase_label, stall_rounds, block_severity=config.as_str("block_severity")) if stall_rounds > 0 else []
    )
    persisting_line = f"persisting findings:  {', '.join(point.anchor.file for point in persisting_points)}\n" if persisting_points else ""

    # What this activation has spent, for a harness that reports it. Display only, and totalled
    # here rather than stored as a running counter: a counter would have to be kept correct
    # across every abort, reclaim and generation bump, which is real machinery for a number
    # nothing depends on. Summing the rounds is exact whenever the rounds are, and simply omits
    # what it cannot read.
    cost_line = _cost_line(state.get_array_of_dicts("round_history"), phase_history)

    # Phase 6: the transient-failure budget and any active retry backoff -- distinct from
    # `operational failures` above, which is the ordinary operational/contract/bundle budget.
    retry_not_before = state.get_int("retry_not_before")
    remaining = retry_not_before - now()
    backoff_line = f"retry backoff:       {remaining}s remaining\n" if remaining > 0 else ""

    ended_line = f"ended:               {_ended_display(state, effective)}\n" if effective in ENDED_EVIDENCE_STATUSES else ""

    sys.stdout.write(
        f"""\
adversarial-review-loop status
---------------------------
worktree:            {state.get("worktree")}
session:             {state.get("session_id")}
status:              {status_line}
reason:              {state.get("reason")}
plan:                {state.get("plan_path")}
frozen plan:         {activation.act_dir}/{active_plan_file}
plan revision:       {revision_count - 1} ({revision_count} recorded)
review guide:        {guide_line}
baseline tree:       {state.get("baseline_tree")}
info/exclude:        {_exclude_display(state, activation.repo)}
activation commit:   {state.get("activation_commit")}
last approved tree:  {state.get("last_approved_tree")}
pending approval:    {state.get("pending_approved_tree")}
phase:               {state.get("phase")} of {state.phase_count()}
pause target:        {pause_target}
operational failures:{state.get("failures")} / {config.as_int("max_failures")}
transient failures:  {state.get("transient_failures")} / {config.as_int("max_transient_failures")}
{backoff_line}no-progress blocks:  {state.get("stop_blocks")} / {config.as_int("max_stop_blocks")}
defers used:         {state.get("defers")} / {config.as_int("max_defers")}
manual accepts:      {accepts_line}
harness:             {config.as_str("harness")}
model:               {harness.display_model(config)} {config.as_str("variant")}
block_severity:      {config.as_str("block_severity")}
rounds this phase:   {len(phase_history)}
reviewer session:    {reviewer.continuity_summary(state, config)}
{cost_line}{persisting_line}{ended_line}state directory:     {activation.act_dir}

phases:
{phases}

reports:
{reports}
"""
    )
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def report_cmd(argv: list[str]) -> int:
    """Print one stored report in full: the ``n``-th, or the newest when ``n`` is omitted."""
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write(NOT_ARMED)
        return 0

    raw = argv[0] if argv else ""
    which: int | None = None
    if raw:
        try:
            which = int(raw)
        except ValueError:
            # `printf '%03d' foo` complained and used 0; there is no report 000, so this
            # lands on "No such report" with the list of the ones that do exist.
            log(f"not a report number: {raw!r}")
            which = 0

    sys.stdout.write(report.render(activation.act_dir, which))
    return 0


# --------------------------------------------------------------------------
# finish
# --------------------------------------------------------------------------


#: The states a final review may be started from. An allow-list, because the fingerprint
#: below only catches states that *change* during the review: an activation that is already
#: STALE, already escalated or already stopped comes through unchanged and would complete.
#: STALE is the sharpest of those -- its baseline can no longer be trusted, which is why
#: every other gate blocks on it instead of silently disarming.
_FINISHABLE: tuple[str, ...] = ("ARMED", "ACTIVE", "RECONCILE")


def _refuse_unless_finishable(state: State, config: Config) -> None:
    """Refuse to run the review at all unless the activation is one that may complete.

    Called with the lock held, before ``finish_requested`` is recorded and before the
    reviewer runs -- a model call for an activation that cannot be completed is wasted, and
    completing one anyway is the failure this guards.

    ``RECONCILE`` is deliberately finishable: the cumulative review covers the end state
    regardless of what happened per commit, which is the defence AGENTS.md relies on. What is
    not permitted is *entering* reconcile mid-review, and the fingerprint catches that.
    """
    status = state.effective_status(config)
    if status in _FINISHABLE:
        return
    if status == "STALE":
        raise commands.Refused(
            f"adversarial-review-loop: this activation is past ttl_hours ({config.as_int('ttl_hours')}), so its baseline can no longer "
            "be trusted and it blocks rather than silently disarming. The final review did not run. Continue with "
            "/adversarial-review-loop:resume, which refreshes the activation and keeps the baseline and every approval, then "
            "finish again; re-arm with /adversarial-review-loop:implement <plan.md> only to start over from scratch, or leave "
            "the mode with /adversarial-review-loop:stop.\n"
        )
    if status == "COMPLETE":
        raise commands.Refused(
            f"adversarial-review-loop: this activation is already COMPLETE ({state.get('reason')}). "
            "The mode has already disarmed itself, so there is nothing left to finish.\n"
        )
    raise commands.Refused(
        f"adversarial-review-loop: cannot finish while the activation is {status} ({state.get('reason')}). "
        "The final review did not run. Re-arm with /adversarial-review-loop:implement <plan.md>, "
        "or leave the mode with /adversarial-review-loop:stop.\n"
    )


def _prepare(state: State, *, config: Config, repo: str) -> tuple[gitsnap.Snapshot, Completion]:
    """Everything that must hold before a model is called. Raises ``commands.Refused``.

    Returns the snapshot the review will be run against and the pending completion the
    approval will be checked against.
    """
    with state.transaction():
        # Both under the same lock, and in this order: an activation that may not finish must
        # not even have `finish_requested` recorded, since that is what stops the Stop gate
        # insisting on the outstanding phases.
        _refuse_unless_finishable(state, config)
        state.update(finish_requested=True)
        pending = completion.start(state, config=config, repo=repo)

    if not gitsnap.worktree_clean(repo):
        raise commands.Refused(
            "adversarial-review-loop: the worktree is not clean. Commit the outstanding work first — "
            "the final review runs over committed history plus the working state, and every phase "
            f"must land in a reviewed commit.\n\n{gitsnap.dirty_summary(repo)}\n"
        )

    try:
        snap = gitsnap.snapshot(repo)
    except SnapshotError as exc:
        # No tree means nothing to review against, and "nothing to review" is never an
        # approval (Rule 1).
        raise commands.Refused(
            f"adversarial-review-loop: the working state could not be snapshotted ({exc}), so the final review did not run.\n"
        ) from exc
    return snap, pending


def finish(argv: list[str]) -> int:
    """Run the final cumulative review now, at the user's request.

    ``finish_requested`` is recorded **before** the review runs, so that a review the user
    interrupts still lets the Stop gate stop insisting on the remaining phases. What it does
    not do is approve anything: only an approving final review sets ``COMPLETE``.
    """
    del argv
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write(NOT_ARMED)
        return 0

    state, config, repo = activation.state, activation.config, activation.repo
    try:
        snap, pending = _prepare(state, config=config, repo=repo)
    except commands.Refused as exc:
        sys.stdout.write(str(exc))
        return 1

    base = state.get("baseline_tree")
    sys.stdout.write(f"adversarial-review-loop: running the final cumulative review ({base} -> {snap.tree}). This can take a few minutes.\n\n")

    target = reviewer.Target(repo=repo, base=base, head=snap.tree, scope="final", phase=state.phase_count())
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict != "APPROVED":
        text = report.reason(review, "adversarial-review-loop: the final cumulative review did not pass. The mode stays armed.", config=config)
        # One trailing newline, as `printf '%s\n' "$( … )"` produced.
        sys.stdout.write(text.rstrip("\n") + "\n")
        return 1

    try:
        pending.commit(reviewed=snap.tree, reason="final cumulative review approved (user-invoked finish)", review=review)
    except commands.Refused as exc:
        sys.stdout.write(str(exc))
        return 1

    sys.stdout.write(
        "adversarial-review-loop: COMPLETE. The final review passed. The mode has disarmed itself; "
        f"further commits are ungated.\n\nFull report: {review.report}\n"
    )
    return 0


# --------------------------------------------------------------------------
# deactivate
# --------------------------------------------------------------------------


def _session_arg(argv: list[str]) -> str:
    """The ``--session <id>`` the ``stop`` skill passes, or ``""``."""
    for index, arg in enumerate(argv):
        if arg == "--session" and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def _discard_intent(session: str) -> str:
    """Discard the session's intent marker, if any. Returns a line for the user, or ``""``.

    This is the **only** path that removes a marker the gate could not scope, and it is a
    user action -- ``/adversarial-review-loop:stop`` -- which is exactly the recovery the gate
    names when it denies on one. A marker the pointer already acknowledges is cleaned up here
    too; that one was inert anyway.
    """
    if not session or not paths.is_safe_component(session):
        return ""
    marker = paths.intent_path(session)
    try:
        marker.unlink()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return (
            f"adversarial-review-loop: an enforcement request for this session is recorded at {marker} and could not be "
            f"removed ({exc}); every mutation stays denied until it is.\n"
        )
    return "adversarial-review-loop: discarded this session's pending enforcement request (an arm that never completed).\n"


#: The statuses at which this activation has stopped enforcing and must not be rewritten.
#: ``RESUMED`` is **not** here: it is terminal for this document but it still denies every
#: mutation, so it gets its own message rather than this one's claim. See
#: :data:`_ALREADY_ENDED_STATUSES` for the full guard set.
_ENFORCEMENT_OVER_STATUSES: Final = frozenset({"COMPLETE", "DISARMED"})

#: Every status ``deactivate`` refuses to rewrite. A retired (``RESUMED``) activation is the
#: one document AGENTS.md forbids mutating at all.
_ALREADY_ENDED_STATUSES: Final = _ENFORCEMENT_OVER_STATUSES | {"RESUMED"}

ALREADY_ENDED = """\
adversarial-review-loop: this worktree's activation already ended ({status}), so nothing was changed.

Commits and file changes are not gated. The record of how the mode ended -- which the
reporting channels read instead of current HEAD -- is kept exactly as it was written.

State and reports are at:
  {act_dir}

Re-arm at any time with /adversarial-review-loop:implement <plan.md>.
"""

#: ``RESUMED``'s own message, because the one above would be a false claim here. A retired
#: activation is terminal for its *document* but not for the worktree: ``pretool`` denies every
#: mutation under ``RESUMED`` (see ``pretool.RESUMED``), so telling the user commits are ungated
#: would leave them believing a wedged worktree is free.
#:
#: **Reaching this branch proves the resume never repointed ``latest``.**
#: ``deactivate`` resolves through ``commands.resolve_local_activation()``, which reads that
#: pointer, so a resume that finished would have resolved the *successor* and never landed
#: here at all. The single message this replaced offered "run /adversarial-review-loop:stop
#: again now that this worktree's pointer names it" as the remedy for a completed resume --
#: advice unreachable by construction, which left the user re-running ``/stop`` against the
#: identical text forever. What is actually known is narrower and more useful: retirement
#: committed to ``resumed_into`` under the predecessor's own lock, and publication stopped
#: somewhere after that. Which side of the successor's own document it stopped on is the only
#: open question, and it is answerable by reading it -- so ``deactivate`` reads it, finishes
#: the pointer the resume did not, and stops the successor if one is live. None of that
#: touches the retired document, which AGENTS.md forbids mutating at all.
#: The caveat both published-pointer messages owe the user, because ``latest`` is not what the
#: hooks read for a *bound* session: ``pretool``, ``gate-stop`` and ``confirm-commit`` resolve
#: the calling session's own document first, so every session bound to a retired activation
#: keeps denying however the pointer is repaired. **Every** retired activation in the chain,
#: not just the one this command resolved -- an A-into-B-into-C history leaves sessions bound to
#: A *and* to B denying, and naming only A would describe B's session as free.
_BOUND_SESSION_CAVEAT = """\
One exception is worth knowing about. A Claude session still *bound* to any retired activation
in this worktree's chain ({retired}) keeps being denied, and its turn-end and post-commit
reports keep reading that retired document rather than {successor}'s -- the gate resolves a
bound session's own state before it ever looks at the worktree pointer. Every other session,
including any new one, passes. If the session you are in is a denied one, re-arm with
/adversarial-review-loop:implement <plan.md> or start a fresh session."""

_RETIRED_STOPPED = """\
adversarial-review-loop: STOPPED for this worktree -- through {successor}, which a resume had already handed it to.

The activation this session resolved ({resolved}) was retired by that resume and must never be
rewritten, so it was left exactly as it was. The resume died before it could repoint this
worktree at {successor}; that pointer is now published, and {successor} itself is DISARMED.

Commits and file changes are no longer gated. {caveat}

State and reports are at:
  {act_dir}
"""

#: The successor was published and had already ended on its own. Nothing to stop -- but the
#: pointer still has to be finished, or every later command resolves the retired predecessor
#: again and the worktree stays wedged under a mode that is over.
_RETIRED_SUCCESSOR_ENDED = """\
adversarial-review-loop: this worktree's activation was retired by a resume ({successor} took over), and {successor} was no longer live ({status}).

Neither document was changed. The resume died before it could repoint this worktree at
{successor}; that pointer is now published, so commits and file changes are no longer gated.
{caveat}

State and reports are at:
  {act_dir}
"""

#: ``latest`` stopped naming the activation this repair was resolved from, so the repair is
#: stale. Publishing anyway is a fail-open: ``arm``, ``resume`` and ``hooks`` all publish that
#: pointer and **nothing serialises them**, so the write this command was about to make could
#: bury an activation another command has just armed -- leaving ``latest`` on a stopped session
#: while a live one gates nothing that resolves through it. Refusing costs one re-run.
_RETIRED_POINTER_MOVED = """\
adversarial-review-loop: this worktree's activation pointer moved while /stop was working, so nothing was changed.

This command resolved the retired activation {resolved} and was finishing the pointer a dead
resume left behind. Something else published a different activation in the meantime -- an
/adversarial-review-loop:implement, or another resume -- and overwriting that could hide a live
activation behind a stopped one.

Run /adversarial-review-loop:stop again to act on whatever is armed now.

State and reports are at:
  {act_dir}
"""

#: The successor was never published. Both sides deny, exactly as AGENTS.md's fail-closed
#: retirement order intends, and there is nothing ``/stop`` can free: rewriting the retired
#: document is forbidden, and there is no successor to stop or to point at. ``implement`` is
#: the documented recovery, and saying so plainly -- including that re-running ``/stop`` will
#: not help -- is the whole of what this case can honestly offer.
_RETIRED_UNPUBLISHED = """\
adversarial-review-loop: this worktree's activation was retired by a resume that never finished, so nothing was changed.

The resume wrote this activation off in favour of {successor}, then died before publishing
it -- {successor} has no state at all. Both sides deny by design: a retired activation cannot
be stopped and must not be rewritten, and a session with no document can prove nothing about
this armed worktree and therefore denies too (Rule 0). **/stop cannot free this worktree**,
and running it again prints this same message.

Re-arm from scratch with /adversarial-review-loop:implement <plan.md>. Nothing is lost --
the retired activation's reports stay where they are.

State and reports are at:
  {act_dir}
"""

#: The retired document names a successor whose own ``resumed_from`` does not name it back.
#: Retirement writes that pair together, so no retirement this gate performed produced this --
#: which makes it the one case where acting on ``resumed_into`` would be acting on an edit.
#: Refusing costs the user a re-arm; obeying would disarm whatever the field happens to name.
_RETIRED_UNLINKED = """\
adversarial-review-loop: this worktree's activation names {successor} as its successor, but {successor} does not name it back.

Nothing was changed. A retirement writes both halves of that link at once, so a chain that is
only half there was not written by this gate -- state.json has been edited, or corrupted.
Acting on it would mean stopping whatever session that field happens to name, so /stop refuses
rather than guess.

Re-arm from scratch with /adversarial-review-loop:implement <plan.md>.

State and reports are at:
  {act_dir}
"""

#: The walk kept finding a successor that had been retired again by the time the lock was
#: taken. Reporting beats publishing into a chain that is still moving: whichever session this
#: command picked would be stale before the pointer landed.
_RETIRED_CONTENDED = """\
adversarial-review-loop: this worktree is being resumed right now, so /stop had nothing stable to act on.

Nothing was changed. Each time this command read the successor it had already been retired
into another one. Wait for the resume in flight to finish, then run
/adversarial-review-loop:stop again.

State and reports are at:
  {act_dir}
"""

#: Hard cap on the ``resumed_into`` walk. A chain longer than this is a corrupted document,
#: not a history: ``resumed_into`` is written once per retirement, so even a pathological run
#: of back-to-back resumes falls orders of magnitude short. The cap and the visited set are
#: both needed -- the set alone still walks an arbitrarily long chain, and the cap alone spins
#: on a document whose ``resumed_into`` names itself.
_RESUME_CHAIN_LIMIT: Final = 16


class _AlreadyEnded(Exception):
    """Raised inside ``deactivate``'s transaction to abandon it **without saving**.

    Mirrors ``stop._Terminal``, for the same reason: ``transaction()``'s exit calls ``save()``
    unconditionally, so a plain ``return`` would resave the document -- including a retired one
    AGENTS.md forbids mutating, and including stamping *today's* HEAD in as the end-of-mode
    evidence of a mode that ended long ago.
    """

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class _PointerMoved(Exception):
    """``latest`` stopped naming the activation this repair was resolved from.

    Abandons the transaction without saving, so a refused repair changes nothing at all --
    neither the successor's status nor the pointer.
    """


class _Contended(Exception):
    """The successor was retired between the unlocked walk and the locked write.

    Abandons the transaction without saving, exactly as :class:`_AlreadyEnded` does, and asks
    the caller to walk again -- the chain has grown a link since it was read.
    """


class _Successor(NamedTuple):
    """What the ``resumed_into`` walk found, and why it stopped."""

    session: str
    state: State | None
    #: Every retired activation walked through, starting with the one this command resolved.
    #: The messages name all of them: a session bound to *any* of them keeps denying, so
    #: naming only the first would describe the others' sessions as free.
    retired: tuple[str, ...] = ()
    #: Set when a document was found but its ``resumed_from`` does not name the session that
    #: pointed at it. Distinct from "no document": one is an unfinished resume, the other is a
    #: chain no retirement in this gate ever wrote, and they get different answers.
    unlinked: bool = False


def _successor_of(repo: str, retired: State, retired_session: str) -> _Successor:
    """Walk ``resumed_into`` to the activation a retirement handed this worktree to.

    Chained retirements are real -- A retired into B, B into C -- and a crash anywhere in the
    chain leaves ``latest`` on A. Walking to the end finds the one activation that is not
    itself retired, which is the only one worth stopping or pointing at.

    **Every link is checked in both directions before it is followed.** ``state.json`` is not a
    trust boundary, so an edited or corrupt ``resumed_into`` can name *any* session in this
    worktree -- and what this walk authorises is a status write and a ``latest`` publication
    against whatever it names. Retirement writes the pair together (``resumed_into`` on the
    predecessor, ``resumed_from`` on the successor), so a successor whose ``resumed_from`` does
    not name the session that pointed at it was never handed this worktree by that retirement,
    and is refused rather than stopped. Without that check, editing one field of a retired
    document would let ``/stop`` disarm an unrelated live activation and publish it as
    ``latest``, which is a wider escape than the state-edit bypass it would ride in on.

    Exhausting :data:`_RESUME_CHAIN_LIMIT` answers like an unpublished successor: both mean "no
    successor this command may act on", and that case's reply -- re-arm, ``/stop`` cannot help
    -- is the correct one for a corrupted chain too.
    """
    seen: set[str] = set()
    chain: tuple[str, ...] = (retired_session,)
    previous = retired_session
    session = str(retired.get("resumed_into") or "")
    for _ in range(_RESUME_CHAIN_LIMIT):
        if not session or session in seen:
            return _Successor(session, None, chain)
        seen.add(session)
        try:
            successor = State(repo, session)
        except UnsafePathError:
            # A hand-edited `resumed_into` that cannot name a directory names no activation.
            return _Successor(session, None, chain)
        if not successor.load():
            return _Successor(session, None, chain)
        if str(successor.get("resumed_from") or "") != previous:
            return _Successor(session, None, chain, unlinked=True)
        if successor.get("status") != "RESUMED":
            return _Successor(session, successor, chain)
        chain += (session,)
        previous = session
        session = str(successor.get("resumed_into") or "")
    return _Successor(session, None, chain)


def _finish_the_retirement(activation: commands.Activation) -> str:
    """Honour ``/stop`` for a worktree whose retirement never finished publishing.

    The retired document is never touched -- AGENTS.md forbids mutating one at all. What this
    finishes is the *pointer*: ``resumed_into`` was committed under the predecessor's own lock,
    so republishing ``latest`` as that successor is not inventing a transition, it is
    converging on the value retirement already decided and the dead resume was on its way to
    writing. Without it, ``/stop`` leaves ``latest`` naming a ``RESUMED`` document, every later
    command resolves that same retired activation, and the worktree stays wedged under a mode
    the user has asked twice to end.

    The walk runs unlocked, so it is re-run whenever the locked write finds the world has
    moved. Bounded by the same limit the walk itself uses: a chain that keeps growing under
    this command is a resume storm, not a state to converge on, and saying so beats publishing
    into it.
    """
    for _ in range(_RESUME_CHAIN_LIMIT):
        found = _successor_of(activation.repo, activation.state, activation.session)
        if found.unlinked:
            return _RETIRED_UNLINKED.format(successor=found.session, act_dir=activation.act_dir)
        if found.state is None:
            return _RETIRED_UNPUBLISHED.format(successor=found.session or "an unnamed successor", act_dir=activation.act_dir)
        try:
            return _stop_the_successor(activation, found)
        except _Contended:
            continue
    return _RETIRED_CONTENDED.format(act_dir=activation.act_dir)


def _stop_the_successor(activation: commands.Activation, found: _Successor) -> str:
    """Stop one successor and publish the pointer, both under that successor's own lock.

    **The pointer write belongs inside the transaction.** A retirement of this successor takes
    exactly this lock (``resume``'s ``_retire`` runs inside the predecessor's own
    ``transaction()``), so holding it across the publication is what stops a concurrent resume
    from retiring this document and repointing ``latest`` at *its* successor, only for this
    call to overwrite that pointer with a session that is now ``RESUMED`` -- wedging the
    worktree while reporting it freed.

    **That lock is not enough on its own, and nothing available here would be.** ``latest`` has
    five publishers -- ``arm`` twice, ``resume``, ``hooks`` and this -- and none of them
    serialise against each other, so the successor's lock orders this against a resume *of that
    successor* and against nothing else. A concurrent ``implement`` can arm and publish a wholly
    unrelated activation while this call is inside the lock, and overwriting that pointer would
    leave ``latest`` on a stopped session while a live one gates every unbound session that
    resolves through it: a fail-open. So the publication is conditional on ``latest`` still
    naming the activation this repair was resolved from, which is the premise that made the
    repair valid at all. That is a compare-and-swap without an atomic swap -- a publisher
    landing between the read and the write still wins -- and closing it properly means putting
    every ``latest`` write behind one worktree-scoped lock, which is a change to ``arm`` and
    ``resume``, not to this. The residual window is the same one those publishers already race
    among themselves; what this must not do is widen it by ignoring the pointer entirely.
    """
    successor = found.state
    assert successor is not None
    session = found.session
    retired = ", ".join(found.retired)
    try:
        with successor.transaction():
            # Re-read under the lock: the walk loaded this unlocked, and a concurrent resume or
            # completion may have moved it since.
            stored = successor.get("status")
            if stored == "RESUMED":
                raise _Contended
            if stored in _ENFORCEMENT_OVER_STATUSES:
                if commands.latest_session(activation.repo) != activation.session:
                    raise _PointerMoved
                commands.write_latest(activation.repo, session)
                raise _AlreadyEnded(stored)
            successor.update(status="DISARMED", reason="stopped by the user", **hooks.ended_evidence(activation.repo))
            if commands.latest_session(activation.repo) != activation.session:
                # Before the pointer write *and* before the transaction saves, so a moved
                # pointer leaves this successor exactly as it was: refusing changes nothing.
                raise _PointerMoved
            commands.write_latest(activation.repo, session)
    except _AlreadyEnded as ended:
        # The pointer was published above, before abandoning the write: a successor that ended
        # on its own is exactly the case where leaving `latest` on the retired predecessor
        # wedges a finished worktree.
        caveat = _BOUND_SESSION_CAVEAT.format(retired=retired, successor=session)
        return _RETIRED_SUCCESSOR_ENDED.format(successor=session, status=ended.status, caveat=caveat, act_dir=successor.act_dir)
    except _PointerMoved:
        return _RETIRED_POINTER_MOVED.format(resolved=activation.session, act_dir=activation.act_dir)
    caveat = _BOUND_SESSION_CAVEAT.format(retired=retired, successor=session)
    return _RETIRED_STOPPED.format(successor=session, resolved=activation.session, caveat=caveat, act_dir=successor.act_dir)


def deactivate(argv: list[str]) -> int:
    """Leave the mode. Nothing is reverted and nothing is deleted.

    The session pointer deliberately stays (Rule 0): the hooks are still registered for this
    session, and a missing pointer is what "arming never executed" looks like, so removing it
    would turn every later tool call into a denial. ``DISARMED`` is what makes the gates pass
    through.

    **A second run over an already-terminal document is a no-op.**
    ``commands.resolve_local_activation()`` filters on loadability, never on status, so this
    used to rewrite a ``COMPLETE`` document with ``DISARMED`` unconditionally, discarding which
    of the two actually ended the mode. Once the end-state record exists that is worse than
    untidy: re-running ``/adversarial-review-loop:stop`` -- the obvious remedy for a report the
    user disagrees with -- would stamp today's unapproved HEAD in as the evidence and make the
    alarm permanent and "evidenced". First terminal transition wins, always.

    **``RESUMED`` is not a no-op at all**, because it is terminal for the document and *not*
    for the worktree: ``pretool`` denies every mutation under it, so telling the user commits
    are ungated would describe a wedged worktree as a free one. A cross-session resume retires
    the predecessor before publishing the successor or repointing ``latest``, and resolution
    reads that pointer -- so arriving here at all is proof the resume died mid-publication.
    ``_finish_the_retirement`` reads the successor the retirement already named, stops it if it
    is live, and publishes the pointer the dead resume did not, which is the only way ``/stop``
    can honour its own name here. The retired document itself is never touched.
    """
    discarded = _discard_intent(_session_arg(argv))
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write(discarded + "adversarial-review-loop: not armed in this worktree, so there is nothing to stop.\n")
        return 0
    sys.stdout.write(discarded)

    state = activation.state
    try:
        with state.transaction():
            # The *stored* status, not the effective one: a TTL-expired `ACTIVE` activation is
            # still live enforcement the user is entitled to stop.
            stored = state.get("status")
            if stored in _ALREADY_ENDED_STATUSES:
                raise _AlreadyEnded(stored)
            # `ended_evidence` is folded into the same write as the status, so the record and
            # the transition land together or not at all. This is the only git call on this
            # path, and it can never fail the transition -- it records failure as an outcome.
            state.update(status="DISARMED", reason="stopped by the user", **hooks.ended_evidence(activation.repo))
    except _AlreadyEnded as ended:
        if ended.status in _ENFORCEMENT_OVER_STATUSES:
            sys.stdout.write(ALREADY_ENDED.format(status=ended.status, act_dir=activation.act_dir))
        else:
            sys.stdout.write(_finish_the_retirement(activation))
        return 0

    sys.stdout.write(
        f"""\
adversarial-review-loop: STOPPED for this worktree.

Commits and file changes are no longer gated. Nothing was reverted — the repository
is exactly as you left it, at phase {state.get("phase")} of {state.phase_count()}.

State and reports are kept at:
  {activation.act_dir}

Re-arm at any time with /adversarial-review-loop:implement <plan.md>.
"""
    )
    return 0


# --------------------------------------------------------------------------
# reorient
# --------------------------------------------------------------------------


def _reorient_text(activation: commands.Activation) -> str:
    """What a just-compacted session needs to know to carry on, or "" when it needs nothing.

    **Every value here is gate-derived** -- ``state.json`` fields, the frozen phase
    description, and a *count* of the last review's blocking findings. No reviewer prose and
    no repository content: this text is injected straight into Claude's context, and the one
    thing that must not happen is model-authored text from a review re-entering the session as
    though the gate had said it. The report is named by path instead, for Claude to open if it
    needs the findings themselves.
    """
    state, config = activation.state, activation.config
    status = state.effective_status(config)
    if status in ("COMPLETE", "DISARMED", "RESUMED"):
        return ""

    phase = state.get_int("phase")
    total = state.phase_count()
    root = commands.plugin_root()
    head = [
        f"adversarial-review-loop: this session was compacted or resumed while the review loop is {status} in {state.get('worktree')}.",
        "Re-orienting you, because the loop is still enforced and the current context may no longer carry it.",
        "",
    ]

    if status in ("NEEDS_HUMAN", "STALE"):
        head += [
            f"The activation is {status} and needs the user, not another attempt. Stop, tell the user, and let them run",
            "/adversarial-review-loop:status and decide. Do not keep implementing.",
        ]
        return "\n".join(head) + "\n"

    description = state.phase_desc(phase)
    head += [
        f"Phase {phase} of {total} is in progress:",
        "",
        f"    {description}" if description else "    (no description recorded)",
        "",
        f"The frozen plan is at {activation.act_dir}/{_active_plan_file(state)} -- re-read the part phase {phase} implements",
        "before you continue. It is the plan the reviewer judges against, not the original file.",
        "",
    ]

    last = _last_round_line(state, config, phase)
    if last:
        head += [last, ""]

    head += ["Rules still in force:", REORIENT_RULES.format(root=root).rstrip("\n"), ""]
    head.append(f"Continue with phase {phase}.")
    return "\n".join(head) + "\n"


def _active_plan_file(state: State) -> str:
    """The frozen plan's filename, or a plain fallback when the evidence will not verify.

    ``reorient`` never escalates and never raises (see :func:`reorient`), so a corrupted
    revision list degrades to naming the original frozen copy -- which is where a reader
    should look anyway -- rather than turning a re-orientation into a crash.
    """
    try:
        return planrev.active_filename(state.data.get("plan_revisions") or [])
    except planrev.EvidenceCorrupted:
        return "plan.frozen.md"


def _last_round_line(state: State, config: Config, phase: int) -> str:
    """One line about the newest recorded round of the current phase, or "".

    The blocking findings are *counted*, never quoted: they are reviewer prose. The stored
    report is named so Claude can read them itself.
    """
    label = f"phase{phase}"
    generation = state.get_int("activation_generation")
    rounds = [entry for entry in state.get_array_of_dicts("round_history") if entry.get("label") == label and entry.get("generation") == generation]
    if not rounds:
        return f"No review of phase {phase} has run yet in this activation."
    entry = rounds[-1]
    seq = entry.get("seq")
    verdict = entry.get("verdict")
    findings = entry.get("findings")
    count = len(findings) if isinstance(findings, list) else 0
    del config
    if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(verdict, str):
        return f"The last recorded round of phase {phase} cannot be read back; run /adversarial-review-loop:status."
    return (
        f"The last review of phase {phase} was report {seq:03d}: {verdict}, {count} finding(s) recorded. "
        f"Read them with /adversarial-review-loop:report {seq}."
    )


def reorient(argv: list[str]) -> int:
    """``SessionStart(compact|resume)``: re-inject the loop's own state after a compaction or a resume.

    **Plain text on stdout, never JSON.** ``SessionStart`` is one of the few events whose
    plain stdout Claude Code adds to the context, which is the entire mechanism here; a JSON
    object would be parsed as a decision document instead and the text would never be seen.

    **Silent on anything unexpected, and never a failure.** Not our session, not the armed
    worktree, no activation, a terminal one, an unreadable document -- each prints nothing and
    exits 0. This hook grants nothing, blocks nothing and decides nothing: it is the one
    entrypoint in the plugin with no fail-closed direction, because the failure it could cause
    is noise in a session that is otherwise fine, and the failure it prevents is only that
    Claude has to re-read the plan itself.

    The plugin cannot trigger a compaction or a ``/clear`` -- no hook or plugin API exposes
    that -- so this reacts to one rather than avoiding it. ``docs/how-it-works.md`` carries the
    manual pattern for very long plans.
    """
    del argv
    try:
        payload = hookio.read_hook_input()
        worktree = pointer_read(payload.session_id)
        if not worktree:
            return 0
        cwd = payload.cwd or os.getcwd()
        if hooks.resolve_repo(cwd, worktree) != worktree:
            return 0
        activation = commands.resolve_local_activation(payload.session_id)
        if activation is None or activation.repo != worktree:
            return 0
        text = _reorient_text(activation)
    except Exception as exc:  # noqa: BLE001 - reorient is advisory: any failure is silence, never a denial
        log(f"reorient: {exc}")
        return 0
    if text:
        sys.stdout.write(text)
    return 0
