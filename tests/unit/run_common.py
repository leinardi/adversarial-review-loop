"""Shared fixtures for the run-state, workspace and command tests.

Everything a fixture creates *before* a run starts is dated an hour back -- file stamps through
``os.utime``, commits and reflog entries through ``GIT_COMMITTER_DATE`` -- so the late-root
checks, which count anything newer than the run start (less a tolerance) as the run's work,
see setup as old and every change a test makes after ``init`` as new.
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

import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from arl import cli

#: One hour before the test runs, as a git date and as a file stamp.
PAST = int(time.time()) - 3600


def git_at(repo: Path, *args: str, when: int | None = None) -> str:
    """Run git in ``repo``; ``when`` dates the commit and its reflog entry."""
    env = {**os.environ}
    if when is not None:
        env |= {"GIT_COMMITTER_DATE": f"@{when} +0000", "GIT_AUTHOR_DATE": f"@{when} +0000"}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"git {args!r} failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout.rstrip("\n")


def make_repo(path: Path, *, when: int | None = PAST, files: dict[str, str] | None = None) -> Path:
    """A repository at ``path`` with one commit holding ``files`` (default: one README)."""
    path.mkdir(parents=True, exist_ok=True)
    git_at(path, "init", "-q", "-b", "main")
    git_at(path, "config", "user.email", "run@example.invalid")
    git_at(path, "config", "user.name", "arl run test")
    git_at(path, "config", "commit.gpgsign", "false")
    # git starts detached auto-maintenance after a commit; it would race `backdate`'s walk.
    git_at(path, "config", "maintenance.auto", "false")
    git_at(path, "config", "gc.auto", "0")
    for name, text in (files or {"README.md": f"{path.name}\n"}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git_at(path, "add", "-A")
    git_at(path, "commit", "-qm", "seed", when=when)
    return path


def backdate(top: Path, when: int = PAST) -> None:
    """Stamp every worktree entry under ``top`` (and ``top``) with ``when``, deepest first.

    ``.git`` is left alone: its stamps decide nothing here, and a background git process may be
    creating and removing lock files in it while this walks.
    """
    for directory, dirs, files in os.walk(top, topdown=True):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in [*files, *dirs]:
            with contextlib.suppress(FileNotFoundError):
                os.utime(Path(directory) / name, (when, when), follow_symlinks=False)
    os.utime(top, (when, when))


@pytest.fixture
def run_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated HOME and config, so no developer config reaches ``config.load``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in [key for key in os.environ if key.startswith("ARL_")]:
        monkeypatch.delenv(key)
    # Repositories a test creates with plain `git init` or `git clone` get no background
    # maintenance either.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "maintenance.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "gc.auto")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "0")
    return home


@pytest.fixture
def ws(run_env: Path) -> Path:
    """A launch directory holding two repositories and a plain folder, all an hour old."""
    top = run_env / "ws"
    top.mkdir()
    make_repo(top / "api", files={"src/main.py": "print('api')\n"})
    make_repo(top / "web", files={"src/main.py": "print('web')\n"})
    (top / "notes").mkdir()
    (top / "notes" / "todo.md").write_text("todo\n")
    (top / "plan.md").write_text("# Plan\n\n1. Change api.\n2. Change web.\n")
    backdate(top)
    return top


def arl(launch: Path, *args: str, stdin: str = "", monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Run one engine command in process and answer its JSON, asserting it printed exactly one line."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    status = cli.main(["--dir", str(launch), *args])
    out = capsys.readouterr().out
    assert out.count("\n") == 1, f"expected one JSON line, got {out!r}"
    answer: dict[str, Any] = json.loads(out)
    assert (status == 0) == answer["ok"], answer
    return answer


def shadow_files(launch: Path, alias: str, tree: str) -> list[str]:
    """The paths a shadow snapshot tree holds."""
    git_dir = launch / ".arl" / "shadow" / f"{alias}.git"
    return git_at(launch, "--git-dir", str(git_dir), "ls-tree", "-r", "--name-only", tree).splitlines()
