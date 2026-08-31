"""``resume`` -- a second arming path that must not lose the original baseline or approvals.

Every test here drives the real CLI through ``scripts/arl-bootstrap.py`` (or the real hook
entrypoints, for the retirement/resurrection tests), for the same reason ``test_commands_arm``
does: the property under test is "does an activation exist on disk afterwards, and does it say
the right thing", not a function's return value.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import git, run_bootstrap, run_hook
from test_commands_arm import _path_without_opencode, plan_file, probe_env, read_state, state_dir
from test_commands_posttool import COMMIT, confirm
from test_commands_pretool import armed, patch_state, payload, pretool
from test_commands_stop import ended, stop

from arl import gitsnap, paths
from arl.commands import hooks
from arl.commands import resume as resume_module

S1 = "s1"
S2 = "s2"


def resume(repo: Path, env: dict[str, str], session: str = S2, args: str = "") -> tuple[int, str]:
    proc = run_bootstrap(["resume", "--session", session, "--args", args], cwd=repo, env=env)
    return proc.returncode, proc.stdout


def resume_argv(repo: Path, env: dict[str, str], session: str, argv: list[str]) -> tuple[int, str]:
    proc = run_bootstrap(["resume", "--session", session, *argv], cwd=repo, env=env)
    return proc.returncode, proc.stdout


def arm(repo: Path, tmp_path: Path, env: dict[str, str], session: str = S1, extra_args: str = "") -> Path:
    plan = plan_file(tmp_path)
    proc = run_bootstrap(["arm", "--session", session, "--args", f"{plan} {extra_args}".strip()], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout
    return plan


def set_phases(repo: Path, env: dict[str, str], *phases: str) -> None:
    argv = ["set-phases"]
    for phase in phases:
        argv += ["--phase", phase]
    proc = run_bootstrap(argv, cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr


def active(repo: Path, tmp_path: Path, env: dict[str, str], *phases: str, extra_args: str = "") -> Path:
    plan = arm(repo, tmp_path, env, extra_args=extra_args)
    set_phases(repo, env, *(phases or ("phase one", "phase two", "phase three")))
    return plan


def commit_phase(repo: Path, env: dict[str, str], text: str = "work\n", session: str = S1) -> None:
    """Take a change through the pretool gate under ``session`` and land it, then confirm."""
    (repo / f"{session}-{text[:4]}.txt").write_text(text)
    verdict, _ = pretool(repo, env, command=COMMIT, session=session)
    assert verdict == "allow"
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "phase")
    code, _ = confirm(repo, env, command=COMMIT, session=session)
    assert code == 0


# --------------------------------------------------------------------------
# The ordinary cross-session resume
# --------------------------------------------------------------------------


def test_cross_session_resume_preserves_baseline_and_approvals(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    baseline = read_state(env, git_repo, S1)["baseline_tree"]
    commit_phase(git_repo, env)
    before = read_state(env, git_repo, S1)
    assert before["phase"] == 2

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert "RESUMED" in banner
    after = read_state(env, git_repo, S2)
    assert after["baseline_tree"] == baseline
    assert after["approved_trees"] == before["approved_trees"]
    assert after["phase"] == 2
    assert after["phases"] == before["phases"]
    assert after["session_id"] == S2
    assert after["resumed_from"] == S1
    assert after["resume_count"] == 1
    before_generation = before["activation_generation"]
    assert isinstance(before_generation, int)
    assert after["activation_generation"] == before_generation + 1

    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    assert (root / "sessions" / S2).read_text() == f"{git_repo}\n"
    assert (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").read_text() == S2 + "\n"

    # The successor is a live, working activation: phase 2 can be committed under it.
    commit_phase(git_repo, env, "second\n", session=S2)
    assert read_state(env, git_repo, S2)["phase"] == 3


def test_resume_carries_round_history_but_resets_the_convergence_counters(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``round_history`` is evidence -- carried forward like the reports. ``transient_failures``,
    ``retry_not_before`` and ``clarifications`` are counters -- a fresh run starts them at zero."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    history = [
        {"seq": 1, "label": "phase1", "phase": 1, "verdict": "CHANGES_REQUIRED", "findings": ["FINDING severity=high actionable=yes file=a | x"]}
    ]
    predecessor_path = state_dir(env, git_repo, S1) / "state.json"
    document = json.loads(predecessor_path.read_text())
    document.update(round_history=history, transient_failures=3, retry_not_before=9999999999, clarifications=2)
    predecessor_path.write_text(json.dumps(document))

    code, banner = resume(git_repo, env)
    assert code == 0, banner

    after = read_state(env, git_repo, S2)
    assert after["round_history"] == history, "evidence is carried into the successor untouched"
    assert after["transient_failures"] == 0
    assert after["retry_not_before"] == 0
    assert after["clarifications"] == 0


def test_the_predecessor_is_retired_and_denies_every_mutation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)

    code, _ = resume(git_repo, env)
    assert code == 0

    document = read_state(env, git_repo, S1)
    assert document["status"] == "RESUMED"
    assert document["resumed_into"] == S2

    verdict, reason = pretool(git_repo, env, tool="Write", session=S1)
    assert verdict == "deny"
    assert "retired" in reason
    assert S2 in reason

    # RESUMED is terminal like DISARMED/COMPLETE: the turn may end. Nothing here is unapproved
    # (commit_phase's tree was reviewed and confirmed), so nothing is reported either.
    response = stop(git_repo, env, session=S1)
    assert "decision" not in response
    ended(response)


def test_a_pending_approval_must_land_or_be_abandoned_before_resume_can_retire(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """This is what stands between resume and the resurrection ``posttool._advance`` would
    otherwise risk: it must never retire a predecessor whose approval could still land and
    later confirm against a document that already says ``RESUMED`` -- if it did, ``_advance``
    would compare the confirmation's freshly-captured (already ``RESUMED``) expectation
    against an unchanged reload and see no divergence, and write ``status="ACTIVE"`` straight
    back over the retirement.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"

    refused_code, _ = resume(git_repo, env)
    assert refused_code == 1
    assert read_state(env, git_repo, S1)["status"] != "RESUMED"

    # Let the approval land normally, exactly as it would if the "old" session were simply
    # still working: the phase advances under S1, which is still the one live activation.
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase")
    confirm_code, _ = confirm(git_repo, env, command=COMMIT, session=S1)
    assert confirm_code == 0
    assert read_state(env, git_repo, S1)["phase"] == 2
    assert read_state(env, git_repo, S1)["pending_approved_tree"] == ""

    # Now resume succeeds, because there is nothing left in flight to lose.
    resumed_code, _ = resume(git_repo, env)
    assert resumed_code == 0
    assert read_state(env, git_repo, S1)["status"] == "RESUMED"


# --------------------------------------------------------------------------
# In-flight approvals
# --------------------------------------------------------------------------


def test_resume_refuses_while_an_approval_is_pending(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    assert read_state(env, git_repo, S1)["pending_approved_tree"]

    code, output = resume(git_repo, env)

    assert code == 1
    assert "pending" in output
    assert read_state(env, git_repo, S1)["status"] != "RESUMED"


def test_abandon_pending_clears_and_records_the_marker(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    pending_tree = read_state(env, git_repo, S1)["pending_approved_tree"]
    pending_head = read_state(env, git_repo, S1)["pending_head"]
    assert pending_tree

    code, banner = resume_argv(git_repo, env, S2, ["--abandon-pending", "--allow-dirty"])

    assert code == 0, banner
    predecessor = read_state(env, git_repo, S1)
    assert predecessor["status"] == "RESUMED"
    assert predecessor["abandoned_pending_tree"] == pending_tree
    successor = read_state(env, git_repo, S2)
    assert successor["abandoned_pending_tree"] == pending_tree
    assert successor["abandoned_pending_head"] == pending_head
    assert successor["pending_approved_tree"] == ""


def _approve_and_abandon(git_repo: Path, env: dict[str, str]) -> None:
    """Get S1 into "an approval is pending", then abandon it into S2 via resume."""
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    resume_argv(git_repo, env, S2, ["--abandon-pending", "--allow-dirty"])


def test_an_abandoned_commit_that_lands_anyway_is_caught_before_the_next_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The killed session's Bash call actually completes after the marker was recorded. This
    must fail on the code before this phase: without the pretool-side scan, the successor
    would approve the very next commit with no idea the marker's commit already exists."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    _approve_and_abandon(git_repo, env)

    # The abandoned command completes after all -- exactly the (parent, tree) the marker
    # names, since it stages nothing beyond what was already snapshotted and approved.
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase")

    (git_repo / "more.txt").write_text("more\n")
    verdict, reason = pretool(git_repo, env, command=COMMIT, session=S2)

    assert verdict == "deny"
    assert "landed after all" in reason
    assert read_state(env, git_repo, S2)["status"] == "RECONCILE"


def test_the_marker_does_not_fire_for_an_unrelated_later_commit(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The abandoned command never lands; ordinary work under the successor is unaffected."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    _approve_and_abandon(git_repo, env)
    (git_repo / "work.txt").unlink()  # the abandoned command never actually ran

    commit_phase(git_repo, env, "unrelated\n", session=S2)

    assert read_state(env, git_repo, S2)["status"] == "ACTIVE"
    assert read_state(env, git_repo, S2)["phase"] == 2


def test_the_marker_is_cleared_on_a_confirmed_match_not_a_false_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Rerunning the abandoned phase deterministically produces the same (parent, tree): the
    successor's own commit matches the marker exactly. That must advance normally, and the
    *next* commit must not be a stale false RECONCILE."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    _approve_and_abandon(git_repo, env)

    # S2 reruns the phase deterministically: same content, so the same tree on the same
    # parent -- the marker's exact (parent, tree). `work.txt` is already on disk from the
    # abandoned attempt, unchanged.
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S2)
    assert verdict == "allow"
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase")
    confirm_code, _ = confirm(git_repo, env, command=COMMIT, session=S2)
    assert confirm_code == 0

    successor = read_state(env, git_repo, S2)
    assert successor["abandoned_pending_tree"] == ""
    assert successor["phase"] == 2

    commit_phase(git_repo, env, "next\n", session=S2)
    assert read_state(env, git_repo, S2)["status"] == "ACTIVE"
    assert read_state(env, git_repo, S2)["phase"] == 3


# --------------------------------------------------------------------------
# Same-session resume
# --------------------------------------------------------------------------


def test_same_session_resume_updates_the_pause_target_in_place(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    before = read_state(env, git_repo, S1)

    code, banner = resume_argv(git_repo, env, S1, ["--until", "2"])

    assert code == 0, banner
    after = read_state(env, git_repo, S1)
    assert after["session_id"] == S1
    assert after["stop_after_phase"] == 2
    before_generation = before["activation_generation"]
    assert isinstance(before_generation, int)
    assert after["activation_generation"] == before_generation + 1
    # In-place: no successor directory, no new pointers.
    assert not (state_dir(env, git_repo, S2)).exists()


def test_same_session_resume_resets_the_convergence_counters_but_keeps_round_history(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str]
) -> None:
    """The in-place path is a fresh start too: an inherited retry backoff or an exhausted
    clarification budget must not survive it. ``round_history`` is evidence and stays."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    history = [{"seq": 1, "label": "phase1", "phase": 1, "verdict": "CHANGES_REQUIRED"}]
    path = state_dir(env, git_repo, S1) / "state.json"
    document = json.loads(path.read_text())
    document.update(round_history=history, transient_failures=4, retry_not_before=9999999999, clarifications=2)
    path.write_text(json.dumps(document))

    code, banner = resume_argv(git_repo, env, S1, ["--until", "2"])
    assert code == 0, banner

    after = read_state(env, git_repo, S1)
    assert after["transient_failures"] == 0
    assert after["retry_not_before"] == 0
    assert after["clarifications"] == 0
    assert after["round_history"] == history


def test_same_session_resume_refuses_a_pending_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"

    code, output = resume_argv(git_repo, env, S1, ["--until", "2"])

    assert code == 1
    assert "pending" in output
    assert read_state(env, git_repo, S1)["pending_approved_tree"]


def test_a_same_session_failure_leaves_the_live_document_untouched(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/stored\\n'\n")
    fake.chmod(0o755)
    env = probe_env(clean_env, bindir)
    active(git_repo, tmp_path, env, extra_args="--model vendor/stored")
    before = (state_dir(env, git_repo, S1) / "state.json").read_bytes()

    code, output = resume_argv(git_repo, env, S1, ["--model", "vendor/does-not-exist-anywhere"])

    assert code == 1
    assert 'the configured model "vendor/does-not-exist-anywhere" is not among the models opencode reports' in output
    after = (state_dir(env, git_repo, S1) / "state.json").read_bytes()
    assert after == before, "a same-session failure must write nothing at all"


# --------------------------------------------------------------------------
# Cross-session failures
# --------------------------------------------------------------------------


def test_nothing_armed_is_refused_without_writing_latest(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)

    code, output = resume(git_repo, env, session=S2)

    assert code == 1
    assert "no activation was ever armed" in output
    document = read_state(env, git_repo, S2)
    assert document["status"] == "ARM_FAILED"
    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    assert (root / "sessions" / S2).read_text() == f"{git_repo}\n"
    assert not (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").exists()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("COMPLETE", "already COMPLETE"),
        ("ARM_FAILED", "arming never completed"),
        ("NEEDS_HUMAN", "escalated to NEEDS_HUMAN"),
    ],
)
def test_terminal_statuses_refuse_and_leave_latest_alone(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str, expected: str
) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status, reason="because")

    code, output = resume(git_repo, env)

    assert code == 1
    assert expected in output
    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    assert (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").read_text() == S1 + "\n"


def test_an_already_resumed_activation_is_refused_naming_the_successor(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The loser of a race against a concurrent resume: it resolves the *same* predecessor
    (``latest`` had not yet been repointed at the winner's successor when it read it), finds
    RESUMED, and refuses -- without overwriting whatever ``latest`` already names."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    resume(git_repo, env, session=S2)
    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    latest = root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest"
    latest.write_text(f"{S1}\n")  # simulate the loser reading `latest` before the winner repointed it

    code, output = resume(git_repo, env, session="s3")

    assert code == 1
    assert "already retired" in output
    assert S2 in output
    assert latest.read_text() == f"{S1}\n"


# --------------------------------------------------------------------------
# History integrity and the dirty-worktree gate
# --------------------------------------------------------------------------


def test_rewritten_history_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    activation_commit = read_state(env, git_repo, S1)["activation_commit"]

    git(git_repo, "checkout", "--orphan", "rewritten")
    (git_repo / "new-root.txt").write_text("root\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "new root")

    code, output = resume(git_repo, env)

    assert code == 1
    assert "history was rewritten" in output
    assert str(activation_commit) in output


def test_a_dirty_worktree_is_refused_unless_allowed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "untracked.txt").write_text("wip\n")

    refused_code, refused_out = resume(git_repo, env)
    assert refused_code == 1
    assert "worktree is dirty" in refused_out

    allowed_code, allowed_out = resume_argv(git_repo, env, S2, ["--allow-dirty"])
    assert allowed_code == 0, allowed_out


def test_an_unapproved_head_warns_and_leaves_last_approved_tree_alone(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    last_approved = read_state(env, git_repo, S1)["last_approved_tree"]
    (git_repo / "unreviewed.txt").write_text("slipped in\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "not reviewed")

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert "WARNING" in banner
    assert "never approved" in banner
    # The warning names only the cover that always exists. Naming the cumulative review here
    # would be a promise `final_review` can withdraw, on the one banner a user reads when they
    # have just been told something unreviewed is in their history.
    assert "final cumulative review" not in banner
    assert "unreviewed-work sweep" in banner
    assert read_state(env, git_repo, S2)["last_approved_tree"] == last_approved


def test_a_same_session_resume_warns_about_an_unapproved_head_the_same_way(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The in-place path builds its own copy of the warning, so it needs its own assertion."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("slipped in\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "not reviewed")

    code, banner = resume_argv(git_repo, env, S1, [])

    assert code == 0, banner
    assert "never approved" in banner
    assert "final cumulative review" not in banner
    assert "unreviewed-work sweep" in banner


def test_the_unapproved_head_warning_names_the_sweep_when_no_phase_is_left(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """report 024: after the last phase there is no "next phase's review" to fold anything into.

    This warning is the one place a user is told their own history holds something no review
    approved, so it has to name cover that actually exists at that point: the turn-end sweep,
    which runs from the deliberately untouched ``last_approved_tree`` to whatever the worktree
    then holds.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, phase=2)
    (git_repo / "unreviewed.txt").write_text("slipped in\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "not reviewed")

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert "never approved" in banner
    assert "no phase is left" in banner
    assert "unreviewed-work sweep" in banner


# --------------------------------------------------------------------------
# --until, --model, --variant
# --------------------------------------------------------------------------


def test_until_carries_forward_when_not_given(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, extra_args="--until 2")
    assert read_state(env, git_repo, S1)["stop_after_phase"] == 2

    code, _ = resume(git_repo, env)

    assert code == 0
    assert read_state(env, git_repo, S2)["stop_after_phase"] == 2


def test_until_beyond_the_phase_count_is_clamped_with_a_warning(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, "one", "two")

    code, banner = resume_argv(git_repo, env, S2, ["--until", "99"])

    assert code == 0, banner
    assert "clamped to 2" in banner
    assert read_state(env, git_repo, S2)["stop_after_phase"] == 2


def test_a_model_override_is_probed_instead_of_the_stored_default(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``--model`` must be validated against itself: probing the stored model would pass on
    the strength of a model this resume will not use."""
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/stored\\n'\n")
    fake.chmod(0o755)
    env = probe_env(clean_env, bindir)
    active(git_repo, tmp_path, env, extra_args="--model vendor/stored")
    assert read_state(env, git_repo, S1)["overrides"] == {"harness": "opencode", "model": "vendor/stored"}

    code, output = resume_argv(git_repo, env, S2, ["--model", "vendor/not-reported"])

    assert code == 1
    assert 'the configured model "vendor/not-reported" is not among the models opencode reports' in output
    assert read_state(env, git_repo, S2)["status"] == "ARM_FAILED"
    assert "vendor/stored" not in output


def test_model_and_variant_overrides_are_persisted_and_merged_over_the_stored_ones(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, extra_args="--model vendor/original")
    assert read_state(env, git_repo, S1)["overrides"] == {"harness": "claude-code", "model": "vendor/original"}

    code, _ = resume_argv(git_repo, env, S2, ["--variant", "fast"])

    assert code == 0
    assert read_state(env, git_repo, S2)["overrides"] == {"harness": "claude-code", "model": "vendor/original", "variant": "fast"}


# --------------------------------------------------------------------------
# Plan revision
# --------------------------------------------------------------------------


def test_an_edited_plan_is_auto_detected_and_recorded_as_a_new_revision(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    plan = active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    plan.write_text("# plan\n\nphase one, revised\n")

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert "changed just now" in banner
    document = read_state(env, git_repo, S2)
    revisions = document["plan_revisions"]
    assert isinstance(revisions, list)
    assert len(revisions) == 2
    assert revisions[-1]["file"] == "plan.rev1.md"
    assert (state_dir(env, git_repo, S2) / "plan.rev1.md").read_text() == plan.read_text()
    assert (state_dir(env, git_repo, S2) / "plan.frozen.md").read_text() != plan.read_text()


def test_a_plan_revision_requires_a_clean_worktree_even_with_allow_dirty(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    plan = active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    plan.write_text("# plan\n\nrevised\n")
    (git_repo / "untracked.txt").write_text("wip\n")

    code, output = resume_argv(git_repo, env, S2, ["--allow-dirty"])

    assert code == 1
    assert "plan revision" in output
    assert "clean worktree" in output


def test_an_unchanged_plan_records_no_new_revision(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert "unchanged" in banner
    document = read_state(env, git_repo, S2)
    unchanged_revisions = document["plan_revisions"]
    assert isinstance(unchanged_revisions, list)
    assert len(unchanged_revisions) == 1


def test_an_explicit_plan_flag_forces_a_revision(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    new_plan = tmp_path / "revised-plan.md"
    new_plan.write_text("# a wholly different plan\n")

    code, banner = resume_argv(git_repo, env, S2, ["--plan", str(new_plan)])

    assert code == 0, banner
    document = read_state(env, git_repo, S2)
    forced_revisions = document["plan_revisions"]
    assert isinstance(forced_revisions, list)
    assert len(forced_revisions) == 2
    assert document["plan_path"] == str(new_plan)


# --------------------------------------------------------------------------
# Review findings: same-session DISARMED / STALE, missing plan.frozen.md, git
# failure during the abandoned-marker scan, publication-time dirty recheck.
# --------------------------------------------------------------------------


def test_same_session_resume_reactivates_a_deactivated_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A same-session resume of a ``deactivate``d activation must actually re-enforce the
    gate, not just print a RESUMED banner over a document still saying ``DISARMED``."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    deactivate_proc = run_bootstrap(["deactivate"], cwd=git_repo, env=env)
    assert deactivate_proc.returncode == 0, deactivate_proc.stdout
    assert read_state(env, git_repo, S1)["status"] == "DISARMED"

    # Under DISARMED the commit gate is a pure pass-through -- zero bytes, no decision to
    # parse -- and nothing is reviewed or recorded.
    (git_repo / "unreviewed.txt").write_text("slipped through\n")
    proc = run_hook("pretool", payload(git_repo, command=COMMIT, session=S1), cwd=git_repo, env=env)
    assert proc.returncode == 0 and proc.stdout == ""
    assert read_state(env, git_repo, S1)["pending_approved_tree"] == ""
    (git_repo / "unreviewed.txt").unlink()

    code, banner = resume_argv(git_repo, env, S1, [])

    assert code == 0, banner
    document = read_state(env, git_repo, S1)
    assert document["status"] == "ACTIVE"

    # The same commit shape is now actually gated: a real approval gets recorded.
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    assert read_state(env, git_repo, S1)["pending_approved_tree"] != ""


def test_same_session_resume_un_stales_an_expired_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env, ARL_TTL_HOURS="1")
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, armed_at=1)

    verdict, reason = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "deny"
    assert "ttl_hours" in reason

    code, banner = resume_argv(git_repo, env, S1, [])

    assert code == 0, banner
    document = read_state(env, git_repo, S1)
    assert isinstance(document["armed_at"], int)
    assert document["armed_at"] > 1

    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"


@pytest.mark.parametrize("missing", ["deleted", "symlinked-outside"])
def test_a_missing_or_symlinked_frozen_plan_refuses_rather_than_fabricating_a_revision(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], missing: str
) -> None:
    """The frozen plan is the evidence every review to date was run against. A version-2
    document that has never had a revision recorded on it (the ordinary case, since ``arm``
    does not record revision 0 until a later phase) must fail closed rather than synthesize
    revision 0 from an empty read."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    frozen = state_dir(env, git_repo, S1) / "plan.frozen.md"
    if missing == "deleted":
        frozen.unlink()
    else:
        outside = tmp_path / "outside.md"
        outside.write_text("# not the frozen plan\n")
        frozen.unlink()
        frozen.symlink_to(outside)

    code, output = resume(git_repo, env)

    assert code == 1
    assert "plan revision file" in output
    assert "missing, is a symlink, or is not a plain file" in output
    assert read_state(env, git_repo, S2)["status"] == "ARM_FAILED"


def test_git_failure_during_the_abandoned_marker_scan_denies_rather_than_passing(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable history must not read as "the abandoned commit didn't land"."""

    def broken_git_run(repo: str, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args and args[0] == "rev-list":
            return subprocess.CompletedProcess(args=["git", *args], returncode=128, stdout=b"", stderr=b"fatal: broken\n")
        return real_git_run(repo, args, **kwargs)  # type: ignore[arg-type]

    real_git_run = gitsnap.git_run
    monkeypatch.setattr(gitsnap, "git_run", broken_git_run)

    with pytest.raises(gitsnap.GitUnavailable):
        # A well-formed id, so the shape guard passes and the broken `rev-list` is what raises.
        hooks.find_abandoned_marker_commit(str(git_repo), activation_commit="a" * 40, marker_head="deadbeef", marker_tree="cafefeed")


def test_a_git_option_shaped_activation_commit_is_refused_before_rev_list(git_repo: Path) -> None:
    """state.json is not a trust boundary: a tampered `activation_commit` shaped like a git
    option must not be interpolated into `rev-list` argv."""
    with pytest.raises(gitsnap.GitUnavailable, match="not a usable git object id"):
        hooks.find_abandoned_marker_commit(str(git_repo), activation_commit="--output=/tmp/x", marker_head="a" * 40, marker_tree="b" * 40)


def test_publication_time_dirty_recheck_applies_even_without_a_plan_revision(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-check immediately before publication must enforce the *same* dirty policy as the
    earlier check, not only the unconditional one a decided plan revision imposes -- otherwise
    a worktree that goes dirty in the retirement window is published over regardless of
    --allow-dirty."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    real_worktree_clean = gitsnap.worktree_clean
    calls = {"n": 0}

    def dirtying_worktree_clean(repo: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        (git_repo / "appeared-during-publication.txt").write_text("late\n")
        return real_worktree_clean(repo)

    monkeypatch.setattr(gitsnap, "worktree_clean", dirtying_worktree_clean)
    # In-process rather than through run_bootstrap, so the monkeypatch above takes effect --
    # which means os.environ is overlaid, not replaced, so any ARL_*/XDG_* the host happens
    # to carry has to be cleared first (mirrors test_commands_arm's racing-arm test).
    for key in list(os.environ):
        if key.startswith(("ARL_", "XDG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(git_repo)

    rc = resume_module.run(["--session", S2])

    assert rc == 1
    document = read_state(env, git_repo, S2)
    assert document["status"] == "ARM_FAILED"
    assert "dirty" in str(document["reason"])


def test_an_unrecognised_stored_status_refuses_rather_than_being_treated_as_resumable(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str]
) -> None:
    """An allow-list, not a deny-list: a status this build has never written -- corruption, a
    future build, direct tampering, since state.json is explicitly not a trust boundary --
    must not be published into a successor and then treated as live by pretool."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="SOMETHING_NEVER_WRITTEN")

    code, output = resume(git_repo, env)

    assert code == 1
    assert "SOMETHING_NEVER_WRITTEN" in output
    assert "not one this build knows how to resume" in output
    assert read_state(env, git_repo, S2)["status"] == "ARM_FAILED"


def test_frozen_plan_integrity_is_verified_on_every_resume_not_only_the_first(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The gap a deleted-on-the-first-resume check does not close: once a revision (even the
    backfilled revision 0) is already recorded, a later resume must still verify the file it
    names, not skip straight past because the list is no longer empty."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)

    first_code, _ = resume(git_repo, env)
    assert first_code == 0
    first_revisions = read_state(env, git_repo, S2)["plan_revisions"]
    assert isinstance(first_revisions, list)
    assert len(first_revisions) == 1

    (state_dir(env, git_repo, S2) / "plan.frozen.md").unlink()
    second_code, output = resume_argv(git_repo, env, "s3", [])

    assert second_code == 1
    assert "plan revision file" in output
    assert "missing, is a symlink, or is not a plain file" in output
    assert read_state(env, git_repo, "s3")["status"] == "ARM_FAILED"


def test_a_broken_rev_parse_during_the_marker_scan_raises_git_unavailable(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-commit rev-parse failure (as opposed to rev-list itself failing) must be raised
    too, not folded into "no match" the way plain ``rev_parse`` would. ``pretool`` and
    ``stop._review`` already have end-to-end coverage for *catching* ``GitUnavailable`` from
    this same function (the rev-list-failure tests above), through the real subprocess
    entrypoint -- both catch it by type, not by which internal git call raised it, so what
    remains to prove here is that this specific call now raises it too."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "work.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    resume_argv(git_repo, env, S2, ["--abandon-pending", "--allow-dirty"])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase")  # the abandoned commit lands after all

    real_git_run = gitsnap.git_run

    def broken_rev_parse(repo: str, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args and args[0] == "rev-parse":
            return subprocess.CompletedProcess(args=["git", *args], returncode=128, stdout=b"", stderr=b"fatal: broken\n")
        return real_git_run(repo, args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gitsnap, "git_run", broken_rev_parse)

    document = read_state(env, git_repo, S2)
    with pytest.raises(gitsnap.GitUnavailable):
        hooks.find_abandoned_marker_commit(
            str(git_repo),
            activation_commit=str(document["activation_commit"]),
            marker_head=str(document["abandoned_pending_head"]),
            marker_tree=str(document["abandoned_pending_tree"]),
        )


def test_every_recorded_revision_is_verified_not_only_the_active_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The reviewer still always reads ``plan.frozen.md`` (revision 0) regardless of which
    revision is "active" -- disclosure of the active one is not wired until a later phase (see
    the banner's own note) -- so revision 0's integrity has to be checked even when a *later*
    revision is the one that changed and is being verified as active."""
    env = armed(clean_env)
    plan = active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    resume(git_repo, env)  # S1 -> S2: backfills revision 0

    plan.write_text("# revised\n")
    resume_argv(git_repo, env, "s3", [])  # S2 -> s3: records revision 1 as active
    revisions = read_state(env, git_repo, "s3")["plan_revisions"]
    assert isinstance(revisions, list)
    assert len(revisions) == 2

    (state_dir(env, git_repo, "s3") / "plan.frozen.md").unlink()
    code, output = resume_argv(git_repo, env, "s4", [])

    assert code == 1
    assert "plan revision file" in output
    assert "plan.frozen.md" in output
    assert read_state(env, git_repo, "s4")["status"] == "ARM_FAILED"


def test_a_symlink_within_the_activation_directory_does_not_substitute_a_revision(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A realpath-based containment check alone would accept this: the symlink's target still
    resolves *inside* the activation directory (another file this same activation legitimately
    wrote), so only requiring the named file itself to be a literal regular file -- not merely
    something that resolves under the directory -- catches it."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    resume(git_repo, env)  # S1 -> S2: backfills revision 0, records plan.frozen.md's hash

    act_dir = state_dir(env, git_repo, S2)
    decoy = act_dir / "decoy.md"
    decoy.write_text("# not the frozen plan, but lives inside the same activation directory\n")
    frozen = act_dir / "plan.frozen.md"
    frozen.unlink()
    frozen.symlink_to(decoy)

    code, output = resume_argv(git_repo, env, "s3", [])

    assert code == 1
    assert "is a symlink" in output
    assert read_state(env, git_repo, "s3")["status"] == "ARM_FAILED"


def test_an_abandoned_marker_does_not_wedge_an_empty_repository_before_the_root_commit_lands(tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Armed against an empty repository, the first-ever approval is abandoned before it lands:
    ``activation_commit`` and the marker's own recorded head are both empty, so the scan's
    range is ``HEAD`` on a still-unborn branch -- ``git rev-list HEAD`` fails there for a
    completely unrelated, legitimate reason, and that must not wedge the successor before its
    very first commit."""
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    env = armed(clean_env)
    plan = plan_file(tmp_path)
    # A brand-new repository's empty tree never equals its (nonexistent) HEAD tree, so
    # `worktree_clean` reports it dirty regardless of what is on disk -- unrelated to this
    # test, and pre-existing.
    proc = run_bootstrap(["arm", "--session", S1, "--args", f"{plan} --allow-dirty"], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(repo, env, "one")

    (repo / "first.txt").write_text("first\n")
    verdict, _ = pretool(repo, env, command=COMMIT, session=S1)
    assert verdict == "allow"
    assert read_state(env, repo, S1)["activation_commit"] == ""

    code, banner = resume_argv(repo, env, S2, ["--abandon-pending", "--allow-dirty"])
    assert code == 0, banner
    document = read_state(env, repo, S2)
    assert document["abandoned_pending_head"] == ""

    # The abandoned commit never actually landed -- HEAD is still unborn. A further commit
    # attempt under the successor must proceed normally, not deny on an unverifiable scan.
    verdict, reason = pretool(repo, env, command=COMMIT, session=S2)
    assert verdict == "allow", reason


def test_a_recorded_revision_with_no_valid_sha256_refuses_rather_than_skipping_verification(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str]
) -> None:
    """``expected_sha256=None`` is reserved for synthesizing a brand-new revision 0, where
    there is nothing yet to compare against. An *already recorded* entry with a missing or
    malformed ``sha256`` must not be read the same way -- that would silently skip verifying
    it entirely, accepting whatever the named file now contains."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    resume(git_repo, env)  # S1 -> S2: backfills revision 0 with a real sha256

    state_path = state_dir(env, git_repo, S2) / "state.json"
    document = json.loads(state_path.read_text())
    document["plan_revisions"][0].pop("sha256", None)
    state_path.write_text(json.dumps(document))
    (state_dir(env, git_repo, S2) / "plan.frozen.md").write_text("# substituted content\n")

    code, output = resume_argv(git_repo, env, "s3", [])

    assert code == 1
    assert "no valid sha256 recorded" in output


def test_evidence_corruption_escalates_the_live_activation_to_needs_human(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The gap a plain "same-session failure writes nothing" would leave open: without this
    escalation, the activation keeps reporting ACTIVE after the failed resume, and the
    *ordinary* commit path never verifies plan.frozen.md's integrity itself -- it would just
    go on consuming the corrupted file."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    resume(git_repo, env)  # S1 -> S2: backfills revision 0

    (state_dir(env, git_repo, S2) / "plan.frozen.md").unlink()

    same_session_code, _ = resume_argv(git_repo, env, S2, [])
    assert same_session_code == 1
    assert read_state(env, git_repo, S2)["status"] == "NEEDS_HUMAN"

    # The closed gap, proven end to end: a commit that would otherwise sail through the
    # ordinary review gate is now denied because the activation is NEEDS_HUMAN.
    (git_repo / "s2-more.txt").write_text("more\n")
    verdict, reason = pretool(git_repo, env, command=COMMIT, session=S2)
    assert verdict == "deny"
    assert "NEEDS_HUMAN" in reason


def test_evidence_corruption_escalates_the_predecessor_across_sessions_too(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The same escalation for the pre-retirement cross-session path: the corruption is
    discovered before any retirement happens, so it is the predecessor itself -- not a
    successor -- that must end up NEEDS_HUMAN."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    commit_phase(git_repo, env)
    resume(git_repo, env)  # S1 -> S2: backfills revision 0

    (state_dir(env, git_repo, S2) / "plan.frozen.md").unlink()

    code, _ = resume_argv(git_repo, env, "s3", [])

    assert code == 1
    assert read_state(env, git_repo, S2)["status"] == "NEEDS_HUMAN"
    # Retirement never happened -- the corruption was caught before dispatch -- so `latest`
    # still names the (now escalated) predecessor, not the failed session.
    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    assert (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").read_text() == S2 + "\n"


def test_a_genuine_git_failure_while_checking_for_an_unborn_head_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unborn-HEAD short-circuit must not itself become a way past a real git failure:
    only a *confirmed* unborn HEAD (exit 1, no stderr) may short-circuit to "no match"."""
    real_git_run = gitsnap.git_run

    def broken_git_run(repo: str, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if args[:1] == ["rev-parse"]:
            return subprocess.CompletedProcess(args=["git", *args], returncode=128, stdout=b"", stderr=b"fatal: broken\n")
        return real_git_run(repo, args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gitsnap, "git_run", broken_git_run)

    with pytest.raises(gitsnap.GitUnavailable):
        hooks.find_abandoned_marker_commit("/nonexistent", activation_commit="", marker_head="", marker_tree="deadbeef")


# --------------------------------------------------------------------------
# --harness
# --------------------------------------------------------------------------


def test_an_activation_keeps_its_harness_across_a_resume(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The stored override stands unless this resume names another: a resume that silently
    reverted to the default harness would move a live activation onto a reviewer nobody chose.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env, extra_args="--harness claude-code")
    assert read_state(env, git_repo, S1)["overrides"] == {"harness": "claude-code"}

    code, banner = resume(git_repo, env)

    assert code == 0, banner
    assert read_state(env, git_repo, S2)["overrides"] == {"harness": "claude-code"}
    assert "claude-code" in banner


def test_the_harness_can_be_switched_mid_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, extra_args="--harness claude-code")

    code, banner = resume_argv(git_repo, env, S2, ["--harness", "opencode"])

    assert code == 0, banner
    assert read_state(env, git_repo, S2)["overrides"] == {"harness": "opencode"}


def test_resuming_onto_an_unimplemented_harness_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The same hard refusal as at arm time, and the live activation is left untouched."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    before = (state_dir(env, git_repo, S1) / "state.json").read_bytes()

    code, output = resume_argv(git_repo, env, S2, ["--harness", "not-a-harness"])

    assert code == 1
    assert "unknown harness 'not-a-harness'" in output
    assert (state_dir(env, git_repo, S1) / "state.json").read_bytes() == before


def test_a_resume_pins_the_probed_harness_not_an_environment_masked_flag(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The same rule as ``arm``: ``ARL_HARNESS`` outranks the overlay, so a ``--harness`` it
    masks was never probed and must not be stored as though it had been."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    code, banner = resume_argv(git_repo, {**env, "ARL_HARNESS": "opencode"}, S2, ["--harness", "claude-code"])

    assert code == 0, banner
    assert read_state(env, git_repo, S2)["overrides"] == {"harness": "opencode"}
