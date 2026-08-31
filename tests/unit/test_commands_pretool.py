"""``pretool`` -- the hook that decides whether a mutation happens.

Everything here drives the real entrypoint through ``scripts/arl-bootstrap.py`` with a
payload on stdin, because the property under test is the whole contract: what lands on
stdout, what the exit status is, and what the activation says afterwards. A helper returning
the right verdict while the entrypoint emits the wrong JSON is not a gate.

The invariant every test in this file is ultimately about: **no failure becomes an allow.**
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
from conftest import FAKE_REVIEWER, decision, git, run_bootstrap, run_hook
from test_commands_arm import armed_env, plan_file, read_state, state_dir

from arl import paths

SESSION = "s1"

#: The gate's own script, as ``arm`` prints it for the model to copy. ``set-phases`` accepts
#: this exact path and nothing else, so the tests pin it rather than inherit it.
PLUGIN_ROOT = "/plugin"
ENTRYPOINT = f"{PLUGIN_ROOT}/scripts/arl.sh"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def armed(clean_env: dict[str, str], **extra: str) -> dict[str, str]:
    """``armed_env`` with the plugin root pinned, so the trusted entrypoint path is known."""
    return armed_env(clean_env, CLAUDE_PLUGIN_ROOT=PLUGIN_ROOT, **extra)


def payload(repo: Path, tool: str = "Bash", command: str = "", session: str = SESSION) -> dict[str, object]:
    return {"session_id": session, "cwd": str(repo), "tool_name": tool, "tool_input": {"command": command}}


def pretool(repo: Path, env: dict[str, str], **kwargs: object) -> tuple[str, str]:
    proc = run_hook("pretool", payload(repo, **kwargs), cwd=repo, env=env)  # type: ignore[arg-type]
    assert proc.returncode == 0, proc.stderr
    return decision(proc)


def arm(repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    proc = run_bootstrap(["arm", "--session", SESSION, "--plan", str(plan_file(tmp_path))], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout


def set_phases(repo: Path, env: dict[str, str], *phases: str) -> None:
    argv = ["set-phases"]
    for phase in phases:
        argv += ["--phase", phase]
    proc = run_bootstrap(argv, cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr


def active(repo: Path, tmp_path: Path, env: dict[str, str], *phases: str) -> None:
    """An armed activation with its phase list frozen -- the ordinary working state."""
    arm(repo, tmp_path, env)
    set_phases(repo, env, *(phases or ("phase one",)))


def active_until(repo: Path, tmp_path: Path, env: dict[str, str], until: int, *phases: str) -> None:
    """An armed activation with a pause target and its phase list frozen."""
    plan = plan_file(tmp_path)
    proc = run_bootstrap(["arm", "--session", SESSION, "--args", f"{plan} --until {until}"], cwd=repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(repo, env, *(phases or ("phase one", "phase two", "phase three")))


def patch_state(env: dict[str, str], repo: Path, **values: object) -> None:
    path = state_dir(env, repo, SESSION) / "state.json"
    document = json.loads(path.read_text())
    document.update(values)
    path.write_text(json.dumps(document))


# --------------------------------------------------------------------------
# Rule 0: no pointer means this session never bound to an activation
# --------------------------------------------------------------------------


def test_a_payload_with_no_session_id_denies(git_repo: Path, clean_env: dict[str, str]) -> None:
    """The gate cannot tell which activation this is, so it denies rather than guessing."""
    verdict, reason = pretool(git_repo, clean_env, session="")

    assert verdict == "deny"
    assert "carried no session id" in reason


def test_no_pointer_in_a_worktree_nobody_armed_passes(git_repo: Path, clean_env: dict[str, str]) -> None:
    """The hooks register at plugin load, so this is every session that never armed: silent."""
    proc = run_hook("pretool", payload(git_repo, tool="Write"), cwd=git_repo, env=clean_env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not list((Path(clean_env["XDG_STATE_HOME"]) / "adversarial-review-loop").rglob("state.json"))


def test_no_pointer_in_a_worktree_armed_by_another_session_denies(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A second session in an armed worktree is unbound: denied until ``resume`` binds it."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, tool="Write", session="s2")

    assert verdict == "deny"
    assert "not bound to it" in reason
    assert f"activation {SESSION}, status ACTIVE" in reason
    assert "/adversarial-review-loop:resume" in reason
    assert "Do not implement the plan" in reason
    # Nothing was written: the activation belongs to the other session.
    assert read_state(env, git_repo, SESSION)["status"] == "ACTIVE"
    assert not (state_dir(env, git_repo, "s2")).exists()


@pytest.mark.parametrize("status", ["COMPLETE", "DISARMED"])
def test_no_pointer_passes_once_the_other_activation_ended(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)

    proc = run_hook("pretool", payload(git_repo, tool="Write", session="s2"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize("status", ["ARMED", "ARM_FAILED", "NEEDS_HUMAN", "STALE", "RESUMED"])
def test_no_pointer_denies_under_every_other_status(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    """Only a loop that ended releases an unbound session; anything else still guards the tree."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)

    verdict, reason = pretool(git_repo, env, tool="Write", session="s2")

    assert verdict == "deny"
    assert f"status {status}" in reason


def test_no_pointer_denies_when_the_latest_activation_has_no_document(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``latest`` says the worktree was armed and nothing says it stopped: fail closed."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").unlink()

    verdict, reason = pretool(git_repo, env, tool="Write", session="s2")

    assert verdict == "deny"
    assert "status unreadable" in reason


def test_no_pointer_with_git_unavailable_denies(git_repo: Path, clean_env: dict[str, str]) -> None:
    """ "Nothing to enforce" must be proven: a git that cannot run is a denial, never a pass."""
    env = {**clean_env, "PATH": str(Path(clean_env["HOME"]) / "empty-path")}

    verdict, reason = pretool(git_repo, env, tool="Write", session="s2")

    assert verdict == "deny"
    assert "could not tell which repository" in reason
    assert "git could not be run" in reason
    assert not list((Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop").rglob("state.json"))


def test_no_pointer_with_a_vanished_cwd_denies(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_hook("pretool", {**payload(git_repo, tool="Write", session="s2"), "cwd": str(git_repo / "gone")}, cwd=git_repo, env=clean_env)
    assert proc.returncode == 0, proc.stderr

    verdict, reason = decision(proc)

    assert verdict == "deny"
    assert "not a directory" in reason


def test_a_bound_session_denies_when_git_cannot_place_a_subdirectory(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """ "Somewhere else" must be proven: with git off PATH, a subdirectory of the armed worktree is not "another repository"."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    sub = git_repo / "sub"
    sub.mkdir()
    env = {**env, "PATH": str(Path(clean_env["HOME"]) / "empty-path")}

    proc = run_hook(
        "pretool", {**payload(git_repo, tool="Bash", command="/usr/bin/git add -A && /usr/bin/git commit -m x"), "cwd": str(sub)}, cwd=sub, env=env
    )
    assert proc.returncode == 0, proc.stderr

    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "could not tell which repository" in reason


def test_a_bound_session_denies_when_its_cwd_is_gone(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_hook("pretool", {**payload(git_repo, tool="Write"), "cwd": str(git_repo / "gone")}, cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stderr

    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "not a directory" in reason


def test_a_bound_session_still_passes_a_proven_other_repository(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    other = tmp_path / "plain"
    other.mkdir()

    proc = run_hook("pretool", {**payload(git_repo, tool="Write"), "cwd": str(other)}, cwd=other, env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def test_no_pointer_in_another_worktree_of_the_same_session_passes(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The unbound check is per worktree: an unrelated repository is not guarded by this one."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")

    proc = run_hook("pretool", payload(other, tool="Write", session="s2"), cwd=other, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_no_pointer_still_lets_a_read_through(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Read-only tools are permitted in every state, including this one."""
    proc = run_hook("pretool", payload(git_repo, tool="Read"), cwd=git_repo, env=clean_env)

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (state_dir(clean_env, git_repo, SESSION) / "state.json").exists()


def test_an_unusable_session_id_is_not_turned_into_a_state_path(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A traversing id names no activation, so it is unbound like any other; no path is ever composed from it."""
    proc = run_hook("pretool", payload(git_repo, tool="Write", session="../escape"), cwd=git_repo, env=clean_env)
    assert proc.returncode == 0
    assert proc.stdout == ""
    root = Path(clean_env["XDG_STATE_HOME"]) / "adversarial-review-loop"
    assert not (root / "worktrees" / "escape").exists()
    assert not list(root.rglob("escape*"))

    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    verdict, reason = pretool(git_repo, env, tool="Write", session="../escape")

    assert verdict == "deny"
    assert "not bound to it" in reason
    assert not list(root.rglob("escape*"))


def test_missing_state_denies_rather_than_passing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The pointer says this session armed, so absent state is the fail-open case."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").unlink()

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "activation state for this session is missing" in reason


# --------------------------------------------------------------------------
# Scope and the hot-path hoist
# --------------------------------------------------------------------------


def test_work_outside_the_armed_worktree_is_untouched(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = run_hook("pretool", payload(elsewhere, tool="Write"), cwd=elsewhere, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize("status", ["ARM_FAILED", "NEEDS_HUMAN", "RECONCILE", "ARMED", "RESUMED"])
def test_a_read_only_tool_is_permitted_in_every_denying_state(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
) -> None:
    """The hoist is what makes this true, and it is why a read-only deny would be unreachable."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status, reason="whatever")

    proc = run_hook("pretool", payload(git_repo, tool="Read"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize("status", ["COMPLETE", "DISARMED"])
def test_a_finished_activation_stops_gating(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)

    proc = run_hook("pretool", payload(git_repo, tool="Write"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


# --------------------------------------------------------------------------
# Status branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param("ARM_FAILED", "Arming failed, so the review loop is NOT active", id="arm-failed"),
        pytest.param("NEEDS_HUMAN", "escalated to NEEDS_HUMAN", id="needs-human"),
    ],
)
def test_a_denying_status_denies_a_mutation(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
    expected: str,
) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status, reason="because")

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert expected in reason
    assert "because" in reason


def test_a_resumed_activation_denies_every_mutation_naming_the_successor(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A retired activation must deny in the old session, not gate as if it were still live.

    Otherwise the old session can still approve a commit through the ordinary commit gate,
    and ``confirm-commit`` would then advance a document that ``resume`` already retired --
    resurrecting it into a second live activation over the same worktree.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RESUMED", resumed_into="s2")

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "retired" in reason
    assert "s2" in reason


def test_a_resumed_activation_ignores_the_ttl(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """RESUMED already denies everything; the TTL turning it into a generic STALE would lose
    the message naming the successor session."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RESUMED", resumed_into="s2", armed_at=1)

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "retired" in reason
    assert "older than ttl_hours" not in reason


def test_an_expired_activation_blocks_rather_than_disarming(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """STALE is a denial, not a timer that quietly turns enforcement off (Rule 1)."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, armed_at=1)

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "older than ttl_hours" in reason


def test_nothing_may_change_before_the_phase_list_is_frozen(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "phase list has not been frozen yet" in reason


def test_set_phases_is_the_one_command_allowed_while_armed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=f"{ENTRYPOINT} set-phases --phase 'one'")

    assert verdict == "allow"
    assert "set-phases is the one command allowed" in reason


# --------------------------------------------------------------------------
# A corrupted plan_revisions entry escalates rather than substituting evidence
# --------------------------------------------------------------------------


def test_a_corrupted_active_revision_escalates_before_allowing_set_phases_while_armed(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str]
) -> None:
    """The active revision is verified *before* ``set-phases`` is even considered for an
    allow: a corrupted record must block the freeze itself, not just an unrelated denial's
    wording."""
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)
    patch_state(env, git_repo, plan_revisions=[{"at": 1, "phase": 1, "sha256": "not-a-real-hash", "file": "plan.frozen.md"}])

    verdict, reason = pretool(git_repo, env, command=f"{ENTRYPOINT} set-phases --phase 'one'")

    assert verdict == "deny"
    assert "NEEDS_HUMAN" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert "no valid sha256" in str(document["reason"])


def test_a_traversal_file_in_plan_revisions_never_reaches_a_message_while_armed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)
    patch_state(env, git_repo, plan_revisions=[{"at": 1, "phase": 1, "sha256": "a" * 64, "file": "../../etc/passwd"}])

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    # Denied as NEEDS_HUMAN before `PHASES_NOT_FROZEN` is ever reached -- the traversal path
    # is quoted in the diagnostic (naming what is wrong), but never turned into an instruction
    # telling the model to go read it.
    assert "Read the frozen plan" not in reason
    assert "NEEDS_HUMAN" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_a_corrupted_active_revision_escalates_during_a_replan(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    active(git_repo, tmp_path, env, "phase one")
    patch_state(
        env,
        git_repo,
        replan_pending=True,
        plan_revisions=[{"at": 1, "phase": 1, "sha256": "a" * 64, "file": "missing.md"}],
    )

    verdict, reason = pretool(git_repo, env, command=f"{ENTRYPOINT} set-phases --phase 'one revised'")

    assert verdict == "deny"
    assert "NEEDS_HUMAN" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_a_non_object_plan_revisions_entry_escalates_rather_than_crashing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A malformed ``plan_revisions`` entry (not even an object) must still produce a durable
    ``NEEDS_HUMAN`` with a diagnostic, not an uncontrolled crash caught only by the generic
    fail-closed guard."""
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)
    patch_state(env, git_repo, plan_revisions=["not-an-object"])

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "NEEDS_HUMAN" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert "not an object" in str(document["reason"])


# --------------------------------------------------------------------------
# Rule 4: the user owns the exits
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "arl.sh finish",
        "/some/where/arl deactivate",
        "cd /x && arl.sh finish",
        "arl.sh resume",
        "arl.sh config model x",
        "arl.sh accept",
        "arl.sh accept --reason x",
        "arl.sh pause",
        "arl.sh pause 2",
    ],
)
def test_claude_may_not_end_the_loop_itself(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], command: str) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "user-only commands" in reason


def test_the_escape_denial_outranks_a_commit_in_the_same_command(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command="git commit -m x && arl.sh finish")

    assert verdict == "deny"
    assert "user-only commands" in reason


# --------------------------------------------------------------------------
# The commit gate
# --------------------------------------------------------------------------


def test_a_non_bash_tool_is_left_to_the_normal_flow(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_hook("pretool", payload(git_repo, tool="Write"), cwd=git_repo, env=env)

    assert proc.returncode == 0
    assert proc.stdout == ""


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("git commit --amend -m x", "amending rewrites the commit", id="amend"),
        pytest.param("git commit -m x a.txt", "partial commit", id="pathspec"),
        pytest.param("make build && git commit -m x", 'segment starts with "make"', id="build-first"),
        pytest.param("git commit -m 'a message' extra.txt", "partial commit", id="second-pathspec"),
        pytest.param("git commit -m x; rm -rf /", "shell metacharacter", id="sequencing"),
        pytest.param('git add -A && git commit -m "x" 2>&1 | tail -40', "piped or redirected", id="piped-to-trim-the-output"),
        pytest.param("git commit -m x &>out.log", "piped or redirected", id="ampersand-redirect"),
    ],
)
def test_a_commit_command_that_cannot_be_shown_safe_is_denied(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    command: str,
    expected: str,
) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "This commit command was not accepted" in reason
    assert expected in reason


def test_an_unchanged_tree_needs_no_review(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command='git commit -m "x"')

    assert verdict == "allow"
    assert "byte-identical to the last approved tree" in reason


def test_a_git_option_shaped_base_tree_in_state_is_refused_not_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``state.json`` is not a trust boundary. A ``last_approved_tree`` shaped like
    ``--output=<file>`` would have ``git diff`` write inside the reviewed repo and report an
    empty diff -- which the gate would read as "no changes" and approve. It must deny."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    pwned = git_repo / "PWNED"
    patch_state(env, git_repo, last_approved_tree=f"--output={pwned}")
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "not a usable git object id" in reason
    assert not pwned.exists(), "git must never have been asked to write this file"
    assert not read_state(env, git_repo, SESSION)["pending_approved_tree"]


def test_a_well_formed_but_unresolvable_base_tree_still_denies(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Shape is fine, so it reaches ``git diff`` -- whose non-zero exit then denies (Rule 1)."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, last_approved_tree="0" * 40)
    (git_repo / "new.txt").write_text("work\n")

    verdict, _ = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert not read_state(env, git_repo, SESSION)["pending_approved_tree"]


def test_an_approving_review_allows_the_commit_and_records_the_pending_tree(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "allow"
    assert "the reviewer approved phase 1" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["pending_approved_tree"]
    assert document["pending_command"] == 'git add -A && git commit -m "x"'
    assert document["pending_approved_tree"] in document["approved_trees"]  # type: ignore[operator]


def test_a_blocking_review_denies_and_returns_every_finding(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "requires changes before phase 1" in reason
    assert "Returns success on a failed lookup" in reason
    assert not read_state(env, git_repo, SESSION)["pending_approved_tree"]


def test_a_blocking_review_offers_clarify_with_the_budget_left(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes", ARL_MAX_CLARIFICATIONS="2")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "Clarifications left: 2 of 2." in reason
    # Its own line, under the headline and above the findings -- not trailing the headline
    # sentence, which is where it went unread for two whole activations.
    lines = reason.splitlines()
    hint = next(i for i, line in enumerate(lines) if line.startswith("If a finding is ambiguous"))
    assert lines[hint - 1] == ""
    assert lines[0].startswith("adversarial-review-loop: the reviewer requires changes before phase 1")
    assert reason.index("Clarifications left") < reason.index("Blocking findings")


def test_a_blocking_review_drops_the_clarify_offer_when_none_are_left(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pointing at a command that can only refuse would cost a round to find out."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes", ARL_MAX_CLARIFICATIONS="0")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "requires changes before phase 1" in reason
    assert "clarify" not in reason


def test_a_reviewer_the_gate_contradicts_still_blocks(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The reviewer's own verdict is advisory; an actionable critical finding blocks anyway."""
    env = armed_env(clean_env, ARL_FAKE_MODE="approve-with-critical")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "Nil deref" in reason


def test_a_failing_reviewer_counts_and_then_escalates(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A failed review is never an approval, and a run of them is not an infinite retry."""
    env = armed_env(clean_env, ARL_FAKE_MODE="nonzero", ARL_MAX_FAILURES="1")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    first = pretool(git_repo, env, command=command)
    assert first[0] == "deny"
    assert "1 of 1 operational failures since the last approval" in first[1]
    assert read_state(env, git_repo, SESSION)["failures"] == 1

    second = pretool(git_repo, env, command=command)
    assert second[0] == "deny"
    assert "escalated to NEEDS_HUMAN" in second[1]
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_a_transient_failure_is_counted_separately_from_operational_ones(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A timeout or a rate limit is not the same failure as a missing binary (phase 6): it
    paces its own retries against ``max_transient_failures`` and leaves the ordinary
    ``failures``/``max_failures`` budget untouched.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="rate-limited", ARL_MAX_FAILURES="1")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "1 of 5 transient failures since the last approval" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["failures"] == 0
    assert document["transient_failures"] == 1
    assert int(document["retry_not_before"]) > 0  # type: ignore[call-overload]


def test_a_backoff_in_effect_denies_without_invoking_the_reviewer(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The retry-not-before check runs after the snapshot and the free-shortcut checks
    (byte-identical tree, already-approved tree, no diff, ignore_globs) -- see
    ``_check_retry_backoff``'s docstring for why it is not ahead of them -- but always ahead
    of another provider call. Proven here by pointing the retried attempt at a reviewer
    command that does not exist: if the check were skipped, that attempt would crash rather
    than deny, and if it invoked anyway ``report_seq`` would advance.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="rate-limited")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    pretool(git_repo, env, command=command)
    before = read_state(env, git_repo, SESSION)["report_seq"]

    missing_reviewer_env = {**env, "ARL_REVIEWER_CMD": str(tmp_path / "reviewer-must-not-run")}
    verdict, reason = pretool(git_repo, missing_reviewer_env, command=command)

    assert verdict == "deny"
    assert "Retry in" in reason
    assert read_state(env, git_repo, SESSION)["report_seq"] == before


def test_exhausting_the_transient_budget_escalates_to_needs_human(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="rate-limited", ARL_MAX_TRANSIENT_FAILURES="1")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    pretool(git_repo, env, command=command)
    # Clears the backoff so this attempt reaches the reviewer rather than being denied by
    # the wait itself -- this test is about the budget being exhausted, not about pacing.
    patch_state(env, git_repo, retry_not_before=0)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "escalated to NEEDS_HUMAN" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_an_approval_clears_the_transient_counters(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="rate-limited")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    pretool(git_repo, env, command=command)
    assert read_state(env, git_repo, SESSION)["transient_failures"] == 1
    patch_state(env, git_repo, retry_not_before=0)

    approving_env = {**env, "ARL_FAKE_MODE": "approve"}
    verdict, _ = pretool(git_repo, approving_env, command=command)

    assert verdict == "allow"
    document = read_state(env, git_repo, SESSION)
    assert document["transient_failures"] == 0
    assert document["retry_not_before"] == 0


def test_a_stale_backoff_never_blocks_a_tree_already_approved(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A busy-slot loser records its own transient failure -- and sets a fresh

    ``retry_not_before`` -- independently of whatever a concurrent, winning review for the
    same label did; the two writes are not ordered against each other. If the winner's
    approval happens first, the loser's later write must not have the backoff it sets block
    the tree the approval already cleared -- ``_check_retry_backoff`` runs only after every
    free shortcut (byte-identical tree, an already-approved tree, no diff, ignore_globs) has
    been ruled out, so this survives regardless of write order. Simulated directly, by
    setting ``retry_not_before`` into the future *after* the approval already landed, rather
    than through real concurrency.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="approve")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    verdict, _ = pretool(git_repo, env, command=command)
    assert verdict == "allow"

    patch_state(env, git_repo, retry_not_before=int(time.time()) + 100)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "allow"
    assert "already approved" in reason


def test_an_approval_superseded_by_a_newer_round_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The window the active-review claim cannot cover, because it is already released.

    ``reviewer.execute`` releases the claim on the way out; the approval is written afterwards,
    in the caller's own transaction. A second review of the same label can claim the freed slot
    and record a ``CHANGES_REQUIRED`` in that window -- and because ``round_history`` is not one
    of ``hooks.Activation``'s fields, the fingerprint check still matches, so the stale
    ``APPROVED`` was written straight over the newer, blocking verdict.

    The stand-in reviewer plays the concurrent review's part: it appends the newer round and
    then returns ``APPROVED``. Fails on the old code, which allowed the commit."""
    env = armed_env(clean_env, ARL_FAKE_MODE="approve-superseded")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    before = read_state(env, git_repo, SESSION)["approved_trees"]

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "no longer the current one" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["pending_approved_tree"] == "", "nothing may be left pending"
    assert document["approved_trees"] == before, "and this tree must not have been marked approved either"


def test_a_transient_failure_whose_activation_moved_first_is_not_counted(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Phase 6's fingerprint guard in ``_review_failed``. A genuinely concurrent, winning

    review of the same label can approve -- writing ``pending_approved_tree``, one of
    ``hooks.Activation``'s own fields -- while this attempt is still deciding it hit a rate
    limit; counting the failure against whatever state exists by the time it is recorded
    would attribute it to an activation this specific attempt never saw. The fake reviewer
    mutates ``pending_approved_tree`` itself, mid-invocation, the same technique
    ``clarify-mutate`` uses.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="rate-limited-elsewhere")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    command = 'git add -A && git commit -m "x"'

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "while this commit was being gated" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["transient_failures"] == 0
    assert document["retry_not_before"] == 0


def test_an_oversized_file_is_never_silently_left_out_of_the_snapshot(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_MAX_FILE_BYTES="16")
    active(git_repo, tmp_path, env)
    (git_repo / "big.bin").write_bytes(b"x" * 64)

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "above max_file_bytes (16)" in reason
    assert "big.bin" in reason


def test_a_stale_pending_approval_is_cleared_before_a_new_attempt(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """An approval from an earlier attempt is stale by definition once a new commit is gated."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    patch_state(env, git_repo, pending_approved_tree="deadbeef", pending_command="git commit -m old")

    verdict, _ = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert read_state(env, git_repo, SESSION)["pending_approved_tree"] == ""


def test_a_rejected_shape_never_grants_a_pending_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The shape check runs first, exactly as the shell ordered it.

    A command the gate cannot read as a safe commit sequence therefore leaves an earlier
    pending approval where it was. That is inert rather than a hole: ``confirm-commit`` still
    requires an exact ``HEAD^{tree}`` match *and* the very command that was approved, so a
    leftover approval cannot be consumed by this one.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, pending_approved_tree="deadbeef", pending_command="git commit -m old")

    verdict, _ = pretool(git_repo, env, command="git commit --amend -m x")

    assert verdict == "deny"
    assert read_state(env, git_repo, SESSION)["pending_approved_tree"] == "deadbeef"


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def test_a_bounded_reset_is_permitted_during_a_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    parent = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "bad.txt").write_text("bad\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "bad")
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent=parent)

    verdict, reason = pretool(git_repo, env, command=f"git reset --soft {parent}")

    assert verdict == "allow"
    assert "bounded recovery reset" in reason


def test_a_reset_to_anything_but_the_diverging_parent_is_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    head = git(git_repo, "rev-parse", "HEAD")
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent="0" * 40)

    verdict, reason = pretool(git_repo, env, command=f"git reset --soft {head}")

    assert verdict == "deny"
    assert "the only permitted target during this reconcile" in reason


def test_a_reset_target_that_predates_the_activation_commit_is_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The guard working as intended: `bad_commit_parent` is a real ancestor of the
    activation commit, so resetting to it would rewind history the loop started after."""
    older = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "later.txt").write_text("later\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "later")
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)  # armed at the "later" commit
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent=older)

    verdict, reason = pretool(git_repo, env, command=f"git reset --soft {older}")

    assert verdict == "deny"
    assert "rewind history that predates it" in reason


def test_a_tampered_activation_commit_denies_the_reset_rather_than_failing_open(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """state.json is not a trust boundary. An option-shaped `activation_commit` would make
    the plain ancestry check fold git's error into "does not predate" -> allow. The shape
    guard plus `is_ancestor_checked` deny on an unanswered history question instead."""
    older = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "later.txt").write_text("later\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "later")
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent=older, activation_commit=f"--output={git_repo / 'PWNED'}")

    verdict, reason = pretool(git_repo, env, command=f"git reset --soft {older}")

    assert verdict == "deny"
    assert "could not be checked" in reason
    assert not (git_repo / "PWNED").exists()


def test_a_hard_reset_is_denied_during_a_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """--hard discards the very working-tree content the gate exists to review."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    head = git(git_repo, "rev-parse", "HEAD")
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent=head)

    verdict, reason = pretool(git_repo, env, command=f"git reset --hard {head}")

    assert verdict == "deny"
    assert "would discard working-tree content" in reason


def test_a_reset_may_not_rewind_a_reviewed_commit_outside_a_reconcile(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    first = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "more.txt").write_text("more\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "more")

    verdict, reason = pretool(git_repo, env, command=f"git reset --soft {first}")

    assert verdict == "deny"
    assert "only permitted during a reconcile" in reason


# --------------------------------------------------------------------------
# The fail-closed guard
# --------------------------------------------------------------------------


def test_an_unreadable_payload_denies(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Malformed JSON yields no session id, and no session id is a denial, not a pass."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_bootstrap(["pretool"], cwd=git_repo, env=env, stdin=b"{not json")

    assert proc.returncode == 0
    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "carried no session id" in reason


def test_the_reviewer_seam_is_the_only_thing_that_makes_these_tests_cheap() -> None:
    """A guard on the fixture itself: without it every commit test would call a model."""
    assert FAKE_REVIEWER.is_file()


# --------------------------------------------------------------------------
# Detection bypasses
#
# Every case here executed ungated before the fix, so each one fails on the old code.
# --------------------------------------------------------------------------


def test_a_commit_chained_ahead_of_set_phases_is_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The one exception permitted while ARMED was matched as a *substring* of the command.

    So a commit could be chained in front of it and ran with no snapshot and no review, at the
    one moment nothing has been frozen yet -- and ``confirm-commit`` then saw no pending
    approval and said nothing about it.
    """
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(
        git_repo,
        env,
        command=f'git add -A && git commit -m "x" && {ENTRYPOINT} set-phases --phase one',
    )

    assert verdict == "deny"
    assert "phase list has not been frozen yet" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "ARMED"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(f"{ENTRYPOINT} set-phases --phase one", id="one-phase"),
        pytest.param(f'{ENTRYPOINT} set-phases --phase "one" --phase "two"', id="several-phases"),
    ],
)
def test_a_plain_set_phases_is_still_allowed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], command: str) -> None:
    """The fix must not close the exception it is narrowing."""
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)

    verdict, _ = pretool(git_repo, env, command=command)

    assert verdict == "allow"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("./arl set-phases --phase one", id="a-program-the-repo-ships"),
        pytest.param("arl.sh set-phases --phase one", id="bare-name-off-PATH"),
        pytest.param("/elsewhere/arl.sh set-phases --phase one", id="another-copy"),
    ],
)
def test_only_this_gates_own_script_is_the_armed_exception(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    command: str,
) -> None:
    """Matching by basename trusts any executable named ``arl`` -- including one the repo ships.

    That is an arbitrary program allowed to run at the one moment everything else is denied.
    """
    env = armed(clean_env)
    arm(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "phase list has not been frozen yet" in reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(r'g\it add -A && g\it commit -m "x"', id="backslash"),
        pytest.param("'g'it add -A && 'g'it commit -m \"x\"", id="single-quotes"),
        pytest.param('g"i"t add -A && g"i"t commit -m "x"', id="double-quotes"),
    ],
)
def test_a_disguised_commit_command_still_reaches_the_gate(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    command: str,
) -> None:
    r"""``g\it commit`` is ``git commit`` to bash, and the detector matched the raw string.

    So none of these were recognised as commits at all: ``pretool`` fell through to its silent
    pass and the commit ran with no snapshot and no review. A blocking reviewer is used here
    so the assertion proves the command was actually *reviewed*, not merely denied.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "requires changes before phase 1" in reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(r"/plugin/scripts/a\rl.sh finish", id="backslash"),
        pytest.param("/plugin/scripts/'a'rl.sh deactivate", id="quotes"),
    ],
)
def test_a_disguised_escape_is_still_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], command: str) -> None:
    """Rule 4 is not escapable by quoting the name of the script that ends the loop."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "user-only commands" in reason


def test_a_disguised_reset_is_classified_as_a_reset(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    r"""``g\it reset`` reaches the reconcile gate, where the raw tokenizer reads it correctly."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    head = git(git_repo, "rev-parse", "HEAD")
    patch_state(env, git_repo, status="RECONCILE", bad_commit_parent=head)

    verdict, reason = pretool(git_repo, env, command=r"g\it reset --hard " + head)

    assert verdict == "deny"
    assert "would discard working-tree content" in reason


# --------------------------------------------------------------------------
# An approval may not land on an activation that moved
# --------------------------------------------------------------------------


def test_an_approval_does_not_land_on_an_activation_that_moved(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A review takes minutes, and the loop can transition during them.

    The reviewer stand-in escalates the activation from inside the review, which is exactly
    that window. Reloading under the lock is not enough on its own -- the approval would still
    be written over the escalation and the commit allowed.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    env["ARL_REVIEWER_CMD"] = str(escalating_reviewer(tmp_path, state_dir(env, git_repo, SESSION) / "state.json"))

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "while this commit was being gated" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["pending_approved_tree"] == ""
    # Only the baseline arming banked; the reviewed tree was never marked approved.
    assert document["approved_trees"] == [document["baseline_tree"]]


def escalating_reviewer(tmp_path: Path, state_path: Path) -> Path:
    """A reviewer that escalates the activation and *then* approves.

    Stands in for the ordinary case of something else transitioning the loop while a slow
    review runs; doing it from inside the reviewer is what makes the race deterministic.
    """
    script = tmp_path / "escalating-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "NEEDS_HUMAN"\n'
        'd["reason"] = "escalated while the review ran"\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'Fine.\\n\\n<<<ARL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<ARL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    return script


# --------------------------------------------------------------------------
# Words the gate cannot read
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("$(printf git) commit -m x", id="command-substitution"),
        pytest.param("`printf git` commit -m x", id="backtick"),
        pytest.param("${GIT} commit -m x", id="braced-variable"),
        pytest.param("$'\\x67it' commit -m x", id="ansi-c-quoting"),
    ],
)
def test_a_command_whose_name_is_an_expansion_is_denied(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    command: str,
) -> None:
    """Each of these runs ``git commit`` and contains no ``git`` for any detector to match.

    No textual pass resolves them, and neither does a real parser -- bashlex would report a
    command whose *name is a substitution node*, and the only sound answer to that is still
    refusal. So the deny-list absorbs it, which is the trade the tokenizer already makes.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "the gate cannot tell what program it will run" in reason
    assert "in the command name" in reason


def test_an_ordinary_command_with_no_expansion_is_untouched(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The denial must not swallow the builds and tests the loop exists to run."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    for command in ("make test", "grep -r '$foo' .", "pytest -q tests"):
        proc = run_hook("pretool", payload(git_repo, command=command), cwd=git_repo, env=env)
        assert proc.returncode == 0
        assert proc.stdout == "", command


def test_a_quoted_heredoc_carrying_expansions_runs(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The measured friction: a script written into a quoted heredoc. Bash expands nothing in
    that body, so a ``$`` or a backtick in it decides no command name -- and refusing it cost a
    scratchpad file plus a second Bash call every time."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    for command in (
        "python3 - <<'PY'\nif re.match(r'/^\\/api$/', line):\n    print(1)\nPY",
        "cat <<'TS'\nconst q = `SELECT ${id}`;\nTS",
        'fallow > /dev/null 2>&1; echo "exit=$?"',
    ):
        proc = run_hook("pretool", payload(git_repo, command=command), cwd=git_repo, env=env)
        assert proc.returncode == 0
        assert proc.stdout == "", command


def test_a_heredoc_body_naming_a_commit_is_still_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The heredoc allowance must not become a way to hide a commit. ``mentions_commit`` reads
    the raw text multiline, so this still routes into the commit gate -- where ``validate_commit``
    refuses the shape. Over-detection stays the safe direction."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command="cat <<'PY'\nsubprocess.run('git commit -m x')\nPY")

    assert verdict == "deny"
    assert "the gate cannot tell what program it will run" not in reason, "this is the commit gate refusing a shape, not the expansion pre-check"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("# <<':'\n$(printf git) commit -m x\n:", id="heredoc-opened-inside-a-comment"),
        pytest.param("cat <<E'OF'\nbody\nEOF\n$(printf git) commit -m x", id="delimiter-quoted-in-the-middle"),
        pytest.param("\\\n# <<':'\n$(printf git) commit -m x\n:", id="comment-after-a-line-continuation"),
        pytest.param("((1 << 'true'))\n$(printf git) commit -m x\ntrue", id="left-shift-in-an-arithmetic-command"),
        pytest.param('cat <<"E\\qOF"\nbody\nE\\qOF\n$(printf git) commit -m x', id="backslash-kept-in-a-double-quoted-delimiter"),
        pytest.param("cat <<E\\\nOF\nbody\nEOF\n$(printf git) commit -m x", id="continuation-inside-the-delimiter"),
        pytest.param("cat <<\\\nEOF\nbody\nEOF\n$(printf git) commit -m x", id="continuation-immediately-after-the-operator"),
    ],
)
def test_a_heredoc_the_scan_misreads_cannot_smuggle_a_command_name(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], command: str) -> None:
    """Both were verified against real bash: it runs ``$(printf git) commit -m x``.

    Neither contains a literal ``git commit`` for detection to match, so the expansion
    pre-check is the only thing standing between them and an ungated commit. It must not skip
    them as heredoc body -- a ``<<`` inside a comment opens nothing, and ``<<E'OF'`` delimits
    on ``EOF``.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "the gate cannot tell what program it will run" in reason


def test_an_unquoted_heredoc_body_with_an_expansion_is_denied(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``<<EOF`` is expanded by bash and bashlex files the body under a ``heredoc`` node rather
    than a word, so only the textual scan can answer for it."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    verdict, reason = pretool(git_repo, env, command="cat <<EOF\n$(printf hi)\nEOF")

    assert verdict == "deny"
    assert "heredoc" in reason


# --------------------------------------------------------------------------
# git by any other path
# --------------------------------------------------------------------------


def test_an_absolute_git_commit_still_reaches_the_gate(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``/usr/bin/git commit`` is the same program, and the detector matched the bare word.

    The strict validator then refuses the non-canonical spelling, which is the safe direction:
    it is denied rather than silently passed through to run.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")

    verdict, reason = pretool(git_repo, env, command="/usr/bin/git add -A && /usr/bin/git commit -m x")

    assert verdict == "deny"
    assert "This commit command was not accepted" in reason
    assert "/usr/bin/git" in reason


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git reset --hard HEAD~1", id="hard"),
        pytest.param("git reset HEAD~1", id="mixed-is-the-default"),
        pytest.param("/usr/bin/git reset --soft HEAD~1", id="absolute-path"),
        pytest.param("git reset", id="unstage-everything"),
    ],
)
def test_a_reset_the_gate_cannot_read_is_denied_rather_than_passed(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    command: str,
) -> None:
    """The shell only denied when it could *parse* the target, so every other shape passed.

    ``git reset --hard HEAD~1`` and a bare ``git reset HEAD~1`` both move HEAD off a reviewed
    commit, and both were allowed. "The gate could not parse it" is not a reason to allow it.
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "more.txt").write_text("more\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "more")

    verdict, reason = pretool(git_repo, env, command=command)

    assert verdict == "deny"
    assert "This reset was not accepted" in reason


# --------------------------------------------------------------------------
# An escalation may not undo a user's exit
# --------------------------------------------------------------------------


def test_an_escalation_does_not_reopen_a_mode_the_user_stopped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Rule 4: the user owns the exits, and a review that fails afterwards may not undo one.

    The reviewer stand-in disarms the activation from inside the review -- the window in which
    a user actually runs ``/adversarial-review-loop:stop`` -- and then fails. Escalating over that
    would turn their ``DISARMED`` back into a state that denies every mutation.
    """
    env = armed(clean_env, ARL_MAX_FAILURES="0")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    env["ARL_REVIEWER_CMD"] = str(stopping_reviewer(tmp_path, state_dir(env, git_repo, SESSION) / "state.json"))

    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "while this commit was being gated" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "DISARMED"


def stopping_reviewer(tmp_path: Path, state_path: Path) -> Path:
    """A reviewer that disarms the activation and then fails, in that order."""
    script = tmp_path / "stopping-reviewer.sh"
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
    return script


# --------------------------------------------------------------------------
# The gate's own state is not an ordinary file
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit"])
def test_an_editing_tool_may_not_rewrite_the_gates_own_state(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    tool: str,
) -> None:
    """``pretool`` passes every non-Bash tool, so this route needed no shell at all.

    A ``Write`` of ``{"status": "DISARMED"}`` over ``state.json`` ends the mode without any of
    the checks that exist to stop exactly that (Rule 4).
    """
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    target = state_dir(env, git_repo, SESSION) / "state.json"
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"

    proc = run_hook(
        "pretool",
        {"session_id": SESSION, "cwd": str(git_repo), "tool_name": tool, "tool_input": {key: str(target)}},
        cwd=git_repo,
        env=env,
    )

    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "review loop's own state directory" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "ACTIVE"


def test_an_ordinary_file_in_the_repository_is_still_writable(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The check must not cost the model its ability to implement the plan."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)

    proc = run_hook(
        "pretool",
        {"session_id": SESSION, "cwd": str(git_repo), "tool_name": "Write", "tool_input": {"file_path": "src/thing.py"}},
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_a_symlink_into_the_state_root_does_not_bypass_the_guard(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Containment was decided lexically, so an alias inside the repository shared no prefix."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "alias").symlink_to(Path(env["XDG_STATE_HOME"]) / "adversarial-review-loop")
    target = f"alias/worktrees/{paths.sha256_hex(str(git_repo))}/{SESSION}/state.json"

    proc = run_hook(
        "pretool",
        {"session_id": SESSION, "cwd": str(git_repo), "tool_name": "Write", "tool_input": {"file_path": target}},
        cwd=git_repo,
        env=env,
    )

    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "review loop's own state directory" in reason


def test_a_relative_state_dir_does_not_bypass_the_guard(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``ARL_STATE_DIR`` is used verbatim, so a relative one never matched an absolute target."""
    env = armed(clean_env, ARL_STATE_DIR="relative-state")
    proc = run_bootstrap(["arm", "--session", SESSION, "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    proc = run_bootstrap(["set-phases", "--phase", "one"], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stderr
    target = f"relative-state/worktrees/{paths.sha256_hex(str(git_repo))}/{SESSION}/state.json"

    proc = run_hook(
        "pretool",
        {"session_id": SESSION, "cwd": str(git_repo), "tool_name": "Write", "tool_input": {"file_path": target}},
        cwd=git_repo,
        env=env,
    )

    verdict, reason = decision(proc)
    assert verdict == "deny"
    assert "review loop's own state directory" in reason


# --------------------------------------------------------------------------
# Late-round rule: deferred findings on the approval message
# --------------------------------------------------------------------------


def test_a_round_two_medium_outside_the_changed_paths_is_deferred_and_shown(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    verdict, _ = pretool(git_repo, env, command='git add -A && git commit -m "x"')
    assert verdict == "deny", "round 1: a.txt:1 high"

    (git_repo / "other.txt").write_text("round two\n")
    env["ARL_FAKE_MODE"] = "medium-file"
    env["ARL_FAKE_FILE"] = "new.txt:1"  # in the full diff, unchanged since round 1, never raised
    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "allow"
    assert "the reviewer approved phase 1" in reason
    assert "Deferred findings" in reason
    assert "did not block this commit" in reason
    assert "new.txt:1" in reason
    assert read_state(env, git_repo, SESSION)["pending_approved_tree"]


def test_a_round_two_medium_in_a_changed_path_still_denies(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "new.txt").write_text("work\n")
    assert pretool(git_repo, env, command='git add -A && git commit -m "x"')[0] == "deny"

    (git_repo / "other.txt").write_text("round two\n")
    env["ARL_FAKE_MODE"] = "medium-file"
    env["ARL_FAKE_FILE"] = "other.txt:1"
    verdict, reason = pretool(git_repo, env, command='git add -A && git commit -m "x"')

    assert verdict == "deny"
    assert "other.txt:1" in reason
    assert "Deferred findings" not in reason
