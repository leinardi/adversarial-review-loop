"""``gate-stop`` -- the backstop that decides whether a turn may end.

Two shapes of answer, and every test here is about which one comes out:

- ``{"decision":"block"}`` sends the turn back, and is counted so a wedged loop escalates
  rather than blocking forever;
- ``{"systemMessage":…}`` (or nothing at all) lets the turn end -- which is **not** an
  approval, and the tests assert the messages say so.

The one transition that disarms the mode is ``COMPLETE``, and it is reachable only through
an approving final cumulative review that nothing invalidated while it ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import git, run_hook
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_posttool import COMMIT, gated_commit
from test_commands_pretool import SESSION, active, arm, patch_state, payload


def stop(repo: Path, env: dict[str, str], **kwargs: object) -> dict[str, object]:
    """Run the Stop gate and return its parsed response (``{}`` for zero bytes)."""
    proc = run_hook("gate-stop", payload(repo, **kwargs), cwd=repo, env=env)  # type: ignore[arg-type]
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout:
        return {}
    document: dict[str, object] = json.loads(proc.stdout)
    return document


def blocked(response: dict[str, object]) -> str:
    assert response.get("decision") == "block", response
    return str(response["reason"])


def ended(response: dict[str, object]) -> str:
    assert "decision" not in response, response
    return str(response.get("systemMessage", ""))


def committed_phase(repo: Path, env: dict[str, str], text: str = "work\n") -> None:
    """One whole phase: gate, commit, confirm."""
    gated_commit(repo, env, text)
    proc = run_hook("confirm-commit", payload(repo, command=COMMIT), cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Rule 0
# --------------------------------------------------------------------------


def test_no_session_id_ends_the_turn_saying_nothing_was_reviewed(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Nothing can be recorded without a key, so the turn ends -- explicitly not approved."""
    message = ended(stop(git_repo, clean_env, session=""))

    assert "cannot identify this activation" in message
    assert "not an approval" in message


def test_no_pointer_records_arm_failed_and_blocks(git_repo: Path, clean_env: dict[str, str]) -> None:
    reason = blocked(stop(git_repo, clean_env))

    assert "arming never ran" in reason
    assert read_state(clean_env, git_repo, SESSION)["status"] == "ARM_FAILED"


def test_the_unstarted_arm_block_is_counted_rather_than_endless(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Without counting, the same message repeats on every turn end until the host cap hits."""
    env = {**clean_env, "OCRL_MAX_STOP_BLOCKS": "1"}
    blocked(stop(git_repo, env))

    message = ended(stop(git_repo, env))

    assert "STALLED" in message
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


# --------------------------------------------------------------------------
# Status branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["COMPLETE", "DISARMED"])
def test_a_finished_activation_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)

    assert stop(git_repo, env) == {}


def test_needs_human_ends_the_turn_without_approving_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="NEEDS_HUMAN", reason="a reviewer failure")

    message = ended(stop(git_repo, env))

    assert "still in NEEDS_HUMAN" in message
    assert "not reviewed to completion" in message


def test_an_unfrozen_phase_list_blocks_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    reason = blocked(stop(git_repo, env))

    assert "the phase list has not been frozen" in reason
    assert "set-phases" in reason


def test_an_unfinished_reconcile_blocks_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RECONCILE", reason="a commit diverged", bad_commit_parent="abc123")

    reason = blocked(stop(git_repo, env))

    assert "the reconcile is unfinished" in reason
    assert "git reset --soft abc123" in reason


def test_an_expired_activation_blocks_rather_than_disarming(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, armed_at=1)

    reason = blocked(stop(git_repo, env))

    assert "past ttl_hours" in reason


def test_arm_failed_blocks_and_names_the_reason(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_hook("gate-stop", payload(git_repo), cwd=git_repo, env=env)
    assert proc.returncode == 0
    patch_state(env, git_repo, reason="the plan path does not resolve")

    reason = blocked(stop(git_repo, env))

    assert "arming failed" in reason
    assert "the plan path does not resolve" in reason


# --------------------------------------------------------------------------
# Defer
# --------------------------------------------------------------------------


def test_a_deferred_turn_may_end_once(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, defer_pending=True)

    message = ended(stop(git_repo, env))

    assert "deferred once at your request" in message
    assert read_state(env, git_repo, SESSION)["defer_pending"] is False

    # And exactly once: the next turn end is gated again.
    assert blocked(stop(git_repo, env))


# --------------------------------------------------------------------------
# The sweep, the outstanding phases, and the final review
# --------------------------------------------------------------------------


def test_uncommitted_work_is_reviewed_before_the_turn_may_end(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Work that never reached a commit still gets reviewed; a blocking review blocks."""
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")

    reason = blocked(stop(git_repo, env))

    assert "uncommitted work that OpenCode requires changes to" in reason
    assert "Returns success on a failed lookup" in reason


def test_a_failed_sweep_review_is_never_an_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="nonzero")
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")

    reason = blocked(stop(git_repo, env))

    assert "the review of the uncommitted work failed" in reason
    assert "never an approval" in reason


def test_outstanding_phases_block_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)

    reason = blocked(stop(git_repo, env))

    assert "phases 2..2 are still outstanding" in reason
    assert "phase two" in reason


def test_a_dirty_worktree_blocks_before_the_final_review(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    (git_repo / "left-over.txt").write_text("uncommitted\n")

    reason = blocked(stop(git_repo, env))

    assert "the worktree is not clean" in reason
    assert "left-over.txt" in reason


def test_an_approving_final_review_completes_the_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")


def test_a_blocking_final_review_blocks_and_leaves_the_mode_armed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_FAKE_MODE"] = "changes"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review found problems" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_a_failed_final_review_is_never_an_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_FAKE_MODE"] = "nonzero"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review failed" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_an_escalation_during_the_final_review_is_not_overwritten_by_it(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The one direction that must never happen: an approval landing on somebody's denial.

    The reviewer seam escalates the activation to ``NEEDS_HUMAN`` from *inside* the review,
    which is exactly the window a slow model call opens. Without the fingerprint check the
    approval that follows would overwrite it with ``COMPLETE`` and disarm the mode.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    escalate = tmp_path / "escalating-reviewer.sh"
    document_path = state_dir(env, git_repo, SESSION) / "state.json"
    escalate.write_text(
        "#!/usr/bin/env bash\n"
        f"python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(document_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "NEEDS_HUMAN"\n'
        'd["reason"] = "escalated while the review ran"\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'Fine.\\n\\n<<<OCRL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<OCRL-END>>>\\n'\n"
    )
    escalate.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(escalate)

    reason = blocked(stop(git_repo, env))

    assert "while the final review was running" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["final_done_tree"] == ""


def test_a_second_turn_end_on_an_already_reviewed_tree_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``final_done_tree`` is what stops the same tree being reviewed on every turn end."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    ended(stop(git_repo, env))
    patch_state(env, git_repo, status="ACTIVE")

    assert stop(git_repo, env) == {}


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def test_progress_resets_the_no_progress_count(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Only genuine stalls count, or a long correct session would escalate for being long."""
    env = armed_env(clean_env, OCRL_MAX_STOP_BLOCKS="3")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    blocked(stop(git_repo, env))
    blocked(stop(git_repo, env))
    assert read_state(env, git_repo, SESSION)["stop_blocks"] == 2

    committed_phase(git_repo, env, "second phase of work\n")
    ended(stop(git_repo, env))

    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


def test_the_fail_closed_fallback_for_this_event_is_a_block(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A crash in the Stop gate must not read as "the turn is fine to end"."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    lock = state_dir(env, git_repo, SESSION) / "lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to("/dev/null")

    reason = blocked(stop(git_repo, env))

    assert "internal error in the Stop gate" in reason
    assert "final review did not run" in reason


def test_unreadable_state_blocks_the_turn_instead_of_ending_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A live pointer with no readable state is the fail-open case, and it must not pass.

    The shell ended the turn silently here, which reports finished work as reviewed when
    nothing was. It escalates rather than merely blocking because a block has to be counted to
    be bounded, and there is no document to count in until one is written.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").unlink()

    reason = blocked(stop(git_repo, env))

    assert "could not be read" in reason
    assert "not approved" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"

    # Bounded: the next turn end takes the NEEDS_HUMAN branch and may end, still not approved.
    message = ended(stop(git_repo, env))
    assert "still in NEEDS_HUMAN" in message


def test_corrupt_state_blocks_the_turn_too(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").write_text("[not, an, object]")

    reason = blocked(stop(git_repo, env))

    assert "could not be read" in reason


def test_a_late_escalation_does_not_reopen_a_mode_the_user_stopped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Rule 4: a review failing may not undo a stop the user ran while it was running.

    The reviewer stand-in disarms the activation from inside the review -- the window in which
    the user actually runs ``/opencode-review-loop:stop`` -- and then fails. With the block
    limit at zero the very next step is the escalation, and escalating over ``DISARMED`` would
    turn it back into a state that denies every mutation.
    """
    env = armed_env(clean_env, OCRL_MAX_STOP_BLOCKS="0")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    script = tmp_path / "stopping-final-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "DISARMED"\n'
        'd["reason"] = "stopped by the user"\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "exit 3\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    message = ended(stop(git_repo, env))

    assert "while this turn was being reviewed" in message
    assert "NOT an approval" in message
    assert read_state(env, git_repo, SESSION)["status"] == "DISARMED"


@pytest.mark.parametrize("status", ["DISARMED", "COMPLETE"])
def test_a_turn_ending_on_unreviewed_work_tells_the_user(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
) -> None:
    """``systemMessage`` reaches the user, so relaying it is not the model's decision.

    This is the only place a Rule 4 escape surfaces: a Bash command that commits and then
    disarms leaves an unapproved HEAD under a mode that looks deliberately stopped, and the
    turn used to end in silence.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    patch_state(env, git_repo, status=status)

    message = ended(stop(git_repo, env))

    assert "no review ever approved" in message
    assert "without passing the review gate" in message


@pytest.mark.parametrize("status", ["DISARMED", "COMPLETE"])
def test_a_turn_ending_cleanly_still_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    """The warning must not fire for the ordinary way a session ends."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, status=status)

    assert stop(git_repo, env) == {}


def test_an_unreadable_repository_is_reported_at_turn_end(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The Stop-gate half of the same suppression: empty must not read as "nothing to see"."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    patch_state(env, git_repo, status="DISARMED")
    (git_repo / ".git" / "HEAD").unlink()

    message = ended(stop(git_repo, env))

    assert "could not be read" in message
    assert "says nothing about whether the history was reviewed" in message
