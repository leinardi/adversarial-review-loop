"""``dry-run`` is how a change to the reviewer invocation is inspected without a model call.

It has one property worth pinning beyond its output: the scratch activation it invents when
nothing is armed must not be reachable by a hook. A session pointer for it would make every
tool call in the worktree resolve to an activation the user never asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import run_bootstrap
from test_commands_arm import armed_env, plan_file, state_dir

import ocrl


def test_dry_run_prints_the_whole_invocation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)
    (git_repo / "changed.txt").write_text("new work\n")

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    permission = json.loads(proc.stdout.split("# OPENCODE_PERMISSION\n", 1)[1].split("\n", 1)[0])
    assert permission["*"] == "deny"
    assert permission["read"] == "allow"

    argv_block = proc.stdout.split("# argv (one element per line)\n", 1)[1].split("\n# the prompt argument\n", 1)[0]
    argv = argv_block.splitlines()
    assert argv[:3] == ["opencode", "run", "<the prompt printed below, as a single argument>"]
    assert "--dir" in argv
    assert str(git_repo) in argv
    bundle = state_dir(env, git_repo, "s1") / "bundles" / "dry-run"
    assert f"{bundle}/range.txt" in argv

    prompt = ocrl.prompt_path("reviewer-phase").read_text()
    assert prompt in proc.stdout
    assert f"# bundle: {bundle}\n" in proc.stdout
    # The listing comes from `ls -la`, printed after everything the gate composed itself.
    assert "range.txt" in proc.stdout.split(f"# bundle: {bundle}\n", 1)[1]


def test_dry_run_without_an_activation_invents_no_reachable_session(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    (git_repo / "changed.txt").write_text("new work\n")

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "# OPENCODE_PERMISSION" in proc.stdout
    sessions = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "sessions"
    assert not sessions.exists() or list(sessions.iterdir()) == []


def test_dry_run_outside_a_repository_fails(tmp_path: Path, clean_env: dict[str, str]) -> None:
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()

    proc = run_bootstrap(["dry-run"], cwd=elsewhere, env=armed_env(clean_env))

    assert proc.returncode == 1
    assert proc.stderr == "not a git repository\n"
