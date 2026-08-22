"""Recording ``COMPLETE``, and refusing to when the approval no longer applies.

Shared by the two places a final cumulative review can end: ``finish``, which the user runs,
and the Stop gate, which runs on the last turn. Both call a model for minutes and then write
the one status that disarms the loop, so both need the same guard -- and duplicating it is
how the two drift until only one of them has it.

The guard exists because a final review is slow, and three things can happen while it runs,
each of which turns "approved" into a lie:

- the worktree changes, so the tree that was reviewed is no longer the tree on disk;
- the loop transitions -- escalates, enters reconcile, is stopped, goes stale -- and
  completing would overwrite somebody else's decision with an approval;
- the activation is re-armed, so the approval belongs to a plan that is no longer active.

Usage is two calls around the review: :func:`start` **before** it, so the fingerprint is of
the activation the review is about, and :meth:`Completion.commit` after it. Refusal is
reported as ``commands.Refused``, which abandons the transaction with the previous document
intact: the mode stays armed, which is the safe direction.
"""

from __future__ import annotations

from dataclasses import dataclass

from ocrl import commands, gitsnap
from ocrl.config import Config
from ocrl.gitsnap import SnapshotError
from ocrl.state import State

__all__ = ["Completion", "Fingerprint", "describe_change", "fingerprint", "start"]

#: ``armed_at``, ``baseline_tree``, ``session_id``, stored status, effective status,
#: ``activation_generation``.
type Fingerprint = tuple[str, str, str, str, str, int]


def fingerprint(state: State, config: Config) -> Fingerprint:
    """Everything that must be **unchanged** for a finished review to still mean anything.

    Deliberately an equality check rather than a list of statuses that may not be overwritten.
    A deny-list has to enumerate every denying state, and it silently fails open the day one
    is added or renamed: ``RECONCILE`` was missing from exactly such a list, so an approving
    review overwrote "a commit diverged from the reviewed tree" with ``COMPLETE``.

    So instead: whatever the activation was when the review started, it must still be that
    when the review lands. Both statuses are captured because they answer different questions
    -- the stored one changes when something transitions the loop, the effective one also
    changes when the TTL expires underneath a long review, and a stale baseline is exactly
    what must not be signed off.

    ``armed_at``, ``baseline_tree`` and ``session_id`` identify *which* activation this is:
    ``arm`` writes a fresh document, so a re-arm mid-review changes them. ``activation_generation``
    catches what identity does not: a same-session ``resume`` leaves all three unchanged but
    swaps the active plan revision or the model override underneath a review already in
    flight, and every resume -- same-session included -- increments it for exactly this.
    """
    return (
        state.get("armed_at"),
        state.get("baseline_tree"),
        state.get("session_id"),
        state.get("status"),
        state.effective_status(config),
        state.get_int("activation_generation"),
    )


def describe_change(before: Fingerprint, now: Fingerprint, reason: str) -> str:
    if before[:3] != now[:3]:
        return (
            "opencode-review-loop: this activation was re-armed while the final review was running, "
            "so the approval belongs to an activation that is no longer current. The new activation stays armed.\n"
        )
    if before[5] != now[5]:
        return (
            "opencode-review-loop: a resume changed the activation while the final review was running "
            "(the plan or the model may have changed underneath it), so the approval no longer applies. "
            "The activation stays armed; finish again.\n"
        )
    return (
        f"opencode-review-loop: the activation moved from {before[4]} to {now[4]} ({reason}) while the final review was running. "
        "That is not overwritten by an approval — the review is recorded, but the mode does not complete.\n"
    )


@dataclass(frozen=True)
class Completion:
    """A pending completion: which activation, and what it looked like before the review."""

    state: State
    config: Config
    repo: str
    before: Fingerprint

    def commit(self, *, reviewed: str, reason: str) -> None:
        """Record ``COMPLETE``, but only if nothing invalidated the review while it ran.

        **Every check runs with the activation lock held**, and the lock is not released
        until ``COMPLETE`` is on disk. Verifying the worktree before taking the lock leaves a
        window in which the tree is checked, the process then waits for the lock, and the
        content changes while it waits -- the verification would be of a tree that no longer
        exists by the time the approval is written.

        **A residual window remains, and it cannot be closed here.** Between
        ``gitsnap.snapshot`` returning and ``os.replace`` publishing the document -- a few
        milliseconds -- a file watcher, an editor writing back a buffer or an MCP server
        dropping a state directory can change the worktree. That content is then outside the
        reviewed set while the report says the activation was reviewed. The lock does not help:
        it is this gate's own lock, and nothing that writes to a worktree honours it. Shrinking
        the window further only moves it, and re-checking *after* publishing would have a
        window of its own plus a new failure mode when the rollback fails.

        What bounds the consequence is that ``COMPLETE`` is by design the moment enforcement
        ends -- content appearing a millisecond before it is ungated for the same reason
        content appearing a millisecond after is. The claim to keep accurate is therefore
        which *tree* was reviewed, and both callers state it. AGENTS.md already names this
        hazard under "Known environment hazards": gitignore such paths before arming.
        """
        state, config, repo = self.state, self.config, self.repo
        with state.transaction():
            now = fingerprint(state, config)
            if now != self.before:
                raise commands.Refused(describe_change(self.before, now, state.get("reason")))

            try:
                after = gitsnap.snapshot(repo)
            except SnapshotError as exc:
                raise commands.Refused(
                    f"opencode-review-loop: the final review passed, but the working state could not be re-checked afterwards ({exc}), "
                    "so completion is refused. The mode stays armed.\n"
                ) from exc

            if after.tree != reviewed:
                raise commands.Refused(
                    f"opencode-review-loop: the final review approved tree {reviewed}, but the worktree is now {after.tree} — "
                    "content changed while the review was running, so what was approved is not what is on disk. "
                    "The mode stays armed; commit the change and finish again.\n"
                )
            if after.tree != gitsnap.head_tree(repo):
                raise commands.Refused(
                    "opencode-review-loop: the final review passed, but the worktree is no longer clean — "
                    f"a commit or a reset moved HEAD while the review was running.\n\n{gitsnap.dirty_summary(repo)}\n"
                )

            state.update(final_done_tree=reviewed, status="COMPLETE", reason=reason)


def start(state: State, *, config: Config, repo: str) -> Completion:
    """Capture what the activation is **now**, before a final review is started against it."""
    return Completion(state=state, config=config, repo=repo, before=fingerprint(state, config))
