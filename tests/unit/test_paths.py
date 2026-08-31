"""Path layout must stay byte-compatible with ``sha256sum``.

The worktree digest names the on-disk activation directory, so a digest that drifts from
``printf '%s' … | sha256sum`` orphans every activation an existing install already has on
disk under the old digest -- state silently becomes unfindable rather than failing loudly.
``sha256_hex`` is checked against the real ``sha256sum`` binary, not a re-derived expectation,
so this is the same guarantee for exotic input (spaces, Unicode, the empty string) as for the
ordinary case.
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

import subprocess
from pathlib import Path

import pytest

from arl import paths
from arl.errors import RepoResolutionError


def sha256sum_of(text: str) -> str:
    """What `printf '%s' "$text" | sha256sum` produces, from the real tool."""
    proc = subprocess.run(["sha256sum"], input=text, capture_output=True, text=True, check=True)
    return proc.stdout.split(" ", 1)[0]


@pytest.mark.parametrize("text", ["/home/u/repo", "/repo with spaces", "/tmp/ünïcode", ""])
def test_sha256_matches_the_shell(text: str) -> None:
    assert paths.sha256_hex(text) == sha256sum_of(text)


def test_state_root_prefers_the_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STATE_DIR", "/somewhere/else")
    monkeypatch.setenv("XDG_STATE_HOME", "/ignored")
    assert paths.state_root() == Path("/somewhere/else")


def test_state_root_falls_back_through_xdg_then_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARL_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/x")
    assert paths.state_root() == Path("/x/adversarial-review-loop")

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/h")
    assert paths.state_root() == Path("/h/.local/state/adversarial-review-loop")


def test_an_empty_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell used `${VAR:-default}`, which falls back on empty as well as unset."""
    monkeypatch.setenv("ARL_STATE_DIR", "")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("HOME", "/h")
    assert paths.state_root() == Path("/h/.local/state/adversarial-review-loop")


def test_an_empty_home_stays_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"$HOME/.local/state"` with an empty HOME is "/.local/state", not a relative path."""
    monkeypatch.delenv("ARL_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "")
    root = paths.state_root()
    assert root.is_absolute()
    assert root == Path("/.local/state/adversarial-review-loop")


def test_activation_and_pointer_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STATE_DIR", "/s")
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


def test_repo_root_or_raise_answers_not_a_repository_with_empty(tmp_path: Path) -> None:
    assert paths.repo_root_or_raise(str(tmp_path)) == ""


def test_repo_root_or_raise_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RepoResolutionError, match="not a directory"):
        paths.repo_root_or_raise(str(tmp_path / "nope"))


def test_repo_root_or_raise_refuses_when_git_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure the unbound-session check exists to tell apart from "no repository"."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RepoResolutionError, match="git could not be run"):
        paths.repo_root_or_raise(str(tmp_path))
    # And the lenient wrapper still folds it into "", for callers that may treat unknown as none.
    assert paths.repo_root(str(tmp_path)) == ""


def test_have_finds_a_real_binary() -> None:
    assert paths.have("git")
    assert not paths.have("definitely-not-a-real-binary-xyzzy")
