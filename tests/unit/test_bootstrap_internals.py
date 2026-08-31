"""Unit-level tests for the bootstrap's cache-safety predicates.

The end-to-end tests in ``test_bootstrap.py`` can only observe *absence* of bytecode, which
a hostile-but-unwritable path satisfies for the wrong reason. These drive the decision
itself, so a check that never fires is still caught.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from conftest import BOOTSTRAP


def load_bootstrap() -> ModuleType:
    """Import ``arl-bootstrap.py``, whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location("arl_bootstrap_under_test", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


boot = load_bootstrap()


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("/x/cache", "/x/cache", True),
        ("/x/cache", "/x", True),
        ("/x", "/x/cache", True),
        # The root case: a naive `startswith(other + os.sep)` answers False here, because
        # "/" + os.sep is "//" and no path starts with that.
        ("/", "/repo", True),
        ("/repo", "/", True),
        ("/x/cache", "/y/cache", False),
        # A shared textual prefix is not containment.
        ("/x/cache", "/x/cachet", False),
    ],
)
def test_overlaps(a: str, b: str, expected: bool) -> None:
    assert boot._overlaps(a, b) is expected


def test_a_root_cache_prefix_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sys.pycache_prefix` mirrors absolute source paths beneath the prefix.

    With a prefix of "/", the bytecode for "/plugin/scripts/arl/x.py" lands right back at
    "/plugin/scripts/arl/x.pyc" -- inside the plugin checkout.
    """
    monkeypatch.chdir(tmp_path)
    assert not boot._cache_dir_is_safe("/")


def test_a_cache_prefix_under_cwd_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert not boot._cache_dir_is_safe(str(tmp_path / ".cache" / "pycache"))


def test_a_cache_prefix_elsewhere_in_the_reviewed_repo_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    workdir = repo / "sub"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert not boot._cache_dir_is_safe(str(repo / ".cache" / "pycache"))


def test_a_cache_prefix_inside_the_plugin_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plugin_root = os.path.dirname(boot._SCRIPTS_DIR)
    assert not boot._cache_dir_is_safe(os.path.join(plugin_root, ".cache", "pycache"))


def test_a_relative_cache_prefix_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative prefix resolves against cwd, which under a hook is the repo under review."""
    monkeypatch.chdir(tmp_path)
    assert not boot._cache_dir_is_safe(".cache/pycache")


def test_an_unrelated_absolute_cache_prefix_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check must not be so broad that it disables the cache in normal use."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    assert boot._cache_dir_is_safe(str(tmp_path / "elsewhere" / "pycache"))


def test_enclosing_repo_root_finds_the_reviewed_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    assert boot._enclosing_repo_root(str(deep)) == os.path.realpath(str(repo))


def test_enclosing_repo_root_is_empty_outside_a_repository(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    # tmp_path itself is not inside a repository, so the walk reaches / and gives up.
    assert boot._enclosing_repo_root(str(outside)) == ""
