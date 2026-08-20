"""Shared fixtures for the Python unit tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import ocrl

PLUGIN_ROOT = ocrl.PLUGIN_ROOT
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
BOOTSTRAP = SCRIPTS_DIR / "ocrl-bootstrap.py"
SOCKET_STDIN = PLUGIN_ROOT / "tests" / "fixtures" / "socket-stdin.py"
BASH_FIXTURE = PLUGIN_ROOT / "tests" / "fixtures" / "bash-config-state.sh"


def bash_fixture(
    args: list[str],
    *,
    env: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Drive the still-live shell config/state libraries.

    The environment is passed in full rather than inherited, because a developer's shell may
    carry ``OCRL_*`` overrides -- one of them is a documented leftover from the STEP0 runs --
    and those would silently skew a differential comparison.
    """
    proc = subprocess.run(
        [str(BASH_FIXTURE), *args],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"bash fixture {args!r} failed ({proc.returncode}): {proc.stderr}")
    return proc


#: Modules a hostile repository under review would shadow to hijack the gate. ``ocrl`` and
#: ``pathlib`` are on the import path of even ``--help``; the rest are reached as later
#: phases import them, and cost nothing to plant now.
SHADOWED_MODULES = ("json", "subprocess", "pathlib", "hashlib", "shutil")


def run_bootstrap(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    bootstrap: Path = BOOTSTRAP,
) -> subprocess.CompletedProcess[str]:
    """Invoke the gate exactly as the shim must: absolute path, ``-I``, never ``-m``."""
    return subprocess.run(
        [sys.executable, "-I", str(bootstrap), *args],
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        input=stdin.decode() if stdin is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def clean_env(tmp_path: Path) -> dict[str, str]:
    """An environment isolated from the developer's real state and cache directories."""
    # Every OCRL_* override goes, not just OCRL_STATE_DIR: a developer's shell may carry
    # one (OCRL_MAX_STOP_BLOCKS is a documented leftover from the STEP0 runs), and it would
    # silently skew both the Python result and the shell one it is compared against.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OCRL_")}
    env["HOME"] = str(tmp_path / "home")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    os.makedirs(env["HOME"], exist_ok=True)
    return env


@pytest.fixture
def plugin_copy(tmp_path: Path) -> Path:
    """A pristine copy of ``scripts/``, so ``__pycache__`` assertions are unambiguous.

    The working tree already carries ``__pycache__`` directories from pytest's own runs, so
    asserting on it directly would test the wrong thing.
    """
    dest = tmp_path / "plugin" / "scripts"
    shutil.copytree(
        SCRIPTS_DIR,
        dest,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return dest


@pytest.fixture
def hostile_repo(tmp_path: Path) -> Path:
    """A git repository under review that tries to shadow the gate's own imports."""
    repo = tmp_path / "hostile"
    (repo / "ocrl").mkdir(parents=True)
    marker = repo / "HIJACKED"

    # pathlib is itself one of the shadowed modules, so the payload writes its marker with
    # the builtin rather than importing anything that might be the shadow.
    hijack = (
        "import sys\n"
        f"open({str(marker)!r}, 'a').write(__name__ + chr(10))\n"
        'sys.stderr.write("*** HOSTILE " + __name__ + " ran as the gate ***")\n'
        'raise SystemExit("hijacked")\n'
    )

    (repo / "ocrl" / "__init__.py").write_text(hijack)
    (repo / "ocrl" / "__main__.py").write_text(hijack)
    (repo / "ocrl" / "cli.py").write_text(hijack)
    for name in SHADOWED_MODULES:
        (repo / f"{name}.py").write_text(hijack)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "hostile"],
        cwd=repo,
        check=True,
    )
    return repo


def pycache_dirs(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("__pycache__")}


def git_status_ignored(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--ignored"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout
