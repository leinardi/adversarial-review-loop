"""``accept`` -- the user-owned escape from a review loop stuck on one tree.

An accept mints exactly one artifact: the current tree in ``approved_trees``, the same
record a passing review would have written. So the tests here are about *scope*: what an
accept touches, what it deliberately leaves alone, and that it is bound to one exact tree
rather than a blanket pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import BOOTSTRAP, git, run_bootstrap
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_posttool import COMMIT, confirm, context
from test_commands_pretool import SESSION, active, arm, patch_state, pretool

import ocrl


def accept(repo: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    proc = run_bootstrap(["accept", *args], cwd=repo, env=env)
    return proc.returncode, proc.stdout


# --------------------------------------------------------------------------
# The happy path: what an accept grants, and only that
# --------------------------------------------------------------------------


def test_accept_marks_the_tree_approved_and_touches_nothing_else(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    (git_repo / "new.txt").write_text("work\n")

    before = read_state(env, git_repo, SESSION)
    code, out = accept(git_repo, env, "--reason", "false positive, already checked by hand")

    assert code == 0, out
    assert "accepted phase 1's current tree" in out
    after = read_state(env, git_repo, SESSION)
    # The baseline tree is already pre-approved by `arm`; accept adds exactly one more.
    assert len(after["approved_trees"]) == len(before["approved_trees"]) + 1  # type: ignore[arg-type]
    assert after["last_approved_tree"] == before["last_approved_tree"]
    assert after["pending_approved_tree"] == ""
    assert after["phase"] == before["phase"]
    assert after["status"] == "ACTIVE"


def test_accept_lets_the_exact_tree_through_without_reviewing_it_again(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The end-to-end case the command exists for: a stuck loop, broken without a review."""
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    (git_repo / "new.txt").write_text("work\n")

    first = pretool(git_repo, env, command=COMMIT)
    assert first[0] == "deny"
    second = pretool(git_repo, env, command=COMMIT)
    assert second[0] == "deny"

    code, out = accept(git_repo, env, "--reason", "still flagging the same non-issue")
    assert code == 0, out
    document = read_state(env, git_repo, SESSION)
    # `approved_trees` is sorted, not insertion-ordered -- the accepted tree is whichever
    # entry is not the pre-approved baseline.
    (accepted_tree,) = set(document["approved_trees"]) - {document["last_approved_tree"]}  # type: ignore[call-overload]
    assert document["manual_accepts"] == [
        {
            "at": document["manual_accepts"][0]["at"],  # type: ignore[index]
            "phase": 1,
            "tree": accepted_tree,
            "base": document["last_approved_tree"],
            "reason": "still flagging the same non-issue",
            "reviews": 2,
            "report": "003-phase1-accepted.md",
        }
    ]

    # The reviewer is still wired to deny everything (OCRL_FAKE_MODE=changes) -- if this
    # allow came from a fresh review rather than `tree_approved`, it would have failed.
    verdict, reason = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    assert "already approved by a previous review" in reason

    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")
    _, stdout = confirm(git_repo, env, command=COMMIT)
    assert "phase 1 of 2 committed and verified" in context(stdout)
    assert read_state(env, git_repo, SESSION)["phase"] == 2

    # Phase 2 is gated exactly as normal -- the acceptance does not leak past its own tree.
    (git_repo / "second.txt").write_text("more work\n")
    verdict, reason = pretool(git_repo, env, command=COMMIT)
    assert verdict == "deny"
    assert "requires changes before phase 2" in reason


def test_accept_clears_a_needs_human_escalation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    patch_state(env, git_repo, status="NEEDS_HUMAN", reason="the reviewer failed 4 times in a row")

    code, out = accept(git_repo, env, "--reason", "verified by hand, moving on")

    assert code == 0, out
    assert "clears the NEEDS_HUMAN escalation" in out
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "ACTIVE"
    assert "manually accepted by the user" in document["reason"]  # type: ignore[operator]


def test_accept_clears_a_pending_transient_backoff(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Phase 6: a transient failure sets ``transient_failures``/``retry_not_before``. Left
    standing, the very tree an accept just approved would still be denied by
    ``pretool._check_retry_backoff`` for up to ``max_transient_failures``' worth of delay --
    exactly the "stuck loop" case ``accept`` exists to break.
    """
    env = armed_env(clean_env, OCRL_FAKE_MODE="rate-limited")
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    pretool(git_repo, env, command='git add -A && git commit -m "x"')
    before = read_state(env, git_repo, SESSION)
    assert before["transient_failures"] == 1
    assert before["retry_not_before"]

    code, out = accept(git_repo, env, "--reason", "verified by hand")

    assert code == 0, out
    document = read_state(env, git_repo, SESSION)
    assert document["transient_failures"] == 0
    assert document["retry_not_before"] == 0


def test_accept_does_not_clear_a_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    patch_state(env, git_repo, status="RECONCILE", bad_commit="deadbeef", bad_commit_parent="cafef00d", reason="a commit landed outside the gate")

    code, out = accept(git_repo, env)

    assert code == 0, out
    assert "does NOT clear the outstanding reconcile" in out
    assert "deadbeef" in out
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "RECONCILE"
    assert document["bad_commit"] == "deadbeef"
    # The divergence explanation is what `status` and the Stop gate's reconcile block both
    # surface -- an accept that overwrote it with "manually accepted" would hide *why* the
    # reconcile exists behind the one thing the acceptance did not actually resolve.
    assert document["reason"] == "a commit landed outside the gate"


def test_accept_resets_the_counters_a_stuck_loop_ran_up(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    patch_state(env, git_repo, failures=3, stop_blocks=2, stop_marker="some-marker")

    code, out = accept(git_repo, env)

    assert code == 0, out
    document = read_state(env, git_repo, SESSION)
    assert document["failures"] == 0
    assert document["stop_blocks"] == 0
    assert document["stop_marker"] == ""


# --------------------------------------------------------------------------
# The grant is bound to one exact tree
# --------------------------------------------------------------------------


def test_editing_after_an_accept_re_engages_the_gate(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    accept(git_repo, env)

    (git_repo / "new.txt").write_text("work, but different now\n")
    verdict, reason = pretool(git_repo, env, command=COMMIT)

    assert verdict == "deny"
    assert "requires changes before phase 1" in reason


def test_accepting_then_committing_unchanged_confirms_rather_than_reconciling(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    accept(git_repo, env)

    # `pretool` first, as the real loop always runs it before the commit itself -- it is
    # what records `pending_approved_tree`, which `confirm-commit` verifies against.
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")
    _, stdout = confirm(git_repo, env, command=COMMIT)

    assert "committed and verified" in context(stdout)
    assert read_state(env, git_repo, SESSION)["status"] == "ACTIVE"


# --------------------------------------------------------------------------
# Refusals: an allow-list, and each one writes nothing
# --------------------------------------------------------------------------


def test_accept_refuses_before_the_phase_list_is_frozen(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    before = read_state(env, git_repo, SESSION)

    code, out = accept(git_repo, env)

    assert code == 1
    assert "nothing may be accepted before the phase list is frozen" in out
    document = read_state(env, git_repo, SESSION)
    assert document["approved_trees"] == before["approved_trees"]
    assert document["manual_accepts"] == []


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ("COMPLETE", "nothing is gated in this worktree"),
        ("DISARMED", "nothing is gated in this worktree"),
        ("ARM_FAILED", "there is no live activation here"),
        ("RESUMED", "there is no live activation here"),
    ],
)
def test_accept_refuses_terminal_statuses(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str, fragment: str) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    before = read_state(env, git_repo, SESSION)
    patch_state(env, git_repo, status=status, reason="whatever got it here")

    code, out = accept(git_repo, env)

    assert code == 1
    assert fragment in out
    document = read_state(env, git_repo, SESSION)
    assert document["approved_trees"] == before["approved_trees"]
    assert document["manual_accepts"] == []
    assert document["status"] == status


def test_accept_refuses_a_stale_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_TTL_HOURS="1")
    active(git_repo, tmp_path, env, "phase one")
    before = read_state(env, git_repo, SESSION)
    patch_state(env, git_repo, armed_at=1)

    code, out = accept(git_repo, env)

    assert code == 1
    assert "past ttl_hours" in out
    assert "/opencode-review-loop:resume" in out
    document = read_state(env, git_repo, SESSION)
    assert document["approved_trees"] == before["approved_trees"]


def test_accept_without_an_activation(git_repo: Path, clean_env: dict[str, str]) -> None:
    code, out = accept(git_repo, armed_env(clean_env))
    assert code == 0
    assert out == "opencode-review-loop: not armed in this worktree.\n"


def test_accept_refuses_once_every_frozen_phase_is_committed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The window between the last phase's commit and the Stop gate's own completion is not
    a phase an acceptance can name -- accepting the final review is explicitly out of scope."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")
    confirm(git_repo, env, command=COMMIT)
    before = read_state(env, git_repo, SESSION)
    assert before["phase"] == 2  # past the one and only frozen phase

    code, out = accept(git_repo, env)

    assert code == 1
    assert "every frozen phase is already committed" in out
    after = read_state(env, git_repo, SESSION)
    assert after["approved_trees"] == before["approved_trees"]
    assert after["manual_accepts"] == []


def test_accept_bumps_activation_generation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    before = read_state(env, git_repo, SESSION)

    code, out = accept(git_repo, env)

    assert code == 0, out
    after = read_state(env, git_repo, SESSION)
    assert after["activation_generation"] == before["activation_generation"] + 1  # type: ignore[operator]


def test_accept_with_an_explicit_session(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``--session`` targets a specific activation rather than the worktree's latest one."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")

    code, out = accept(git_repo, env, "--session", SESSION, "--reason", "explicit session")

    assert code == 0, out
    assert read_state(env, git_repo, SESSION)["manual_accepts"] != []


def test_accept_with_an_unknown_session_reports_not_armed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")

    code, out = accept(git_repo, env, "--session", "does-not-exist")

    assert code == 0
    assert out == "opencode-review-loop: not armed in this worktree.\n"
    assert read_state(env, git_repo, SESSION)["manual_accepts"] == []


def _mid_review_accept_script(tmp_path: Path) -> Path:
    """A reviewer stand-in that runs a *real* ``ocrl accept`` mid-review, then approves.

    Stands in for a human running ``/opencode-review-loop:accept`` in a second terminal while
    a slow review of the same tree is in flight -- the exact race ``activation_generation``
    exists to catch. The nested command inherits this process's environment and working
    directory, which is exactly the same activation the outer review is running against.
    """
    script = tmp_path / "accepting-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"{sys.executable} -I {BOOTSTRAP} accept --reason 'concurrent accept from another terminal' >/dev/null\n"
        "printf 'Fine.\\n\\n<<<OCRL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    return script


def test_a_concurrent_accept_stops_a_stale_review_from_landing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The vulnerability ``activation_generation`` closes: without the bump, a review already
    in flight when an accept runs would see no difference in the fields it compares, and
    would land its own approval over a decision the accept had already superseded."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    env["OCRL_REVIEWER_CMD"] = str(_mid_review_accept_script(tmp_path))

    verdict, reason = pretool(git_repo, env, command=COMMIT)

    assert verdict == "deny"
    assert "an accept changed the activation while this was in progress" in reason
    document = read_state(env, git_repo, SESSION)
    # The concurrent accept's own approval stands -- and nothing from the stale review's
    # `approve()` landed on top of it.
    assert len(document["manual_accepts"]) == 1  # type: ignore[arg-type]
    assert document["pending_approved_tree"] == ""

    # Recoverable: the tree the stale review was denied for is already approved, so an
    # ordinary retry of the same command succeeds without another review.
    verdict, reason = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    assert "already approved by a previous review" in reason


# --------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------


def test_the_acceptance_is_visible_in_status_and_report(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    (git_repo / "new.txt").write_text("work\n")
    code, out = accept(git_repo, env, "--reason", "hand-verified")
    assert code == 0, out

    status = run_bootstrap(["status"], cwd=git_repo, env=env)
    assert "manual accepts:      1 (phases 1)\n" in status.stdout

    report = run_bootstrap(["report", "1"], cwd=git_repo, env=env)
    assert "# Manual acceptance 001 (phase1)" in report.stdout
    assert "ACCEPTED (manual" in report.stdout
    assert "hand-verified" in report.stdout

    reports_dir = state_dir(env, git_repo, SESSION) / "reports"
    names = sorted(p.name for p in reports_dir.glob("*.md"))
    assert names == ["001-phase1-accepted.md"]
    assert not list(reports_dir.glob(".accept-*"))


def test_a_later_review_is_told_about_the_acceptance(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    (git_repo / "new.txt").write_text("work\n")
    accept(git_repo, env, "--reason", "false positive")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")
    confirm(git_repo, env, command=COMMIT)

    (git_repo / "second.txt").write_text("more\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "deny"

    bundles = sorted((state_dir(env, git_repo, SESSION) / "bundles").iterdir())
    range_text = (bundles[-1] / "range.txt").read_text()
    assert "## Manually accepted phases" in range_text
    assert "phase 1" in range_text
    assert "false positive" in range_text


# --------------------------------------------------------------------------
# Rule 4: the model may never reach this
# --------------------------------------------------------------------------


def test_the_skill_disables_model_invocation() -> None:
    text = (ocrl.PLUGIN_ROOT / "skills" / "accept" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in text
    assert "user-invocable: true" in text


def test_claude_may_not_run_accept_itself(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    before = read_state(env, git_repo, SESSION)

    verdict, reason = pretool(git_repo, env, command='ocrl.sh accept --reason "x"')

    assert verdict == "deny"
    assert "user-only commands" in reason
    assert read_state(env, git_repo, SESSION)["approved_trees"] == before["approved_trees"]
