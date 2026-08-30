"""Exceptions the gate raises when it cannot safely continue.

None of these are caught by the entrypoints. They propagate to the fail-closed guard in
``hookio.Hook.run``, which denies (Rule 1): a gate that cannot prove where it is writing, or
what state it is enforcing, has nothing to say except "no".
"""

from __future__ import annotations

__all__ = ["OcrlError", "RepoResolutionError", "StateLoadError", "UnsafePathError"]


class OcrlError(Exception):
    """Base for every condition that must stop the gate rather than be worked around."""


class UnsafePathError(OcrlError):
    """A path component would escape the state root, or a component is not what it claims.

    Covers traversal (``..``), absolute components, embedded separators, and directories
    that turn out to be symlinks pointing outside the tree the gate owns.
    """


class StateLoadError(OcrlError):
    """The activation state could not be read, so it must not be mutated.

    Distinct from "no activation exists": callers that legitimately create state say so
    explicitly. Everything else denies rather than publishing a document built on top of
    one that could not be parsed.
    """


class RepoResolutionError(OcrlError):
    """Git could not say which repository a directory belongs to.

    Distinct from "not a repository", which is an answer. This is the absence of one: no
    ``git`` on the hook's PATH, a directory that does not exist, a timeout, an unexpected exit.
    A gate that cannot tell what worktree a call is about cannot tell whether it is guarded.
    """
