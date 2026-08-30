"""``intent``: the ``UserPromptSubmit`` hook that records a request for enforcement.

Exercised through the bootstrap, like every other entrypoint, because the contract under
test is what reaches stdout and the exit status -- not the function.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import git, run_bootstrap, run_hook
from test_commands_arm import armed_env, plan_file, read_state
from test_commands_pretool import SESSION, active, armed, patch_state, pretool


def prompt_payload(repo: Path, prompt: str, session: str = SESSION) -> dict[str, object]:
    return {"session_id": session, "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": prompt}


def intent(repo: Path, env: dict[str, str], prompt: str, session: str = SESSION) -> str:
    proc = run_hook("intent", prompt_payload(repo, prompt, session), cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def marker(env: dict[str, str], session: str = SESSION) -> Path:
    return Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "intents" / session


@pytest.mark.parametrize(
    "prompt",
    [
        "/opencode-review-loop:implement plan.md",
        "/opencode-review-loop:implement",
        "  /opencode-review-loop:resume --until 3",
        "/opencode-review-loop:resume\n",
    ],
)
def test_an_arming_prompt_records_intent(git_repo: Path, clean_env: dict[str, str], prompt: str) -> None:
    assert intent(git_repo, clean_env, prompt) == ""
    worktree, token = marker(clean_env).read_text().split("\n")[:2]
    assert Path(worktree).resolve() == git_repo.resolve()
    assert re.fullmatch(r"intent=[0-9a-f]{16}", token)


@pytest.mark.parametrize(
    "prompt",
    [
        "continue",
        "yesterday I ran /opencode-review-loop:implement and it worked",
        "/opencode-review-loop:implementation plan.md",
        "/opencode-review-loop:status",
        "x/opencode-review-loop:implement",
        "",
    ],
)
def test_any_other_prompt_records_nothing(git_repo: Path, clean_env: dict[str, str], prompt: str) -> None:
    assert intent(git_repo, clean_env, prompt) == ""
    assert not marker(clean_env).exists()
    assert not (Path(clean_env["XDG_STATE_HOME"]) / "opencode-review-loop").exists()


def test_an_unusable_session_id_records_nothing(git_repo: Path, clean_env: dict[str, str]) -> None:
    assert intent(git_repo, clean_env, "/opencode-review-loop:implement plan.md", session="../escape") == ""
    assert not (Path(clean_env["XDG_STATE_HOME"]) / "opencode-review-loop").exists()


def test_a_marker_that_cannot_be_written_blocks_the_prompt(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Enforcement was requested and could not be recorded: the arm must not run on top of that."""
    root = Path(clean_env["XDG_STATE_HOME"]) / "opencode-review-loop"
    root.mkdir(parents=True)
    (root / "intents").write_text("a file where the directory should be\n")

    out = intent(git_repo, clean_env, "/opencode-review-loop:implement plan.md")

    document = json.loads(out)
    assert document["decision"] == "block"
    assert "could not be recorded" in document["reason"]


def test_arming_clears_the_marker(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    assert marker(env).exists()

    active(git_repo, tmp_path, env)

    assert not marker(env).exists()
    # The pointer superseded it; Rule 0 reads the pointer first.
    assert (Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "sessions" / SESSION).is_file()


@pytest.mark.parametrize("status", ["DISARMED", "COMPLETE", "ACTIVE"])
def test_intent_outranks_a_pointer_the_session_already_held(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    """A re-arm whose expansion failed must not hide behind an earlier activation's pointer."""
    env = armed(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "Arming never ran" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "ARM_FAILED"
    assert not marker(env).exists()
    # The commit that used to slip through a terminal pointer is denied too.
    verdict, _ = pretool(git_repo, env, tool="Bash", command="git add -A && git commit -m x")
    assert verdict == "deny"


def test_intent_for_one_worktree_is_neither_enforced_on_nor_consumed_by_another(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")

    proc = run_hook("pretool", {"session_id": SESSION, "cwd": str(other), "tool_name": "Write", "tool_input": {}}, cwd=other, env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # B is not guarded, and nothing was recorded for it
    assert marker(env).exists()  # A's request is still pending
    assert not list((Path(env["XDG_STATE_HOME"]) / "opencode-review-loop").rglob("state.json"))

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "Arming never ran" in reason
    assert not marker(env).exists()


@pytest.mark.parametrize("damage", ["unreadable", "relative", "empty", "no-token"])
def test_a_marker_that_cannot_be_scoped_denies_without_being_consumed(git_repo: Path, clean_env: dict[str, str], damage: str) -> None:
    """It could be any repository's request, so it is never assigned to this one."""
    env = armed_env(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    if damage == "unreadable":
        marker(env).chmod(0)
    elif damage == "relative":
        marker(env).write_text("relative/path\nintent=0123456789abcdef\n")
    elif damage == "no-token":
        marker(env).write_text(f"{git_repo}\n")
    else:
        marker(env).write_text("")

    try:
        verdict, reason = pretool(git_repo, env, tool="Write")

        assert verdict == "deny"
        assert "cannot tell which repository" in reason
        assert "/opencode-review-loop:stop" in reason
        # Neither recorded against this repository nor consumed.
        assert not list((Path(env["XDG_STATE_HOME"]) / "opencode-review-loop").rglob("state.json"))
        assert marker(env).exists()
        # A read is still allowed, and the turn ends rather than blocking forever.
        proc = run_hook("pretool", prompt_payload(git_repo, "") | {"tool_name": "Read", "tool_input": {}}, cwd=git_repo, env=env)
        assert proc.stdout == ""
    finally:
        marker(env).chmod(0o600)


def test_only_stop_discards_a_marker_that_cannot_be_scoped(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    marker(env).write_text("")

    proc = run_bootstrap(["deactivate", "--session", SESSION], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "discarded this session's pending enforcement request" in proc.stdout
    assert not marker(env).exists()

    proc = run_hook("pretool", {"session_id": SESSION, "cwd": str(git_repo), "tool_name": "Write", "tool_input": {}}, cwd=git_repo, env=env)
    assert proc.stdout == ""


def test_a_pointer_answers_its_own_marker_even_if_cleanup_never_ran(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The pointer is published first and carries the token, so a crash before cleanup is inert."""
    env = armed(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    token = marker(env).read_text().split("\n")[1].removeprefix("intent=")
    active(git_repo, tmp_path, env)
    pointer = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "sessions" / SESSION
    assert pointer.read_text().split("\n")[1] == f"intent={token}"

    # Simulate the crash window: the pointer landed, the unlink did not.
    marker(env).parent.mkdir(parents=True, exist_ok=True)
    marker(env).write_text(f"{git_repo}\nintent={token}\n")

    proc = run_hook("pretool", {"session_id": SESSION, "cwd": str(git_repo), "tool_name": "Write", "tool_input": {}}, cwd=git_repo, env=env)

    assert proc.stdout == ""  # ACTIVE: an edit is simply allowed through
    assert not marker(env).exists()  # answered, so the retry cleaned it up
    assert read_state(env, git_repo, SESSION)["status"] == "ACTIVE"


def test_a_marker_whose_cleanup_fails_does_not_fail_the_arm(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The pointer is the acknowledgement; cleanup is best effort, and a stale marker is inert."""
    env = armed(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")
    intents = marker(env).parent
    intents.chmod(0o500)
    try:
        proc = run_bootstrap(["arm", "--session", SESSION, "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

        assert proc.returncode == 0, proc.stdout
        assert read_state(env, git_repo, SESSION)["status"] == "ARMED"
        assert marker(env).exists()  # cleanup could not run
    finally:
        intents.chmod(0o700)

    # And the leftover marker does not reopen the activation: the pointer acknowledges it.
    verdict, _ = pretool(git_repo, env, tool="Write")
    assert verdict == "deny"  # ARMED: phases not frozen yet -- not an unstarted arm
    assert "set-phases" in pretool(git_repo, env, tool="Write")[1]


def test_intent_with_no_pointer_is_an_arm_that_never_ran(git_repo: Path, clean_env: dict[str, str]) -> None:
    """The expansion never executed: the very next mutation records ARM_FAILED and denies."""
    env = armed_env(clean_env)
    intent(git_repo, env, "/opencode-review-loop:implement plan.md")

    verdict, reason = pretool(git_repo, env, tool="Write")

    assert verdict == "deny"
    assert "Arming never ran" in reason
    assert "Do not implement the plan" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "ARM_FAILED"
    assert "arming never executed" in str(document["reason"])
    assert not marker(env).exists()

    # Recorded, so the ordinary ARM_FAILED path now owns the session.
    verdict, reason = pretool(git_repo, env, tool="Write")
    assert verdict == "deny"
    assert "Arming failed" in reason
