"""Snapshotting the working state into an immutable tree id.

Ports ``scripts/lib/gitsnap.sh``. A snapshot is committed + staged + unstaged + non-ignored
untracked content, captured through a **throwaway index**: ``GIT_INDEX_FILE`` points at a
temporary file, so ``read-tree``/``add -A``/``write-tree`` never touch the repository's real
index (Rule 3 -- nothing the gate does is visible inside the repository under review).

Only the index is redirected. ``git add -A`` still writes the blobs it hashes into
``.git/objects``, exactly as the shell did; that is unavoidable for ``write-tree`` and is
invisible to ``git status``.

The temporary index lands wherever ``tempfile`` puts it (``TMPDIR``), which is the shell's
behaviour too. It is not placed inside the repository, and it is removed on every path out,
including the ``index.lock`` git leaves behind when it is interrupted -- the shell leaked
that one.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from arl import globmatch
from arl.config import Config
from arl.errors import OcrlError
from arl.util import log

__all__ = [
    "GIT_TIMEOUT_SEC",
    "ChangedPathsUnavailable",
    "GitUnavailable",
    "Snapshot",
    "SnapshotError",
    "all_paths_ignored",
    "changed_paths",
    "changed_paths_strict",
    "checked_tree",
    "dirty_summary",
    "format_oversized",
    "git_run",
    "head_commit",
    "head_tree",
    "head_tree_checked",
    "is_ancestor",
    "is_ancestor_checked",
    "looks_like_object_id",
    "oversized",
    "rev_parse",
    "rev_parse_checked",
    "snapshot",
    "stageable",
    "submodule_warnings",
    "worktree_clean",
]

#: How long any one ``git`` metadata call is given. Generous by design -- it is a ceiling on
#: a hang, not a performance budget -- but finite, because several of these run inside the
#: reviewer's active-review lease and an unbounded step there is a lease that can expire while
#: its owner is still working. See :func:`git_run`.
GIT_TIMEOUT_SEC: Final = 120

#: Lines of ``git status --porcelain`` kept for a denial message, as ``head -n 40`` did.
DIRTY_SUMMARY_LINES: Final = 40

#: A full-length git object id -- 40 hex (SHA-1) or 64 hex (SHA-256) -- which is the shape a
#: commit, tree or blob id all share. A value read out of ``state.json``
#: (``last_approved_tree``, ``activation_commit``, a ``round_history`` entry's ``tree``) that
#: is not exactly this shape must never reach a git command line: ``git diff --output=<file>``
#: and ``git log --output=<file>`` are real options, so a crafted
#: ``"--output=../../repo/x"`` would have git write *inside* the repository under review,
#: breaking Rule 3 -- and its empty stdout would then read as "no changes" and approve,
#: breaking Rule 1. ``state.json`` is not a trust boundary. A trailing ``--`` on the argv
#: does **not** help here: the hostile value is a leading option, before the ``--``.
#:
#: Matched with :meth:`re.Pattern.fullmatch`, never ``match`` -- ``$`` also matches just
#: before a trailing ``\n``, so ``match`` would accept ``"<40 hex>\n"``.
_OBJECT_ID_RE: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class GitUnavailable(OcrlError):
    """git could not answer at all -- an unreadable ``.git``, or no git on PATH.

    Deliberately distinct from a repository that simply has no commits yet. Both make
    ``head_tree`` return ``""``, and a caller that reads the empty string as "nothing to check
    here" turns "the gate cannot see the history" into "the history is fine" -- which is the
    failure-into-approval Rule 1 forbids. Measured: ``rev-parse --verify --quiet`` exits 1
    with **no stderr** for an unborn HEAD and 128 *with* stderr for a repository it cannot
    read, so the two are told apart by the exit status and the presence of a complaint, never
    by its text (git localises it).
    """


class SnapshotError(OcrlError):
    """The working state could not be turned into a tree id.

    Deliberately *not* in ``arl.errors``: unlike the errors there, this one is caught, by
    the gate, to deny with a message naming the reason (the shell carried that reason in
    ``ARL_SNAP_ERROR``). Left uncaught it still denies, via the fail-closed guard -- there
    is no path on which a failed snapshot becomes an approval (Rule 1).
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

    Bytes rather than text because these commands report *paths*, and a path is not
    required to be valid UTF-8; decoding is done per call site, with ``surrogateescape``.

    An interpreter-level failure -- git missing entirely -- is reported as a non-zero
    result rather than raised, because that is what the shell's ``|| true`` produced and
    every caller here already treats "no output" as failure.

    **Bounded by :data:`GIT_TIMEOUT_SEC`.** A git command in the gate is metadata: a
    ``rev-parse``, a ``log``, a ``--name-only``. None of them has a legitimate reason to take
    minutes, and some of them run *inside* a lease -- ``reviewer._claim_active_review``'s
    active-review slot is honoured for a computed window, and an unbounded step inside that
    window lets the lease expire while the call is still legitimately running, a second review
    reclaim the slot, and the two race to a verdict. An expiry is reported as status ``124``,
    the same status :func:`arl.reviewer.run_bounded` reports it as, and reaches callers
    through the non-zero path they already handle -- which for every one of them means
    denying, never approving (Rule 1).
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


def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """Is ``ancestor`` reachable by walking ``descendant``'s first-parent-and-beyond history?

    ``False`` on anything git cannot resolve. That is the safe direction **only for a caller
    that refuses unless the answer is a confirmed yes** -- ``resume``, which will not trust a
    baseline it cannot place. A caller that instead *denies on a yes* (``pretool``'s recovery
    reset guard) must use :func:`is_ancestor_checked`: for it, "git could not answer" has to
    deny too, and this function would silently let it through.
    """
    return git_run(repo, ["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def is_ancestor_checked(repo: str, ancestor: str, descendant: str) -> bool:
    """:func:`is_ancestor`, but a git error is raised rather than folded into ``False``.

    ``git merge-base --is-ancestor`` exits ``0`` for yes, ``1`` for a definite no, and
    ``128`` (or anything else) when it could not evaluate the question at all -- an
    unresolvable ref, a corrupt object. The plain helper cannot tell the last two apart, so
    a caller that treats ``False`` as "safe to allow" would allow on an unreadable ref.
    Raises :class:`GitUnavailable` for that case; the caller denies on it (Rule 1).
    """
    proc = git_run(repo, ["merge-base", "--is-ancestor", ancestor, descendant])
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitUnavailable(_decode(proc.stderr).strip() or f"git merge-base --is-ancestor exited with status {proc.returncode} in {repo}")


def rev_parse_checked(repo: str, spec: str) -> str:
    """Like :func:`rev_parse`, but raises rather than folding a genuine git failure into
    "this spec does not resolve".

    ``--verify --quiet`` exits 1 with **no stderr** when the spec legitimately names nothing
    (a root commit's ``^``, for one) and 128 *with* stderr when git could not answer at all --
    the same distinction :func:`head_tree_checked` already relies on, and any caller for whom
    that difference decides whether something is reported should use this instead of
    :func:`rev_parse`.
    """
    proc = git_run(repo, ["rev-parse", "--verify", "--quiet", spec])
    if proc.returncode == 0:
        return _first_line_output(proc)
    complaint = _decode(proc.stderr).strip()
    if proc.returncode == 1 and not complaint:
        return ""
    raise GitUnavailable(complaint or f"git rev-parse exited with status {proc.returncode} in {repo}")


def head_tree_checked(repo: str) -> str:
    """HEAD's tree id, ``""`` for a repository with no commits, or raise.

    Raises :class:`GitUnavailable` when git could not answer, so "the history cannot be read"
    cannot be mistaken for "there is nothing here to look at".
    """
    return rev_parse_checked(repo, "HEAD^{tree}")


def checked_tree(repo: str, value: object) -> str:
    """Resolve a **state-supplied** object id to its tree id, or ``""`` on any failure.

    ``state.json`` is not a trust boundary, and ``value`` comes straight out of it. It is
    matched against :data:`_OBJECT_ID_RE` (via :func:`looks_like_object_id`) *before* it is
    allowed anywhere near argv -- a value
    like ``--output=../x`` never reaches git -- and then resolved through ``<value>^{tree}``
    so a blob id, a tag, or a commit that no longer exists all fail rather than being
    diffed. ``""`` on every failure path, never an exception and never the raw ``value``.

    A caller building a git command from the result must **still** terminate its argument
    list with ``--``: this helper guarantees the id is well formed, not that every other
    argument on that line is.
    """
    if not looks_like_object_id(value):
        return ""
    try:
        return rev_parse_checked(repo, f"{value}^{{tree}}")
    except GitUnavailable:
        return ""


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


def stageable(repo: str) -> list[str]:
    """Paths a snapshot would newly stage: worktree-modified tracked, plus untracked.

    Already-committed content is deliberately not re-checked, so a large blob that is
    already in history does not wedge the gate forever.
    """
    proc = git_run(repo, ["ls-files", "-z", "--others", "--exclude-standard", "--modified"])
    if proc.returncode != 0:
        return []
    return [_decode(entry) for entry in proc.stdout.split(b"\0") if entry]


def oversized(repo: str, limit: int) -> list[tuple[str, int]]:
    """Stageable regular files strictly larger than ``limit`` bytes.

    Symlinks are skipped: the snapshot records the link, not the target, so the target's
    size is not what would be committed.
    """
    found: list[tuple[str, int]] = []
    for rel in stageable(repo):
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


def format_oversized(entries: Sequence[tuple[str, int]]) -> str:
    """``path<TAB>bytes`` per line, the shape the denial message embeds."""
    return "".join(f"{path}\t{size}\n" for path, size in entries)


# --------------------------------------------------------------------------
# Submodules
# --------------------------------------------------------------------------


def submodule_warnings(repo: str) -> str:
    """Declare every submodule for the report header. Content is never diffed.

    The gate reviews one tree; a submodule is a pointer to another repository's history,
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


def snapshot(repo: str) -> Snapshot:
    """Turn the current working state into a tree id.

    Raises ``SnapshotError`` with the reason the shell put in ``ARL_SNAP_ERROR``. There is
    no "best effort" result: a tree that does not represent the working state would be
    reviewed instead of the code about to be committed.
    """
    with _temp_index() as index:
        env = {**os.environ, "GIT_INDEX_FILE": index}
        seed = ["read-tree", "HEAD"] if head_commit(repo) else ["read-tree", "--empty"]
        if git_run(repo, seed, env=env).returncode != 0:
            raise SnapshotError("could not seed the temporary index from HEAD")
        if git_run(repo, ["add", "-A"], env=env).returncode != 0:
            raise SnapshotError("git add -A failed against the temporary index")
        write = git_run(repo, ["write-tree"], env=env)
        tree = _first_line_output(write) if write.returncode == 0 else ""

    if not tree:
        raise SnapshotError("git write-tree failed against the temporary index")
    return Snapshot(tree=tree, warnings=submodule_warnings(repo))


def worktree_clean(repo: str) -> bool:
    """True when the worktree holds nothing beyond HEAD.

    A snapshot failure answers False -- "not clean" -- which is what the shell's non-zero
    return meant. Callers refuse to arm, or refuse to finish, on False; the failure never
    becomes permission to proceed.
    """
    try:
        snap = snapshot(repo)
    except SnapshotError as exc:
        log(f"treating {repo} as dirty: {exc}")
        return False
    return bool(snap.tree) and snap.tree == head_tree(repo)


def dirty_summary(repo: str) -> str:
    """Human-readable summary of what makes the worktree dirty, bounded in length."""
    proc = git_run(repo, ["status", "--porcelain=v1"])
    if proc.returncode != 0:
        return ""
    return "\n".join(_decode(proc.stdout).splitlines()[:DIRTY_SUMMARY_LINES])


def changed_paths(repo: str, base: str, head: str) -> list[str]:
    """Paths differing between two tree-ish ids. Empty on any failure."""
    proc = git_run(repo, ["diff", "--name-only", base, head, "--"])
    if proc.returncode != 0:
        return []
    return [line for line in _decode(proc.stdout).splitlines() if line]


class ChangedPathsUnavailable(OcrlError):
    """:func:`changed_paths_strict` could not produce an honest path set.

    Distinct from :func:`changed_paths`'s silent ``[]`` on purpose: that one feeds
    :func:`all_paths_ignored`, where an empty list already means "do not skip the review", so
    failing quietly there is the safe direction. Here the set decides which findings may be
    *deferred* (``reviewer.LateScope``), and an empty or partial set would defer more than the
    truth allows -- so the caller has to hear about it and refuse to build the scope.
    """


#: The one-path ``--name-status`` letters. ``R``/``C`` carry a score and two paths and are
#: handled separately; anything else is a record this parser does not know and refuses.
_ONE_PATH_STATUSES: Final = frozenset({b"A", b"M", b"D", b"T", b"U", b"X"})


def _strict_path(raw: bytes) -> str:
    """Decode one ``-z`` path record and refuse any the ``FINDING`` grammar could not spell.

    A path holding a newline or ``|`` can never appear in a ``FINDING`` line's ``file=``
    field, and neither can one with leading or trailing whitespace (``reviewer._FINDING_RE``
    forbids both ends). A changed file with such a name can therefore never be matched by a
    finding, which means no scope built from it would be honest -- the finding the reviewer
    raised about it would look "outside the changed paths" and be deferred. Refuse instead.
    """
    path = raw.decode("utf-8", "surrogateescape")
    if not path or "\n" in path or "|" in path or path != path.strip():
        raise ChangedPathsUnavailable(f"a changed path cannot be named by a finding: {path!r}")
    return path


def changed_paths_strict(repo: str, base: str, head: str) -> frozenset[str]:
    """Every path differing between two tree-ish ids, or :class:`ChangedPathsUnavailable`.

    ``git diff --name-status -z -M base head --``, parsed **as NUL-separated bytes**:
    ``<status>\\0<path>\\0`` for ``A``/``M``/``D``/``T`` (and the unmerged/unknown letters
    git can print for a worktree diff), ``<status><score>\\0<old>\\0<new>\\0`` for ``R``/``C``
    -- both sides of a rename are kept, because a reviewer may name either. Decoding is
    ``surrogateescape``, like every other path read in this module.

    Every doubt is an exception, never a shorter set: a non-zero exit (which is also how
    :func:`git_run` reports its own timeout), a record whose status letter is not one this
    parser knows, a record truncated before its path, or a path :func:`_strict_path` refuses.
    """
    proc = git_run(repo, ["diff", "--name-status", "-z", "-M", base, head, "--"])
    if proc.returncode != 0:
        raise ChangedPathsUnavailable(
            f"git diff --name-status {base}..{head} failed (status {proc.returncode}): {_decode(proc.stderr[:512]).strip()}"
        )
    fields = proc.stdout.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    out: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            raise ChangedPathsUnavailable("git diff --name-status emitted an empty status record")
        letter = status[:1]
        if letter in (b"R", b"C"):
            if index + 2 >= len(fields):
                raise ChangedPathsUnavailable("git diff --name-status emitted a truncated rename/copy record")
            out.add(_strict_path(fields[index + 1]))
            out.add(_strict_path(fields[index + 2]))
            index += 3
            continue
        if letter not in _ONE_PATH_STATUSES or len(status) != 1:
            raise ChangedPathsUnavailable(f"git diff --name-status emitted an unknown record: {status[:16]!r}")
        if index + 1 >= len(fields):
            raise ChangedPathsUnavailable("git diff --name-status emitted a truncated record")
        out.add(_strict_path(fields[index + 1]))
        index += 2
    return frozenset(out)


def all_paths_ignored(repo: str, base: str, head: str, config: Config) -> bool:
    """True when every changed path matches one of the configured ignore globs.

    False whenever the answer is not certain -- no globs configured, no changed paths, or
    any path that matches none of them -- because True is what skips the review entirely.

    Matching goes through ``arl.globmatch``, which reproduces ``[[ $p == $g ]]`` -- the
    shell's own comparison -- rather than ``fnmatch``. The difference is not cosmetic:
    ``fnmatch`` reads ``[^a]`` as the set ``{'^', 'a'}`` where bash reads it as *not* ``a``,
    so ``fnmatch`` would skip the review of a path bash would have sent to the reviewer.
    """
    globs = config.as_list("ignore_globs")
    if not globs:
        return False
    paths = changed_paths(repo, base, head)
    if not paths:
        return False
    return all(any(globmatch.matches(path, glob) for glob in globs) for path in paths)
