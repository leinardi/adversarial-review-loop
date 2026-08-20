"""State-directory layout and repository resolution.

Ports the path half of ``scripts/lib/common.sh``. Repository paths stay ``str`` rather
than ``Path`` on purpose: a worktree path is hashed into the state layout and compared for
equality against the hook payload's ``cwd``, and ``Path`` would silently normalise
duplicate and trailing slashes out of both.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

__all__ = [
    "activation_dir",
    "have",
    "latest_pointer_path",
    "repo_root",
    "sessions_dir",
    "sha256_hex",
    "state_root",
]


def state_root() -> Path:
    """Root of everything this plugin persists (Rule 3: never inside the reviewed repo)."""
    override = os.environ.get("OCRL_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    # Reproduces "$HOME/.local/state" textually, so an empty HOME yields "/.local/state"
    # exactly as the shell did, rather than a relative path under cwd.
    base = xdg if xdg else f"{os.environ.get('HOME', '')}/.local/state"
    return Path(base) / "opencode-review-loop"


def sessions_dir() -> Path:
    return state_root() / "sessions"


def sha256_hex(text: str) -> str:
    """Hex digest of ``text``, matching ``printf '%s' … | sha256sum``.

    ``os.fsencode`` rather than ``str.encode`` because the input is a filesystem path and
    may carry surrogates from undecodable bytes.
    """
    return hashlib.sha256(os.fsencode(text)).hexdigest()


def _worktree_dir(worktree: str) -> Path:
    return state_root() / "worktrees" / sha256_hex(worktree)


def activation_dir(worktree: str, session: str) -> Path:
    """Activation directory for a (worktree, session) pair."""
    return _worktree_dir(worktree) / session


def latest_pointer_path(worktree: str) -> Path:
    """File naming the worktree's most recent activation, for shell-run subcommands."""
    return _worktree_dir(worktree) / "latest"


def have(command: str) -> bool:
    return shutil.which(command) is not None


def repo_root(directory: str) -> str:
    """Resolve the git worktree root of ``directory``. Empty string on any failure."""
    if not os.path.isdir(directory):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    # Command substitution strips trailing newlines and nothing else; a path may
    # legitimately end in a space.
    return proc.stdout.rstrip("\n")
