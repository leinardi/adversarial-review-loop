"""``accept`` -- a user-owned manual approval that breaks a review loop stuck on one tree.

An approving review does exactly one thing that matters to the commit gate: it puts the
reviewed tree hash into ``approved_trees`` (``state.mark_tree_approved``), which
``pretool._gate_commit`` checks before ever calling the reviewer again. ``accept`` mints
exactly that artifact, and nothing else -- it does not advance the phase, does not set
``last_approved_tree``, does not touch a pending approval. Everything downstream -- the
commit gate, ``confirm-commit``'s post-commit verification, the phase advance -- runs
unchanged, through paths that are already tested.

The grant is bound to **one exact tree hash**, which is what makes it safe to hand to a user
command rather than build into the hook: it cannot pre-approve future work. Edit anything
after accepting and the tree hash changes, and the gate re-engages on its own.

Like every resume, it bumps ``activation_generation``. Neither ``hooks.Activation`` nor
``completion.Fingerprint`` includes ``approved_trees`` or ``manual_accepts``, so without this a
slow operation already in flight when an accept runs -- a per-commit review in ``pretool``, or
a final review under ``finish``/the Stop gate -- could complete afterwards, see no difference
in the fields it *does* compare, and act on a decision this accept had already superseded.

This is one of the exits Rule 4 reserves for the user (AGENTS.md). Two independent locks keep
Claude from reaching it: ``skills/accept/SKILL.md`` carries ``disable-model-invocation: true``,
and ``accept`` is in ``cmdshape._ESCAPE_RE``, so ``pretool`` denies the Bash route too.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from arl import commands, gitsnap, report
from arl.commands import hooks
from arl.config import Config
from arl.gitsnap import SnapshotError
from arl.state import State
from arl.util import log, now

__all__ = ["run"]

NOT_ARMED: Final = "adversarial-review-loop: not armed in this worktree.\n"

#: Statuses ``accept`` may grant anything for -- and only when ``phases`` is non-empty too.
#: See ``_refusal`` for why an empty phase list refuses even a listed status.
_ACCEPTABLE: Final = frozenset({"ACTIVE", "NEEDS_HUMAN", "RECONCILE"})

_PHASES_NOT_FROZEN: Final = """\
adversarial-review-loop: nothing may be accepted before the phase list is frozen -- there is no \
phase for an approval to apply to.

Read the frozen plan and run set-phases to freeze it, then try again. If this is a \
NEEDS_HUMAN reached before phases were ever frozen, there was no review loop to break out of \
here -- the exit is /adversarial-review-loop:resume, or a fresh /adversarial-review-loop:implement \
<plan.md>.
"""

_ALL_PHASES_COMMITTED: Final = """\
adversarial-review-loop: every frozen phase is already committed -- there is no phase left for an \
acceptance to apply to. Accepting the final review is out of scope for this command: \
/adversarial-review-loop:finish reports what a failing final review found and leaves the mode \
armed to keep iterating, or /adversarial-review-loop:stop leaves the mode entirely.
"""


def _refusal(state: State, config: Config) -> str | None:  # noqa: PLR0911 - one return per status this command must name, matched exactly
    """``None`` when this activation may be accepted into; the refusal text otherwise.

    An allow-list, deliberately, matching the shape ``resume._refuse_unless_resumable``
    already uses for the same reason: a deny-list fails open the moment a new status is
    added and this function is not updated for it.
    """
    status = state.effective_status(config)
    if status in _ACCEPTABLE:
        if state.phase_count() == 0:
            return _PHASES_NOT_FROZEN
        # `phase` past the last frozen one is the window between the last phase's commit and
        # the Stop gate's own completion machinery -- not a phase an acceptance can name, and
        # accepting the final review is explicitly out of scope (module docstring).
        phase = state.get_int("phase")
        if phase < 1 or phase > state.phase_count():
            return _ALL_PHASES_COMMITTED
        return None
    if status == "ARMED":
        return _PHASES_NOT_FROZEN
    if status in ("COMPLETE", "DISARMED"):
        return f"adversarial-review-loop: nothing is gated in this worktree ({status}), so there is nothing to accept.\n"
    if status == "STALE":
        return (
            f"adversarial-review-loop: this activation is past ttl_hours ({config.as_int('ttl_hours')}), so its baseline "
            "can no longer be trusted and what the accepted tree would be a delta from is unknown. Run "
            "/adversarial-review-loop:resume first, then accept again.\n"
        )
    if status in ("ARM_FAILED", "RESUMED"):
        return f"adversarial-review-loop: this activation is {status}; there is no live activation here to accept anything into.\n"
    return f"adversarial-review-loop: cannot accept while the activation is {status}.\n"


def _parse_args(argv: list[str]) -> tuple[str, str]:
    """``(reason, session)``. Unrecognised tokens are ignored, matching ``session.defer``."""
    reason = ""
    session = ""
    index = 0
    while index < len(argv):
        if argv[index] == "--reason":
            reason = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
            continue
        if argv[index] == "--session":
            session = argv[index + 1] if index + 1 < len(argv) else ""
            index += 2
            continue
        index += 1
    return reason, session


@dataclass(frozen=True)
class _Outcome:
    """Everything the success message needs to report, gathered under the transaction's lock."""

    phase: int
    tree: str
    status_before: str
    bad_commit: str
    report_path: Path
    promote_error: str = ""


def _message(outcome: _Outcome) -> str:
    lines = [f"adversarial-review-loop: accepted phase {outcome.phase}'s current tree ({outcome.tree}) by user override.\n"]
    if outcome.status_before == "NEEDS_HUMAN":
        lines.append("\nThis clears the NEEDS_HUMAN escalation; the activation is ACTIVE again.\n")
    if outcome.status_before == "RECONCILE":
        lines.append(
            f"\nThis does NOT clear the outstanding reconcile: the per-commit invariant it flagged still stands "
            f"(bad commit {outcome.bad_commit}), and the Stop gate still refuses to complete the activation until "
            "that is resolved through the normal recovery reset. The rebuilt commit still goes through the "
            "ordinary review gate once the reconcile is resolved.\n"
        )
    lines.append(
        "\nThis grants an approval for this exact tree only -- the same artifact a passing review would have "
        "minted, and nothing else. It does not advance the phase and does not complete the activation; commit "
        "normally and the gate lets this exact tree through without calling the reviewer again. Any further edit "
        "changes the tree hash, and the gate re-engages on its own -- the next tree is reviewed normally.\n"
    )
    if outcome.promote_error:
        lines.append(
            f"\nThe acceptance itself is durably recorded -- the tree is approved, and this is reflected in "
            f"status and in every later review's evidence -- but its report could not be published to its final, "
            f"listed name: {outcome.promote_error}. The report text still exists, staged at: {outcome.report_path}\n"
        )
    else:
        lines.append(f"\nRecorded: {outcome.report_path}\n")
    return "".join(lines)


def _accept(state: State, *, config: Config, tree: str, expected: hooks.Activation, reason: str) -> str:
    """Grant the approval under one transaction. Raises ``commands.Refused`` on any refusal."""
    staged: Path | None = None
    seq_text = ""
    label = ""
    status_before = ""
    bad_commit = ""
    phase = 0

    with state.transaction():
        current = hooks.activation(state, config)
        if current != expected:
            raise commands.Refused(
                f"adversarial-review-loop: {hooks.describe_move(expected, current)} while this accept was being "
                f"prepared, so nothing was accepted. The activation is now {current.summary}. Run "
                "/adversarial-review-loop:accept again.\n"
            )

        problem = _refusal(state, config)
        if problem is not None:
            raise commands.Refused(problem)

        status_before = state.get("status")
        bad_commit = state.get("bad_commit")
        phase = state.get_int("phase")
        label = f"phase{phase}"
        base = state.get("last_approved_tree")

        # Allocated and written *before* the approval-bearing mutation below, and staged
        # under a name no reader can see yet: a report that lands at its final,
        # `list_reports`-visible name for an approval that was never durably recorded would
        # let a later review reuse the same sequence and shadow it with the real report --
        # see `report.promote_accept`.
        seq = state.get_int("report_seq") + 1
        state.update(report_seq=seq)
        seq_text = f"{seq:03d}"

        reviews = [name for name in report.list_reports(state.act_dir) if f"-{label}-" in name]
        record = report.AcceptRecord(seq=seq_text, phase=phase, tree=tree, base=base, reason=reason, reviews=reviews)
        staged = report.stage_accept(report.render_accept(record), act_dir=state.act_dir, seq=seq_text, label=label)

        state.mark_tree_approved(tree)
        # A stuck loop's counters are exactly what this command exists to reset -- leaving
        # them would let the next hiccup re-escalate immediately. `transient_failures` and
        # `retry_not_before` (phase 6) are the same kind of counter as `failures` -- an accept
        # that left a backoff standing would have the very tree it just approved still denied
        # by `pretool._check_retry_backoff` for up to `max_transient_failures`' worth of delay.
        updates: dict[str, object] = {"failures": 0, "transient_failures": 0, "retry_not_before": 0, "stop_blocks": 0, "stop_marker": ""}
        # Left untouched during RECONCILE: `reason` there is the divergence explanation
        # `stop.py`'s reconcile block and `status` both surface (`bad_commit_parent` alongside
        # it), and it must keep saying why the invariant broke -- the acceptance not clearing
        # the reconcile (see `_message`) means that explanation is still exactly what the user
        # needs next. The acceptance itself is recorded in full elsewhere: `manual_accepts`,
        # the promoted report, and every later review's evidence.
        if status_before != "RECONCILE":
            composed_reason = f"phase {phase} manually accepted by the user: {reason}" if reason else f"phase {phase} manually accepted by the user"
            updates["reason"] = composed_reason
        # Bumped on every accept, not only ones that clear NEEDS_HUMAN: a slow operation
        # already in flight -- a per-commit review in `pretool`, or a final review under
        # `finish`/the Stop gate -- captured its own `expected` fingerprint before this ran,
        # and neither `hooks.Activation` nor `completion.Fingerprint` includes `approved_trees`
        # or `manual_accepts`. Without this, such an operation can complete afterwards, see no
        # difference, and act on a now-stale decision -- writing a stray approval over this
        # tree, or worse, silently re-escalating NEEDS_HUMAN right after this cleared it. See
        # `hooks.Activation` and `completion.fingerprint`.
        updates["activation_generation"] = state.get_int("activation_generation") + 1
        if status_before == "NEEDS_HUMAN":
            # The only code in the tree that clears a NEEDS_HUMAN -- that is the point of
            # this command. There is no ARMED fallback: an empty `phases` was already
            # refused by `_refusal` above.
            updates["status"] = "ACTIVE"
        state.update(**updates)

        report_name = f"{seq_text}-{label}-accepted.md"
        accepts = state.get_array_of_dicts("manual_accepts")
        accepts.append(
            {
                "at": now(),
                "phase": phase,
                "tree": tree,
                "base": base,
                "reason": reason,
                "reviews": len(reviews),
                "report": report_name,
            }
        )
        state.data["manual_accepts"] = accepts

    # Only now, after the transaction above has itself saved, is the approval durable --
    # which is what makes it safe to publish the report at its real, discoverable name. `with`
    # completing without raising means every assignment inside it ran, `staged` included.
    try:
        final_path = report.promote_accept(staged, act_dir=state.act_dir, seq=seq_text, label=label)
    except OSError as exc:
        # The approval above is already durable -- `approved_trees`, `manual_accepts` and
        # `status` are all saved. Only the rename to the report's final, listed name failed,
        # so this is the harmless residue the ordering was designed to leave: reported, not
        # raised, and naming exactly where the report text still is.
        log(f"accept: could not promote the staged report {staged} to its final name: {exc}")
        return _message(
            _Outcome(phase=phase, tree=tree, status_before=status_before, bad_commit=bad_commit, report_path=staged, promote_error=str(exc))
        )

    return _message(_Outcome(phase=phase, tree=tree, status_before=status_before, bad_commit=bad_commit, report_path=final_path))


def run(argv: list[str]) -> int:
    reason, session = _parse_args(argv)
    activation = commands.resolve_local_activation(session)
    if activation is None:
        sys.stdout.write(NOT_ARMED)
        return 0

    state, config, repo = activation.state, activation.config, activation.repo

    # Captured before the snapshot -- which, like every slow operation between reading the
    # document and writing an approval, is exactly the window `hooks.Activation` exists to
    # close. See `arl.commands.hooks.Activation`'s own docstring.
    expected = hooks.activation(state, config)

    try:
        snap = gitsnap.snapshot(repo)
    except SnapshotError as exc:
        # No tree means nothing to identify what is being accepted, and an accept that
        # cannot name its tree must not be recorded (Rule 1).
        sys.stdout.write(f"adversarial-review-loop: the working state could not be snapshotted ({exc}); nothing was accepted.\n")
        return 1

    try:
        message = _accept(state, config=config, tree=snap.tree, expected=expected, reason=reason)
    except commands.Refused as exc:
        sys.stdout.write(str(exc))
        return 1

    sys.stdout.write(message)
    return 0
