"""Workspace roots: registration, snapshots, nested-repository discovery and late baselines.

A run reviews every **root** it knows about. A root is a git worktree (``kind`` ``git``),
snapshotted with :func:`arl.gitsnap.snapshot`'s throwaway index, or a plain folder (``kind``
``dir``), snapshotted through a private "shadow" repository under ``.arl/shadow/<alias>.git``
whose work tree is the folder. Either way a snapshot is one tree id, and a phase review diffs a
root's ``approved_tree`` against a fresh one.

**Two trees per root.** ``start_tree`` is the snapshot taken when the root joined the run (or
the baseline the user chose for a late root) and never moves; ``approved_tree`` moves on every
approval. ``baseline`` is ``set`` once both are known and ``unknown`` while the user still has
to choose one.

**Nothing nests twice.** A root's snapshot leaves out every other root inside it and the run's
own ``.arl/``, so a nested repository's files are reviewed once, under its own alias. A git root
gets that through ``:(exclude,literal)`` pathspecs; a shadow, through ``git rm --cached`` of the
excluded paths before ``add -A``, since its index persists between snapshots. A git root that
holds ``.arl/`` also gets an ``info/exclude`` entry for it, so the run's files never show in
``git status`` or reach a commit. Nothing is written into tracked content (AGENTS.md rule 4).

**A late root has no trustworthy baseline.** A root registered after ``init`` whose content has
changed since the run started -- a file modified, a commit made -- would be diffed from a
snapshot that already holds this run's changes. It is registered with ``baseline`` ``unknown``
and nothing is reviewed for it until the user picks one (:func:`set_baseline`). The same holds
for every repository :func:`discover` finds after ``init``: it was created or cloned through
something that registered nothing, so whatever is in it may already be the run's work.

Known limits. A late root's "touched" check reads timestamps, so a file replaced with its old
stamp kept (``cp -p``, ``rsync -a``) reads as untouched and is baselined as found. A repository
nested deeper than :data:`SCAN_DEPTH`, or inside ``node_modules``, is not discovered: a git
container records it as a gitlink, so changes inside it are not shown, and one with no commit
fails the container's snapshot loudly. A repository a git container *ignores* is not registered
either (it is a dependency checkout, and never reaches the container's snapshot): a repository
cloned into an ignored folder and changed only through Bash is reviewed only once something
registers it -- ``root-add``, or the mod's ``Edit``/``Write`` hook. A scan that passes
:data:`WALK_LIMIT` entries stops and logs it. A repository discovered inside a plain-folder root leaves that root's snapshot
from then on, so the folder's next diff shows its files as removed while the new root, once
baselined, shows them under its own alias.
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

import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

from arl import config, gitsnap
from arl.atomic import ensure_private_dir
from arl.gitsnap import SnapshotError, git_run
from arl.state import RunError, RunPaths, RunState
from arl.util import log

__all__ = [
    "EMPTY_TREE_SHA1",
    "EMPTY_TREE_SHA256",
    "HISTORY_SEARCH",
    "SCAN_DEPTH",
    "TOUCH_TOLERANCE_NS",
    "WALK_LIMIT",
    "Oversized",
    "alias_for",
    "current_branch",
    "discover",
    "empty_tree",
    "follow_moves",
    "git_command",
    "register",
    "remember_commit",
    "remember_heads",
    "resolve",
    "scan_nested",
    "set_baseline",
    "snapshot",
    "snapshot_all",
    "taken_aliases",
    "touched_since",
]


class Oversized(SnapshotError):
    """A root holds new or modified files past ``max_file_bytes``; nothing was snapshotted.

    Refused before staging rather than after: a snapshot writes every new file into an object
    store, and a run launched from a folder with a multi-gigabyte file in it would otherwise
    copy all of it before anything could say no.
    """

    code: Final = "oversized"

    def __init__(self, alias: str, entries: Sequence[tuple[str, int]], limit: int) -> None:
        self.entries = [(f"{alias}/{path}", size) for path, size in entries]
        listing = "".join(f"\n  {path} ({size} bytes)" for path, size in self.entries)
        super().__init__(f"root {alias} holds files larger than max_file_bytes ({limit} bytes):{listing}")


#: The empty tree's id in a SHA-1 and in a SHA-256 repository.
EMPTY_TREE_SHA1: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
EMPTY_TREE_SHA256: Final = "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321"
#: Both object formats' empty tree: a base that holds nothing.
EMPTY_TREES: Final = frozenset({EMPTY_TREE_SHA1, EMPTY_TREE_SHA256})
SHA256_HEX_LEN: Final = 64

#: How many directory levels below a root :func:`scan_nested` descends.
SCAN_DEPTH: Final = 4

#: Entries :func:`scan_nested` and :func:`touched_since` visit before giving up. A scan that
#: gives up finds nothing more; a touch check that gives up answers "touched", the direction that
#: asks the user rather than guessing a baseline.
WALK_LIMIT: Final = 100_000

#: Directories no walk descends into: git's own metadata, the run's state, and dependency trees
#: that hold neither the user's repositories nor reviewable work.
_SKIP_DIRS: Final = frozenset({".git", ".arl", "node_modules"})

#: The touch check enters ``node_modules`` too: a folder's shadow snapshot includes it.
_TOUCH_SKIP_DIRS: Final = frozenset({".git", ".arl"})

#: A file modified this close **before** the run started still counts as touched by it. Kernel
#: file timestamps come from a coarse clock that can trail ``time.time_ns()`` by a scheduler
#: tick, so a write made just after the start can carry an earlier stamp. Erring early only ever
#: asks the user for a baseline that could have been guessed.
TOUCH_TOLERANCE_NS: Final = 2_000_000_000

#: Variables that would point git at some other repository, index or object store than the
#: one a shadow command names explicitly.
_GIT_LOCATION_VARS: Final = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)

_ALIAS_UNSAFE_RE: Final = re.compile(r"[^A-Za-z0-9._-]+")
_REFLOG_TIME_RE: Final = re.compile(r"@\{(\d+)\}$")


# --------------------------------------------------------------------------
# Paths and aliases
# --------------------------------------------------------------------------


def resolve(path: Path) -> tuple[Path, str]:
    """The root ``path`` belongs to: its git worktree's top level, or a folder.

    Roots are directories. A path inside a git worktree is that worktree, so registering
    ``repo/src`` registers ``repo``. An existing file stands for the folder holding it. A path
    that does not exist yet is a folder about to be created -- a caller registering a file it is
    about to write passes that file's directory, never the file. A folder not created yet inside
    a git worktree is that worktree, like an existing one.
    """
    target = path.expanduser().resolve()
    if target.exists() and not target.is_dir():
        target = target.parent
    # A folder not created yet belongs to the worktree its nearest existing ancestor is in.
    probe = target
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    proc = git_run(str(probe), ["rev-parse", "--show-toplevel"])
    top = proc.stdout.decode("utf-8", "surrogateescape").strip() if proc.returncode == 0 else ""
    if top:
        return Path(top).resolve(), "git"
    return target, "dir"


def _within(inner: Path, outer: Path) -> bool:
    return inner == outer or outer in inner.parents


def taken_aliases(run: RunState) -> list[str]:
    """Every alias a new root may not take: the current roots', and those of removed roots."""
    retired = run.data.get("retired_aliases")
    return [root["alias"] for root in run.data["roots"]] + [alias for alias in retired or [] if isinstance(alias, str)]


def alias_for(path: Path, taken: Sequence[str]) -> str:
    """The directory name, made one safe path component and unique among ``taken``."""
    base = _ALIAS_UNSAFE_RE.sub("-", path.name).strip(".-") or "root"
    alias, suffix = base, 2
    while alias in taken:
        alias, suffix = f"{base}-{suffix}", suffix + 1
    return alias


def _excluded(root: dict[str, Any], roots: Sequence[dict[str, Any]], paths: RunPaths) -> list[str]:
    """Paths relative to ``root`` that its snapshot leaves out: the outermost inner roots and ``.arl/``.

    Only the outermost: excluding ``b`` already leaves out ``b/c``, and git refuses a pathspec that
    reaches *into* a submodule ("pathspec 'b/c' is in submodule 'b'"), so naming both fails the
    whole snapshot. ``.arl/`` inside an inner root is that root's to exclude, for the same reason.
    """
    here = Path(root["path"])
    inner = [Path(other["path"]) for other in roots if other is not root and Path(other["path"]) != here and _within(Path(other["path"]), here)]
    outermost = [path for path in inner if not any(other != path and _within(path, other) for other in inner)]
    out = [str(path.relative_to(here)) for path in outermost]
    if _within(paths.run_dir, here) and not any(_within(paths.run_dir, path) for path in outermost):
        out.append(str(paths.run_dir.relative_to(here)))
    return sorted(set(out))


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------


def _shadow_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _GIT_LOCATION_VARS}


def _shadow_dir(root: dict[str, Any], paths: RunPaths) -> Path:
    return paths.shadow_dir / f"{root['alias']}.git"


def _shadow_args(root: dict[str, Any], paths: RunPaths) -> list[str]:
    git_dir = _shadow_dir(root, paths)
    return [
        "--git-dir",
        str(git_dir),
        "--work-tree",
        root["path"],
        # A plain folder is not the user's repository, so their global ignore rules and line
        # ending conversion are not applied to it: the shadow records the bytes on disk.
        "-c",
        f"core.excludesFile={os.devnull}",
        "-c",
        "core.autocrlf=false",
    ]


def _shadow_git(root: dict[str, Any], paths: RunPaths, args: Sequence[str]) -> Any:
    return git_run(root["path"], [*_shadow_args(root, paths), *args], env=_shadow_env())


def git_command(root: dict[str, Any], paths: RunPaths) -> tuple[list[str], dict[str, str] | None] | None:
    """The ``git`` argv prefix and environment that read ``root``'s snapshot trees.

    A git root's trees live in its own repository; a folder's in its shadow. ``None`` for a git
    root whose directory is gone, since its object store went with it.
    """
    if root["kind"] == "git":
        if not Path(root["path"]).is_dir():
            return None
        return ["git", "-C", root["path"]], None
    # The git dir alone: a tree-to-tree diff needs no work tree, and the folder may be gone.
    return ["git", "--git-dir", str(_shadow_dir(root, paths))], _shadow_env()


def _ensure_shadow(root: dict[str, Any], paths: RunPaths) -> None:
    git_dir = _shadow_dir(root, paths)
    if (git_dir / "HEAD").is_file():
        return
    ensure_private_dir(paths.shadow_dir, root=paths.run_dir)
    proc = git_run(str(paths.shadow_dir), ["init", "-q", "--bare", str(git_dir)], env=_shadow_env())
    if proc.returncode != 0:
        raise SnapshotError(f"could not create the shadow repository for {root['alias']}")


def _first_line(proc: Any) -> str:
    return str(proc.stdout.decode("utf-8", "surrogateescape").strip()) if proc.returncode == 0 else ""


def snapshot(root: dict[str, Any], roots: Sequence[dict[str, Any]], paths: RunPaths) -> str:
    """The tree id of ``root``'s current content, or :class:`SnapshotError`. Never a stale tree.

    :class:`Oversized` when a file it would newly stage is past ``max_file_bytes``.
    """
    excluded = _excluded(root, roots, paths)
    here = Path(root["path"])
    if not here.exists():
        # A root that is gone holds nothing: the change is the deletion of all of it. A git root
        # takes the well-known empty tree id, since its own object store went with it; the
        # bundle has to report such a root as removed, because its old objects cannot be read.
        if root["kind"] == "git":
            return EMPTY_TREE_SHA256 if len(str(root.get("approved_tree", ""))) == SHA256_HEX_LEN else EMPTY_TREE_SHA1
        return empty_tree(root, paths)
    if not here.is_dir():
        raise SnapshotError(f"root {root['alias']} is no longer a directory: {here}")
    if root["kind"] == "git":
        # A repository whose `.git` is gone resolves to whatever repository contains it; its
        # snapshot would then be that container's tree, filed under this root's alias.
        top = _first_line(git_run(root["path"], ["rev-parse", "--show-toplevel"]))
        if not top or Path(top).resolve() != here.resolve():
            raise SnapshotError(f"root {root['alias']} is no longer the top of a git repository: {here}")
        _guard_size(root, paths)
        return gitsnap.snapshot(root["path"], exclude=excluded).tree
    _ensure_shadow(root, paths)
    _guard_size(root, paths)
    if excluded:
        specs = [f":(literal){path}" for path in excluded]
        if _shadow_git(root, paths, ["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *specs]).returncode != 0:
            raise SnapshotError(f"could not drop nested roots from the shadow index of {root['alias']}")
    # The same `add -A` refusal `gitsnap.not_ignored` documents: the folder's own .gitignore may
    # ignore a nested root.
    kept = gitsnap.not_ignored(root["path"], excluded, git_args=_shadow_args(root, paths), env=_shadow_env())
    pathspecs = ["--", ".", *(f":(exclude,literal){path}" for path in kept)]
    if _shadow_git(root, paths, ["add", "-A", *pathspecs]).returncode != 0:
        raise SnapshotError(f"git add -A failed in the shadow repository of {root['alias']}")
    tree = _first_line(_shadow_git(root, paths, ["write-tree"]))
    if not tree:
        raise SnapshotError(f"git write-tree failed in the shadow repository of {root['alias']}")
    return tree


def _guard_size(root: dict[str, Any], paths: RunPaths) -> None:
    limit = config.load(str(paths.launch_dir)).as_int("max_file_bytes")
    if root["kind"] == "git":
        found = gitsnap.oversized(root["path"], limit)
    else:
        found = gitsnap.oversized(root["path"], limit, git_args=_shadow_args(root, paths), env=_shadow_env())
    if found:
        raise Oversized(root["alias"], found, limit)


def empty_tree(root: dict[str, Any], paths: RunPaths) -> str:
    """The empty tree, written into the object store ``root``'s snapshots use."""
    args = ["hash-object", "-t", "tree", "-w", os.devnull]
    if root["kind"] == "git":
        tree = _first_line(git_run(root["path"], args))
    else:
        _ensure_shadow(root, paths)
        tree = _first_line(git_run(str(paths.shadow_dir), ["--git-dir", str(_shadow_dir(root, paths)), *args], env=_shadow_env()))
    if not tree:
        raise SnapshotError(f"could not write the empty tree for {root['alias']}")
    return tree


def snapshot_all(run: RunState) -> dict[str, str]:
    """Every root's current tree, alias to id. Refuses while a root's baseline is unknown."""
    unknown = run.unknown_baselines()
    if unknown:
        raise RunError("baseline-unknown", "baseline unknown for " + ", ".join(root["alias"] for root in unknown))
    roots = run.data["roots"]
    return {root["alias"]: snapshot(root, roots, run.paths) for root in roots}


# --------------------------------------------------------------------------
# Discovery and late baselines
# --------------------------------------------------------------------------


def _walk(top: Path, max_depth: int, *, skip: frozenset[str] = _SKIP_DIRS) -> Iterator[tuple[Path, os.DirEntry[str]]]:
    """Every entry under ``top`` to ``max_depth`` levels, never following a symlink or entering ``skip``."""
    seen = 0
    stack: list[tuple[Path, int]] = [(top, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > WALK_LIMIT:
                raise _WalkLimit
            yield directory, entry
            if depth < max_depth and entry.name not in skip and entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), depth + 1))


class _WalkLimit(Exception):
    pass


def scan_nested(top: Path, *, max_depth: int = SCAN_DEPTH) -> list[Path]:
    """The outermost git repositories below ``top`` (a ``.git`` directory or file), ``top`` excluded.

    The scan does not descend into a repository it finds: what is nested in *that* one is its
    own scan's business, under its own ignore rules, once it is a root. A dangling gitfile (one
    left behind by a removed submodule, its ``gitdir:`` gone) is no repository: git stages that
    folder's files as plain content of whatever contains it, so the scan treats it as a folder.
    """
    found: list[Path] = []
    seen = 0
    stack: list[tuple[Path, int]] = [(top, 0)]
    while stack and seen <= WALK_LIMIT:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        seen += len(entries)
        if seen > WALK_LIMIT:
            log(f"nested-repository scan of {top} stopped after {WALK_LIMIT} entries; a repository below it may be missed")
            break
        if directory != top and any(entry.name == ".git" for entry in entries) and not _dangling_gitfile(directory):
            found.append(directory.resolve())
            continue
        if depth < max_depth:
            stack.extend((Path(entry.path), depth + 1) for entry in entries if entry.name not in _SKIP_DIRS and entry.is_dir(follow_symlinks=False))
    return sorted(set(found))


_GITFILE_PREFIX: Final = b"gitdir: "
_NUL: Final = b"\0"


def _dangling_gitfile(directory: Path) -> bool:
    """Whether ``directory/.git`` is a gitfile whose ``gitdir:`` names a path that does not exist.

    Decided from the file alone, never from a git probe: a probe also fails on a real repository
    git refuses (``safe.directory``, permissions), whose container would then record only a
    gitlink and hide its changes. Anything not positively dangling stays a repository, and its
    snapshot fails closed.
    """
    gitfile = directory / ".git"
    try:
        if gitfile.is_symlink() or not gitfile.is_file():
            return False
        content = gitfile.read_bytes()
    except OSError:
        return False
    # Parsed as git's `read_gitfile_gently` does: the whole file, only trailing CR and LF
    # dropped, then `gitdir: ` and the path as it stands -- spaces in it are part of the path.
    content = content.rstrip(b"\r\n")
    if not content.startswith(_GITFILE_PREFIX):
        return False
    raw = content[len(_GITFILE_PREFIX) :]
    # A NUL no path can hold: git reads such a file differently, so it is not ours to call dangling.
    if not raw or _NUL in raw:
        return False
    target = os.path.join(os.fsencode(directory), raw)
    if os.path.lexists(target):
        return False
    log(f"{gitfile} points at {os.fsdecode(target)}, which does not exist; its folder is taken as plain content")
    return True


def _nested_roots(container: dict[str, Any]) -> list[Path]:
    """The repositories :func:`scan_nested` finds in ``container`` that can be roots.

    In a git container, a repository the container ignores is a dependency checkout
    (``.terraform/modules/...``, ``.venv/src/...``), not workspace: it never reaches the
    container's snapshot and is not the run's work, so it is not registered.
    """
    top = Path(container["path"])
    found = scan_nested(top)
    if container["kind"] != "git" or not found:
        return found
    kept = set(gitsnap.not_ignored(str(top), [str(path.relative_to(top)) for path in found]))
    return [path for path in found if str(path.relative_to(top)) in kept]


def touched_since(root: dict[str, Any], start_ns: int) -> bool:
    """Whether ``root``'s content may have changed since ``start_ns``.

    A git root is touched when its reflog moved or a modified or untracked file is that recent;
    ignored files (build output, caches) do not count. A folder is touched when any entry in it,
    file or directory (a deletion moves its parent's stamp), is that recent. A walk too large to
    finish answers ``True``.
    """
    since = start_ns - TOUCH_TOLERANCE_NS
    top = Path(root["path"])
    try:
        if root["kind"] == "git":
            return _git_touched(root["path"], since)
        return top.lstat().st_mtime_ns >= since or any(
            entry.stat(follow_symlinks=False).st_mtime_ns >= since for _, entry in _walk(top, max_depth=1_000, skip=_TOUCH_SKIP_DIRS)
        )
    except (_WalkLimit, OSError):
        return True


def _git_touched(repo: str, since: int) -> bool:
    reflog = git_run(repo, ["reflog", "show", "--date=unix", "--format=%gd", "HEAD"])
    for line in reflog.stdout.decode("utf-8", "surrogateescape").splitlines() if reflog.returncode == 0 else []:
        match = _REFLOG_TIME_RE.search(line)
        if match and int(match.group(1)) * 1_000_000_000 >= since:
            return True
    listed = git_run(repo, ["ls-files", "-z", "--modified", "--others", "--exclude-standard"])
    # Changes that live only in the index: a staged addition matching the worktree, a staged
    # deletion. Against HEAD, or every index entry when there is no HEAD yet.
    staged = (
        git_run(repo, ["diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD", "--"])
        if gitsnap.head_commit(repo)
        else git_run(repo, ["ls-files", "-z", "--cached"])
    )
    if listed.returncode != 0 or staged.returncode != 0:
        return True
    names = {name for name in [*listed.stdout.split(b"\0"), *staged.stdout.split(b"\0")] if name}
    # A listed path that no longer exists is a deletion, and lstat raises for it: touched.
    return any((Path(repo) / os.fsdecode(name)).lstat().st_mtime_ns >= since for name in names)


def _proposed_commit(repo: str, start_ns: int) -> str:
    """Where HEAD was when the run started, from the reflog; else the commit it was cloned at.

    The newest reflog entry older than the start is HEAD as the run found it. A repository with
    no such entry was created or cloned during the run. Only a clone arrived with content the
    run did not write, so only a ``clone:`` oldest entry is proposed; a repository the run
    ``git init``-ed has no commit that predates its work, and gets ``""`` -- the user picks an
    empty baseline or the current content.
    """
    proc = git_run(repo, ["reflog", "show", "--date=unix", "--format=%H%x09%gd%x09%gs", "HEAD"])
    entries: list[tuple[str, int, str]] = []
    for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines() if proc.returncode == 0 else []:
        commit, _, rest = line.partition("\t")
        selector, _, subject = rest.partition("\t")
        match = _REFLOG_TIME_RE.search(selector)
        if match and gitsnap.looks_like_object_id(commit):
            entries.append((commit, int(match.group(1)) * 1_000_000_000, subject))
    before = [commit for commit, at, _ in entries if at < start_ns - TOUCH_TOLERANCE_NS]
    if before:
        return before[0]
    if entries and entries[-1][2].startswith("clone:"):
        return entries[-1][0]
    return ""


def _exclude_run_dir(root: dict[str, Any], paths: RunPaths) -> None:
    """Add ``.arl/`` to a git root's ``info/exclude``, once. Never a tracked file."""
    if root["kind"] != "git" or not _within(paths.run_dir, Path(root["path"])):
        return
    exclude = gitsnap.exclude_path(root["path"])
    if exclude is None:
        return
    relative = paths.run_dir.relative_to(Path(root["path"])).as_posix()
    line = "/" + re.sub(r"([\\*?\[\]!# ])", r"\\\1", relative) + "/"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if line in current.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(("" if not current or current.endswith("\n") else "\n") + line + "\n")
    except OSError as exc:
        raise SnapshotError(f"could not add {line} to {exclude}: {exc}") from exc


# --------------------------------------------------------------------------
# The commit and branch a base was taken on, and a checkout that left them
# --------------------------------------------------------------------------


def current_branch(root: dict[str, Any]) -> str:
    """The branch a git root's checkout is on; ``""`` for a folder, a detached HEAD or an error."""
    if root.get("kind") != "git" or not Path(root["path"]).is_dir():
        return ""
    proc = git_run(root["path"], ["symbolic-ref", "--short", "-q", "HEAD"])
    return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""


def _clean_head(root: dict[str, Any], tree: str) -> str:
    """HEAD's commit when its tree is ``tree`` (the base is a clean checkout of it), else ``""``."""
    if root.get("kind") != "git" or not tree or tree in EMPTY_TREES or not Path(root["path"]).is_dir():
        return ""
    commit = gitsnap.head_commit(root["path"])
    return commit if commit and gitsnap.rev_parse(root["path"], f"{commit}^{{tree}}") == tree else ""


def remember_heads(roots: Sequence[dict[str, Any]], *, start: bool = False) -> None:
    """Record the branch each git root is on with its approved base and, ``start``, with its
    starting one, and the commit that starting base is a clean checkout of (``""`` when it holds
    uncommitted changes, or holds nothing). A new base drops what was said about the old one.

    Only a starting base gets a commit, and only the one HEAD was as it was taken: that commit is
    what lets it follow the checkout (:func:`follow_moves`). No commit is ever inferred later.
    """
    for root in roots:
        branch = current_branch(root)
        sides = ("approved", "start") if start else ("approved",)
        for side in sides:
            root[f"{side}_branch"] = branch
            root.pop(f"{side}_moved", None)
        if start:
            root["start_commit"] = _clean_head(root, str(root.get("start_tree") or ""))


def remember_commit(root: dict[str, Any], commit: str) -> None:
    """Record a starting base the user chose as ``commit``: on no branch, and a clean checkout of it."""
    remember_heads([root], start=True)
    root["start_branch"] = root["approved_branch"] = ""
    root["start_commit"] = commit


def _is_ancestor(repo: str, commit: str) -> bool:
    return git_run(repo, ["merge-base", "--is-ancestor", commit, "HEAD"]).returncode == 0


def _merge_base(repo: str, commit: str) -> str:
    proc = git_run(repo, ["merge-base", commit, "HEAD"])
    return proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""


#: How far back HEAD's own history is searched for an approved base, to say nothing needlessly.
HISTORY_SEARCH: Final = 1000


def _in_history(repo: str, tree: str) -> bool:
    """Whether one of HEAD's last :data:`HISTORY_SEARCH` commits holds exactly ``tree``.

    Only ever silences a warning: an approved base committed on the current line is still there.
    """
    proc = git_run(repo, ["log", f"--max-count={HISTORY_SEARCH}", "--format=%T", "HEAD"])
    return proc.returncode == 0 and tree in proc.stdout.decode("utf-8", errors="replace").split()


def _where(branch: str) -> str:
    return f"branch {branch!r}" if branch else "a detached HEAD"


def follow_moves(run: RunState, *, final: bool) -> list[str]:
    """Move a git root's starting base along when its checkout left the base's line of history;
    one line per root moved, or per base that cannot follow and whose diff may show more.

    Only a starting base moves, and only one that was a clean checkout of a commit C as it was
    taken (:func:`remember_heads`): when C is no longer an ancestor of HEAD, the base moves to the
    merge-base of C and HEAD. What C's line has since is the repository as it was before the run,
    in neither the base nor the result, and it would otherwise read as removed by the run. A phase
    moves it only while it is still the approved base too (nothing approved since); the final
    review moves it whenever.

    Nothing else moves. An approved base is the run's own approved work: one the checkout left
    behind stays, so work that vanished from the current line reads as removed, as it is. An empty
    base (``--empty``, a root the run created) means every file is the run's own work, and one with
    uncommitted changes has no commit to follow. A base that stays while the checkout changed
    branch, or that shares no history with HEAD, gets a line saying why its diff may show more.

    A move is said again to every review until the base changes (``<side>_moved``), since the
    review that first saw it may never have reached the reviewer or Claude.
    """
    side = "start" if final else "approved"
    lines = []
    for root in run.roots:
        if root.get("kind") != "git" or root.get("baseline") != "set" or not Path(root["path"]).is_dir():
            continue
        said = [line for line in root.get(f"{side}_moved") or [] if isinstance(line, str)]
        line, moved = _follow(root, final=final)
        if moved:
            # The starting base moved: the final review is told too, and the approved base, while
            # it is the same snapshot, moved with it.
            for which in ("start", "approved") if not final else ("start",):
                root[f"{which}_moved"] = [*[kept for kept in root.get(f"{which}_moved") or [] if isinstance(kept, str)], line]
            said.append(line)
        lines.extend(said if moved or not line else [*said, line])
    return lines


def _why_it_stays(root: dict[str, Any], *, approved: bool, was_branch: str, now: str) -> str:
    """What to say of a base with no commit to follow, the checkout now on ``now``; ``""`` when
    nothing was lost (the same branch, or the approved snapshot still in HEAD's history)."""
    alias = root["alias"]
    if not was_branch or now == was_branch:
        return ""
    if approved:
        # A branch made after the approval, its commit on it, has lost nothing.
        if _in_history(root["path"], str(root.get("approved_tree") or "")):
            return ""
        return (
            f"{alias}: its base, the snapshot approved last, was taken on branch {was_branch!r}, and it is now on "
            f"{_where(now)}. An approved base does not follow the checkout, so whatever differs between the two shows in "
            "this diff as if this stage changed it: approved work only the old branch has reads as removed."
        )
    return (
        f"{alias}: its base was taken on branch {was_branch!r} with uncommitted changes, and it is now on {_where(now)}. "
        "The base could not follow it, so whatever differs between the two shows in this diff as if this stage changed "
        "it: a change only the old branch has reads as removed."
    )


def _follow(root: dict[str, Any], *, final: bool) -> tuple[str, bool]:
    """Move one root's starting base along if it can (:func:`follow_moves`): what to say (``""``
    for nothing), and whether the base moved."""
    repo, alias, now = root["path"], root["alias"], current_branch(root)
    start = str(root.get("start_tree") or "")
    still_start = final or root.get("approved_tree") == start
    side = "start" if still_start else "approved"
    was_branch = str(root.get(f"{side}_branch") or "")
    commit = str(root.get("start_commit") or "") if still_start else ""
    if (commit and _is_ancestor(repo, commit)) or (root.get(f"{side}_tree") or "") in EMPTY_TREES:
        return "", False
    if not commit:
        return _why_it_stays(root, approved=not still_start, was_branch=was_branch, now=now), False
    merge_base = _merge_base(repo, commit)
    tree = gitsnap.rev_parse(repo, f"{merge_base}^{{tree}}") if merge_base else ""
    if not tree:
        return (
            f"{alias}: its base, commit {commit[:12]}, is not in the history of HEAD (now on {_where(now)}), and the two share "
            "no commit. The base could not follow it, so whatever differs between them shows in this diff as if this stage "
            "changed it."
        ), False
    root["start_tree"] = tree
    root["start_commit"] = merge_base
    root["start_branch"] = now
    if not final:
        root["approved_tree"] = tree
        root["approved_branch"] = now
    on = f" on branch {was_branch!r}" if was_branch else ""
    line = (
        f"{alias}: the checkout left its starting base, commit {commit[:12]}{on}, for {_where(now)}, so the base moved to the "
        f"commit the two lines share, {merge_base[:12]}. Commits only the old line has are in neither the base nor this "
        "tree, and are not in this diff."
    )
    return line, True


def _add(run: RunState, path: Path, kind: str, *, late: bool) -> dict[str, Any]:
    roots = run.data["roots"]
    root: dict[str, Any] = {
        "alias": alias_for(path, taken_aliases(run)),
        "path": str(path),
        "kind": kind,
        "baseline": "set",
        "start_tree": "",
        "approved_tree": "",
        "proposed": "",
    }
    roots.append(root)
    _exclude_run_dir(root, run.paths)
    # Nested repositories first: each is its own root, and the container's snapshot has to leave
    # them out -- a nested repository with no commit yet makes the container's `add -A` fail.
    if path.is_dir():
        for nested in _nested_roots(root):
            if not any(Path(other["path"]) == nested for other in roots):
                _add(run, nested, "git", late=late)
    if not path.exists():
        tree = empty_tree(root, run.paths)
    elif late and touched_since(root, run.data["created_at_ns"]):
        root["baseline"] = "unknown"
        if kind == "git":
            root["proposed"] = _proposed_commit(str(path), run.data["created_at_ns"])
        return root
    else:
        tree = snapshot(root, roots, run.paths)
    root["start_tree"] = root["approved_tree"] = tree
    remember_heads([root], start=True)
    return root


def register(run: RunState, path: Path, *, late: bool) -> dict[str, Any]:
    """Register the root ``path`` belongs to, answering what happened.

    ``known`` when it is a root already; ``covered`` when it lies inside one (and is not a
    repository of its own); ``added`` otherwise, with the new root's ``baseline``. ``late`` is
    every registration after ``init``: one whose content has changed since the run started is
    added with an unknown baseline.
    """
    target, kind = resolve(path)
    for root in run.data["roots"]:
        here = Path(root["path"])
        if target == here:
            return {"status": "known", "alias": root["alias"], "baseline": root["baseline"]}
    for root in run.data["roots"]:
        here = Path(root["path"])
        if _within(target, here) and kind != "git":
            return {"status": "covered", "alias": root["alias"], "baseline": root["baseline"]}
    added = _add(run, target, kind, late=late)
    return {"status": "added", "alias": added["alias"], "baseline": added["baseline"], "proposed": added["proposed"]}


def discover(run: RunState, *, late: bool) -> list[str]:
    """Register every repository nested in a root that is not a root yet; answer their aliases.

    After ``init`` (``late``) a found repository always gets an unknown baseline: nothing
    registered it before it appeared, so its content may already be this run's work.
    """
    added: list[str] = []
    # By index, so a repository found here is scanned in turn: the scan stops at each repository
    # it finds, and what that one holds is its own scan's to find.
    index = 0
    while index < len(run.data["roots"]):
        root = run.data["roots"][index]
        index += 1
        if not Path(root["path"]).is_dir():
            continue
        for nested in _nested_roots(root):
            if any(Path(other["path"]) == nested for other in run.data["roots"]):
                continue
            if late:
                entry: dict[str, Any] = {
                    "alias": alias_for(nested, taken_aliases(run)),
                    "path": str(nested),
                    "kind": "git",
                    "baseline": "unknown",
                    "start_tree": "",
                    "approved_tree": "",
                    "proposed": _proposed_commit(str(nested), run.data["created_at_ns"]),
                }
                run.data["roots"].append(entry)
                _exclude_run_dir(entry, run.paths)
            else:
                entry = _add(run, nested, "git", late=False)
            added.append(entry["alias"])
    return added


def set_baseline(run: RunState, alias: str, choice: str, *, commit: str = "") -> str:
    """Settle an unknown baseline: ``commit`` (a git root's revision), ``empty`` or ``current``.

    ``empty`` makes everything in the root new to the run; ``current`` takes what is there now as
    already reviewed. Both trees are set to the choice, so the next phase review diffs from it and
    so does the final review. Answers the tree.
    """
    root = run.root(alias)
    if root is None:
        raise RunError("unknown-root", f"no root named {alias!r}")
    if root["baseline"] != "unknown":
        raise RunError("baseline-set", f"root {alias!r} already has a baseline")
    if choice == "empty":
        tree = empty_tree(root, run.paths)
    elif choice == "current":
        tree = snapshot(root, run.data["roots"], run.paths)
    elif choice == "commit":
        if root["kind"] != "git":
            raise RunError("not-git", f"root {alias!r} is not a git repository; choose empty or current")
        spec = commit or root.get("proposed", "")
        if not spec or spec.startswith("-"):
            raise RunError("bad-commit", f"no commit to take as the baseline of {alias!r}")
        tree = gitsnap.rev_parse(root["path"], f"{spec}^{{tree}}")
        if not tree:
            raise RunError("bad-commit", f"{spec!r} does not name a commit in {alias!r}")
    else:
        raise RunError("bad-choice", f"unknown baseline choice {choice!r}")
    root["start_tree"] = root["approved_tree"] = tree
    root["baseline"] = "set"
    root["proposed"] = ""
    if choice == "commit":
        remember_commit(root, gitsnap.rev_parse(root["path"], f"{spec}^{{commit}}"))
    else:
        remember_heads([root], start=True)
    return tree
