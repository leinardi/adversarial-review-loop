"""Per-label mutual exclusion: the active_review lease.

The property asserted throughout: **Rule 1**. No reviewer output and no operational
failure produces ``APPROVED``.
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

import json
import os
import time
from pathlib import Path

import pytest
from conftest import config_with
from reviewer_common import (
    _APPROVES,
    _ROUND_1,
    _ROUND_2,
    _ROUND_3,
    _STUCK,
    _intact_bundle,
    _run_scripted,
    _seal,
    dirty,
    execute_fake,
    target_for,
)

from arl import reviewer, state
from arl.commands import hooks
from arl.config import Config
from arl.reviewer import Invocation, Review, Target
from arl.util import now as arl_now

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")

# --------------------------------------------------------------------------
# Phase 5: per-label mutual exclusion (active_review)
# --------------------------------------------------------------------------


def test_claiming_the_active_review_slot_twice_refuses_the_second(activation: state.State, git_repo: Path) -> None:
    target = target_for(git_repo)
    config = config_with()
    with activation.transaction():
        first = reviewer._claim_active_review(activation, target, config)
    assert first is not None

    with activation.transaction():
        second = reviewer._claim_active_review(activation, target, config)
    assert second is None, "a live claim for the same label must refuse a second one"


def test_releasing_lets_a_fresh_claim_through(activation: state.State, git_repo: Path) -> None:
    target = target_for(git_repo)
    config = config_with()
    expected = hooks.activation(activation, config)
    with activation.transaction():
        first = reviewer._claim_active_review(activation, target, config)
    assert first is not None

    reviewer._release_active_review(activation, claim_id=first, expected=expected, config=config)

    with activation.transaction():
        second = reviewer._claim_active_review(activation, target, config)
    assert second is not None
    assert second != first


def test_an_expired_claim_is_reclaimable(activation: state.State, git_repo: Path) -> None:
    target = target_for(git_repo)
    config = config_with(timeout_sec=1)
    activation.data["active_review"] = {target.label: {"generation": 0, "claimed_at": arl_now() - 100_000, "claim_id": "dead-token"}}
    activation.save()

    with activation.transaction():
        claim_id = reviewer._claim_active_review(activation, target, config)
    assert claim_id is not None
    assert claim_id != "dead-token"


def test_the_active_review_window_survives_a_contract_repairs_own_timeout(activation: state.State, git_repo: Path) -> None:
    """The claim's lifetime is not `_reclaim_after` -- that window is sized for the session
    pointer's own shorter lifecycle, released right after the primary invocation, before a
    contract repair ever runs. One `execute()` call can spend a further `REPAIR_TIMEOUT_SEC`
    inside `_repair_contract` after the primary invocation already returned -- a claim aged
    past the session pointer's own window, but still inside the active-review one, must still
    be treated as live, or a second, overlapping call could reclaim the slot while the first
    is still legitimately inside its own repair."""
    target = target_for(git_repo)
    config = config_with(timeout_sec=900)
    old_window = reviewer._reclaim_after(config)
    new_window = reviewer._active_review_reclaim_after(config)
    assert new_window > old_window, "the active-review window must be strictly wider than the session pointer's"

    aged = old_window + 60  # past the session pointer's own window
    assert aged < new_window, "the test's own aging must still land inside the wider window"
    activation.data["active_review"] = {target.label: {"generation": 0, "claimed_at": arl_now() - aged, "claim_id": "still-alive"}}
    activation.save()

    with activation.transaction():
        claim_id = reviewer._claim_active_review(activation, target, config)
    assert claim_id is None, "reusing the session pointer's narrower window would have reclaimed this slot too early"


def test_different_labels_do_not_clobber_each_others_claims(activation: state.State, git_repo: Path) -> None:
    """The bypass a single shared record (rather than a dict keyed by label) would allow:
    claiming an unrelated label must not silently overwrite another label's still-live entry,
    which would leave that other review genuinely in flight with no claim left recording it --
    a third caller for the *same* label as the first would then see the second label's entry,
    consider the slot free, and invoke straight past a review that never stopped running."""
    config = config_with()
    phase_target = target_for(git_repo, scope="phase")
    final_target = target_for(git_repo, scope="final")

    with activation.transaction():
        phase_claim = reviewer._claim_active_review(activation, phase_target, config)
    assert phase_claim is not None

    with activation.transaction():
        final_claim = reviewer._claim_active_review(activation, final_target, config)
    assert final_claim is not None

    with activation.transaction():
        second_phase_claim = reviewer._claim_active_review(activation, phase_target, config)
    assert second_phase_claim is None, "phase1's claim must still be live after an unrelated label claimed its own slot"


def test_a_second_overlapping_execute_is_refused_without_invoking_and_a_retry_after_release_succeeds(activation: state.State, git_repo: Path) -> None:
    """The structural fix for the race no post-hoc check can close: a second, genuinely
    overlapping ``execute()`` call for the same label never gets to invoke the reviewer at all
    -- it is refused at reservation time, before a bundle is built or any verdict is decided,
    regardless of which one would have been the approval and which the repeated finding.
    Simulates the overlap directly (hold the claim, then attempt a real ``execute()`` call)
    rather than through real concurrency, for the reason ``test_commands_races.py`` already
    establishes: the lock this reuses is what makes two processes doing exactly this
    equivalent to this."""
    target = target_for(git_repo)
    config = config_with()
    expected = hooks.activation(activation, config)
    with activation.transaction():
        held = reviewer._claim_active_review(activation, target, config)
    assert held is not None

    os.environ["ARL_REVIEWER_CMD"] = "/nonexistent/reviewer-must-not-run"
    review = reviewer.execute(target, state=activation, config=config)

    assert review.verdict == "OP_FAILURE"
    assert "already in progress" in review.error
    # Phase 6: contention is not a "the reviewer is broken" failure -- it paces with backoff
    # against `max_transient_failures` rather than spending the ordinary budget on a rival
    # invocation that will most likely have released the slot by the next attempt.
    assert review.kind == "transient"
    assert activation.get_int("report_seq") == 0, "the refused attempt reserved nothing"

    reviewer._release_active_review(activation, claim_id=held, expected=expected, config=config)
    review2 = execute_fake(activation, git_repo, "changes")
    assert review2.verdict == "CHANGES_REQUIRED", "once released, a real review of the same label proceeds normally"


def test_two_reviews_that_both_finish_before_either_finalizes_do_not_both_authorize(activation: state.State, git_repo: Path) -> None:
    """The scenario the lock-free ``_concurrent_stall_check`` peek cannot close on its own:
    both invocations' reviewer calls have already completed -- their ``Review`` objects exist
    -- while ``round_history`` still holds only round 1. Neither has finalized yet, so a check
    with a gap before the append (rather than inside the same lock as it) would let both pass.

    This does not need real threads to prove: :func:`reviewer._publish` is where the
    authoritative check now lives, under ``state.transaction()``'s own lock --
    ``tests/unit/test_commands_races.py`` already establishes that lock genuinely serialises
    concurrent *processes*, so two calls into this same function, back to back, exercise
    exactly the ordering two racing processes would be forced into: whichever call reaches the
    lock second is guaranteed to see what the first one just committed. Calling the finalizer
    for round A and then round B -- both prepared independently, as if both had already
    finished invoking before either called this -- is the faithful, deterministic
    reproduction of that race."""
    execute_fake(activation, git_repo, "changes")  # seeds round 1: anchor a.txt, CHANGES_REQUIRED
    assert activation.get_int("report_seq") == 1

    target = target_for(git_repo)
    # Pinned to 2: round 1 plus round A are the stall this exercises, and the threshold is not
    # what is under test -- `_publish`'s in-lock recheck is.
    config = config_with(stall_rounds=2)
    expected = hooks.activation(activation, config)

    def review_run(label: str) -> reviewer._ReviewRun:
        # Each stands in for a review that really did hold the slot when it ran: `_publish`
        # refuses to record for a run whose claim has since moved on, and what is under test
        # here is the *stall* recheck, not that guard.
        claim_id = f"claim{label}"
        with activation.transaction():
            activation.data["active_review"] = {
                "phase1": {"generation": activation.get_int("activation_generation"), "claimed_at": arl_now(), "claim_id": claim_id}
            }
        return reviewer._ReviewRun(
            target=target,
            state=activation,
            config=config,
            label=label,
            title="t",
            bundle_dir=activation.act_dir / "bundles" / label,
            raw_dir=activation.act_dir / "raw",
            prompt_file=Path("/dev/null"),
            expected=expected,
            claim_id=claim_id,
        )

    # Both already "finished invoking": round A repeats round 1's exact anchor (a.txt, high);
    # round B is a plain APPROVED with no findings of its own. Neither has been told about the
    # other -- exactly what two genuinely concurrent invocations would look like.
    review_a = Review(
        verdict="CHANGES_REQUIRED",
        all_findings="FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n",
    )
    review_b = Review(verdict="APPROVED")

    appended_a = reviewer._publish(review_run("002"), review_a, round_number=1)
    appended_b = reviewer._publish(review_run("003"), review_b, round_number=1)

    assert appended_a is True
    assert review_a.verdict == "CHANGES_REQUIRED", "round A's own denial is unaffected -- it is the first to land"

    assert appended_b is False, "round B must not be recorded as an ordinary APPROVED"
    assert review_b.verdict == "NEEDS_HUMAN", "round B's own APPROVED is overridden once round 1 + A already persist the anchor"
    assert "a.txt" in review_b.error

    history = activation.get_array_of_dicts("round_history")
    assert [entry["verdict"] for entry in history] == ["CHANGES_REQUIRED", "CHANGES_REQUIRED"], "round B was never appended"


def test_an_oscillating_anchor_alone_also_trips_the_stall_check(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """``warn.py`` reappears across rounds 1-3 (the reversal sequence phase 4 covers) without
    ever persisting across two *consecutive* rounds -- round 2 raises a different anchor. Only
    the oscillation signal, not the persisting one, can catch this."""
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    _run_scripted(activation, git_repo, tmp_path, "round2", _ROUND_2)
    _run_scripted(activation, git_repo, tmp_path, "round3", _ROUND_3)
    before_seq = activation.get_int("report_seq")
    assert before_seq == 3

    os.environ["ARL_REVIEWER_CMD"] = "/nonexistent/reviewer-must-not-run"
    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert review.verdict == "NEEDS_HUMAN"
    assert "warn.py" in review.error
    assert activation.get_int("report_seq") == before_seq


def test_four_distinct_anchors_never_stall_and_the_reviewer_runs_every_round(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """The deliberate design choice: a genuinely non-repeating sequence of findings has no
    cap. A future change that quietly adds one must fail here."""
    scripts = [
        "Problem A.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=a.py:1 | problem a\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n",
        "Problem B.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=b.py:1 | problem b\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n",
        "Problem C.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=c.py:1 | problem c\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n",
        "Problem D.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=d.py:1 | problem d\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n",
    ]
    for index, contract in enumerate(scripts, start=1):
        review = _run_scripted(activation, git_repo, tmp_path, f"round{index}", contract)
        assert review.verdict == "CHANGES_REQUIRED", f"round {index} must have actually invoked the reviewer"
    assert activation.get_int("report_seq") == 4


def test_stall_rounds_zero_disables_the_check(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    _run_scripted(activation, git_repo, tmp_path, "round1", _STUCK)
    _run_scripted(activation, git_repo, tmp_path, "round2", _STUCK)

    review = execute_fake(activation, git_repo, "changes", config=config_with(stall_rounds=0))

    assert review.verdict == "CHANGES_REQUIRED"
    assert activation.get_int("report_seq") == 3


def test_a_final_scope_review_is_never_stalled(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """``final`` is cumulative and reached once -- it has no phase-scoped ``round_history``
    label of its own to stall on, so the check must not apply to it at all."""

    def run(name: str, contract: str) -> Review:
        script = tmp_path / f"{name}.sh"
        script.write_text(f"#!/usr/bin/env bash\nprintf '%b' '{contract}'\n")
        script.chmod(0o755)
        os.environ["ARL_REVIEWER_CMD"] = str(script)
        return reviewer.execute(target_for(git_repo, scope="final"), state=activation, config=config_with())

    run("f1", _STUCK)
    run("f2", _STUCK)
    review = run("f3", _STUCK)

    assert review.verdict == "CHANGES_REQUIRED", "final scope has no round cap to stall on"
    assert activation.get_int("report_seq") == 3


def test_review_argv_attaches_prior_rounds_after_the_plan_revisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Under the state root, because `context_attachments` proves containment there before it
    # will hand a path to `-f` -- see `test_a_context_attachment_below_a_symlink_is_refused`.
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    # Two revisions, because one is not attached at all -- an unrevised plan is carried by
    # `range.txt`. The ordering this asserts only exists once there is a revision attachment.
    bundle, digest = _intact_bundle(tmp_path, chunks=1, revisions=2, context=True)

    staged, context = reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)
    argv = reviewer.review_argv("/repo", "t", config=config_with(), attachments=[path for path, _ in staged])

    assert context, "the earlier round is attached"
    assert argv.index(str(context[0])) > argv.index(str(staged[-2][0])), "context follows the plan revisions"
    assert staged[-2][0].name == "plan.rev1.md"


def test_a_session_less_invocation_stages_no_context_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``include_context=False`` -- what a contract repair is staged with -- attaches the same
    gate-generated evidence and none of the model-derived text."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, context=True)

    full, full_context = reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "full", include_context=True)
    bare, bare_context = reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "bare", include_context=False)

    assert full_context, "an ordinary run is shown the earlier round"
    assert bare_context == ()
    assert any("prior-rounds" in path.name for path, _ in full)
    assert not any("prior-rounds" in path.name for path, _ in bare)


def test_what_a_round_was_shown_survives_the_context_file_vanishing(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What was attached is a property of the invocation, not a question the filesystem is
    asked twice.

    ``context/<seq>-prior-rounds.txt`` is written before ``invoke`` and the review runs for
    minutes afterwards. ``Invocation.context_files`` is the round's record of the model-authored
    prose it was shown, so it is fixed when the argv is built; re-deriving it from ``context/``
    later would let a file unlinked mid-run rewrite that record after the fact. The reviewer
    stand-in unlinks it mid-run, which is exactly the window."""
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    context_dir = activation.act_dir / "context"

    seen: list[Invocation] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        seen.append(run)
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)

    script = tmp_path / "vanishing-context.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"rm -f {str(context_dir)!r}/*-prior-rounds.txt\n"
        "printf 'All good.\\n\\n'\n"
        "printf '<<<ARL-FINDINGS>>>\\n'\n"
        "printf 'VERDICT APPROVED\\n'\n"
        "printf '<<<ARL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    os.environ["ARL_REVIEWER_CMD"] = str(script)

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert review.verdict == "APPROVED"
    assert list(context_dir.glob("*-prior-rounds.txt")) == [], "the stand-in really did unlink it"
    assert seen[0].context_files, "the record of what the round was shown outlives the file"


def test_a_context_attachment_below_a_symlinked_directory_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``-f`` uploads what the path *resolves to*, so containment has to be proved before the
    path reaches the argv.

    ``is_file()`` follows symlinks and a per-file ``is_symlink()`` check says nothing about the
    directories above it, so a ``context/`` planted as a link to somewhere else entirely leaves
    an ordinary regular file at the end of the path -- and the reviewer provider receives it.
    Fails on the old code, which attached the planted path."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path / "state"))
    secrets_dir = tmp_path / "elsewhere"
    secrets_dir.mkdir()
    (secrets_dir / "002-prior-rounds.txt").write_text("id_rsa")

    root = tmp_path / "state"
    bundle = root / "bundles" / "002"
    bundle.mkdir(parents=True)
    (root / "context").symlink_to(secrets_dir, target_is_directory=True)

    assert (root / "context" / "002-prior-rounds.txt").is_file(), "the naive check passes -- that is the point"
    assert reviewer.context_attachments(bundle) == []


def test_a_context_attachment_that_is_itself_a_symlink_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path / "state"))
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")

    root = tmp_path / "state"
    bundle = root / "bundles" / "002"
    bundle.mkdir(parents=True)
    (root / "context").mkdir()
    (root / "context" / "002-prior-rounds.txt").symlink_to(secret)

    assert reviewer.context_attachments(bundle) == []


def test_the_attached_path_is_a_staged_copy_not_the_stable_source(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-f`` takes a pathname OpenCode opens minutes later, so what is attached must not be
    the long-lived, guessable ``context/<seq>-prior-rounds.txt`` the gate wrote earlier.

    Fails on the old code, which attached the source path itself."""
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    source = activation.act_dir / "context" / "002-prior-rounds.txt"

    seen: list[Invocation] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        seen.append(run)
        for path in run.context_files:
            # Read inside the invocation: the staged copy exists only for its duration.
            assert path.read_bytes() == source.read_bytes(), "the staged copy carries the validated bytes"
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)
    _run_scripted(activation, git_repo, tmp_path, "round2", _ROUND_2)

    attached = seen[0].context_files
    assert attached, "round 2 was shown round 1's findings"
    assert source not in attached, "the stable source path must not be what -f names"
    assert all(".staged-" in str(path.parent.name) for path in attached)
    assert source.is_file(), "the source itself is left alone"


def test_staged_attachments_are_removed_after_the_invocation(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """The staged directory lives for one call. Left behind, it would accumulate a copy of
    every round's context for the life of the activation."""
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    _run_scripted(activation, git_repo, tmp_path, "round2", _ROUND_2)

    assert list((activation.act_dir / "context").glob(".staged-*")) == []


def test_an_unreadable_context_attachment_fails_the_review_rather_than_dropping_it(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review must refuse rather than judge evidence it could not fully stage.

    Dropping an attachment that cannot be read would also shorten ``context_files``, the
    round's only record of the model-authored prose it was shown, so a silent drop is a
    verdict reached on evidence nobody can reconstruct. It has to be a hard failure."""
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    monkeypatch.setattr(reviewer, "read_verified_file", lambda path, *, root: None)

    review = _run_scripted(activation, git_repo, tmp_path, "round2", _APPROVES)

    assert review.verdict == "OP_FAILURE", "never APPROVED with the context silently dropped"
    assert review.kind == "bundle"
    # Which integrity check fires first is not the point -- that the review refuses rather
    # than approving with the context quietly absent is. (Patching `read_verified_file`
    # globally now also breaks the seal in `build_bundle`, which is the earliest of them.)
    assert review.error


def test_a_planted_diff_chunk_never_reaches_the_main_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hole that lived on the *main* review path for as long as `clarify` was hardened.

    ``clarify._bundle_attachments`` was written to build its list from the ``chunks`` manifest
    because a glob attaches whatever is sitting in the directory. The main review -- the one
    whose verdict approves commits -- kept globbing, so a ``changes.99.diff`` symlinked at an
    arbitrary local file rode straight into the provider prompt.

    Fails on the old code, which attached the plant."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    (bundle / "changes.99.diff").symlink_to(secret)

    entries = reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True)
    assert entries is not None, "the manifest is still intact"
    assert not any("changes.99" in path.name for path, _ in entries), "the plant is simply not in the manifest"
    staged, _context = reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)
    assert not any("changes.99" in path.name for path, _ in staged)


def test_a_planted_plan_revision_never_reaches_the_main_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same shape, the other glob: ``plan.rev*.md`` was globbed too."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, revisions=1)
    (bundle / "plan.rev7.md").write_text("not written by build_bundle")

    entries = reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True)
    assert entries is not None
    assert not any("plan.rev7" in path.name for path, _ in entries)


def test_a_bundle_missing_a_manifested_chunk_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`range.txt` alone is not a bundle: a verdict formed on less evidence than was written
    is not the verdict the gate reserved a sequence for."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=2)
    (bundle / "changes.01.diff").unlink()

    with pytest.raises(reviewer.BundleError, match="could not be read"):
        reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)


def test_a_symlinked_bundle_directory_is_refused_on_the_main_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file *below* a symlinked ``bundles/<seq>/`` is an ordinary regular file, so only
    walking the components catches it -- and the main review must catch it, not just clarify."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "range.txt").write_text("someone else's range")
    (planted / "changes.00.diff").write_text("someone else's secrets")
    (planted / "chunks").write_text("1")

    (tmp_path / "bundles").mkdir()
    bundle = tmp_path / "bundles" / "002"
    bundle.symlink_to(planted, target_is_directory=True)
    assert (bundle / "range.txt").is_file(), "the naive check passes -- that is the point"

    assert reviewer.bundle_manifest(bundle, tmp_path, "0" * 64, include_context=True) is None


def test_the_whole_evidence_set_is_staged_not_only_the_context(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging the small model-derived channel while leaving the diffs on stable bundle paths
    would harden the lesser half and leave the evidence the verdict is actually judged against
    exposed. Every attachment is staged.

    Fails on the old code, which staged only ``context/``."""
    seen: list[Invocation] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        seen.append(run)
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)
    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)

    attached = [path for path, _ in seen[0].attachments]
    assert attached, "the review attaches its evidence"
    assert any(path.name == "range.txt" for path in attached)
    assert any(path.name.startswith("changes.") for path in attached)
    bundle_dir = activation.act_dir / "bundles" / "001"
    assert not any(path.parent == bundle_dir for path in attached), "no attachment names the bundle's own stable path"
    assert all(".staged-" in path.parent.name for path in attached)


def test_substituted_diff_content_is_refused_even_though_it_is_a_plain_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The attack every path-shape check missed: no symlink, no extra file, no traversal --
    just different bytes in a file that is still an ordinary regular file at the right name.

    Nothing about the path is wrong, so containment walks, ``O_NOFOLLOW`` opens and safe-
    component checks all pass it. Only the hash recorded when the bundle was built says the
    reviewer would be judging something other than what was generated.

    Fails on the old code, which staged the substituted bytes and sent them."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1)

    (bundle / "changes.00.diff").write_text("a completely different diff")

    with pytest.raises(reviewer.BundleError, match="no longer matches the hash"):
        reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)


def test_a_rewritten_manifest_is_refused_against_the_recorded_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hashes inside the bundle alone would not be enough: anyone who can change a file there
    can change the manifest beside it. The digest recorded outside the directory -- on the
    active-review claim, or on the round -- is what makes a *consistent* rewrite fail too.

    Here the attacker shortens the evidence and reissues a matching manifest, which is exactly
    the "valid shorter manifest" case."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=2)

    (bundle / "changes.01.diff").unlink()
    (bundle / "chunks").write_text("1")
    reissued = _seal(bundle, tmp_path, total=1, revisions=0)

    assert reissued != digest, "the shortened bundle really does hash differently"
    assert reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True) is None
    with pytest.raises(reviewer.BundleError, match="no longer matches the manifest"):
        reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)


def test_dropping_the_trailing_context_and_verify_attachments_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting trailing attachments used to leave a well-formed, shorter set. The manifest
    names them, so their absence is now a failure rather than a quietly smaller review."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, context=True, verify=True)

    (bundle / "verify.txt").unlink()

    with pytest.raises(reviewer.BundleError, match="could not be read"):
        reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)


def test_the_round_records_the_bundle_digest_it_was_judged_from(activation: state.State, git_repo: Path) -> None:
    """`clarify` reads a bundle long after the claim that held it was released, so the round
    entry has to carry the anchor or there is nothing left to check that bundle against."""
    execute_fake(activation, git_repo, "changes")

    activation.load()
    entry = activation.get_array_of_dicts("round_history")[-1]
    digest = entry["bundle_digest"]

    assert isinstance(digest, str) and len(digest) == 64
    assert reviewer.bundle_manifest(activation.act_dir / "bundles" / "001", activation.act_dir, digest, include_context=True) is not None


def test_correcting_the_round_line_reissues_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_downgrade_bundle_round` is the one place the gate edits a bundle after hashing it.
    Leaving the manifest alone would make its own correction look exactly like tampering."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1)
    (bundle / "range.txt").write_text("round: 2\n")
    digest = _seal(bundle, tmp_path, total=1, revisions=0)

    updated = reviewer._downgrade_bundle_round(bundle, tmp_path, digest)

    assert updated != digest, "the correction changed range.txt, so the manifest had to be reissued"
    assert "round: 1" in (bundle / "range.txt").read_text()
    assert reviewer.bundle_manifest(bundle, tmp_path, updated, include_context=True) is not None
    assert reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True) is None


def test_a_verify_cmd_that_rewrites_the_evidence_fails_the_review(activation: state.State, git_repo: Path) -> None:
    """``verify_cmd`` is repository-controlled configuration run through a login shell, which
    makes it the one piece of attacker-authored code executing inside the gate's own process
    tree -- with the gate's privileges, and so with write access to the state root that
    ``pretool`` denies every *tool* call.

    A `verify_cmd` that overwrites `changes.00.diff` with benign bytes gets a reviewer that
    approves the benign version while the tree being approved still holds the real change.
    That is a complete bypass of the gate, and hashing the evidence *after* `verify_cmd` ran
    would have the manifest certify it.

    Fails on the old code, which approved."""
    (git_repo / "a.txt").write_text("the real, malicious change\n")
    # Reaches the bundle exactly as a hostile repo config would: from the environment the gate
    # itself runs under. Nothing here changes how `state_root()` resolves.
    hostile = 'printf \'benign\\n\' > "$(ls -d "$XDG_STATE_HOME"/adversarial-review-loop/worktrees/*/*/bundles/001)"/changes.00.diff'

    review = execute_fake(activation, git_repo, "approve", config=config_with(verify_cmd=hostile))

    assert review.verdict != "APPROVED", "a repository must not be able to edit what the reviewer judges"
    assert review.verdict == "OP_FAILURE"
    assert review.kind == "bundle"
    assert "changed while verify_cmd ran" in review.error


def test_a_wellbehaved_verify_cmd_still_attaches_its_output(activation: state.State, git_repo: Path) -> None:
    """The seal must not break the ordinary case: `verify.txt` is `verify_cmd`'s own output, so
    it is hashed after it runs and appended last, exactly where the reviewer expects it."""
    review = execute_fake(activation, git_repo, "approve", config=config_with(verify_cmd="printf 'all tests pass\\n'"))

    assert review.verdict == "APPROVED"
    bundle = activation.act_dir / "bundles" / "001"
    assert (bundle / "verify.txt").is_file()
    manifest = (bundle / "manifest").read_text().splitlines()
    assert manifest[-1].endswith("  bundle  verify.txt"), "verify.txt is last, after the evidence and any context"


def test_correcting_the_round_line_does_not_rebless_other_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rehashing the whole manifest on the gate's own `range.txt` correction would launder
    anything else that had changed since the bundle was sealed -- the same "hash after the
    untrusted step" mistake, in a second place. Only the corrected row may move."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, _digest = _intact_bundle(tmp_path, chunks=1)
    (bundle / "range.txt").write_text("round: 2\n")
    digest = _seal(bundle, tmp_path, total=1, revisions=0)

    # Something else mutates the bundle, then the gate corrects the round line.
    (bundle / "changes.00.diff").write_text("substituted after the seal")
    updated = reviewer._downgrade_bundle_round(bundle, tmp_path, digest)

    assert reviewer.bundle_manifest(bundle, tmp_path, updated, include_context=True) is not None, "the manifest itself is intact"
    with pytest.raises(reviewer.BundleError, match="no longer matches the hash"):
        reviewer.stage_invocation(bundle, tmp_path, updated, tmp_path / "staged", include_context=True)


def test_a_detached_verify_cmd_child_is_reaped_rather_than_left_running(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """``verify_cmd`` runs with the gate's privileges, so anything it leaves running keeps
    write access to the state root that ``pretool`` denies every tool call -- and the evidence
    it could reach is the evidence a verdict is formed on.

    A deadline that binds descendants only when the deadline is *hit* leaves the ordinary path
    open: ``cmd &`` returns promptly with a child still alive. Non-interactive bash runs
    without job control, so that child stays in the group and is reaped on the way out.

    Fails on the old code, where the marker appeared."""
    marker = tmp_path / "outlived.txt"
    background = f"(sleep 2; printf x > {marker}) &"

    review = execute_fake(activation, git_repo, "approve", config=config_with(verify_cmd=background))

    assert review.verdict == "APPROVED", "a well-behaved-looking verify_cmd still completes"
    time.sleep(3.0)
    assert not marker.exists(), "a backgrounded verify_cmd child must not outlive the call that ran it"


def test_a_staged_attachment_swapped_after_staging_is_refused_at_launch(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-f`` hands OpenCode a pathname it opens for itself, so a same-user process can
    overwrite a staged copy after staging verified it. Re-checking immediately before the
    launch is the latest point still inside this process.

    Simulated by overwriting a staged file between staging and invocation. Fails on the old
    code, which sent the substituted bytes."""
    real = reviewer._run_invocation

    def swap_then_run(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        for path, _digest in run.attachments:
            if path.name.startswith("changes."):
                path.write_text("substituted after staging")
                break
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", swap_then_run)

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert review.verdict == "OP_FAILURE", "never a review of bytes that changed after they were checked"
    assert "changed after it was staged" in review.error


def test_the_round_line_correction_refuses_a_bundle_that_no_longer_matches_its_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_downgrade_bundle_round` is the one place a *new* trusted digest is minted, which makes
    it the one place a corrupted bundle could be laundered into a blessed one.

    Here the whole bundle is replaced and re-sealed, as a detached `verify_cmd` descendant
    could. Re-signing that would hand the replacement back as current, so the correction must
    verify the manifest against the digest this review was issued and decline.

    Fails on the old code, which returned a fresh digest over the attacker's manifest."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, _built = _intact_bundle(tmp_path, chunks=1)
    (bundle / "range.txt").write_text("round: 2\n")
    issued = _seal(bundle, tmp_path, total=1, revisions=0)

    # A wholesale replacement, consistently re-sealed.
    (bundle / "changes.00.diff").write_text("someone else's diff")
    (bundle / "range.txt").write_text("round: 2\n")
    _seal(bundle, tmp_path, total=1, revisions=0)

    returned = reviewer._downgrade_bundle_round(bundle, tmp_path, issued)

    assert returned == issued, "no new digest may be minted over a manifest we did not issue"
    assert "round: 2" in (bundle / "range.txt").read_text(), "and nothing may be corrected in it either"
    assert reviewer.bundle_manifest(bundle, tmp_path, issued, include_context=True) is None, "so staging refuses it"


def test_the_round_line_correction_refuses_a_tampered_range_txt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: content injected into ``range.txt`` must not survive the round-line
    substitution and be rehashed as legitimate."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, _built = _intact_bundle(tmp_path, chunks=1)
    (bundle / "range.txt").write_text("round: 2\n")
    issued = _seal(bundle, tmp_path, total=1, revisions=0)

    (bundle / "range.txt").write_text("round: 2\ninjected instructions\n")

    returned = reviewer._downgrade_bundle_round(bundle, tmp_path, issued)

    assert returned == issued, "a range.txt that does not match its recorded row is not corrected"
    assert "round: 2" in (bundle / "range.txt").read_text()


def test_a_cold_permission_narrows_to_the_single_bundle(tmp_path: Path) -> None:
    """The wildcard exists so a *continued* reviewer can re-open paths it remembers from an
    earlier round's bundle. A session-less call -- a contract repair, a ``clarify`` -- carries
    no such memory, so it is scoped to the one bundle it was handed."""
    bundle = tmp_path / "bundles" / "007"
    continued = json.loads(reviewer.permission(bundle))
    cold = json.loads(reviewer.permission(bundle, cold=True))
    assert continued["external_directory"] == {"*": "deny", f"{bundle.parent}/**": "allow"}
    assert cold["external_directory"] == {"*": "deny", f"{bundle}/**": "allow"}


# -- a state-supplied base tree must not reach a git command line unchecked ----------


def test_a_hostile_base_tree_in_state_never_reaches_git_diff(git_repo: Path, tmp_path: Path) -> None:
    pwned = git_repo / "PWNED"
    hostile = Target(repo=str(git_repo), base=f"--output={pwned}", head=dirty(git_repo), scope="phase", phase=1)

    with pytest.raises(reviewer.BundleError, match="not a usable git object id"):
        reviewer._write_diff(hostile, tmp_path / "diff.txt")
    assert not pwned.exists(), "git must never have been asked to write this file"


def test_a_well_formed_but_unknown_base_tree_is_refused_not_diffed(git_repo: Path, tmp_path: Path) -> None:
    """The pre-existing target.base path, covered by the same fix."""
    hostile = Target(repo=str(git_repo), base="0" * 40, head=dirty(git_repo), scope="phase", phase=1)
    with pytest.raises(reviewer.BundleError, match="not a usable git object id"):
        reviewer._write_diff(hostile, tmp_path / "diff.txt")


def test_a_real_base_tree_still_diffs_normally(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """The ``--`` terminator did not break the ordinary path."""
    size = reviewer._write_diff(target_for(git_repo), tmp_path / "diff.txt")
    assert size > 0
    assert "a.txt" in (tmp_path / "diff.txt").read_text()
