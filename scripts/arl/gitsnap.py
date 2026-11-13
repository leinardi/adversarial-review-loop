"""Snapshotting the working state into an immutable tree id.

A snapshot is committed + staged + unstaged + non-ignored untracked content, captured through
a **throwaway index**: ``GIT_INDEX_FILE`` points at a temporary *copy* of the repository's
index, so ``add -A``/``write-tree`` never touch the real one (AGENTS.md rule 4).

A copy rather than a fresh index seeded from ``HEAD``, because the tree this produces is the
tree ``git add -A && git commit`` is about to produce, and those differ whenever the real index
holds an entry ``add -A`` would not create -- a ``git add -f`` of a gitignored path is the
ordinary way to get one. Seeding from ``HEAD`` left that entry out of the reviewed tree while
the commit carried it, so every such phase ended in ``RECONCILE``. See :func:`_seed`.

Only the index is redirected: ``git add -A`` still writes the blobs it hashes into
``.git/objects``, which is unavoidable for ``write-tree`` and invisible to ``git status``. The
temporary index lands in ``TMPDIR`` and is removed on every path out, including the
``index.lock`` git leaves behind when interrupted.
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

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from arl.errors import OcrlError
from arl.util import log

__all__ = [
    "GIT_TIMEOUT_SEC",
    "Snapshot",
    "SnapshotError",
    "exclude_path",
    "git_run",
    "head_commit",
    "head_tree",
    "looks_like_object_id",
    "not_ignored",
    "oversized",
    "rev_parse",
    "snapshot",
    "stageable",
    "submodule_warnings",
]

#: How long any one ``git`` metadata call is given. Generous by design -- it is a ceiling on
#: a hang, not a performance budget -- but finite, because several of these run inside the
#: reviewer's active-review lease and an unbounded step there is a lease that can expire while
#: its owner is still working. See :func:`git_run`.
GIT_TIMEOUT_SEC: Final = 120


#: A full-length git object id -- 40 hex (SHA-1) or 64 hex (SHA-256) -- which is the shape a
#: commit, tree or blob id all share. A value read out of ``state.json``
#: (``last_approved_tree``, ``activation_commit``, a ``round_history`` entry's ``tree``) that
#: is not exactly this shape must never reach a git command line: ``git diff --output=<file>``
#: and ``git log --output=<file>`` are real options, so a crafted
#: ``"--output=../../repo/x"`` would have git write *inside* the repository under review,
#: breaking rule 4 -- and its empty stdout would then read as "no changes" and approve,
#: breaking Rule 1. ``state.json`` is not a trust boundary. A trailing ``--`` on the argv
#: does **not** help here: the hostile value is a leading option, before the ``--``.
#:
#: Matched with :meth:`re.Pattern.fullmatch`, never ``match`` -- ``$`` also matches just
#: before a trailing ``\n``, so ``match`` would accept ``"<40 hex>\n"``.
_OBJECT_ID_RE: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class SnapshotError(OcrlError):
    """The working state could not be turned into a tree id.

    Deliberately *not* in ``arl.errors``: unlike the errors there, this one is caught by the
    engine and answered as a refusal naming the reason. Left uncaught it is still a failure
    answer -- there is no path on which a failed snapshot becomes an approval (Rule 1).
    """


@dataclass(frozen=True)
class Snapshot:
    """A tree id, plus whatever the caller must be told is *not* represented by it."""

    tree: str
    warnings: str


# --------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------


def git_run(repo: str, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run ``git -C <repo> …``, capturing bytes.

    Bytes rather than text because these commands report *paths*, which are not required to be
    valid UTF-8; decoding is per call site, with ``surrogateescape``. An interpreter-level failure
    -- git missing entirely -- is reported as a non-zero result rather than raised, since every
    caller already treats "no output" as failure.

    **Bounded by :data:`GIT_TIMEOUT_SEC`.** A git command in the engine is metadata and has no
    legitimate reason to take minutes, and some run under the state lock, which an unbounded step
    would hold for as long. An expiry is reported as status ``124``, which reaches callers through
    the non-zero path they already handle -- for every one of them, a failure (Rule 1).
    """
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            check=False,
            env=dict(env) if env is not None else None,
            timeout=GIT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SEC}s")
        return subprocess.CompletedProcess(args=["git", *args], returncode=124, stdout=b"", stderr=b"")
    except OSError as exc:
        log(f"git {' '.join(args)} could not be run: {exc}")
        return subprocess.CompletedProcess(args=["git", *args], returncode=127, stdout=b"", stderr=b"")


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _first_line_output(proc: subprocess.CompletedProcess[bytes]) -> str:
    """Captured stdout with trailing newlines stripped, as command substitution did."""
    return _decode(proc.stdout).rstrip("\n")


def rev_parse(repo: str, spec: str) -> str:
    """Resolve ``spec`` to an object id, or empty when it does not resolve.

    ``--verify --quiet`` because the callers all want "did this name something?" as a plain
    yes/no: git says nothing on stderr and exits non-zero, which becomes an empty string
    here. Every caller treats empty as "no", and no caller treats it as permission.
    """
    proc = git_run(repo, ["rev-parse", "--verify", "--quiet", spec])
    return _first_line_output(proc) if proc.returncode == 0 else ""


def head_commit(repo: str) -> str:
    """HEAD's commit id, or empty in a repository with no commits yet."""
    return rev_parse(repo, "HEAD")


def head_tree(repo: str) -> str:
    """HEAD's tree id, or empty when there is no HEAD.

    Lenient: a repository git cannot read is indistinguishable from one with no commits. Use
    :func:`head_tree_checked` wherever that difference decides whether anything is reported.
    """
    return rev_parse(repo, "HEAD^{tree}")


def exclude_path(repo: str) -> Path | None:
    """Where this worktree's ``info/exclude`` lives, or ``None`` when git will not say.

    ``--git-path`` rather than a hand-built ``.git/info/exclude``: a linked worktree's ``.git``
    is a *file* pointing into the main repository's ``worktrees/<name>/``, and a submodule's is
    elsewhere again. git knows where its own metadata lives; this must not guess.
    """
    proc = git_run(repo, ["rev-parse", "--git-path", "info/exclude"])
    if proc.returncode != 0:
        return None
    resolved = _decode(proc.stdout).strip()
    if not resolved:
        return None
    return Path(resolved) if os.path.isabs(resolved) else Path(repo) / resolved


def looks_like_object_id(value: object) -> bool:
    """Whether ``value`` is *shaped* like a full-length git object id (40 or 64 hex).

    The cheap, no-subprocess guard for a **state-supplied** id that is about to be
    interpolated into a git argument -- ``last_approved_tree`` reaching ``git diff`` on the
    commit hot path, ``activation_commit`` reaching ``git log`` in the bundle. It rejects
    every ``--option``-shaped value (see :data:`_OBJECT_ID_RE`) without paying for a
    ``rev-parse``; :func:`checked_tree` is the stricter check for the paths that can afford
    one. ``state.json`` is not a trust boundary.
    """
    return isinstance(value, str) and _OBJECT_ID_RE.fullmatch(value) is not None


# --------------------------------------------------------------------------
# The oversized guard
# --------------------------------------------------------------------------


def stageable(repo: str, *, git_args: Sequence[str] = (), env: Mapping[str, str] | None = None) -> list[str]:
    """Paths a snapshot would newly stage: worktree-modified tracked, plus untracked.

    Already-committed content is deliberately not re-checked, so a large blob that is
    already in history does not wedge the run forever. ``git_args`` and ``env`` point git at
    another index, as a folder root's shadow repository does.
    """
    proc = git_run(repo, [*git_args, "ls-files", "-z", "--others", "--exclude-standard", "--modified"], env=env)
    if proc.returncode != 0:
        # The size guard cannot pass on an answer git never gave.
        raise SnapshotError(f"git ls-files failed in {repo}: {_decode(proc.stderr).strip()}")
    return [_decode(entry) for entry in proc.stdout.split(b"\0") if entry]


def oversized(repo: str, limit: int, *, git_args: Sequence[str] = (), env: Mapping[str, str] | None = None) -> list[tuple[str, int]]:
    """Stageable regular files strictly larger than ``limit`` bytes.

    Symlinks are skipped: the snapshot records the link, not the target, so the target's
    size is not what would be committed.
    """
    found: list[tuple[str, int]] = []
    for rel in stageable(repo, git_args=git_args, env=env):
        full = os.path.join(repo, rel)
        if os.path.islink(full) or not os.path.isfile(full):
            continue
        try:
            size = os.stat(full).st_size
        except OSError:
            size = 0
        if size > limit:
            found.append((rel, size))
    return found


# --------------------------------------------------------------------------
# Submodules
# --------------------------------------------------------------------------


def submodule_warnings(repo: str) -> str:
    """Declare every submodule for the report header. Content is never diffed.

    A review sees each root's tree; a submodule is a pointer to another repository's history,
    and the reviewer never sees inside it. Saying so is the whole point of this function --
    silence would let a submodule bump pass as a reviewed change.
    """
    proc = git_run(repo, ["submodule", "status", "--recursive"])
    out = _first_line_output(proc) if proc.returncode == 0 else ""
    if not out:
        return ""
    lines: list[str] = []
    for line in out.split("\n"):
        body = line.removeprefix(" ")
        if line.startswith("+"):
            lines.append(f"submodule out of sync (content NOT diffed): {body}")
        elif line.startswith("U"):
            lines.append(f"submodule has merge conflicts (content NOT diffed): {body}")
        elif line.startswith("-"):
            lines.append(f"submodule not initialised (content NOT diffed): {body}")
        else:
            lines.append(f"submodule present (content NOT diffed): {line}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The snapshot itself
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _temp_index() -> Iterator[str]:
    """A throwaway index path, removed however the block is left."""
    try:
        fd, path = tempfile.mkstemp(prefix="arl-index.")
    except OSError as exc:
        raise SnapshotError("could not create a temporary index") from exc
    os.close(fd)
    try:
        yield path
    finally:
        for leftover in (path, f"{path}.lock"):
            with contextlib.suppress(OSError):
                os.unlink(leftover)


def _index_path(repo: str) -> str:
    """Absolute path of the repository's real index file.

    ``rev-parse --git-path`` rather than ``<git-dir>/index`` spelled by hand: ``GIT_INDEX_FILE``
    may already point elsewhere in the environment the hook runs in, and git is the only thing
    that knows where the index for *this* worktree actually is (a linked worktree keeps its own,
    under ``.git/worktrees/<name>/index``).
    """
    proc = git_run(repo, ["rev-parse", "--git-path", "index"])
    path = _first_line_output(proc) if proc.returncode == 0 else ""
    if not path:
        raise SnapshotError("git could not say where this worktree's index is")
    return path if os.path.isabs(path) else os.path.join(repo, path)


def _seed(repo: str, index: str, env: Mapping[str, str]) -> None:
    """Fill the throwaway index with the state ``git add -A`` will be applied to.

    A byte copy of the real index, which is what the commit will build from. Copied rather than
    reconstructed: an index carries entries no tree-level reconstruction reproduces -- a
    force-added gitignored path, an unmerged entry mid-merge -- and those are exactly the ones that
    would make the reviewed tree differ from the committed tree.

    A split index copies safely, since the shared half is resolved against ``$GIT_DIR``; the
    subsequent ``add -A`` runs with ``core.splitIndex=false`` so writing the copy back cannot
    deposit a new ``sharedindex.*`` in the repository.

    Falls back to ``HEAD``, or to an empty index when ``HEAD`` is unborn, only when there is no
    index file at all -- a freshly ``git init``-ed repository nothing has ever staged in. A
    failure to *read* an index that exists raises instead: a snapshot that is not the working
    state is never the safe fallback (Rule 1).
    """
    real = _index_path(repo)
    if os.path.exists(real):
        try:
            # Content and timestamps, but **not** the mode: `shutil.copy2` would copy that
            # too, and a repository index is commonly group-readable (0664 under a shared
            # umask), which would widen the 0600 `mkstemp` gave this file -- publishing every
            # path in the repository, and letting anyone in the group rewrite the very index
            # the review is about to be taken from, in a world-writable `$TMPDIR`.
            shutil.copyfile(real, index)
            # The mtime is the load-bearing half. git calls an entry "racily clean" when its
            # recorded mtime is not older than the index file's own, and re-reads such an
            # entry's content instead of trusting its stat data -- which is the only thing
            # that catches a same-size rewrite made in the same second as the last `git add`.
            # A copy stamped with *now* looks newer than every entry in it, so that protection
            # switches off and the snapshot silently keeps the previous content: a tree the
            # reviewer would approve and the commit would then not match.
            stamp = os.stat(real)
            os.utime(index, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        except OSError as exc:
            raise SnapshotError(f"could not copy this worktree's index: {exc}") from exc
        return
    seed = ["read-tree", "HEAD"] if head_commit(repo) else ["read-tree", "--empty"]
    if git_run(repo, seed, env=env).returncode != 0:
        raise SnapshotError("could not seed the temporary index from HEAD")


def not_ignored(repo: str, paths: Sequence[str], *, git_args: Sequence[str] = (), env: Mapping[str, str] | None = None) -> list[str]:
    """``paths`` less those git already ignores.

    ``add -A`` fails outright when any pathspec, an exclude included, names an ignored path
    ("The following paths are ignored by one of your .gitignore files"), and ``.arl/`` is
    ignored through ``info/exclude`` in every repository that holds it. An ignored path is
    never staged anyway, so dropping it from the excludes changes nothing in the tree. A
    ``check-ignore`` that cannot answer keeps every path, and the snapshot then fails loudly.
    ``git_args`` go before the subcommand (a shadow repository's ``--git-dir``/``--work-tree``).
    """
    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *git_args, "check-ignore", "-z", "--stdin"],
            input=b"".join(os.fsencode(path) + b"\0" for path in paths),
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SEC,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return list(paths)
    if proc.returncode not in (0, 1):
        return list(paths)
    ignored = {os.fsdecode(name) for name in proc.stdout.split(b"\0") if name}
    return [path for path in paths if path not in ignored]


def snapshot(repo: str, *, exclude: Sequence[str] = ()) -> Snapshot:
    """Turn the current working state into a tree id.

    Raises ``SnapshotError``. There is no "best effort" result: a tree that does not represent
    the working state would be reviewed instead of the code about to be committed.

    ``exclude`` names repository-relative paths ``add -A`` must not touch: a nested repository
    that is its own root (which ``add -A`` would otherwise stage as an embedded gitlink, or refuse
    outright when it has no commit yet) and the run's own ``.arl/`` directory. An excluded path
    keeps whatever the real index holds for it, so a tracked submodule stays at its recorded
    commit. The paths are passed as ``:(exclude,literal)`` pathspecs, so no character in them is
    read as a glob.
    """
    exclude = not_ignored(repo, exclude)
    pathspecs = ["--", ".", *(f":(exclude,literal){path}" for path in exclude)] if exclude else []
    with _temp_index() as index:
        env = {**os.environ, "GIT_INDEX_FILE": index}
        _seed(repo, index, env)
        if git_run(repo, ["-c", "core.splitIndex=false", "add", "-A", *pathspecs], env=env).returncode != 0:
            raise SnapshotError("git add -A failed against the temporary index")
        write = git_run(repo, ["write-tree"], env=env)
        tree = _first_line_output(write) if write.returncode == 0 else ""

    if not tree:
        raise SnapshotError("git write-tree failed against the temporary index")
    return Snapshot(tree=tree, warnings=submodule_warnings(repo))
