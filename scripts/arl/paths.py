"""State-directory layout and repository resolution.

Ports the path half of ``scripts/lib/common.sh``. Repository paths stay ``str`` rather
than ``Path`` on purpose: a worktree path is hashed into the state layout and compared for
equality against the hook payload's ``cwd``, and ``Path`` would silently normalise
duplicate and trailing slashes out of both.
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

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

from arl.errors import RepoResolutionError, UnsafePathError

__all__ = [
    "activation_dir",
    "have",
    "is_safe_component",
    "latest_pointer_path",
    "repo_root",
    "session_pointer_path",
    "sessions_dir",
    "sha256_hex",
    "state_root",
    "validate_component",
]

#: Names that are never a single, ordinary directory entry.
_RESERVED_COMPONENTS: Final = frozenset({"", ".", ".."})


def is_safe_component(name: str) -> bool:
    """Is ``name`` exactly one ordinary path component?

    This matters more than it looks. ``Path("/state/sessions") / "/etc/passwd"`` is
    ``/etc/passwd`` -- an absolute right-hand side silently *discards* the base -- and a
    ``..`` component walks straight out of the state root. Since the pointer path is both
    written and unlinked, an unchecked session id is an arbitrary-file-delete primitive.
    """
    if name in _RESERVED_COMPONENTS:
        return False
    if "\0" in name:
        return False
    if os.sep in name or (os.altsep and os.altsep in name):
        return False
    # Catches anything the platform still considers more than one component.
    return os.path.basename(name) == name and not os.path.isabs(name)


def validate_component(name: str, *, what: str) -> str:
    if not is_safe_component(name):
        raise UnsafePathError(f"{what} is not a single safe path component: {name!r}")
    return name


def state_root() -> Path:
    """Root of everything this plugin persists (Rule 3: never inside the reviewed repo)."""
    override = os.environ.get("ARL_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    # Reproduces "$HOME/.local/state" textually, so an empty HOME yields "/.local/state"
    # exactly as the shell did, rather than a relative path under cwd.
    base = xdg if xdg else f"{os.environ.get('HOME', '')}/.local/state"
    return Path(base) / "adversarial-review-loop"


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
    """Activation directory for a (worktree, session) pair.

    The worktree is safe by construction -- it is hashed -- but the session id arrives from
    the hook payload and is used verbatim, so it is validated.
    """
    return _worktree_dir(worktree) / validate_component(session, what="session id")


def session_pointer_path(session: str) -> Path:
    """Path of a session pointer. Raises rather than composing an escaping path."""
    return sessions_dir() / validate_component(session, what="session id")


def latest_pointer_path(worktree: str) -> Path:
    """File naming the worktree's most recent activation, for shell-run subcommands."""
    return _worktree_dir(worktree) / "latest"


#: ``git rev-parse`` answering "not a git repository" exits with this status.
_GIT_NOT_A_REPO_STATUS: Final = 128
#: Generous: ``rev-parse --show-toplevel`` touches no objects, but a hung network filesystem
#: must land in a denial rather than hold the hook until the shim's own timeout does.
GIT_TIMEOUT_SECONDS: Final = 30


def intent_path(session: str) -> Path:
    """Marker that a session asked for enforcement before its ``arm`` could persist anything."""
    return state_root() / "intents" / validate_component(session, what="session id")


def have(command: str) -> bool:
    return shutil.which(command) is not None


def repo_root(directory: str) -> str:
    """Resolve the git worktree root of ``directory``. Empty string on any failure.

    For callers that can treat "unknown" as "none". The gate's unbound-session check cannot
    -- see :func:`repo_root_or_raise`.
    """
    try:
        return repo_root_or_raise(directory)
    except RepoResolutionError:
        return ""


def repo_root_or_raise(directory: str) -> str:
    """Resolve the git worktree root of ``directory``, or raise when git cannot answer.

    Empty string means git *answered* "not a repository" (exit 128 with its own message).
    Anything else -- no ``git`` on PATH, a directory that does not exist, a timeout, any other
    exit status -- raises :class:`RepoResolutionError`, so a caller that must know whether a
    directory is guarded cannot mistake "could not ask" for "nothing here".
    """
    if not os.path.isdir(directory):
        raise RepoResolutionError(f"not a directory: {directory!r}")
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            # The "not a git repository" answer is matched on git's own text, which is
            # localised; pin the locale so the match holds on a machine that is not.
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoResolutionError(f"git could not be run: {exc}") from exc
    if proc.returncode == _GIT_NOT_A_REPO_STATUS and "not a git repository" in proc.stderr:
        return ""
    if proc.returncode != 0:
        raise RepoResolutionError(f"git rev-parse exited {proc.returncode}: {proc.stderr.strip()}")
    # Command substitution strips trailing newlines and nothing else; a path may
    # legitimately end in a space.
    return proc.stdout.rstrip("\n")
