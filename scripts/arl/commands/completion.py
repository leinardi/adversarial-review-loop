"""Recording ``COMPLETE``, and refusing to when the approval no longer applies.

Shared by every place an activation can disarm: ``finish`` and the Stop gate's ``_final``,
which both call a model for minutes and then write the one status that disarms the loop, and
the Stop gate's ``_complete_without_review``, which writes it with no call at all when
``final_review`` is disabled. All three need the same guard against what can happen to the
activation in the window before that write -- and duplicating it, or a fourth site writing
``status`` directly, is how the guard drifts until only some of them have it.

The guard exists because a final review is slow, and three things can happen while it runs,
each of which turns "approved" into a lie:

- the worktree changes, so the tree that was reviewed is no longer the tree on disk;
- the loop transitions -- escalates, enters reconcile, is stopped, goes stale -- and
  completing would overwrite somebody else's decision with an approval;
- the activation is re-armed, so the approval belongs to a plan that is no longer active.

A fourth, narrower pair applies only to ``_complete_without_review``, via ``commit``'s
``refuse_if_review_now_required``: ``finish`` can be invoked concurrently and ask, explicitly,
for the cumulative review this completion is about to skip, or ``final_review`` itself can be
turned on while the skip is in flight. Neither is part of the general fingerprint --
``_final`` legitimately completes a review that started *because* ``finish_requested`` flipped
true underneath it (a concurrent ``finish`` landing during the Stop gate's own unreviewed-work
sweep), so ``finish_requested`` cannot be compared for equality across every caller alike; only
the no-review path needs it re-checked at write time. Config, unlike ``finish_requested``, *is*
reloaded fresh as part of the general fingerprint every caller shares -- see ``commit``'s
docstring for why the effective-status half of the fingerprint needs that regardless of which
caller it is.

Usage is two calls around the review: :func:`start` **before** it, so the fingerprint is of
the activation the review is about, and :meth:`Completion.commit` after it. Refusal is
reported as ``commands.Refused``, which abandons the transaction with the previous document
intact: the mode stays armed, which is the safe direction.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from arl import commands, gitsnap
from arl import config as config_module
from arl.config import Config
from arl.gitsnap import SnapshotError
from arl.state import State

if TYPE_CHECKING:  # pragma: no cover - the Stop gate must not import the reviewer to type-check
    from arl.reviewer import Review

__all__ = ["Completion", "Fingerprint", "describe_change", "fingerprint", "start"]

#: ``armed_at``, ``baseline_tree``, ``session_id``, stored status, effective status,
#: ``activation_generation``.
type Fingerprint = tuple[str, str, str, str, str, int]

SUPERSEDED: Final = (
    "adversarial-review-loop: a newer final review of this activation completed, or is still running, so the approving "
    "verdict this completion rests on is no longer the one that decides. Nothing was completed and the mode stays "
    "armed. Let the newer review finish and act on its verdict.\n"
)


def _reviewer() -> Any:
    """The reviewer module, imported on use only.

    ``completion`` is reached from the Stop gate, which runs on every turn end; the reviewer
    stack is heavy and only the two review-backed completion paths need it. Same
    one-import-per-job rule ``pretool`` states.
    """
    from arl import reviewer  # noqa: PLC0415 - only the review-backed completion paths need it

    return reviewer


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
            "adversarial-review-loop: this activation was re-armed while completion was pending, "
            "so the completion belongs to an activation that is no longer current. The new activation stays armed.\n"
        )
    if before[5] != now[5]:
        return (
            "adversarial-review-loop: a resume or an accept changed the activation while completion was pending "
            "(the plan, the model, or an approved tree may have changed underneath it), so the completion no "
            "longer applies. The activation stays armed; finish again.\n"
        )
    return (
        f"adversarial-review-loop: the activation moved from {before[4]} to {now[4]} ({reason}) while completion was pending. "
        "That is not overwritten by a completion — the review, if any ran, is recorded, but the mode does not complete.\n"
    )


#: A canonical git object ID: full-length lowercase hex, sha1 or sha256. Anything symbolic or
#: abbreviated -- ``HEAD``, a branch name, a short SHA -- is refused before it reaches git,
#: because two such values can name one commit while comparing unequal as strings, and string
#: distinctness is what stops one real commit from standing in for every phase.
_OBJECT_ID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _recorded_phase_commits(state: State, total: int) -> list[str] | None:
    """The ``total`` recorded object IDs in phase order, or ``None`` on any malformed shape.

    Shape only -- one entry per phase, numbered ``1..total`` with no gaps or repeats, each a
    canonical object ID. Whether those IDs mean anything is git's question, asked by the caller.
    """
    recorded = state.get_array_of_dicts("phase_commits")
    if len(recorded) != total:
        return None
    commits: dict[int, str] = {}
    for entry in recorded:
        phase, commit = entry.get("phase"), entry.get("commit")
        # `bool` is an `int` subclass; `True` must not read as phase 1.
        if not isinstance(phase, int) or isinstance(phase, bool) or not isinstance(commit, str) or not _OBJECT_ID.fullmatch(commit):
            return None
        if phase in commits:
            return None
        commits[phase] = commit
    if sorted(commits) != list(range(1, total + 1)):
        return None
    return [commits[phase] for phase in range(1, total + 1)]


def _moves_the_tree(repo: str, earlier: str, later: str) -> bool:
    """Did ``later`` commit a different tree than ``earlier``, as far as git is concerned?

    Fails closed on a tree git will not resolve, so an unreadable object refuses the completion
    rather than passing it as "well, they differ".
    """
    before = gitsnap.rev_parse(repo, f"{earlier}^{{tree}}")
    after = gitsnap.rev_parse(repo, f"{later}^{{tree}}")
    return bool(before) and bool(after) and before != after


def phase_progress_proven(state: State, repo: str) -> bool:
    """Every frozen phase has a recorded commit behind it that git still vouches for.

    The check that ends the regress. ``phase == phase_count() + 1`` says only that an integer
    was incremented, and ``State.phases_match_frozen`` says only that the *list* was not
    truncated -- neither says the phases were **done**. Corrupting ``phase`` alone satisfies
    both, and on the no-review path there is no reviewer left to notice.

    So the proof is moved out of ``state.json`` entirely: ``posttool`` records the commit SHA it
    verified for each phase (parent, tree and clean worktree all already proved there), and this
    re-checks every one of them against git history. Git objects are content-addressed and the
    ancestry check runs against the real repository, so forging this requires producing actual
    commits reachable from ``HEAD`` -- which is the work the gate exists to make someone do.
    Editing the document cannot manufacture it.

    Requires exactly one entry per frozen phase, numbered ``1..total`` with no gaps or repeats,
    naming ``total`` **distinct canonical object IDs** that form an ancestry chain from a
    non-empty ``activation_commit`` (exclusive) through each phase in order, **each one moving
    the tree**, and ending *at* ``HEAD`` rather than merely below it. Fails closed on every
    malformed shape, and on the one legitimate shape it cannot verify -- see the comment on
    ``activation_commit`` below.

    An activation armed before ``phase_commits`` existed has none recorded, so this refuses and
    the no-review path escalates rather than disarming on evidence that was never collected.
    That is the fail-closed direction, and it applies only to an activation carried across this
    change mid-flight -- ``final_review``'s skip path is new, so no completed activation ever
    depended on it before.
    """
    total = len(state.get_array("phases"))
    if total <= 0:
        return False
    chain = _recorded_phase_commits(state, total)
    # A *chain*, not a set of ancestors. "Each is an ancestor of HEAD" lets one real commit
    # stand in for every phase: record phase 1's ID again under phase 2, bump `phase`, and the
    # activation completes with phase 2 never implemented. So the IDs must be distinct, and
    # phase N's must be an ancestor of phase N+1's -- which no reused or reordered ID can
    # satisfy, since `merge-base --is-ancestor` is reflexive and distinctness is what makes the
    # ancestry strict.
    # `activation_commit` is what anchors the chain to *this* activation rather than to history
    # it inherited, so an empty one is refused outright (report 038). `arm` legitimately writes
    # it empty for an unborn HEAD -- but that is a claim made by the document, and there is
    # nothing outside mutable state that can confirm it. Trying to confirm it from git alone
    # does not work: "phase 1 is a non-empty root commit" is satisfied by any seeded
    # repository's own first commit, so blanking the field and recording real historical
    # commits would prove phases nobody implemented. The cost is that an activation armed on an
    # empty repository cannot use the no-review path and escalates instead; the remedy is
    # `final_review` or `finish`, both documented, and both of which put a reviewer back in the
    # loop where this evidence cannot go.
    activation = state.get("activation_commit")
    if chain is None or not activation or len(set(chain)) != total or activation in chain:
        return False
    # A *chain*, not a set of ancestors, and it starts strictly after the activation commit.
    # The last phase commit must *be* HEAD, not merely an ancestor of it (report 039). An
    # ancestor test leaves room for a commit after the final phase, and one shape of that is
    # invisible everywhere else: a wrapper committing a tree already in `approved_trees` -- the
    # baseline, say, which reverts the entire plan. `confirm-commit` stays silent because the
    # tree was approved, the sweep skips it for the same reason, and the chain still holds. The
    # activation would then complete with the work undone and nothing having reviewed the undo.
    try:
        head = gitsnap.rev_parse_checked(repo, "HEAD")
    except gitsnap.GitUnavailable:
        return False
    if chain[-1] != head:
        return False
    links = [(activation, chain[0]), *itertools.pairwise(chain)]
    if not all(gitsnap.is_ancestor(repo, earlier, later) for earlier, later in links):
        return False
    # Distinct commit IDs prove only that `git commit` ran N times. `git commit --allow-empty`
    # runs it without changing anything, and `pretool` approves an unchanged tree straight from
    # `last_approved_tree` without calling the reviewer at all -- so N empty commits would
    # otherwise carry an entirely unimplemented plan to COMPLETE with no model in the loop.
    # Requiring each phase commit to *move* the tree is what makes the chain evidence of work:
    # a moved tree is one the gate had to put in front of a reviewer before it could land.
    return all(_moves_the_tree(repo, earlier, later) for earlier, later in links)


@dataclass(frozen=True)
class Completion:
    """A pending completion: which activation, and what it looked like before completing it.

    "Before completing it" rather than "before the review" -- ``_complete_without_review``
    starts one of these too, and there is no review in that path at all.
    """

    state: State
    config: Config
    repo: str
    before: Fingerprint

    def commit(self, *, reviewed: str, reason: str, refuse_if_review_now_required: bool = False, review: Review | None = None) -> None:
        """Record ``COMPLETE``, but only if nothing invalidated the completion while it was pending.

        ``reviewed`` names the tree being completed, whatever put it there -- an approving
        final cumulative review, or nothing at all when ``final_review`` is disabled and every
        phase having gone through the per-commit gate or the unreviewed-work sweep is being
        trusted alone. ``final_done_tree`` records that tree either way; it does not
        distinguish which.

        The fingerprint itself is computed against config reloaded fresh here, not
        ``self.config`` (whatever was in effect when this turn's hook process started):
        ``fingerprint``'s effective-status half depends on ``ttl_hours``, and a concurrent
        ``arl config ttl_hours ...`` shrinking it during the (possibly minutes-long) review
        must be enough to catch a baseline gone stale in the meantime, not only elapsed
        wall-clock time measured against a threshold that is itself out of date. This reload
        happens **before** the git snapshot calls below, deliberately: it is part of deciding
        whether to proceed with them at all, not a check to delay until afterward.

        ``refuse_if_review_now_required`` is for the no-review caller specifically, and
        re-checks three more things -- last, immediately before the write, for the same reason
        the worktree is re-checked last rather than up front: to leave as little as
        structurally possible between "still say skip" and the write making that irreversible:

        - The stored ``status`` is ``ACTIVE``, ``phases`` is non-empty, and ``phase`` is
          exactly one past the last phase -- the only shape "every phase was committed"
          can take. None of these three are part of ``fingerprint`` (it tracks activation
          *identity* and *status transitions*, not phase progress), so a tampered or
          concurrently rewritten ``phase``/``phases`` would not move it at all; the caller
          already checked this shape once, unlocked, before ever deciding to skip the
          review, and this is the same check repeated against the document the write will
          actually use, not the one that decision was made against.
        - ``finish_requested`` must still be the literal ``False`` the schema writes.
          Anything else -- ``True``, because a concurrent ``finish`` asked for the review this
          completion is about to skip, or a value that is not a well-formed flag at all --
          is refused rather than trusted, so a malformed or tampered document cannot read as
          silent permission to skip the one thing standing between it and disarming the loop.
          This one is checked exactly as atomically as the fingerprint above: same document,
          same lock, no writer of it that does not also take this lock.
        - ``final_review`` itself, against a **second, later** ``config_module.load`` -- not
          the one the fingerprint used above, and not reused for it: if the user turned it back
          on while this completion was queued, the request to require a review again must land
          as close to the write as this function can place the check, exactly like the
          worktree re-check just above it, not be satisfied by a value already stale by the
          time the two git subprocess calls below have run.

        ``_final`` never passes it -- a review it already ran satisfies whatever asking for one
        demanded, regardless of when the request landed, and it has no "skip" to re-validate.

        ``review`` is the approving **final** review this completion rests on, and the two
        callers that have one must pass it. It gets the question the fingerprint structurally
        cannot answer: ``fingerprint`` covers activation identity and status transitions, not
        review history, so a *second* final review of the same activation recording
        ``CHANGES_REQUIRED`` -- or merely still running -- moves nothing it compares. Without
        this check the first review's ``APPROVED`` is written straight over the newer,
        blocking one, and because the write is ``COMPLETE`` the mistake is **permanent**: that
        status disarms the gate, so there is no later round to correct it. That makes it
        strictly worse here than on the per-commit path, where a wrong approval costs one
        commit and the loop keeps enforcing. ``reviewer.approval_is_current`` is the same check
        ``pretool``'s approval and the Stop sweep's already ask, against the ``final`` label.
        ``_complete_without_review`` passes nothing, correctly: it rests on no verdict at all,
        and ``refuse_if_review_now_required`` is what guards *that* path instead.

        **Every check runs with the activation lock held**, and the lock is not released
        until ``COMPLETE`` is on disk. Verifying the worktree before taking the lock leaves a
        window in which the tree is checked, the process then waits for the lock, and the
        content changes while it waits -- the verification would be of a tree that no longer
        exists by the time the approval is written.

        **Residual windows remain, and none of them can be closed here.** Between
        ``gitsnap.snapshot`` returning and ``os.replace`` publishing the document -- a few
        milliseconds -- a file watcher, an editor writing back a buffer or an MCP server
        dropping a state directory can change the worktree. That content is then outside the
        reviewed set while the report says the activation was reviewed. The lock does not help:
        it is this gate's own lock, and nothing that writes to a worktree honours it. Shrinking
        the window further only moves it, and re-checking *after* publishing would have a
        window of its own plus a new failure mode when the rollback fails.

        Both config reloads have the identical shape, for the identical structural reason:
        ``config_module.load`` reads the repo and user config files, and nothing that writes
        them takes this activation's lock -- doing so would mean coordinating an arbitrary
        number of config writers against a lock scoped to one session's activation, which the
        config layer has no notion of. So a `config ttl_hours ...` landing in the instant
        between the fingerprint's reload and this function returning, or a `config final_review
        true` landing in the instant between *its own* later reload and ``state.update`` below
        reaching disk, is not caught, for the same reason the worktree's last millisecond is
        not: each check has been moved as late as it structurally can be, and there is no lock
        either side takes. That is also why the two reloads are not unified into one taken
        early and reused: doing so would widen the ``final_review`` window back out to span two
        git subprocess calls' worth of avoidable exposure, trading a narrow, already-accepted
        sliver for a needlessly wider one.

        What bounds the consequence, for every window above, is that ``COMPLETE`` is by design
        the moment enforcement ends -- content, or a config change, appearing a millisecond
        before it is ungated for the same reason either appearing a millisecond after is. The
        claim to keep accurate is therefore which *tree* is being completed and that a review
        was not skipped except by a value that read as permission at the last possible instant
        this function checked it, and every caller states the former; the latter is this
        docstring. AGENTS.md already names the worktree hazard under "Known environment
        hazards": gitignore such paths before arming.
        """
        state, repo = self.state, self.repo
        with state.transaction():
            # Reloaded fresh here, not ``self.config`` (whatever was in effect when this turn's
            # hook process started) -- ``fingerprint``'s effective-status half depends on
            # ``ttl_hours``, and a concurrent ``arl config ttl_hours ...`` shrinking it during
            # the (possibly minutes-long) review must be enough to catch a baseline that has
            # gone stale in the meantime, not only elapsed wall-clock time against the old
            # threshold. A *second*, later reload -- not this one -- serves the
            # ``final_review`` re-check below: that check's whole point is catching a change
            # landing as late as possible, so it must not be satisfied by a value read this
            # early, before the two git subprocess calls that follow.
            now = fingerprint(state, config_module.load(repo, overrides=state.data.get("overrides")))
            if now != self.before:
                raise commands.Refused(describe_change(self.before, now, state.get("reason")))

            # Asked under this same lock, so a final round `reviewer._publish` committed -- or
            # a claim `_reserve_round` took -- before this transaction opened is visible here.
            if review is not None and not _reviewer().approval_is_current(state, "final", review):
                raise commands.Refused(SUPERSEDED)

            try:
                after = gitsnap.snapshot(repo)
            except SnapshotError as exc:
                raise commands.Refused(
                    f"adversarial-review-loop: about to complete, but the working state could not be re-checked first ({exc}), "
                    "so completion is refused. The mode stays armed.\n"
                ) from exc

            if after.tree != reviewed:
                raise commands.Refused(
                    f"adversarial-review-loop: tree {reviewed} was about to be recorded as complete, but the worktree is now {after.tree} — "
                    "content changed in the meantime, so what is being completed is not what is on disk. "
                    "The mode stays armed; commit the change and finish again.\n"
                )
            if after.tree != gitsnap.head_tree(repo):
                raise commands.Refused(
                    "adversarial-review-loop: about to complete, but the worktree is no longer clean — "
                    f"a commit or a reset moved HEAD in the meantime.\n\n{gitsnap.dirty_summary(repo)}\n"
                )

            if refuse_if_review_now_required:
                # State is not a trust boundary, and none of the fields this checks are part of
                # `fingerprint` -- `phase`/`phases` could be tampered with, or concurrently
                # rewritten, without ever changing it, since the fingerprint tracks activation
                # *identity* and *status*, not phase progress. The caller already checked this
                # shape before the (possibly slow) reviewer-skip decision was even made; it is
                # repeated here, under the same lock as the write, because that earlier check
                # ran against a document this one may no longer describe.
                total = len(state.get_array("phases"))
                phase = state.get_int("phase")
                # `phases` is checked against `phases.frozen`, not just counted: the equality
                # below is "every phase was committed" only if the list it counts is the one
                # `set-phases` actually froze. Truncating `phases` from two entries to one
                # after the first lands satisfies `phase == total + 1` with the second phase
                # never implemented -- see `State.phases_match_frozen`.
                if (
                    state.get("status") != "ACTIVE"
                    or total <= 0
                    or phase != total + 1
                    or not state.phases_match_frozen()
                    or not phase_progress_proven(state, repo)
                ):
                    raise commands.Refused(
                        "adversarial-review-loop: about to complete without a review, but the activation's own state no "
                        f"longer describes every phase as committed (status={state.get('status')!r}, phase={phase}, "
                        f"total={total}, matches frozen evidence={state.phases_match_frozen()}, phase progress proven "
                        f"against git={phase_progress_proven(state, repo)}). State is not a trust boundary; this "
                        "refuses rather than risk disarming on it. The activation stays armed.\n"
                    )

                requested = state.data.get("finish_requested")
                if requested is not False:
                    if requested is True:
                        raise commands.Refused(
                            "adversarial-review-loop: `finish` was requested while this completion was pending, so "
                            "completing now would pre-empt the cumulative review it asked for. The activation "
                            "stays armed; finish's own review will complete it.\n"
                        )
                    raise commands.Refused(
                        f"adversarial-review-loop: finish_requested is {requested!r}, not the boolean the schema "
                        "writes, so whether a review was asked for cannot be trusted. Completion without one is "
                        "refused rather than risked. The activation stays armed.\n"
                    )

                # Reloaded again here, later than the fingerprint's own reload above and after
                # both git subprocess calls -- placed as late as this function can move it, the
                # same reasoning the class docstring already gives for the worktree checks.
                if config_module.load(repo, overrides=state.data.get("overrides")).as_bool("final_review"):
                    raise commands.Refused(
                        "adversarial-review-loop: `final_review` was enabled while this completion was pending, so "
                        "completing without a cumulative review is refused. The activation stays armed; end the "
                        "turn again to run one.\n"
                    )

            state.update(final_done_tree=reviewed, status="COMPLETE", reason=reason)


def start(state: State, *, config: Config, repo: str) -> Completion:
    """Capture what the activation is **now**, before a final review is started against it."""
    return Completion(state=state, config=config, repo=repo, before=fingerprint(state, config))
