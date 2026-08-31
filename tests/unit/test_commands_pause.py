"""``pause`` -- moving the pause target mid-flight, without going back through ``resume``.

The command writes exactly one field, so the tests here are about *scope* (what it leaves
alone, ``activation_generation`` above all), the arithmetic at the two ends of the phase
list, and the status allow-list that decides when a target may move at all.
"""

from __future__ import annotations

from pathlib import Path

from conftest import run_bootstrap
from test_commands_arm import armed_env, read_state
from test_commands_pretool import SESSION, active, arm, patch_state


def pause(repo: Path, env: dict[str, str], *args: str) -> tuple[int, str, str]:
    proc = run_bootstrap(["pause", *args], cwd=repo, env=env)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------
# The happy path: one field, and only that field
# --------------------------------------------------------------------------


def test_bare_pause_targets_the_phase_in_flight(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "pausing after phase 1 of 3" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 1


def test_bare_pause_follows_the_phase_the_loop_has_reached(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The target is the phase *in flight*, not phase 1 -- resolved at the moment it runs."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    patch_state(env, git_repo, phase=2)

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "pausing after phase 2 of 3" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 2


def test_pause_changes_nothing_but_the_target(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Above all ``activation_generation``: bumping it would supersede an in-flight review."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    before = read_state(env, git_repo, SESSION)
    code, out, _ = pause(git_repo, env)
    after = read_state(env, git_repo, SESSION)

    assert code == 0, out
    assert after["stop_after_phase"] == 1
    assert before["stop_after_phase"] == 0
    del before["stop_after_phase"], after["stop_after_phase"]
    assert after == before


def test_pause_n_sets_a_later_target(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env, "2")

    assert code == 0, out
    assert "pausing after phase 2 of 3" in out
    assert "phase two" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 2


def test_a_target_ahead_of_the_phase_in_flight_says_the_loop_keeps_going(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The turn does *not* end on the current phase, and the message must not claim it does."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env, "2")

    assert code == 0, out
    assert "keeps going through phase 2, then ends the turn instead of continuing into phase 3" in out
    assert "finishes the phase it is on" not in out


def test_a_target_on_the_last_phase_says_it_changes_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`posttool._advance` tests `next_phase > total` before the target, and `stop.run`'s pause
    branch is guarded by `phase <= total` -- so the last phase completes rather than pausing,
    and neither a paused turn nor a resume may be promised there."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out, _ = pause(git_repo, env, "2")

    assert code == 0, out
    assert "the last one, so this changes nothing on its own" in out
    assert "no pause to resume from" in out
    assert "resume --until 0" not in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 2


def test_an_ordinary_pause_offers_the_resume_that_undoes_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The counterpart to the case above: a real pause *is* resumable, and says so."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "resume --until 0" in out
    assert "no pause to resume from" not in out


def test_clearing_under_an_unresolved_status_still_carries_the_note(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The clearing path is a second write, and the *more* misleading one to leave bare: no
    target is left, so nothing downstream ever mentions the activation again."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    pause(git_repo, env, "2")
    patch_state(env, git_repo, status="NEEDS_HUMAN")

    code, out, _ = pause(git_repo, env, "all")

    assert code == 0, out
    assert "pause target cleared" in out
    assert "Note: this activation is NEEDS_HUMAN" in out
    assert "do not carry on implementing or try to commit" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_clearing_says_no_turn_will_end_paused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Even under ACTIVE: a cleared target is the opposite of a pause, and must not be read
    as one -- there is no `PAUSE_TARGET_REACHED` and no `PAUSED` coming."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    pause(git_repo, env, "2")

    code, out, _ = pause(git_repo, env, "0")

    assert code == 0, out
    assert "no pause target left, so no turn will end paused" in out
    assert "Note: this activation is" not in out


def test_clearing_a_target_that_was_never_set_says_so(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out, _ = pause(git_repo, env, "0")

    assert code == 0, out
    assert "there was none set" in out
    assert "phase 0" not in out


def test_pause_arrives_through_the_args_channel_as_the_skill_delivers_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The slash command substitutes one string; a bare argv token is the shell spelling."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env, "--args", " 3 ")

    assert code == 0, out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 3


def test_an_empty_args_string_is_a_bare_pause(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``$ARGUMENTS`` is substituted unconditionally, so "no argument" arrives as ""."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out, _ = pause(git_repo, env, "--args", "")

    assert code == 0, out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 1


# --------------------------------------------------------------------------
# Clearing
# --------------------------------------------------------------------------


def test_pause_zero_clears_the_target(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    pause(git_repo, env, "2")

    code, out, _ = pause(git_repo, env, "0")

    assert code == 0, out
    assert "pause target cleared (was phase 2 of 2)" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_pause_all_clears_the_target(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    pause(git_repo, env, "1")

    code, out, _ = pause(git_repo, env, "all")

    assert code == 0, out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


# --------------------------------------------------------------------------
# The two ends of the phase list
# --------------------------------------------------------------------------


def test_a_target_past_the_last_phase_is_clamped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Matching ``phases.run``'s handling of an ``--until`` that outran the list."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out, _ = pause(git_repo, env, "9")

    assert code == 0, out
    assert "clamped to 2" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 2


def test_a_target_already_committed_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    patch_state(env, git_repo, phase=3)

    code, out, _ = pause(git_repo, env, "1")

    assert code == 1
    assert "already committed" in out
    assert "Nothing was changed" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_pausing_with_every_phase_committed_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, phase=3)

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "no phase left to pause after" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


# --------------------------------------------------------------------------
# Argument grammar
# --------------------------------------------------------------------------


def test_a_non_numeric_argument_is_refused_naming_pause_not_until(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The shared resolver's message must blame the channel the user actually typed."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, _out, err = pause(git_repo, env, "later")

    assert code == 2
    assert 'pause "later" is not a positive integer' in err
    assert "--until" not in err
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_a_unicode_digit_is_refused_rather_than_crashing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, _out, err = pause(git_repo, env, "²")

    assert code == 2
    assert "is not a positive integer" in err


def test_more_than_one_argument_is_refused_rather_than_silently_dropped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, _out, err = pause(git_repo, env, "--args", "1 2")

    assert code == 2
    assert "usage" in err
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


# --------------------------------------------------------------------------
# The status allow-list
# --------------------------------------------------------------------------


def test_pausing_before_the_phases_are_frozen_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``ARMED``: ``phase`` names nothing yet, so there is no boundary to pause at."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "phase list is not frozen" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_pausing_a_reconcile_is_allowed_but_says_the_divergence_still_stands(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The reconcile is untouched by this; the target it will pause at is still worth setting.

    The note names the outstanding divergence without claiming work is denied -- it is not,
    and the recovery is Claude's own to run.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    patch_state(env, git_repo, status="RECONCILE")

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "Note: this activation is RECONCILE" in out
    assert "Carry out the recovery the gate already prescribed" in out
    # `RECONCILE` is deliberately absent from `pretool._gate_terminal_status`: an Edit still
    # passes, the bounded reset is permitted, and a commit still goes through the ordinary
    # review gate. Its recovery is Claude's to carry out, so telling it to stop here would
    # strand the phase until the user intervenes again.
    assert "do not carry on implementing" not in out
    assert "every mutation is still denied" not in out
    after = read_state(env, git_repo, SESSION)
    assert after["stop_after_phase"] == 1
    assert after["status"] == "RECONCILE"


def test_pausing_a_needs_human_says_every_mutation_is_still_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`pretool` denies every mutation here, so "carry on and commit the phase" is false."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")
    patch_state(env, git_repo, status="NEEDS_HUMAN")

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "Note: this activation is NEEDS_HUMAN" in out
    assert "do not carry on implementing or try to commit" in out
    # The constraint must land before the description it constrains, and that description
    # must not read as an instruction to go and commit right now.
    assert out.index("every mutation is still denied") < out.index("commits it as usual")
    assert "Once that is resolved, the loop finishes the phase it is on" in out
    after = read_state(env, git_repo, SESSION)
    assert after["stop_after_phase"] == 1
    assert after["status"] == "NEEDS_HUMAN"


def test_an_ordinary_pause_carries_no_unresolved_note(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The counterpart: the note must not fire on the case it would only add noise to."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two", "phase three")

    code, out, _ = pause(git_repo, env)

    assert code == 0, out
    assert "Note: this activation is" not in out


def test_pausing_a_complete_activation_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, status="COMPLETE")

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "nothing is gated in this worktree (COMPLETE)" in out


def test_pausing_a_disarmed_activation_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, status="DISARMED")

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "nothing is gated" in out


def test_pausing_a_retired_activation_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, status="RESUMED")

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "no live activation here to pause" in out


def test_pausing_a_stale_activation_points_at_resume(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A stale baseline cannot be continued as it stands, and ``resume`` does both jobs."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, armed_at=1)

    code, out, _ = pause(git_repo, env)

    assert code == 1
    assert "resume --until N" in out
    assert read_state(env, git_repo, SESSION)["stop_after_phase"] == 0


def test_pausing_an_unarmed_worktree_says_so_without_failing(git_repo: Path, clean_env: dict[str, str]) -> None:
    code, out, _ = pause(git_repo, armed_env(clean_env))

    assert code == 0
    assert "not armed in this worktree" in out
