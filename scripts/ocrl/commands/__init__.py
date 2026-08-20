"""Subcommand implementations, one module per group.

Every module exposes ``run(argv: list[str]) -> int``, and :mod:`ocrl.cli` imports the one
module a call actually needs. The laziness is deliberate: ``pretool`` runs on every single
tool call, and importing the user-facing commands there would be import cost paid thousands
of times for code that never executes.

Shared here is only what more than one command needs: resolving "the activation for this
worktree" for commands run from a shell rather than from a hook payload.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import ocrl
from ocrl import config as config_module
from ocrl import paths
from ocrl.atomic import write_private_atomic
from ocrl.config import Config
from ocrl.errors import UnsafePathError
from ocrl.state import State

__all__ = [
    "Activation",
    "Refused",
    "current_repo",
    "latest_session",
    "plugin_root",
    "resolve_local_activation",
    "write_latest",
]


class Refused(Exception):
    """Abandon a mutation that has already taken the activation lock.

    ``State.transaction`` saves when its block *completes*, so raising out of the block is
    how a command declines to write -- and it is the only correct way to decide, because the
    decision has to be made against the document the transaction reloaded rather than against
    the copy the command read before it queued for the lock.

    The message is what the user is shown; commands print it and return non-zero.
    """


@dataclass(frozen=True)
class Activation:
    """One resolved activation: which repository, which session, and its loaded state."""

    repo: str
    session: str
    config: Config
    state: State

    @property
    def act_dir(self) -> Path:
        return self.state.act_dir


def plugin_root() -> str:
    """The plugin checkout, as the messages address it.

    ``${CLAUDE_PLUGIN_ROOT}`` wins when it is set, because the paths printed here are
    commands the user or Claude is told to run, and the host's own variable is what the rest
    of the plugin is registered under. ``-I`` implies ``-E``, which suppresses ``PYTHON*``
    only, so this variable still reaches the gate.
    """
    return os.environ.get("CLAUDE_PLUGIN_ROOT") or str(ocrl.PLUGIN_ROOT)


def current_repo() -> str:
    """The repository the command was run from, falling back to the working directory."""
    cwd = os.getcwd()
    return paths.repo_root(cwd) or cwd


def latest_session(worktree: str) -> str:
    """The worktree's most recent activation, or empty when it has never been armed."""
    try:
        with paths.latest_pointer_path(worktree).open(encoding="utf-8", errors="surrogateescape") as handle:
            return handle.readline().rstrip("\n")
    except OSError:
        return ""


def write_latest(worktree: str, session: str) -> None:
    """Record which activation a shell-run command should pick up for this worktree.

    Atomic for the same reason the session pointer is: an interrupted write would leave an
    empty pointer, and every later ``status`` or ``report`` would then report a worktree
    that is armed as not armed.
    """
    write_private_atomic(paths.latest_pointer_path(worktree), f"{session}\n", root=paths.state_root())


def resolve_local_activation(session: str = "") -> Activation | None:
    """Resolve the activation for a command run from a shell, with no hook payload.

    An explicit ``--session`` wins; otherwise the worktree's most recent activation is used.
    ``None`` means "nothing armed here", which every caller reports rather than acting on.

    The repository is resolved from the working directory rather than read out of the state
    document, exactly as the shell did: a command run inside a *different* worktree that
    happens to name a session must not operate on the session's own repository.
    """
    repo = current_repo()
    if not session:
        session = latest_session(repo)
    if not session:
        return None
    config = config_module.load(repo)
    try:
        state = State(repo, session)
    except UnsafePathError:
        # A pointer or a --session that cannot name a directory names no activation.
        return None
    if not state.load():
        return None
    return Activation(repo=repo, session=session, config=config, state=state)
