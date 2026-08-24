"""The five commands a user runs against a live activation.

Ports ``cmd_defer``, ``cmd_status``, ``cmd_report``, ``cmd_finish`` and ``cmd_deactivate``.

``finish`` and ``deactivate`` are two of the three exits the user owns (Rule 4). Nothing
here is reachable by Claude: the skills carry ``disable-model-invocation: true``, and
``pretool`` denies the Bash route to both. What that means for this module is that its
output is written for a human -- it is the last thing said before the mode ends.
"""

from __future__ import annotations

import sys

from ocrl import commands, gitsnap, planrev, report, reviewer
from ocrl.commands import completion
from ocrl.commands.completion import Completion
from ocrl.config import Config
from ocrl.gitsnap import SnapshotError
from ocrl.state import State
from ocrl.util import log

__all__ = ["deactivate", "defer", "finish", "report_cmd", "status"]

NOT_ARMED: str = "opencode-review-loop: not armed in this worktree.\n"


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
        sys.stderr.write("opencode-review-loop: nothing armed in this worktree.\n")
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
                    f"opencode-review-loop: {used - 1} defers already used (limit {limit}). "
                    "The turn cannot be deferred again; finish the phase or ask the user to run /opencode-review-loop:stop.\n"
                )
            state.update(defers=used, defer_pending=True, reason=f"deferred: {reason}")
    except commands.Refused as exc:
        sys.stderr.write(str(exc))
        return 1

    sys.stdout.write(f"opencode-review-loop: turn end deferred ({used} of {limit}). Reason recorded: {reason}\n")
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


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
    stop_after_phase = state.get_int("stop_after_phase")
    pause_target = f"{stop_after_phase} of {state.phase_count()}" if stop_after_phase else "none"
    manual_accepts = state.get_array_of_dicts("manual_accepts")
    accepted_phases = ", ".join(str(entry.get("phase")) for entry in manual_accepts)
    accepts_line = f"{len(manual_accepts)} (phases {accepted_phases})" if manual_accepts else "0"
    plan_revisions = state.data.get("plan_revisions") or []
    # `status` never changes anything (see the docstring above), so a corrupted revision
    # entry is reported inline rather than escalated the way `pretool`/`gate-stop` do --
    # there is no mutation here to gate, only a diagnostic to print honestly.
    try:
        active_plan_file = planrev.active_filename(plan_revisions)
    except planrev.EvidenceCorrupted as exc:
        active_plan_file = f"<corrupted: {exc}>"
    revision_count = len(plan_revisions) or 1

    sys.stdout.write(
        f"""\
opencode-review-loop status
---------------------------
worktree:            {state.get("worktree")}
session:             {state.get("session_id")}
status:              {status_line}
reason:              {state.get("reason")}
plan:                {state.get("plan_path")}
frozen plan:         {activation.act_dir}/{active_plan_file}
plan revision:       {revision_count - 1} ({revision_count} recorded)
baseline tree:       {state.get("baseline_tree")}
activation commit:   {state.get("activation_commit")}
last approved tree:  {state.get("last_approved_tree")}
pending approval:    {state.get("pending_approved_tree")}
phase:               {state.get("phase")} of {state.phase_count()}
pause target:        {pause_target}
consecutive failures:{state.get("failures")} / {config.as_int("max_failures")}
no-progress blocks:  {state.get("stop_blocks")} / {config.as_int("max_stop_blocks")}
defers used:         {state.get("defers")} / {config.as_int("max_defers")}
manual accepts:      {accepts_line}
model:               {config.as_str("model")} {config.as_str("variant")}
block_severity:      {config.as_str("block_severity")}
state directory:     {activation.act_dir}

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
            f"opencode-review-loop: this activation is past ttl_hours ({config.as_int('ttl_hours')}), so its baseline can no longer "
            "be trusted and it blocks rather than silently disarming. The final review did not run. Re-arm with "
            "/opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop.\n"
        )
    if status == "COMPLETE":
        raise commands.Refused(
            f"opencode-review-loop: this activation is already COMPLETE ({state.get('reason')}). "
            "The mode has already disarmed itself, so there is nothing left to finish.\n"
        )
    raise commands.Refused(
        f"opencode-review-loop: cannot finish while the activation is {status} ({state.get('reason')}). "
        "The final review did not run. Re-arm with /opencode-review-loop:implement <plan.md>, "
        "or leave the mode with /opencode-review-loop:stop.\n"
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
            "opencode-review-loop: the worktree is not clean. Commit the outstanding work first — "
            "the final review runs over committed history plus the working state, and every phase "
            f"must land in a reviewed commit.\n\n{gitsnap.dirty_summary(repo)}\n"
        )

    try:
        snap = gitsnap.snapshot(repo)
    except SnapshotError as exc:
        # No tree means nothing to review against, and "nothing to review" is never an
        # approval (Rule 1).
        raise commands.Refused(
            f"opencode-review-loop: the working state could not be snapshotted ({exc}), so the final review did not run.\n"
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
    sys.stdout.write(f"opencode-review-loop: running the final cumulative review ({base} -> {snap.tree}). This can take a few minutes.\n\n")

    target = reviewer.Target(repo=repo, base=base, head=snap.tree, scope="final", phase=state.phase_count())
    review = reviewer.execute(target, state=state, config=config, warnings=snap.warnings)

    if review.verdict != "APPROVED":
        text = report.reason(review, "opencode-review-loop: the final cumulative review did not pass. The mode stays armed.", config=config)
        # One trailing newline, as `printf '%s\n' "$( … )"` produced.
        sys.stdout.write(text.rstrip("\n") + "\n")
        return 1

    try:
        pending.commit(reviewed=snap.tree, reason="final cumulative review approved (user-invoked finish)")
    except commands.Refused as exc:
        sys.stdout.write(str(exc))
        return 1

    sys.stdout.write(
        "opencode-review-loop: COMPLETE. The final review passed. The mode has disarmed itself; "
        f"further commits are ungated.\n\nFull report: {review.report}\n"
    )
    return 0


# --------------------------------------------------------------------------
# deactivate
# --------------------------------------------------------------------------


def deactivate(argv: list[str]) -> int:
    """Leave the mode. Nothing is reverted and nothing is deleted.

    The session pointer deliberately stays (Rule 0): the hooks are still registered for this
    session, and a missing pointer is what "arming never executed" looks like, so removing it
    would turn every later tool call into a denial. ``DISARMED`` is what makes the gates pass
    through.
    """
    del argv
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write("opencode-review-loop: not armed in this worktree, so there is nothing to stop.\n")
        return 0

    state = activation.state
    with state.transaction():
        state.update(status="DISARMED", reason="stopped by the user")

    sys.stdout.write(
        f"""\
opencode-review-loop: STOPPED for this worktree.

Commits and file changes are no longer gated. Nothing was reverted — the repository
is exactly as you left it, at phase {state.get("phase")} of {state.phase_count()}.

State and reports are kept at:
  {activation.act_dir}

Re-arm at any time with /opencode-review-loop:implement <plan.md>.
"""
    )
    return 0
