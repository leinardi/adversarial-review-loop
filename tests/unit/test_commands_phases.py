"""``set-phases`` and the replan fence.

Two different mutations share one entrypoint here: the ordinary once-only freeze
(``ARMED -> ACTIVE``), covered mostly in ``test_commands_session.py``, and the narrower
``--replan`` freeze a resume may grant permission for (``phases 1..phase-1`` immutable, the
rest redefinable). This file is about the second one, and the fence around it: while
``replan_pending`` is set, ``pretool`` must deny everything except the exact ``set-phases``
command that consumes the token, exactly as it does for an unfrozen (``ARMED``) activation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import run_bootstrap
from test_commands_arm import armed_env, read_state
from test_commands_pretool import ENTRYPOINT, active, arm, armed, patch_state, pretool

SESSION = "s1"


def set_phases(repo: Path, env: dict[str, str], *phases: str) -> subprocess.CompletedProcess[str]:
    argv = ["set-phases"]
    for phase in phases:
        argv += ["--phase", phase]
    return run_bootstrap(argv, cwd=repo, env=env)


# --------------------------------------------------------------------------
# The replan fence in pretool
# --------------------------------------------------------------------------


def test_replan_pending_denies_everything_but_set_phases(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    patch_state(env, git_repo, replan_pending=True)

    verdict, reason = pretool(git_repo, env, tool="Edit")
    assert verdict == "deny"
    assert "permission to redefine the remaining phases" in reason

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')
    assert verdict == "deny"

    verdict, reason = pretool(git_repo, env, command=f"{ENTRYPOINT} set-phases --phase 'two revised'")
    assert verdict == "allow"
    assert "set-phases is the one command allowed while a replan is pending" in reason


# --------------------------------------------------------------------------
# phases.py's replan branch
# --------------------------------------------------------------------------


def test_set_phases_refuses_without_replan_pending_once_frozen(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "phase one", "phase two")

    proc = set_phases(git_repo, env, "something else")
    assert proc.returncode == 1
    assert "the phase list is already frozen" in proc.stderr
    assert read_state(env, git_repo, SESSION)["phases"] == ["phase one", "phase two"]


def test_replan_accepts_and_preserves_the_committed_prefix(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "phase one", "phase two", "phase three")
    before_generation = read_state(env, git_repo, SESSION)["activation_generation"]
    assert isinstance(before_generation, int)
    # Simulate phase 1 having already been committed and the activation moved to phase 2 --
    # `--replan` only ever affects the current phase onward.
    patch_state(env, git_repo, phase=2, replan_pending=True)

    proc = set_phases(git_repo, env, "phase two, revised", "phase three, revised", "phase four, new")
    assert proc.returncode == 0, proc.stderr
    assert "phases 2..4 redefined" in proc.stdout

    document = read_state(env, git_repo, SESSION)
    assert document["phases"] == ["phase one", "phase two, revised", "phase three, revised", "phase four, new"]
    assert document["replan_pending"] is False
    assert document["activation_generation"] == before_generation + 1
    assert document["status"] == "ACTIVE"


def test_replan_refused_when_the_worktree_is_dirty(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "phase one", "phase two")
    patch_state(env, git_repo, replan_pending=True)
    (git_repo / "dirty.txt").write_text("uncommitted\n")

    proc = set_phases(git_repo, env, "phase two, revised")
    assert proc.returncode == 1
    assert "the worktree is dirty" in proc.stderr
    assert read_state(env, git_repo, SESSION)["phases"] == ["phase one", "phase two"]
    assert read_state(env, git_repo, SESSION)["replan_pending"] is True


def test_replan_pending_does_not_survive_the_first_freeze(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Belt and braces: even if a stray ``replan_pending`` reached an unfrozen activation, the
    ordinary first freeze clears it, so a *second* ``set-phases`` cannot use it to rewrite work
    already in flight."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    patch_state(env, git_repo, replan_pending=True)

    proc = set_phases(git_repo, env, "phase one", "phase two")
    assert proc.returncode == 0, proc.stderr
    assert read_state(env, git_repo, SESSION)["replan_pending"] is False

    proc = set_phases(git_repo, env, "something else")
    assert proc.returncode == 1
    assert "the phase list is already frozen" in proc.stderr


def test_replan_max_phases_counts_the_whole_combined_list(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "phase one", "phase two")
    # Phase 1 is committed and immutable, so only it is carried into `combined`; the tail
    # supplied below must push the total (1 committed + N tail) past MAX_PHASES on its own.
    patch_state(env, git_repo, phase=2, replan_pending=True)

    proc = set_phases(git_repo, env, *[f"tail {n}" for n in range(30)])
    assert proc.returncode == 1
    assert "is more than the 30 this gate accepts" in proc.stderr
    assert read_state(env, git_repo, SESSION)["phases"] == ["phase one", "phase two"]
