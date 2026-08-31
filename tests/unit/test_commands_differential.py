"""The user-facing commands, driven through the guarded shim against the bootstrap directly.

``scripts/arl.sh`` is a thin guarded shim over ``python3 -I scripts/arl-bootstrap.py`` (see
"Interpreter invocation" in ``AGENTS.md``); for non-hook subcommands it simply ``exec``s the
bootstrap. This proves that pass-through is real: the same argv and the same environment
produce byte-identical output whether invoked through the shim or the bootstrap directly. That
is a stronger claim than "the output looks right" -- these strings are what the user reads
when the mode refuses to arm, and the state document is what a hook decides on afterwards.

Each test runs the shim first, snapshots what it produced, wipes the state root, and runs the
bootstrap directly over the *same* repository -- so the paths embedded in both answers are
identical and any difference is a real one.
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
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import PLUGIN_ROOT, git, run_bootstrap
from test_commands_arm import armed_env, plan_file, state_dir

ARL_SH = PLUGIN_ROOT / "scripts" / "arl.sh"

#: Fields whose value is a clock reading; equality would be a flake, presence is the claim.
_VOLATILE = ("armed_at",)


def shell(argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(ARL_SH), *argv], cwd=str(cwd), env=dict(env), capture_output=True, text=True, check=False)


def wipe(env: Mapping[str, str]) -> None:
    shutil.rmtree(Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop", ignore_errors=True)


def normalised_state(env: dict[str, str], repo: Path, session: str) -> dict[str, object]:
    document: dict[str, object] = json.loads((state_dir(env, repo, session) / "state.json").read_text())
    for key in _VOLATILE:
        assert key in document, f"{key} disappeared from the state document"
        document[key] = "<volatile>"
    # `plan_revisions[*].at` is a clock reading too, one level down: `arm` records it when
    # revision 0 is frozen, and the shim/bootstrap comparison runs `arm` twice, seconds apart.
    revisions = document.get("plan_revisions")
    if isinstance(revisions, list):
        for entry in revisions:
            if isinstance(entry, dict) and "at" in entry:
                entry["at"] = "<volatile>"
    return document


def compare(
    argv_sequence: list[list[str]],
    *,
    repo: Path,
    env: dict[str, str],
    session: str = "s1",
) -> None:
    """Run a sequence of subcommands both ways and assert the last answer and state agree."""
    wipe(env)
    for argv in argv_sequence[:-1]:
        shell(argv, cwd=repo, env=env)
    shell_last = shell(argv_sequence[-1], cwd=repo, env=env)
    shell_state = normalised_state(env, repo, session)

    wipe(env)
    for argv in argv_sequence[:-1]:
        run_bootstrap(argv, cwd=repo, env=env)
    python_last = run_bootstrap(argv_sequence[-1], cwd=repo, env=env)
    python_state = normalised_state(env, repo, session)

    assert python_last.stdout == shell_last.stdout
    assert python_last.stderr == shell_last.stderr
    assert python_last.returncode == shell_last.returncode
    assert python_state == shell_state


def test_arming_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare([["arm", "--session", "s1", "--plan", str(plan)]], repo=git_repo, env=env)


def test_arming_through_the_slash_command_argument_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``--args`` is the only shape the ``implement`` skill ever produces."""
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare([["arm", "--session", "s1", "--args", f"  {plan} --allow-dirty "]], repo=git_repo, env=env)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["arm", "--session", "s1"], id="no-plan"),
        pytest.param(["arm", "--session", "s1", "--plan", "does-not-exist.md"], id="missing-file"),
        pytest.param(["arm", "--session", "s1", "--args", 'x"; id; echo "'], id="injection-shaped"),
        pytest.param(["arm", "--session", "s1", "--args", "plan.md --nonsense"], id="unknown-flag"),
    ],
)
def test_arming_failures_read_identically(git_repo: Path, clean_env: dict[str, str], argv: list[str]) -> None:
    """The refusal text *is* the product here: it is what the user is shown and acts on."""
    compare([argv], repo=git_repo, env=armed_env(clean_env))


def test_a_dirty_worktree_is_refused_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    (git_repo / "untracked.txt").write_text("work in progress\n")
    compare([["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))]], repo=git_repo, env=env)


def test_set_phases_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "first thing", "--phase", "second thing"],
        ],
        repo=git_repo,
        env=env,
    )


def test_a_second_set_phases_is_refused_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "first thing"],
            ["set-phases", "--phase", "something else"],
        ],
        repo=git_repo,
        env=env,
    )


def test_status_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Including the empty-list spacing, which is easy to get one newline wrong."""
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare([["arm", "--session", "s1", "--plan", str(plan)], ["status"]], repo=git_repo, env=env)


def test_status_with_phases_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "first thing", "--phase", "second thing"],
            ["status"],
        ],
        repo=git_repo,
        env=env,
    )


def test_defer_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_MAX_DEFERS="1")
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "one"],
            ["defer", "--reason", "need a decision"],
        ],
        repo=git_repo,
        env=env,
    )


def test_an_exhausted_defer_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_MAX_DEFERS="1")
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "one"],
            ["defer", "--reason", "first"],
            ["defer", "--reason", "second"],
        ],
        repo=git_repo,
        env=env,
    )


def test_deactivate_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare(
        [
            ["arm", "--session", "s1", "--plan", str(plan)],
            ["set-phases", "--phase", "one", "--phase", "two"],
            ["deactivate"],
        ],
        repo=git_repo,
        env=env,
    )


def test_report_reads_identically(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    compare([["arm", "--session", "s1", "--plan", str(plan)], ["report"]], repo=git_repo, env=env)


def test_the_unarmed_answers_read_identically(git_repo: Path, clean_env: dict[str, str]) -> None:
    """No state exists on this path, so the comparison is of the messages alone."""
    env = armed_env(clean_env)
    for argv in (["status"], ["report"], ["finish"], ["deactivate"], ["set-phases", "--phase", "one"], ["defer", "--reason", "x"]):
        wipe(env)
        from_shell = shell(argv, cwd=git_repo, env=env)
        wipe(env)
        from_python = run_bootstrap(argv, cwd=git_repo, env=env)
        assert from_python.stdout == from_shell.stdout, argv
        assert from_python.stderr == from_shell.stderr, argv
        assert from_python.returncode == from_shell.returncode, argv


def test_the_frozen_plan_is_byte_identical(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The frozen copy is what every review is shown; a re-encoded one is a different plan."""
    env = armed_env(clean_env)
    plan = tmp_path / "plan.md"
    # Deliberately not valid UTF-8: the shell's `cp` did not care, and neither may this.
    plan.write_bytes(b"# plan\n\n\xff\xfe raw bytes \xc3\xa9\n")

    wipe(env)
    shell(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    from_shell = (state_dir(env, git_repo, "s1") / "plan.frozen.md").read_bytes()

    wipe(env)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    from_python = (state_dir(env, git_repo, "s1") / "plan.frozen.md").read_bytes()

    assert from_python == from_shell == plan.read_bytes()


def test_the_session_pointer_is_byte_identical(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A pointer the other implementation cannot read is a rollback that denies everything."""
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    root = Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop"

    wipe(env)
    shell(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    shell_pointer = (root / "sessions" / "s1").read_bytes()

    wipe(env)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    python_pointer = (root / "sessions" / "s1").read_bytes()

    assert python_pointer == shell_pointer == f"{git_repo}\n".encode()


def test_an_empty_repository_gets_the_same_baseline(tmp_path: Path, clean_env: dict[str, str]) -> None:
    """With no HEAD the baseline is the empty tree, computed by git rather than hard-coded."""
    env = armed_env(clean_env)
    repo = tmp_path / "empty"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    plan = plan_file(tmp_path)

    compare([["arm", "--session", "s1", "--plan", str(plan)]], repo=repo, env=env)
