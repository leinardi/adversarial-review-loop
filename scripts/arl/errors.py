"""Exceptions the engine raises when it cannot safely continue.

An engine command that cannot prove where it is writing, or which state it is reading, stops
with one of these rather than guessing. The command exits non-zero, and whoever ran it treats
that as a failure, never as an approval.
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
