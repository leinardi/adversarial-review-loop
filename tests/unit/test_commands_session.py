"""``set-phases``, ``defer``, ``status``, ``report``, ``finish`` and ``deactivate``.

Two of these end the mode, and Rule 4 says only the user may. What that means here is that
the tests check *state transitions*, not just output: ``deactivate`` must leave the session
pointer in place, and ``finish`` must reach ``COMPLETE`` only through an approving review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, git, run_bootstrap
from test_commands_arm import armed_env, plan_file, read_state, state_dir

from ocrl import paths


def arm(repo: Path, tmp_path: Path, env: dict[str, str], session: str = "s1") -> None:
    proc = run_bootstrap(["arm", "--session", session, "--plan", str(plan_file(tmp_path))], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout


def set_phases(repo: Path, env: dict[str, str], *phases: str) -> None:
    argv = ["set-phases"]
    for phase in phases:
        argv += ["--phase", phase]
    proc = run_bootstrap(argv, cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# set-phases
# --------------------------------------------------------------------------


def test_set_phases_freezes_the_list_and_starts_phase_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    proc = run_bootstrap(["set-phases", "--phase", "first thing", "--phase", "second thing"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "opencode-review-loop: 2 phases frozen. Now on phase 1 of 2:\n\n"
        "  1. first thing\n"
        "  2. second thing\n"
        "\nImplement phase 1, then commit it. The commit is the review gate.\n"
    )

    document = read_state(env, git_repo, "s1")
    assert document["status"] == "ACTIVE"
    assert document["phases"] == ["first thing", "second thing"]
    assert document["phase"] == 1
    assert (state_dir(env, git_repo, "s1") / "phases.frozen").read_text() == "first thing\nsecond thing\n"


def test_the_phase_list_cannot_be_rewritten(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The descriptions are evidence: a list that can be edited can be made to fit the work."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")

    proc = run_bootstrap(["set-phases", "--phase", "something else"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the phase list is already frozen" in proc.stderr
    assert read_state(env, git_repo, "s1")["phases"] == ["first thing"]


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        pytest.param([], 'at least one --phase "…" is required', id="none"),
        pytest.param([""], "empty phase description", id="empty"),
        pytest.param(["   "], "empty phase description", id="spaces"),
        pytest.param([f"phase {n}" for n in range(65)], "is more than the 64 this gate accepts", id="too-many"),
    ],
)
def test_set_phases_rejects_an_unusable_list(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    phases: list[str],
    expected: str,
) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    argv = ["set-phases"]
    for phase in phases:
        argv += ["--phase", phase]
    proc = run_bootstrap(argv, cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert expected in proc.stderr
    assert read_state(env, git_repo, "s1")["status"] == "ARMED"


def test_set_phases_without_an_activation_says_so(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["set-phases", "--phase", "one"], cwd=git_repo, env=armed_env(clean_env))

    assert proc.returncode == 1
    assert "no activation found for this worktree" in proc.stderr


def test_set_phases_is_refused_after_arming_failed(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A failed arming froze nothing, so there is no plan for a phase list to describe."""
    env = armed_env(clean_env)
    run_bootstrap(["arm", "--session", "s1", "--plan", "does-not-exist.md"], cwd=git_repo, env=env)

    proc = run_bootstrap(["set-phases", "--phase", "one"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "cannot set phases while the activation is ARM_FAILED" in proc.stderr


# --------------------------------------------------------------------------
# defer
# --------------------------------------------------------------------------


def test_defer_is_counted_and_bounded(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """ "Let me ask the user something" is also the shape of an agent that has stalled."""
    env = armed_env(clean_env, OCRL_MAX_DEFERS="2")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")

    first = run_bootstrap(["defer", "--reason", "need a decision"], cwd=git_repo, env=env)
    assert first.returncode == 0
    assert first.stdout == "opencode-review-loop: turn end deferred (1 of 2). Reason recorded: need a decision\n"
    document = read_state(env, git_repo, "s1")
    assert document["defers"] == 1
    assert document["defer_pending"] is True
    assert document["reason"] == "deferred: need a decision"

    assert run_bootstrap(["defer", "--reason", "again"], cwd=git_repo, env=env).returncode == 0

    third = run_bootstrap(["defer", "--reason", "and again"], cwd=git_repo, env=env)
    assert third.returncode == 1
    assert "2 defers already used (limit 2)" in third.stderr
    assert read_state(env, git_repo, "s1")["defers"] == 2


def test_defer_without_an_activation_fails(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["defer", "--reason", "x"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 1
    assert "nothing armed in this worktree" in proc.stderr


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_status_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["status"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 0
    assert proc.stdout == "opencode-review-loop: not armed in this worktree.\n"


def test_status_reports_the_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing", "second thing")

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert f"worktree:            {git_repo}\n" in proc.stdout
    assert "session:             s1\n" in proc.stdout
    assert "status:              ACTIVE\n" in proc.stdout
    assert f"baseline tree:       {git(git_repo, 'rev-parse', 'HEAD^{tree}')}\n" in proc.stdout
    assert "phase:               1 of 2\n" in proc.stdout
    assert "consecutive failures:0 / 2\n" in proc.stdout
    assert "phases:\n  1. first thing\n  2. second thing\n\nreports:\n\n" in proc.stdout


def test_status_shows_the_effective_status_alongside_the_stored_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """An expired activation reads as STALE, and STALE blocks -- it never silently disarms."""
    env = armed_env(clean_env, OCRL_TTL_HOURS="1")
    arm(git_repo, tmp_path, env)

    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["armed_at"] = 1
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)
    assert "status:              STALE (stored: ARMED)\n" in proc.stdout


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def test_report_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["report"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 0
    assert proc.stdout == "opencode-review-loop: not armed in this worktree.\n"


def test_report_with_no_reports_yet(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    proc = run_bootstrap(["report"], cwd=git_repo, env=env)
    assert proc.returncode == 0
    assert proc.stdout == "No reports have been produced for this activation yet.\n"


def test_report_prints_the_newest_or_the_numbered_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    reports = state_dir(env, git_repo, "s1") / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "001-phase1-approved.md").write_text("first report\n")
    (reports / "002-final-changes_required.md").write_text("second report\n")

    assert run_bootstrap(["report"], cwd=git_repo, env=env).stdout == "second report\n"
    assert run_bootstrap(["report", "1"], cwd=git_repo, env=env).stdout == "first report\n"

    missing = run_bootstrap(["report", "7"], cwd=git_repo, env=env)
    assert missing.stdout == "No such report. Available:\n001-phase1-approved.md\n002-final-changes_required.md\n"

    nonsense = run_bootstrap(["report", "not-a-number"], cwd=git_repo, env=env)
    assert nonsense.stdout.startswith("No such report. Available:\n")


# --------------------------------------------------------------------------
# finish
# --------------------------------------------------------------------------


def test_finish_requires_a_clean_worktree(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Uncommitted work at the end is work that never landed in a reviewed commit."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    (git_repo / "loose.txt").write_text("not committed\n")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the worktree is not clean" in proc.stdout
    assert "loose.txt" in proc.stdout
    # Recorded before the review runs, so an interrupted finish still stops the Stop gate
    # insisting on the remaining phases.
    assert read_state(env, git_repo, "s1")["finish_requested"] is True
    assert read_state(env, git_repo, "s1")["status"] == "ARMED"


def test_finish_completes_on_an_approving_review(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="approve")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert "running the final cumulative review" in proc.stdout
    assert "opencode-review-loop: COMPLETE." in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")
    assert document["reason"] == "final cumulative review approved (user-invoked finish)"


@pytest.mark.parametrize("mode", ["changes", "approve-with-critical"])
def test_finish_never_completes_on_a_review_that_did_not_approve(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    mode: str,
) -> None:
    """Rule 1, at the last gate the user can reach: not approved is not complete."""
    env = armed_env(clean_env, OCRL_FAKE_MODE=mode)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the final cumulative review did not pass. The mode stays armed." in proc.stdout
    assert "FINDING severity=" in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""


def test_finish_refuses_a_resumed_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``_FINISHABLE`` is an allow-list that already excludes ``RESUMED`` -- pin it in a test."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    state_path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(state_path.read_text())
    document.update(status="RESUMED", resumed_into="s2", reason="resumed into s2")
    state_path.write_text(json.dumps(document))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "cannot finish while the activation is RESUMED" in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "RESUMED"


def test_finish_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["finish"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 0
    assert proc.stdout == "opencode-review-loop: not armed in this worktree.\n"


# --------------------------------------------------------------------------
# deactivate
# --------------------------------------------------------------------------


def test_deactivate_disarms_but_keeps_the_pointer(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Rule 0: a missing pointer reads as "arming never executed", which denies everything."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one", "two")

    proc = run_bootstrap(["deactivate"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "opencode-review-loop: STOPPED for this worktree." in proc.stdout
    assert "at phase 1 of 2." in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "DISARMED"
    assert document["reason"] == "stopped by the user"

    pointer = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "sessions" / "s1"
    assert pointer.read_text() == f"{git_repo}\n"


def test_deactivate_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["deactivate"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 0
    assert proc.stdout == "opencode-review-loop: not armed in this worktree, so there is nothing to stop.\n"


# --------------------------------------------------------------------------
# Activation lookup
# --------------------------------------------------------------------------


def test_commands_ignore_an_activation_from_another_worktree(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The repository is resolved from the working directory, never from the state document."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    other = tmp_path / "other"
    other.mkdir()

    proc = run_bootstrap(["status"], cwd=other, env=env)
    assert proc.stdout == "opencode-review-loop: not armed in this worktree.\n"
    assert not (Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "worktrees" / paths.sha256_hex(str(other))).exists()


def test_a_reviewer_seam_is_all_these_tests_ever_call(clean_env: dict[str, str]) -> None:
    """Guard the guard: the suite must never be able to reach a real model."""
    assert FAKE_REVIEWER.is_file()
    assert "OCRL_REVIEWER_CMD" in armed_env(clean_env)
