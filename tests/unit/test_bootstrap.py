"""The bootstrap is the gate's trust boundary. These tests fail on a naive implementation."""

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
import sys
from pathlib import Path

from conftest import (
    SHADOWED_MODULES,
    git_status_ignored,
    pycache_dirs,
    run_bootstrap,
)


def test_hostile_repo_cannot_shadow_the_gate(hostile_repo: Path, clean_env: dict[str, str]) -> None:
    """A repository under review must not be able to supply the gate's own modules."""
    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=clean_env)

    assert not (hostile_repo / "HIJACKED").exists(), "a shadowed module executed as the gate"
    assert "HOSTILE" not in proc.stderr
    assert proc.returncode == 0
    assert "usage: arl.sh <subcommand>" in proc.stdout


def test_naive_invocation_is_the_thing_being_defended_against(hostile_repo: Path, clean_env: dict[str, str]) -> None:
    """Pin the exploit itself, so the defence above is not passing vacuously.

    ``-m`` with the repository as cwd runs the repository's code. If this ever stops being
    true the test above stops proving anything, and this failure says so.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "arl"],
        cwd=str(hostile_repo),
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (hostile_repo / "HIJACKED").exists()
    assert "HOSTILE" in proc.stderr
    assert proc.returncode != 0


def test_stdlib_resolves_to_the_stdlib_not_the_repo(hostile_repo: Path, clean_env: dict[str, str]) -> None:
    """Merely planting ``json.py`` next to the gate must not reach it."""
    for name in SHADOWED_MODULES:
        assert (hostile_repo / f"{name}.py").is_file(), "fixture is not actually hostile"

    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=clean_env)
    assert proc.returncode == 0
    assert not (hostile_repo / "HIJACKED").exists()


def test_bytecode_never_lands_in_either_repository(
    hostile_repo: Path,
    plugin_copy: Path,
    clean_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Rule 3, applied to ``__pycache__``: neither the reviewed repo nor the plugin repo."""
    bootstrap = plugin_copy / "arl-bootstrap.py"
    assert pycache_dirs(plugin_copy) == set()

    # Deliberately not the session-shared cache the other tests run against: the closing
    # assertion is that *these* invocations populated it, which a pre-warmed directory would
    # satisfy on its own.
    env = {**clean_env, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    for _ in range(3):
        proc = run_bootstrap(["--help"], cwd=hostile_repo, env=env, bootstrap=bootstrap)
        assert proc.returncode == 0

    assert pycache_dirs(plugin_copy) == set(), "the gate wrote bytecode into the plugin repo"
    assert pycache_dirs(hostile_repo) == set(), "the gate wrote bytecode into the reviewed repo"
    assert git_status_ignored(hostile_repo) == "", "the gate dirtied the repository under review"

    cache_root = Path(env["XDG_CACHE_HOME"]) / "adversarial-review-loop" / "pycache"
    assert list(cache_root.rglob("*.pyc")), "the bytecode cache was disabled, not relocated"


def test_bytecode_is_disabled_rather_than_written_relative(hostile_repo: Path, plugin_copy: Path, clean_env: dict[str, str]) -> None:
    """An unusable cache location must never degrade into writing beside the source.

    With no ``XDG_CACHE_HOME`` and no ``HOME`` the prefix would be relative, and a relative
    prefix resolves against cwd -- which under a hook is the repository under review.
    """
    env = dict(clean_env)
    env.pop("XDG_CACHE_HOME", None)
    env["HOME"] = ""
    bootstrap = plugin_copy / "arl-bootstrap.py"

    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=env, bootstrap=bootstrap)

    assert proc.returncode == 0
    assert pycache_dirs(hostile_repo) == set()
    assert pycache_dirs(plugin_copy) == set()
    assert git_status_ignored(hostile_repo) == ""


def test_an_absolute_cache_path_inside_the_reviewed_repo_is_refused(hostile_repo: Path, plugin_copy: Path, clean_env: dict[str, str]) -> None:
    """`XDG_CACHE_HOME` is ordinary environment, not a trusted input.

    `$PWD/.cache` is perfectly absolute and lands squarely inside the repository under
    review, so absoluteness alone is not the property that matters.
    """
    env = dict(clean_env)
    env["XDG_CACHE_HOME"] = str(hostile_repo / ".cache")
    bootstrap = plugin_copy / "arl-bootstrap.py"

    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=env, bootstrap=bootstrap)

    assert proc.returncode == 0
    assert not list(hostile_repo.rglob("*.pyc")), "bytecode was written into the reviewed repo"
    assert git_status_ignored(hostile_repo) == ""


def test_a_cache_path_elsewhere_in_the_reviewed_repo_is_refused(hostile_repo: Path, plugin_copy: Path, clean_env: dict[str, str]) -> None:
    """Not only under cwd: the whole repository under review is off limits."""
    workdir = hostile_repo / "sub" / "deeper"
    workdir.mkdir(parents=True)
    env = dict(clean_env)
    env["XDG_CACHE_HOME"] = str(hostile_repo / ".cache")
    bootstrap = plugin_copy / "arl-bootstrap.py"

    proc = run_bootstrap(["--help"], cwd=workdir, env=env, bootstrap=bootstrap)

    assert proc.returncode == 0
    assert not list(hostile_repo.rglob("*.pyc"))
    assert git_status_ignored(hostile_repo) == ""


def test_a_cache_path_inside_the_plugin_is_refused(hostile_repo: Path, plugin_copy: Path, clean_env: dict[str, str]) -> None:
    """The plugin gates work on itself, so its own checkout is a repository under review."""
    env = dict(clean_env)
    env["XDG_CACHE_HOME"] = str(plugin_copy / ".cache")
    bootstrap = plugin_copy / "arl-bootstrap.py"

    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=env, bootstrap=bootstrap)

    assert proc.returncode == 0
    assert pycache_dirs(plugin_copy) == set()
    assert not list(plugin_copy.rglob("*.pyc"))


def test_a_cache_path_that_is_an_ancestor_is_refused(hostile_repo: Path, plugin_copy: Path, clean_env: dict[str, str]) -> None:
    """`sys.pycache_prefix` mirrors absolute source paths beneath the prefix.

    So a prefix that merely *contains* a source tree still writes into it: with a prefix of
    `/`, the bytecode for `/plugin/scripts/arl/x.py` lands at `/plugin/scripts/arl/x.pyc`.
    """
    env = dict(clean_env)
    env["XDG_CACHE_HOME"] = "/"
    bootstrap = plugin_copy / "arl-bootstrap.py"

    proc = run_bootstrap(["--help"], cwd=hostile_repo, env=env, bootstrap=bootstrap)

    assert proc.returncode == 0
    assert pycache_dirs(plugin_copy) == set()
    assert not list(plugin_copy.rglob("*.pyc"))
    assert not list(hostile_repo.rglob("*.pyc"))


def test_unknown_subcommand_exits_two(hostile_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["no-such-subcommand"], cwd=hostile_repo, env=clean_env)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "usage: arl.sh <subcommand>" in proc.stderr
