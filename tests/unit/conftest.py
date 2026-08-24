"""Shared fixtures for the Python unit tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

import ocrl

PLUGIN_ROOT = ocrl.PLUGIN_ROOT
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
BOOTSTRAP = SCRIPTS_DIR / "ocrl-bootstrap.py"
SOCKET_STDIN = PLUGIN_ROOT / "tests" / "fixtures" / "socket-stdin.py"
BASH_GLOB = PLUGIN_ROOT / "tests" / "fixtures" / "bash-glob.sh"
FAKE_REVIEWER = PLUGIN_ROOT / "tests" / "fixtures" / "fake-reviewer.sh"


#: Verdicts already obtained from bash, keyed by ``(path, glob)``. Answers come from the one
#: shell in ``bash_glob_many``; a pair nobody pre-declared still forks its own.
_BASH_GLOB_MEMO: dict[tuple[str, str], bool] = {}


def bash_glob_many(pairs: Iterable[tuple[str, str]]) -> None:
    """Resolve every pair in one bash and memoise the verdicts.

    A fork per pair cost ~2.8ms, and the differential tests ask about ~4000 pairs; batching
    them turns 11s of the suite into one process. Pairs already answered are not re-asked.
    """
    wanted = [pair for pair in dict.fromkeys(pairs) if pair not in _BASH_GLOB_MEMO]
    if not wanted:
        return
    payload = b"".join(f"{path}\0{glob}\0".encode() for path, glob in wanted)
    proc = subprocess.run([str(BASH_GLOB), "--batch"], input=payload, capture_output=True, check=True)
    verdicts = proc.stdout.decode().split("\n")[:-1]
    assert len(verdicts) == len(wanted), f"bash answered {len(verdicts)} of {len(wanted)} pairs"
    for pair, verdict in zip(wanted, verdicts, strict=True):
        _BASH_GLOB_MEMO[pair] = verdict == "1"


def bash_glob(path: str, glob: str) -> bool:
    """Ask a real bash whether ``[[ $path == $glob ]]``.

    Unlike the other bash_* fixtures this plugin used to carry, this one does not depend on
    the plugin's own (now-deleted) Bash implementation -- it drives the system's bash
    directly, so it stays as the reference for globmatch's from-scratch reimplementation of
    ``[[ $p == $g ]]`` semantics.
    """
    cached = _BASH_GLOB_MEMO.get((path, glob))
    if cached is not None:
        return cached
    matched = subprocess.run([str(BASH_GLOB), path, glob], capture_output=True, check=False).returncode == 0
    _BASH_GLOB_MEMO[path, glob] = matched
    return matched


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


def run_hook(
    sub: str,
    payload: Mapping[str, object],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Drive one hook entrypoint with a payload on stdin, exactly as Claude Code does.

    Through the real bootstrap rather than by calling the function: the thing under test is
    the whole contract -- what reaches stdout, and what the process exit status is -- and the
    exit status is the only discriminator the shim is allowed to use.
    """
    return run_bootstrap([sub], cwd=cwd, env=env, stdin=json.dumps(payload).encode())


def hook_json(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The single JSON object a hook emitted. Fails the test if stdout is not exactly one."""
    document: dict[str, Any] = json.loads(proc.stdout)
    return document


def decision(proc: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    """``(permissionDecision, permissionDecisionReason)`` from a ``PreToolUse`` response."""
    output = hook_json(proc)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    return output["permissionDecision"], output["permissionDecisionReason"]


@pytest.fixture(scope="session")
def shared_pycache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One bytecode cache directory shared by every test in this session (per xdist worker).

    ``ocrl-bootstrap.py`` puts ``sys.pycache_prefix`` under ``XDG_CACHE_HOME``. Pointing that
    at each test's own ``tmp_path`` made every one of the ~1200 gate invocations in this suite
    recompile the whole ``ocrl`` package from source: measured at ~145ms of the ~230ms an
    ``arm`` took, i.e. the majority of the suite's runtime. The cache is keyed by absolute
    source path and validated against each source file's mtime and size, so sharing it across
    tests cannot make one test see another's code.

    Tests *about* the cache (Rule 3, unsafe prefixes) override ``XDG_CACHE_HOME`` themselves,
    which is what keeps their assertions about a freshly populated cache honest.
    """
    return tmp_path_factory.mktemp("shared-pycache")


@pytest.fixture
def clean_env(tmp_path: Path, shared_pycache: Path) -> dict[str, str]:
    """An environment isolated from the developer's real state and cache directories."""
    # Every OCRL_* override goes, not just OCRL_STATE_DIR: a developer's shell may carry
    # one (OCRL_MAX_STOP_BLOCKS is a documented leftover from the STEP0 runs), and it would
    # silently skew both the Python result and the shell one it is compared against.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OCRL_")}
    env["HOME"] = str(tmp_path / "home")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["XDG_CACHE_HOME"] = str(shared_pycache)
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


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its stdout, failing the test on a non-zero exit."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {args!r} failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout.rstrip("\n")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A scratch repository with one committed file, matching ``selftest.sh``'s ``new_case``.

    Signing and the user's identity are pinned locally so the fixture does not depend on --
    or trip over -- the developer's global git configuration.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "selftest@example.invalid")
    git(repo, "config", "user.name", "ocrl selftest")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return repo


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
