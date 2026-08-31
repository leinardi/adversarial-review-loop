"""Plan revisions: the immutable frozen-plan files a ``plan_revisions`` entry names.

Revision 0 is always ``plan.frozen.md``, written once by ``arm``; every later revision is a
new ``plan.rev<n>.md``, written by ``resume`` and never renamed or overwritten. The state
document is the index -- each ``plan_revisions`` entry is ``{at, phase, sha256, file}`` -- and
the **last** entry is the active plan.

**A ``file`` read out of ``state.json`` is untrusted input.** AGENTS.md is explicit that the
document is not a trust boundary, so every read here goes through the same discipline: the
name must be one safe path component, the path it names must be a literal regular file
directly inside the activation directory (``lstat`` without following symlinks -- a
``realpath``-then-containment check is not enough, because it would happily follow a symlink
planted at one revision's name pointing at *another* file that legitimately lives in the same
directory, silently substituting its content), and its bytes must match the recorded
``sha256`` before anything downstream is allowed to trust them. Any failure is
:class:`EvidenceCorrupted` -- never a placeholder, never a skipped attachment, and never a
silent substitution of ``plan.frozen.md``. Both ``resume`` and the reviewer bundle it into a
hard failure (``NEEDS_HUMAN`` for the reviewer; a same-session-writes-nothing refusal, or an
escalation of the live activation, for resume).
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
import re
import stat
from pathlib import Path
from typing import Any, Final

from arl import paths
from arl.errors import OcrlError
from arl.util import now

__all__ = [
    "PLAN_FROZEN_NAME",
    "EvidenceCorrupted",
    "active_filename",
    "active_revision",
    "read_verified",
    "revisions_with_backfill",
    "verified_revisions",
]

#: The name ``arm`` freezes the plan under, and the name revision 0 always carries.
PLAN_FROZEN_NAME: Final = "plan.frozen.md"

#: What a real ``hashlib.sha256(...).hexdigest()`` looks like -- lowercase, exactly 64 hex
#: digits. Checked before treating a missing value as "nothing to compare".
_SHA256_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")


class EvidenceCorrupted(OcrlError):
    """A plan revision's recorded evidence could not be verified as itself.

    Raised by every function in this module on the first problem found: a filename that is
    not a single safe path component, a path that is missing, a symlink, or not a plain file
    directly inside the activation directory, or content whose hash no longer matches what
    was recorded when the revision was written. Callers decide what a hard failure means in
    their own context; this module never degrades one into something softer.
    """


def read_verified(act_dir: Path, filename: str, expected_sha256: str | None, *, what: str = "plan revision") -> bytes:
    """The bytes of ``filename`` inside ``act_dir``, checked before they are trusted.

    ``expected_sha256=None`` is reserved for synthesizing a brand-new revision 0 from
    scratch, where there is nothing yet to compare against. Every already-recorded entry
    must supply a real hash, checked by the caller before this is reached.

    ``what`` names the kind of evidence being verified, for the messages only -- the checks
    are identical whatever it says. ``arl.guide`` reuses this function for the frozen review
    guide, and a failure there must not tell a human that a *plan* revision is corrupt.
    """
    if not paths.is_safe_component(filename):
        raise EvidenceCorrupted(f'a {what} names an unsafe file ("{filename}"); nothing was resumed.')
    candidate = act_dir / filename
    try:
        info = candidate.lstat()
    except OSError:
        info = None
    if info is None or not stat.S_ISREG(info.st_mode):
        raise EvidenceCorrupted(
            f"the {what} file ({candidate}) is missing, is a symlink, or is not a plain file directly inside "
            "the activation directory; it is evidence a review was run against, and it can no longer be verified as "
            "such. Nothing was resumed."
        )
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise EvidenceCorrupted(f"the {what} file ({candidate}) could not be read ({exc}). Nothing was resumed.") from exc
    if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise EvidenceCorrupted(
            f"the {what} file ({candidate}) no longer matches the hash recorded when it was written -- its "
            "content may have changed since. Nothing was resumed."
        )
    return content


def verified_revisions(act_dir: Path, existing: list[dict[str, Any]]) -> list[tuple[dict[str, Any], bytes]]:
    """Every plan revision, verified and paired with its content.

    Synthesizes revision 0 from ``plan.frozen.md`` when ``existing`` is empty -- honest that
    the hash only attests to the file as found *now*, not as it was when the activation was
    armed, since no earlier hash was ever recorded to check it against. Every already-recorded
    entry is re-verified on **every** call, not only the active (last) one: the reviewer always
    reads whichever revision is active, but a stale or replaced revision-0 file would otherwise
    go unnoticed while a later revision is what gets checked.

    Raises :class:`EvidenceCorrupted` on the first problem -- never a placeholder, never a
    partial list.
    """
    if not existing:
        frozen_bytes = read_verified(act_dir, PLAN_FROZEN_NAME, expected_sha256=None)
        entry = {"at": now(), "phase": 1, "sha256": hashlib.sha256(frozen_bytes).hexdigest(), "file": PLAN_FROZEN_NAME}
        return [(entry, frozen_bytes)]
    pairs: list[tuple[dict[str, Any], bytes]] = []
    for raw_entry in existing:
        if not isinstance(raw_entry, dict):
            raise EvidenceCorrupted(f"a plan revision entry is not an object ({raw_entry!r}); its integrity cannot be verified. Nothing was resumed.")
        entry = dict(raw_entry)
        recorded_hash = entry.get("sha256")
        if not isinstance(recorded_hash, str) or not _SHA256_HEX_RE.fullmatch(recorded_hash):
            raise EvidenceCorrupted(
                f'the plan revision recorded for "{entry.get("file")}" has no valid sha256 recorded; its integrity '
                "cannot be verified. Nothing was resumed."
            )
        content = read_verified(act_dir, str(entry.get("file")), expected_sha256=recorded_hash)
        pairs.append((entry, content))
    return pairs


def revisions_with_backfill(act_dir: Path, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``existing`` (verified), or a synthesized revision 0 when it is empty. Metadata only."""
    return [entry for entry, _content in verified_revisions(act_dir, existing)]


def active_revision(act_dir: Path, existing: list[dict[str, Any]]) -> dict[str, Any]:
    """The last ``plan_revisions`` entry, verified, or a synthesized one when the list is empty."""
    return revisions_with_backfill(act_dir, existing)[-1]


def active_filename(existing: list[dict[str, Any]]) -> str:
    """The active revision's file name, *without* reading, verifying, or trusting its bytes.

    For messages that merely need to point the reader at the right file -- never for anything
    that trusts its *content*, which must go through :func:`verified_revisions` or
    :func:`read_verified` instead. This function still validates the *name*, in a narrower
    sense: ``state.json`` is not a trust boundary (AGENTS.md), so a ``file`` of
    ``"../../etc/passwd"`` or an absolute path must not reach a message that then tells the
    model to read it -- read tools are permitted in every activation status, including
    ``ARMED`` and while ``replan_pending`` fences everything else, so a message is the one
    place this can still steer a read before any hash is ever checked. Validated with
    :func:`arl.paths.is_safe_component`, the same check :func:`read_verified` applies before
    it will open anything.

    **An empty ``existing`` is not an error** -- it is the legitimate "nothing recorded yet"
    case :func:`verified_revisions` itself synthesizes revision 0 for, so it resolves to
    :data:`PLAN_FROZEN_NAME` the same way. **A non-empty ``existing`` whose last entry is
    malformed or unsafe is different**, and is not silently substituted with
    :data:`PLAN_FROZEN_NAME` either: something recorded a revision, and if what it recorded
    cannot even be *named* safely, falling back to revision 0 would point every caller at
    stale or wrong evidence while saying nothing looks wrong -- exactly what let a corrupted
    ``replan_pending`` activation direct the model to redefine phases from evidence nobody
    verified. Raises :class:`EvidenceCorrupted` instead, so callers that show this in a
    message can escalate to ``NEEDS_HUMAN`` rather than quietly recovering.
    """
    if not existing:
        return PLAN_FROZEN_NAME
    last = existing[-1]
    if not isinstance(last, dict):
        raise EvidenceCorrupted(f"the active plan revision entry is not an object ({last!r}); its identity cannot be trusted.")
    candidate = last.get("file")
    if isinstance(candidate, str) and paths.is_safe_component(candidate):
        return candidate
    raise EvidenceCorrupted(f"the active plan revision names an unsafe or malformed file ({candidate!r}); its identity cannot be trusted.")
