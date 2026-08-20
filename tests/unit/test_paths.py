"""Path layout must stay byte-compatible with the shell implementation.

Phase 6 has to be revertible mid-session, so a session created by one implementation must
be found by the other. That starts with hashing the worktree path identically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ocrl import paths


def sha256sum_of(text: str) -> str:
    """What `printf '%s' "$text" | sha256sum` produces, from the real tool."""
    proc = subprocess.run(["sha256sum"], input=text, capture_output=True, text=True, check=True)
    return proc.stdout.split(" ", 1)[0]


@pytest.mark.parametrize("text", ["/home/u/repo", "/repo with spaces", "/tmp/ünïcode", ""])
def test_sha256_matches_the_shell(text: str) -> None:
    assert paths.sha256_hex(text) == sha256sum_of(text)


def test_state_root_prefers_the_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCRL_STATE_DIR", "/somewhere/else")
    monkeypatch.setenv("XDG_STATE_HOME", "/ignored")
    assert paths.state_root() == Path("/somewhere/else")


def test_state_root_falls_back_through_xdg_then_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCRL_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/x")
    assert paths.state_root() == Path("/x/opencode-review-loop")

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/h")
    assert paths.state_root() == Path("/h/.local/state/opencode-review-loop")


def test_an_empty_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell used `${VAR:-default}`, which falls back on empty as well as unset."""
    monkeypatch.setenv("OCRL_STATE_DIR", "")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("HOME", "/h")
    assert paths.state_root() == Path("/h/.local/state/opencode-review-loop")


def test_an_empty_home_stays_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"$HOME/.local/state"` with an empty HOME is "/.local/state", not a relative path."""
    monkeypatch.delenv("OCRL_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "")
    root = paths.state_root()
    assert root.is_absolute()
    assert root == Path("/.local/state/opencode-review-loop")


def test_activation_and_pointer_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCRL_STATE_DIR", "/s")
    digest = paths.sha256_hex("/repo")
    assert paths.activation_dir("/repo", "sess") == Path(f"/s/worktrees/{digest}/sess")
    assert paths.latest_pointer_path("/repo") == Path(f"/s/worktrees/{digest}/latest")
    assert paths.sessions_dir() == Path("/s/sessions")


def test_repo_root_resolves_a_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    resolved = paths.repo_root(str(repo / "sub"))
    assert Path(resolved).resolve() == repo.resolve()


def test_repo_root_is_empty_outside_a_repository(tmp_path: Path) -> None:
    assert paths.repo_root(str(tmp_path)) == ""


def test_repo_root_is_empty_for_a_missing_directory(tmp_path: Path) -> None:
    assert paths.repo_root(str(tmp_path / "nope")) == ""


def test_have_finds_a_real_binary() -> None:
    assert paths.have("git")
    assert not paths.have("definitely-not-a-real-binary-xyzzy")
