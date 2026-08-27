"""``confirm-commit`` and ``posttool-failure`` -- the independent check after the fact.

``confirm-commit`` is the second half of the defence: even if a command-shape bypass got a
commit past ``pretool``, the tree that landed is compared against the tree that was approved
here, and a mismatch enters ``RECONCILE`` rather than advancing the phase. So the assertions
below are about *state*, not wording -- what advanced, what did not, and what the pending
approval looks like afterwards.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import git, hook_json, run_bootstrap, run_hook
from test_commands_arm import armed_env, plan_file, read_state, state_dir
from test_commands_pretool import SESSION, active, active_until, patch_state, payload, pretool

COMMIT = 'git add -A && git commit -m "phase"'


def confirm(repo: Path, env: dict[str, str], **kwargs: object) -> tuple[int, str]:
    proc = run_hook("confirm-commit", payload(repo, **kwargs), cwd=repo, env=env)  # type: ignore[arg-type]
    assert proc.returncode == 0, proc.stderr
    return proc.returncode, proc.stdout


def context(stdout: str) -> str:
    """The ``additionalContext`` a ``PostToolUse`` response carries."""
    output = json.loads(stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    text: str = output["additionalContext"]
    return text


def gated_commit(repo: Path, env: dict[str, str], text: str = "work\n") -> None:
    """Take a change through the gate and actually commit it, as the loop does."""
    (repo / "new.txt").write_text(text)
    verdict, _ = pretool(repo, env, command=COMMIT)
    assert verdict == "allow"
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "phase")


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_verified_commit_advances_the_phase(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    gated_commit(git_repo, env)

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "phase 1 of 2 committed and verified" in message
    assert "Continue straight into phase 2 of 2" in message
    assert "phase two" in message
    document = read_state(env, git_repo, SESSION)
    assert document["phase"] == 2
    assert document["status"] == "ACTIVE"
    assert document["pending_approved_tree"] == ""
    assert document["last_approved_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")


def test_advancing_the_phase_clears_a_transient_backoff_left_standing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Phase 6: whatever left ``transient_failures``/``retry_not_before`` standing at the

    moment of confirmation -- a late busy-slot write racing the approval, most plausibly --
    must not carry into the next phase. ``_advance`` resets both, the same way it already
    resets ``failures``. Simulated directly, by patching the counters in after the approval
    already landed, rather than through real concurrency.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    gated_commit(git_repo, env)
    patch_state(env, git_repo, transient_failures=1, retry_not_before=int(time.time()) + 100)

    confirm(git_repo, env, command=COMMIT)

    document = read_state(env, git_repo, SESSION)
    assert document["phase"] == 2
    assert document["transient_failures"] == 0
    assert document["retry_not_before"] == 0


def test_the_last_phase_hands_over_to_the_stop_gate(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: with ``final_review`` on, the hand-over still promises the cumulative review."""
    env = armed_env(clean_env)
    env["OCRL_FINAL_REVIEW"] = "true"
    active(git_repo, tmp_path, env, "the only phase")
    gated_commit(git_repo, env)

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "All 1 phases are now committed" in message
    assert "the Stop gate runs the final" in message
    assert read_state(env, git_repo, SESSION)["phase"] == 2


def test_the_last_phase_promises_no_review_when_final_review_is_disabled(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The default is off, and the hand-over must not promise a review that will never run.

    Telling the model to end its turn *because* a cumulative review follows, when the Stop
    gate is about to complete the activation without one, is the difference between an
    informed hand-over and a silent completion. The remedy is named while it still exists:
    once the activation is COMPLETE, `finish` and `resume` both refuse it forever.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "the only phase")
    gated_commit(git_repo, env)

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "All 1 phases are now committed" in message
    assert "`final_review` is disabled" in message
    assert "the Stop gate runs the final" not in message
    # Claims only what the gate can back: a per-commit *gate* pass, not a model review. An
    # unchanged, already-approved or ignore_globs-matched tree passes without a call, and
    # COMPLETE_UNREVIEWED says so -- the hand-over that precedes it must not say more.
    assert "passed the\nper-commit gate" in message
    assert "reviewed individually" not in message
    assert "/opencode-review-loop:finish" in message
    assert read_state(env, git_repo, SESSION)["phase"] == 2


def test_reaching_the_pause_target_says_stop_rather_than_continue(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The pause message replaces NEXT_PHASE exactly at the target, not before or after."""
    env = armed_env(clean_env)
    active_until(git_repo, tmp_path, env, 2, "one", "two", "three")
    gated_commit(git_repo, env, "phase one work\n")

    _, stdout = confirm(git_repo, env, command=COMMIT)
    message = context(stdout)
    assert "Continue straight into phase 2 of 3" in message
    assert "pause target" not in message

    gated_commit(git_repo, env, "phase two work\n")
    _, stdout = confirm(git_repo, env, command=COMMIT)
    message = context(stdout)
    assert "phase 2 of 3 committed and verified" in message
    assert "The pause target (phase 2 of 3) has been reached" in message
    assert "End your turn now and report to\nthe user" in message
    assert "Continue straight into phase 3" not in message
    assert read_state(env, git_repo, SESSION)["phase"] == 3


def test_a_pause_target_beyond_the_phase_count_never_fires(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``phases.py`` clamps an out-of-range target to the frozen count, so a ``--until``
    given as 99 against a 2-phase plan behaves exactly like an unset target: every commit
    gets the ordinary NEXT_PHASE message, never a pause."""
    env = armed_env(clean_env)
    active_until(git_repo, tmp_path, env, 99, "one", "two")
    gated_commit(git_repo, env)

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "Continue straight into phase 2 of 2" in message
    assert "pause target" not in message


# --------------------------------------------------------------------------
# Divergence
# --------------------------------------------------------------------------


def test_an_amend_instead_of_a_commit_enters_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The approval was for a commit *on top of* the reviewed state, not a rewrite of it.

    The gate approved while HEAD was the seed commit, so an amend leaves HEAD with a parent
    that is not the one the review was approved against -- which is what the parent check is
    for, and it is the shape a tokenizer bypass would produce.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "--amend", "-m", "rewritten")

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "not the tree that was reviewed" in message
    assert "That is an amend or a rewrite" in message
    # The recovery text promises only what holds with `final_review` either way: RECONCILE is
    # refused by `_by_status` before the Stop gate's completion path is reached at all, so the
    # activation cannot complete while it stands -- with or without a cumulative review.
    assert "the Stop gate will not complete this" in message
    assert "final cumulative review" not in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "RECONCILE"
    assert document["phase"] == 1
    assert document["pending_approved_tree"] == ""


def test_content_appearing_after_the_gate_enters_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    gated_commit(git_repo, env)
    (git_repo / "sneaked.txt").write_text("appeared after the gate\n")

    _, stdout = confirm(git_repo, env, command=COMMIT)

    assert "the worktree is not clean afterwards" in context(stdout)
    assert read_state(env, git_repo, SESSION)["status"] == "RECONCILE"


def test_a_commit_that_never_happened_enters_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The tool reported success, but HEAD did not move -- so no phase may advance."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"

    _, stdout = confirm(git_repo, env, command=COMMIT)

    assert "HEAD did not move" in context(stdout)
    assert read_state(env, git_repo, SESSION)["status"] == "RECONCILE"


def test_committing_a_different_tree_than_the_approved_one_enters_reconcile(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A partial stage lands a tree the reviewer never saw. This is what catches it."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    (git_repo / "other.txt").write_text("also work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    git(git_repo, "add", "new.txt")
    git(git_repo, "commit", "-qm", "only half of it")

    _, stdout = confirm(git_repo, env, command=COMMIT)

    assert "but the approved tree was" in context(stdout)
    assert read_state(env, git_repo, SESSION)["status"] == "RECONCILE"


# --------------------------------------------------------------------------
# Not ours: silence, and nothing touched
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"tool": "Write"}, id="not-bash"),
        pytest.param({"command": "git commit -m something-else"}, id="different-command"),
        pytest.param({"session": "unknown"}, id="unknown-session"),
    ],
)
def test_an_irrelevant_call_emits_nothing_and_consumes_nothing(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    kwargs: dict[str, str],
) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    gated_commit(git_repo, env)
    pending = read_state(env, git_repo, SESSION)["pending_approved_tree"]

    proc = run_hook("confirm-commit", payload(git_repo, **kwargs), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert read_state(env, git_repo, SESSION)["pending_approved_tree"] == pending


def test_a_commit_outside_the_armed_worktree_is_not_confirmed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    gated_commit(git_repo, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = run_hook("confirm-commit", payload(elsewhere, command=COMMIT), cwd=elsewhere, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert read_state(env, git_repo, SESSION)["phase"] == 1


def test_nothing_happens_when_there_is_no_pending_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_hook("confirm-commit", payload(git_repo, command=COMMIT), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert read_state(env, git_repo, SESSION)["phase"] == 1


def test_a_crashing_confirm_says_the_commit_was_not_verified(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Silence here reads exactly like a verification that passed, so it must not be silence.

    The shell armed no fallback for this event at all, so a crash between the commit and the
    check emitted nothing and the phase simply never advanced -- indistinguishable, from the
    outside, from a check that ran and was happy. The lockfile is replaced with a symlink,
    which ``ocrl.atomic`` refuses to write through, so the entrypoint reaches its fail-closed
    fallback instead of a decision.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    gated_commit(git_repo, env)
    lock = state_dir(env, git_repo, SESSION) / "lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to("/dev/null")

    proc = run_hook("confirm-commit", payload(git_repo, command=COMMIT), cwd=git_repo, env=env)

    assert proc.returncode == 0
    message = context(proc.stdout)
    assert "internal error in the post-commit check" in message
    assert "NOT confirmed against the approved tree" in message
    assert read_state(env, git_repo, SESSION)["phase"] == 1


# --------------------------------------------------------------------------
# posttool-failure
# --------------------------------------------------------------------------


def test_a_failed_bash_call_drops_the_pending_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"

    proc = run_hook("posttool-failure", payload(git_repo, command=COMMIT), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    document = read_state(env, git_repo, SESSION)
    assert document["pending_approved_tree"] == ""
    assert document["pending_command"] == ""


def test_posttool_failure_says_nothing_at_all_even_when_it_cannot_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Its fallback is silence, matching what it emits on every other path.

    A crash leaves the pending approval stale rather than granting anything: ``confirm-commit``
    still requires an exact ``HEAD^{tree}`` match before a phase advances.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_hook("posttool-failure", payload(git_repo, session="s1/../s1"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_posttool_failure_leaves_a_foreign_session_alone(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, pending_approved_tree="deadbeef")

    proc = run_hook("posttool-failure", payload(git_repo, session="other"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert read_state(env, git_repo, SESSION)["pending_approved_tree"] == "deadbeef"


def test_hook_json_helper_rejects_a_doubled_response(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """One response per call, always: two concatenated objects would not parse at all."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    gated_commit(git_repo, env)

    proc = run_hook("confirm-commit", payload(git_repo, command=COMMIT), cwd=git_repo, env=env)

    assert hook_json(proc)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# --------------------------------------------------------------------------
# The activation may not move underneath a confirmation
# --------------------------------------------------------------------------


def racing_git(tmp_path: Path, state_path: Path, **values: object) -> Path:
    """A ``git`` earlier on ``PATH`` that mutates the activation on its first call.

    ``confirm-commit`` reads the document, then runs four git processes, then takes the lock
    and writes. This lands a competing change inside that window -- deterministically, where
    a real ``deactivate`` or a second hook would land there only sometimes.
    """
    shim = tmp_path / "gitshim"
    shim.mkdir(exist_ok=True)
    marker = tmp_path / "raced"
    script = shim / "git"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ ! -e {str(marker)!r} ]; then\n"
        f"  : >{str(marker)!r}\n"
        "  python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        f"d.update({values!r})\n"
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "fi\n"
        'exec /usr/bin/git "$@"\n'
    )
    script.chmod(0o755)
    return shim


def test_a_stop_landing_mid_confirmation_is_not_turned_back_into_active(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """Rule 4: the user owns the exits, and advancing a phase must not undo one.

    Reloading under the lock is not enough on its own -- the advance would still be written,
    replacing the user's ``DISARMED`` with ``ACTIVE`` and restarting enforcement they ended.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    gated_commit(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    env["PATH"] = f"{racing_git(tmp_path, state_path, status='DISARMED', reason='stopped by the user')}:{env['PATH']}"

    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "while this commit was being confirmed" in message
    assert "The phase was NOT advanced" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "DISARMED"
    assert document["phase"] == 1


def test_a_phase_advanced_by_another_hook_is_not_advanced_twice(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Two overlapping confirmations each verified the same commit and each advanced a phase."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    gated_commit(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    env["PATH"] = f"{racing_git(tmp_path, state_path, phase=2, pending_approved_tree='', pending_command='')}:{env['PATH']}"

    _, stdout = confirm(git_repo, env, command=COMMIT)

    assert "The phase was NOT advanced" in context(stdout)
    assert read_state(env, git_repo, SESSION)["phase"] == 2


def test_a_divergence_does_not_reopen_an_activation_the_user_stopped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """RECONCILE is a denying state, so writing it over DISARMED restarts the mode."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command=COMMIT)
    assert verdict == "allow"
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    env["PATH"] = f"{racing_git(tmp_path, state_path, status='DISARMED', reason='stopped by the user')}:{env['PATH']}"

    # No commit was made, so this would otherwise enter RECONCILE for "HEAD did not move".
    _, stdout = confirm(git_repo, env, command=COMMIT)

    message = context(stdout)
    assert "while this commit was being confirmed" in message
    assert "HEAD did not move" in message
    assert read_state(env, git_repo, SESSION)["status"] == "DISARMED"


# --------------------------------------------------------------------------
# A commit that never reached the gate at all
# --------------------------------------------------------------------------


def test_a_commit_the_gate_never_saw_is_caught_afterwards(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The backstop for every way of running git that no string match can catch.

    ``$(printf git) commit``, ``eval``, ``xargs``, a shell function, a Makefile target: the
    ``PreToolUse`` gate decides from a string and cannot see any of them. This asks git after
    the fact instead -- HEAD's tree against the trees a review actually approved.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "landed without ever reaching the gate")

    _, stdout = confirm(git_repo, env, command="make test")

    message = context(stdout)
    assert "no review ever approved" in message
    assert "The commit gate was never consulted" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "RECONCILE"
    assert document["phase"] == 1


def test_an_ordinary_bash_call_is_not_mistaken_for_an_ungated_commit(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """HEAD's tree is the reviewed one at every point of the normal loop, so nothing fires."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    # Before any work, after a phase is committed and verified, and with the worktree dirty.
    for stage in ("before", "after", "dirty"):
        if stage == "after":
            gated_commit(git_repo, env)
            confirm(git_repo, env, command=COMMIT)
        if stage == "dirty":
            (git_repo / "wip.txt").write_text("in progress\n")
        proc = run_hook("confirm-commit", payload(git_repo, command="make test"), cwd=git_repo, env=env)
        assert proc.returncode == 0, stage
        assert proc.stdout == "", stage


def test_a_wrapper_that_commits_and_disarms_does_not_go_unreported(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The Rule 4 escape, end to end, and the most it can currently be reduced to.

    ``bash escape.sh`` is neither a commit nor an escape to a string-matching gate, so the
    script inside it commits and then disarms the mode. Nothing can undo that from here --
    the user may legitimately have stopped with work outstanding, and reverting would take an
    exit away from them, which is the same rule in the other direction. What must not happen
    is what happened before: silence, leaving an unreviewed commit behind a mode that looks
    deliberately stopped.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "escape.sh").write_text("git add -A && git commit -qm bypass\n")
    __import__("subprocess").run(["bash", "escape.sh"], cwd=git_repo, check=True)
    patch_state(env, git_repo, status="DISARMED", reason="stopped by the user")

    _, stdout = confirm(git_repo, env, command="bash escape.sh")

    message = context(stdout)
    assert "no review approved" in message
    assert "The mode is DISARMED" in message
    # Reported, not acted on: the user's exit stands.
    assert read_state(env, git_repo, SESSION)["status"] == "DISARMED"


@pytest.mark.parametrize("status", ["NEEDS_HUMAN", "STALE", "COMPLETE", "RESUMED"])
def test_an_unapproved_head_is_reported_in_every_state_that_cannot_act_on_it(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
) -> None:
    """Entering RECONCILE would downgrade an escalation, so these report instead.

    ``RESUMED`` belongs in this group for the same reason: writing ``RECONCILE`` over a
    retirement would give a dead session something that looks recoverable, when what
    actually happened is that ``resume`` replaced it.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    extra = {"resumed_into": "s2"} if status == "RESUMED" else {}
    patch_state(env, git_repo, status=status, reason="whatever", armed_at=1 if status == "STALE" else 2**31, **extra)

    _, stdout = confirm(git_repo, env, command="make test")

    assert "no review approved" in context(stdout)
    assert read_state(env, git_repo, SESSION)["status"] == status


def test_an_unreadable_repository_is_reported_rather_than_read_as_clean(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """Breaking ``.git`` used to suppress the only report the wrapper escape has.

    ``head_tree`` answers ``""`` for a repository with no commits *and* for one git cannot
    read, and this guard treated empty as "nothing to check here". So the full escape was:
    commit, disarm, then make ``.git`` unreadable -- and every signal went quiet.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    patch_state(env, git_repo, status="DISARMED")
    (git_repo / ".git" / "HEAD").unlink()

    _, stdout = confirm(git_repo, env, command="make test")

    message = context(stdout)
    assert "could not be read" in message
    assert "not a clean result" in message


def test_a_repository_with_no_commits_is_not_reported(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The other half of the distinction: an unborn HEAD really is nothing to check."""
    env = armed_env(clean_env)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    # `--allow-dirty` because a repository with no commits has no HEAD tree to be clean against.
    proc = run_bootstrap(["arm", "--session", SESSION, "--plan", str(plan_file(tmp_path)), "--allow-dirty"], cwd=empty, env=env)
    assert proc.returncode == 0, proc.stdout
    proc = run_bootstrap(["set-phases", "--phase", "one"], cwd=empty, env=env)
    assert proc.returncode == 0, proc.stderr

    result = run_hook("confirm-commit", payload(empty, command="make test"), cwd=empty, env=env)

    assert result.returncode == 0
    assert result.stdout == ""
