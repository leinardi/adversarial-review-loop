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

from __future__ import annotations

import math
import os
import sys
from typing import Any, Final

from ocrl import commands, gitsnap, harness, hookio, oscillation, paths, planrev, report, reviewer
from ocrl.commands import completion, hooks
from ocrl.commands.completion import Completion
from ocrl.config import Config
from ocrl.gitsnap import SnapshotError
from ocrl.state import State, pointer_read
from ocrl.util import log, now

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
- Every commit is intercepted and reviewed. A denial lists the blocking findings: fix all of
  them and commit again. A failed or malformed review is never an approval.
- Each phase ends with one commit and a clean worktree.
- If a finding is ambiguous or contradicts an earlier round, ask before guessing:
  `{root}/scripts/ocrl.sh clarify --question "..."`.
- To stop mid-phase and ask the user something, run `{root}/scripts/ocrl.sh defer --reason "..."`
  first, then end your turn.
- You cannot end the mode; `/opencode-review-loop:finish` and `/opencode-review-loop:stop` are
  the user's.
"""

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
    persisting_points = oscillation.persisting(phase_history, phase_label, stall_rounds) if stall_rounds > 0 else []
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
{cost_line}{persisting_line}state directory:     {activation.act_dir}

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
        pending.commit(reviewed=snap.tree, reason="final cumulative review approved (user-invoked finish)", review=review)
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


def _session_arg(argv: list[str]) -> str:
    """The ``--session <id>`` the ``stop`` skill passes, or ``""``."""
    for index, arg in enumerate(argv):
        if arg == "--session" and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def _discard_intent(session: str) -> str:
    """Discard the session's intent marker, if any. Returns a line for the user, or ``""``.

    This is the **only** path that removes a marker the gate could not scope, and it is a
    user action -- ``/opencode-review-loop:stop`` -- which is exactly the recovery the gate
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
            f"opencode-review-loop: an enforcement request for this session is recorded at {marker} and could not be "
            f"removed ({exc}); every mutation stays denied until it is.\n"
        )
    return "opencode-review-loop: discarded this session's pending enforcement request (an arm that never completed).\n"


def deactivate(argv: list[str]) -> int:
    """Leave the mode. Nothing is reverted and nothing is deleted.

    The session pointer deliberately stays (Rule 0): the hooks are still registered for this
    session, and a missing pointer is what "arming never executed" looks like, so removing it
    would turn every later tool call into a denial. ``DISARMED`` is what makes the gates pass
    through.
    """
    discarded = _discard_intent(_session_arg(argv))
    activation = commands.resolve_local_activation()
    if activation is None:
        sys.stdout.write(discarded + "opencode-review-loop: not armed in this worktree, so there is nothing to stop.\n")
        return 0
    sys.stdout.write(discarded)

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
        f"opencode-review-loop: this session was compacted or resumed while the review loop is {status} in {state.get('worktree')}.",
        "Re-orienting you, because the loop is still enforced and the current context may no longer carry it.",
        "",
    ]

    if status in ("NEEDS_HUMAN", "STALE"):
        head += [
            f"The activation is {status} and needs the user, not another attempt. Stop, tell the user, and let them run",
            "/opencode-review-loop:status and decide. Do not keep implementing.",
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
        return f"The last recorded round of phase {phase} cannot be read back; run /opencode-review-loop:status."
    return (
        f"The last review of phase {phase} was report {seq:03d}: {verdict}, {count} finding(s) recorded. "
        f"Read them with /opencode-review-loop:report {seq}."
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
    except Exception as exc:
        log(f"reorient: {exc}")
        return 0
    if text:
        sys.stdout.write(text)
    return 0
