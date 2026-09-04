"""``set-phases``, ``defer``, ``status``, ``report``, ``finish`` and ``deactivate``.

Two of these end the mode, and Rule 4 says only the user may. What that means here is that
the tests check *state transitions*, not just output: ``deactivate`` must leave the session
pointer in place, and ``finish`` must reach ``COMPLETE`` only through an approving review.
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
import time
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, git, run_bootstrap
from test_commands_arm import armed_env, plan_file, read_state, state_dir

from arl import harness, paths


def arm(repo: Path, tmp_path: Path, env: dict[str, str], session: str = "s1") -> None:
    proc = run_bootstrap(["arm", "--session", session, "--plan", str(plan_file(tmp_path))], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout


def marking_reviewer(tmp_path: Path, marker: Path) -> Path:
    """An approving reviewer that leaves proof on disk that it was actually executed.

    ``test_commands_races`` has a richer stub, but it imports *this* module, so a local one is
    what keeps the dependency pointing one way. All this needs is the marker.
    """
    stub = tmp_path / "marking-reviewer.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        f"open({str(marker)!r}, 'w').write('reviewed\\n')\n"
        "print('Reviewed the whole diff.')\n"
        "print()\n"
        "print('<<<ARL-FINDINGS>>>')\n"
        "print('VERDICT APPROVED')\n"
        "print('<<<ARL-END>>>')\n"
    )
    stub.chmod(0o755)
    return stub


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
        "adversarial-review-loop: 2 phases frozen. Now on phase 1 of 2:\n\n"
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
    env = armed_env(clean_env, ARL_MAX_DEFERS="2")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")

    first = run_bootstrap(["defer", "--reason", "need a decision"], cwd=git_repo, env=env)
    assert first.returncode == 0
    assert first.stdout == "adversarial-review-loop: turn end deferred (1 of 2). Reason recorded: need a decision\n"
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
    assert proc.stdout == "adversarial-review-loop: not armed in this worktree.\n"


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
    assert "operational failures:0 / 2\n" in proc.stdout
    assert "transient failures:  0 / 5\n" in proc.stdout
    # Deliberately not the `session:` row above, which holds the *Claude* session id.
    assert "reviewer session:    none (the next review starts a fresh session)\n" in proc.stdout
    assert "phases:\n  1. first thing\n  2. second thing\n\nreports:\n\n" in proc.stdout


def test_status_shows_an_active_retry_backoff(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Phase 6: a pending backoff is surfaced the same way phase 5's persisting findings
    are -- only when there is one to show.
    """
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")

    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["retry_not_before"] = int(time.time()) + 100
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "retry backoff:       " in proc.stdout
    assert "s remaining\n" in proc.stdout


def test_status_shows_the_stored_reviewer_session(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Continuity is otherwise invisible: a fresh review looks the same whether it was correct
    (a new phase) or a silent loss, and the difference is worth real tokens per round. The id is
    shown in full because it is what a human pastes into `opencode session delete`.

    Pinned to `opencode`: the stored id is a `ses_…`, and `continuity_summary` shows a pointer
    only when the *configured* harness recognises the shape -- which is the same check that
    stops one harness offering another's session as resumable."""
    env = armed_env(clean_env, ARL_HARNESS="opencode")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")

    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["reviewer_session"] = {"id": "ses_fb7592bccffeVl5WXE354RQsD9", "label": "phase1", "round": 3}
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "reviewer session:    ses_fb7592bccffeVl5WXE354RQsD9 (phase1, round 3)\n" in proc.stdout


def test_status_shows_the_effective_status_alongside_the_stored_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """An expired activation reads as STALE, and STALE denies -- it never silently disarms.

    "Denies" rather than "blocks": every mutation is refused by ``pretool``, but the Stop gate
    deliberately *ends* a stale turn rather than blocking it, since only the user can clear it.
    """
    env = armed_env(clean_env, ARL_TTL_HOURS="1")
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
    assert proc.stdout == "adversarial-review-loop: not armed in this worktree.\n"


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
    env = armed_env(clean_env, ARL_FAKE_MODE="approve")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert "running the final cumulative review" in proc.stdout
    assert "adversarial-review-loop: COMPLETE." in proc.stdout
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
    env = armed_env(clean_env, ARL_FAKE_MODE=mode)
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


def test_finish_reviews_even_when_final_review_is_disabled(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: ``finish`` ignores ``final_review`` entirely, and this is what makes it the escape
    hatch the key's whole design rests on.

    ``session.finish`` calls ``reviewer.execute`` directly rather than routing through
    ``stop.py::_final``, so the key never comes near it -- no production code was changed to
    achieve that. This test is the guard on that accident of structure: a later refactor that
    unified the two paths would make ``final_review=false`` silently disable the *user's*
    explicit request for a review too, and every message that offers ``finish`` as the way to
    get one would become a lie. The marker file is the proof the reviewer really ran, rather
    than the output merely claiming a verdict.
    """
    env = armed_env(clean_env, ARL_FINAL_REVIEW="false")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")
    marker = tmp_path / "the-reviewer-ran"
    env["ARL_REVIEWER_CMD"] = str(marking_reviewer(tmp_path, marker))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert marker.exists(), "finish must invoke the reviewer whatever final_review says"
    assert "running the final cumulative review" in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "COMPLETE"
    assert document["reason"] == "final cumulative review approved (user-invoked finish)"


@pytest.mark.parametrize("mode", ["changes", "approve-with-critical"])
def test_finish_still_refuses_a_failed_review_when_final_review_is_disabled(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    mode: str,
) -> None:
    """Pin: the key cannot turn a refusal into a completion.

    The dangerous shape is not "finish skips the review" but "finish runs it, is refused, and
    completes anyway because nothing downstream is checking". ``final_review=false`` is exactly
    the configuration under which that would go unnoticed.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE=mode, ARL_FINAL_REVIEW="false")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the final cumulative review did not pass. The mode stays armed." in proc.stdout
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
    assert proc.stdout == "adversarial-review-loop: not armed in this worktree.\n"


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
    assert "adversarial-review-loop: STOPPED for this worktree." in proc.stdout
    assert "at phase 1 of 2." in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "DISARMED"
    assert document["reason"] == "stopped by the user"

    pointer = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop" / "sessions" / "s1"
    assert pointer.read_text() == f"{git_repo}\n"


def test_deactivate_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["deactivate"], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 0
    assert proc.stdout == "adversarial-review-loop: not armed in this worktree, so there is nothing to stop.\n"


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
    assert proc.stdout == "adversarial-review-loop: not armed in this worktree.\n"
    assert not (Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop" / "worktrees" / paths.sha256_hex(str(other))).exists()


def test_a_reviewer_seam_is_all_these_tests_ever_call(clean_env: dict[str, str]) -> None:
    """Guard the guard: the suite must never be able to reach a real model."""
    assert FAKE_REVIEWER.is_file()
    assert "ARL_REVIEWER_CMD" in armed_env(clean_env)


def test_status_names_the_harness_and_the_model_it_would_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``model`` is unset by default and resolves to the harness's own, so a status line built
    from the raw config value would show a blank where the reviewer is named."""
    env = armed_env(clean_env, ARL_HARNESS="claude-code")
    arm(git_repo, tmp_path, env)

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "harness:             claude-code\n" in proc.stdout
    assert f"model:               {harness.get('claude-code').default_model} \n" in proc.stdout


def test_status_reports_an_unimplemented_harness_instead_of_crashing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``status`` is how a user finds a bad ``harness`` value -- unwinding on one would take
    away the tool that diagnoses it. It reports; it does not present a session as usable."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["reviewer_session"] = {"id": "ses_fb7592bccffeVl5WXE354RQsD9", "label": "phase1", "round": 3}
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["status"], cwd=git_repo, env={**env, "ARL_HARNESS": "not-a-harness"})

    assert proc.returncode == 0, proc.stderr
    assert "harness:             not-a-harness\n" in proc.stdout
    assert f"model:               {harness.UNIMPLEMENTED_MODEL} \n" in proc.stdout
    assert "reviewer session:    unreadable (unknown harness 'not-a-harness'" in proc.stdout
    assert "ses_fb7592bccffeVl5WXE354RQsD9" not in proc.stdout


# --------------------------------------------------------------------------
# status: reviewer cost
# --------------------------------------------------------------------------


def write_round_history(env: dict[str, str], repo: Path, *entries: dict[str, object]) -> None:
    """Put ``round_history`` entries in place, the shape ``reviewer._record_round`` writes."""
    path = state_dir(env, repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    generation = document.get("activation_generation", 1)
    document["round_history"] = [{"label": "phase1", "generation": generation, **entry} for entry in entries]
    path.write_text(json.dumps(document))


def test_status_totals_what_the_reviews_have_cost(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The per-round figure is in each report; what a human actually asks is what the whole
    activation has spent, and totalling the rounds is exact whenever the rounds are."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    write_round_history(
        env,
        git_repo,
        {"seq": 1, "usage": {"cost_usd": 5.58, "turns": 50}},
        {"seq": 2, "usage": {"cost_usd": 5.28, "turns": 28}},
    )

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "reviewer cost:       $10.86 over 2 round(s), $10.86 this phase (2)\n" in proc.stdout


def test_status_omits_the_cost_when_no_round_reported_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """An OpenCode activation, or one armed before this was recorded. `$0.00` would claim the
    reviews were free, when what is true is that their cost was never reported."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    write_round_history(env, git_repo, {"seq": 1})

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "reviewer cost:" not in proc.stdout


def test_status_skips_a_malformed_cost_rather_than_totalling_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`state.json` is not a trust boundary and `status` changes nothing, so an unreadable
    entry leaves the total -- and the count, which says what the figure covers. `True` is the
    one worth pinning: `bool` is an `int` subclass, so it would otherwise total as one dollar."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    write_round_history(
        env,
        git_repo,
        {"seq": 1, "usage": {"cost_usd": 2.50}},
        {"seq": 2, "usage": {"cost_usd": True}},
        {"seq": 3, "usage": {"cost_usd": "5.00"}},
        {"seq": 4, "usage": "not an object"},
        {"seq": 5, "usage": {"cost_usd": float("inf")}},
    )

    proc = run_bootstrap(["status"], cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert "reviewer cost:       $2.50 over 1 round(s)" in proc.stdout


# --------------------------------------------------------------------------
# reorient, the hook that answers a compaction
# --------------------------------------------------------------------------


def compact_payload(session: str, cwd: Path) -> bytes:
    """The SessionStart payload Claude Code sends after a compaction."""
    return json.dumps({"session_id": session, "cwd": str(cwd), "source": "compact", "hook_event_name": "SessionStart"}).encode()


def reorient(repo: Path, env: dict[str, str], *, session: str = "s1", cwd: Path | None = None) -> str:
    proc = run_bootstrap(["reorient"], cwd=repo, env=env, stdin=compact_payload(session, cwd or repo))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_reorient_restates_the_phase_and_the_plan_after_a_compaction(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A compacted session has lost the plan, the rules and where it is -- but the gate is
    still enforced, so the next commit is still reviewed against all three."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing", "second thing")

    out = reorient(git_repo, env)

    assert "compacted" in out
    assert "Phase 1 of 2 is in progress:" in out
    assert "first thing" in out
    assert "plan.frozen.md" in out
    assert "git add -A && git commit" in out
    assert "Continue with phase 1." in out
    # A compacted session has lost `skills/implement/SKILL.md` along with everything else, so
    # the one commit rule that is not guessable from the shapes above has to be restated here
    # too -- otherwise it rediscovers the deny-list by hitting it, which is what happened.
    assert '`-m "subject" -m "body"`' in out
    assert "`-F`/`--file`, are" in out


def test_reorient_is_plain_text_because_that_is_what_gets_injected(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`SessionStart` adds a hook's plain stdout to Claude's context. A JSON object would be
    read as a decision document and the text would never be seen."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")

    out = reorient(git_repo, env)

    assert out.lstrip()[:1] not in ("{", "["), out[:80]


def test_reorient_quotes_no_reviewer_prose(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """This text is injected straight into Claude's context. Model-authored findings re-entering
    the session as though the gate had said them is the one thing that must not happen, so the
    findings are counted and the report is named instead."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    write_round_history(
        env,
        git_repo,
        {"seq": 7, "verdict": "CHANGES_REQUIRED", "findings": ["FINDING severity=high actionable=yes file=a.py:1 | secret prose"]},
    )

    out = reorient(git_repo, env)

    assert "secret prose" not in out
    assert "report 007: CHANGES_REQUIRED, 1 finding(s)" in out
    assert "/adversarial-review-loop:report 7" in out


def test_reorient_says_nothing_when_no_activation_owns_the_session(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Not our session: print nothing, exit 0. This hook grants nothing and blocks nothing."""
    assert reorient(git_repo, armed_env(clean_env), session="never-armed") == ""


def test_reorient_says_nothing_outside_the_armed_worktree(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Work elsewhere in the same session is untouched, exactly as `pretool` leaves it."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    other = tmp_path / "elsewhere"
    other.mkdir()

    assert reorient(git_repo, env, cwd=other) == ""


def test_reorient_tells_a_needs_human_activation_to_stop(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Re-orienting an escalated activation into "continue with phase N" would send Claude back
    around a loop the gate has already given up on."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["status"] = "NEEDS_HUMAN"
    path.write_text(json.dumps(document))

    out = reorient(git_repo, env)

    assert "NEEDS_HUMAN" in out
    assert "Do not keep implementing." in out
    assert "Continue with phase" not in out


def test_reorient_says_nothing_once_the_activation_is_over(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")
    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["status"] = "COMPLETE"
    path.write_text(json.dumps(document))

    assert reorient(git_repo, env) == ""


def test_reorient_survives_a_malformed_payload_and_a_malformed_history(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """It has no fail-closed direction: the worst thing it can do is disturb a session that was
    otherwise fine."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "first thing")

    assert run_bootstrap(["reorient"], cwd=git_repo, env=env, stdin=b"not json").returncode == 0

    write_round_history(env, git_repo, {"seq": "seven", "verdict": None, "findings": "not a list"})
    out = reorient(git_repo, env)
    assert out, "a broken history entry must not silence the re-orientation"
    assert "cannot be read back" in out
