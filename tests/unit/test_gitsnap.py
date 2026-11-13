"""Snapshotting, mirrored from ``selftest.sh``'s snapshot section.

Beyond that section's verdicts, the properties the shell suite could not easily assert are
covered here: that the repository's real index is byte-identical afterwards, that nothing is
left inside the repository under review (AGENTS.md rule 4), and that the throwaway index is removed on
every path out.
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
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import git, git_status_ignored

from arl import gitsnap
from arl.gitsnap import SnapshotError

#: git's constant id for the empty tree.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@pytest.fixture(autouse=True)
def temp_index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``tempfile`` at a directory of our own, so leftovers are visible.

    Without this the throwaway index lands in the shared ``/tmp`` and "was it cleaned up?"
    is unanswerable.
    """
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


# -- the selftest section, mirrored ----------------------------------------


def test_a_clean_worktree_snapshots_to_the_head_tree(git_repo: Path) -> None:
    assert gitsnap.snapshot(str(git_repo)).tree == gitsnap.head_tree(str(git_repo))


def test_untracked_content_changes_the_snapshot(git_repo: Path) -> None:
    before = gitsnap.snapshot(str(git_repo)).tree
    (git_repo / "untracked.txt").write_text("x\n")
    assert gitsnap.snapshot(str(git_repo)).tree != before


def test_ignored_content_is_excluded_from_the_snapshot(git_repo: Path) -> None:
    (git_repo / "untracked.txt").write_text("x\n")
    (git_repo / ".gitignore").write_text("untracked.txt\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore")

    assert gitsnap.snapshot(str(git_repo)).tree == gitsnap.head_tree(str(git_repo))


# -- the snapshot is the tree the commit will have -------------------------


def commit_tree_of(repo: Path) -> str:
    """The tree ``git add -A && git commit`` actually produces here.

    The gate's whole per-commit invariant is that this equals the snapshot it reviewed, so
    the tests below compare against the real thing rather than against a hand-built expectation.
    """
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "compare")
    return git(repo, "rev-parse", "HEAD^{tree}")


def test_a_force_added_ignored_file_is_in_the_snapshot(git_repo: Path) -> None:
    """The tree that will be committed, not the tree ``add -A`` would build from ``HEAD``.

    ``git add -f`` is the ordinary way to track one file inside an ignored directory
    (``.idea/.gitignore`` under ``.idea/*``). Its index entry survives the ``git add -A`` in
    the approved command, so it is in the commit -- and a snapshot seeded from ``HEAD``
    left it out, making the approved tree short exactly one file and sending every such
    phase to RECONCILE.
    """
    (git_repo / ".gitignore").write_text(".idea/*\n")
    (git_repo / ".idea").mkdir()
    (git_repo / ".idea" / ".gitignore").write_text("shared\n")
    git(git_repo, "add", "-f", ".idea/.gitignore")

    snapshot = gitsnap.snapshot(str(git_repo)).tree

    assert ".idea/.gitignore" in git(git_repo, "ls-tree", "-r", "--name-only", snapshot).splitlines()
    assert snapshot == commit_tree_of(git_repo)


def test_a_force_added_ignored_file_is_in_the_first_commit_of_an_empty_repository(tmp_path: Path) -> None:
    """The activation-in-an-empty-repository case, where the divergence had no recovery."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "selftest@example.invalid")
    git(repo, "config", "user.name", "arl selftest")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text(".idea/*\n")
    (repo / ".idea").mkdir()
    (repo / ".idea" / ".gitignore").write_text("shared\n")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "-f", ".idea/.gitignore")

    assert gitsnap.snapshot(str(repo)).tree == commit_tree_of(repo)


def test_the_copied_index_keeps_mkstemps_private_mode(git_repo: Path, temp_index_dir: Path) -> None:
    """The copy takes the real index's *timestamps*, never its mode.

    A repository index is commonly group-readable (0664 under a shared umask). Copying that
    mode onto a file in a world-writable ``$TMPDIR`` would publish every path in the
    repository and let anyone in the group rewrite the index the review is taken from.
    """
    index_file = git_repo / ".git" / "index"
    index_file.chmod(0o664)
    stamp = index_file.stat()
    copy = temp_index_dir / "copy"
    copy.touch(mode=0o600)

    gitsnap._seed(str(git_repo), str(copy), {})

    assert copy.stat().st_mode & 0o777 == 0o600
    assert copy.stat().st_mtime_ns == stamp.st_mtime_ns


def test_a_same_size_rewrite_right_after_a_stage_is_still_seen(git_repo: Path) -> None:
    """The copied index keeps the original's mtime, so git's racy-clean re-read still fires.

    An entry whose recorded mtime is not older than the index file's own is "racily clean":
    git re-reads its content rather than trusting the stat data, which is what catches a
    same-size rewrite in the same second as the last ``git add``. A copy stamped with *now*
    looks newer than every entry in it, turning that protection off -- and the snapshot would
    then carry the previous content of a file the commit carries the new content of.
    """
    (git_repo / "new.txt").write_text("phase one work\n")
    git(git_repo, "add", "-A")
    (git_repo / "new.txt").write_text("phase two work\n")  # same length, same second

    snapshot = gitsnap.snapshot(str(git_repo)).tree

    assert git(git_repo, "show", f"{snapshot}:new.txt") == "phase two work"
    assert snapshot == commit_tree_of(git_repo)


def test_a_split_index_snapshots_to_the_tree_that_will_be_committed(git_repo: Path) -> None:
    """A copied split index resolves its shared half against ``$GIT_DIR``, so the copy is usable.

    And the copy is written back with ``core.splitIndex=false``, so no new ``sharedindex.*``
    is deposited in the repository under review (rule 4).
    """
    git(git_repo, "update-index", "--split-index")
    (git_repo / "new.txt").write_text("new\n")
    git(git_repo, "add", "-A")
    # Captured after the repository's own writes, so what this asserts is that the *snapshot*
    # adds nothing -- git's own split-index bookkeeping is the repository's business.
    shared_before = sorted(path.name for path in (git_repo / ".git").glob("sharedindex.*"))

    snapshot = gitsnap.snapshot(str(git_repo)).tree

    assert sorted(path.name for path in (git_repo / ".git").glob("sharedindex.*")) == shared_before
    assert snapshot == commit_tree_of(git_repo)


def test_nothing_is_oversized_in_a_small_repo(git_repo: Path) -> None:
    assert gitsnap.oversized(str(git_repo), 1_000_000) == []


def test_an_oversized_stageable_file_is_reported(git_repo: Path) -> None:
    (git_repo / "big.bin").write_bytes(b"a" * 2000)
    found = gitsnap.oversized(str(git_repo), 1000)

    assert [path for path, _ in found] == ["big.bin"]
    assert found[0][1] == 2000


def test_committed_content_is_not_re_checked_for_size(git_repo: Path) -> None:
    """A large blob already in history must not wedge the gate forever."""
    (git_repo / "big.bin").write_bytes(b"a" * 2000)
    git(git_repo, "add", "big.bin")
    git(git_repo, "commit", "-qm", "big")

    assert gitsnap.oversized(str(git_repo), 1000) == []


def test_an_oversized_symlink_target_is_not_charged_to_the_link(git_repo: Path, tmp_path: Path) -> None:
    """The snapshot records the link, not the target, so the target's size is not committed."""
    target = tmp_path / "big-target.bin"
    target.write_bytes(b"a" * 2000)
    (git_repo / "link.bin").symlink_to(target)

    assert gitsnap.oversized(str(git_repo), 1000) == []


def test_ignored_files_are_not_stageable(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("junk.txt\n")
    (git_repo / "junk.txt").write_text("noise\n")
    assert gitsnap.stageable(str(git_repo)) == [".gitignore"]


# -- Rule 4: the repository under review is not touched --------------------


def test_the_real_index_is_byte_identical_afterwards(git_repo: Path) -> None:
    """The snapshot goes through ``GIT_INDEX_FILE``; the repository's own index is not it.

    If this ever regresses, the gate would be staging the user's work for them -- and a
    later ``git commit`` would commit whatever the gate had staged.
    """
    (git_repo / "untracked.txt").write_text("x\n")
    (git_repo / "seed.txt").write_text("seed\nmodified\n")
    git(git_repo, "status", "--porcelain")  # let git refresh the index first
    index = git_repo / ".git" / "index"
    before = index.read_bytes()

    gitsnap.snapshot(str(git_repo))

    assert index.read_bytes() == before
    assert git(git_repo, "diff", "--cached", "--name-only") == "", "nothing may end up staged"


def test_nothing_is_left_inside_the_repository(git_repo: Path) -> None:
    (git_repo / "untracked.txt").write_text("x\n")
    before = git_status_ignored(git_repo)

    gitsnap.snapshot(str(git_repo))

    assert git_status_ignored(git_repo) == before


def test_the_throwaway_index_is_removed(git_repo: Path, temp_index_dir: Path) -> None:
    (git_repo / "untracked.txt").write_text("x\n")
    gitsnap.snapshot(str(git_repo))
    assert list(temp_index_dir.iterdir()) == []


def test_the_throwaway_index_is_removed_even_when_the_snapshot_fails(tmp_path: Path, temp_index_dir: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(SnapshotError):
        gitsnap.snapshot(str(not_a_repo))

    assert list(temp_index_dir.iterdir()) == []


# -- failure modes ---------------------------------------------------------


def test_a_directory_that_is_not_a_repository_raises(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    # The first thing the snapshot asks git is where this worktree's index is, so that is
    # the question a directory git refuses to answer for fails on.
    with pytest.raises(SnapshotError, match="could not say where this worktree's index is"):
        gitsnap.snapshot(str(not_a_repo))


def test_a_repository_with_no_commits_snapshots_from_the_empty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")

    assert gitsnap.head_commit(str(repo)) == ""
    assert gitsnap.head_tree(str(repo)) == ""
    assert gitsnap.snapshot(str(repo)).tree == EMPTY_TREE

    (repo / "first.txt").write_text("first\n")
    assert gitsnap.snapshot(str(repo)).tree != EMPTY_TREE


def test_head_accessors_are_empty_rather_than_raising_outside_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitsnap.head_commit(str(plain)) == ""
    assert gitsnap.head_tree(str(plain)) == ""


# -- the dirty summary -----------------------------------------------------


# -- changed paths and the ignore globs ------------------------------------


@pytest.fixture
def two_trees(git_repo: Path) -> tuple[Path, str, str]:
    """A base tree and a snapshot tree differing in three paths."""
    base = gitsnap.head_tree(str(git_repo))
    (git_repo / "docs").mkdir()
    (git_repo / "docs" / "guide.md").write_text("doc\n")
    (git_repo / "CHANGELOG.md").write_text("log\n")
    (git_repo / "src.py").write_text("code\n")
    return git_repo, base, gitsnap.snapshot(str(git_repo)).tree


# -- submodules ------------------------------------------------------------


def test_a_submodule_is_declared_as_not_diffed(git_repo: Path, tmp_path: Path) -> None:
    """The reviewer never sees inside a submodule, so the snapshot has to say so."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    git(inner, "config", "user.email", "selftest@example.invalid")
    git(inner, "config", "user.name", "arl selftest")
    (inner / "lib.txt").write_text("lib\n")
    git(inner, "add", "-A")
    git(inner, "commit", "-qm", "lib")

    added = subprocess.run(
        ["git", "-C", str(git_repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "vendor"],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode != 0:
        pytest.skip(f"this git refuses a local submodule: {added.stderr.strip()}")
    git(git_repo, "commit", "-qm", "add submodule")

    warnings = gitsnap.snapshot(str(git_repo)).warnings
    assert "vendor" in warnings
    assert "content NOT diffed" in warnings


def test_no_submodules_means_no_warnings(git_repo: Path) -> None:
    assert gitsnap.snapshot(str(git_repo)).warnings == ""


# -- non-UTF-8 paths -------------------------------------------------------


def test_a_path_that_is_not_valid_utf8_does_not_crash_the_gate(git_repo: Path) -> None:
    """Paths are bytes. Decoding one strictly would raise, and a raising gate is a denial
    for something the user did nothing wrong to trigger."""
    name = os.fsdecode(b"weird-\xff.txt")
    try:
        (git_repo / name).write_text("x\n")
    except (OSError, UnicodeError):  # pragma: no cover - filesystem dependent
        pytest.skip("this filesystem refuses non-UTF-8 names")

    assert name in gitsnap.stageable(str(git_repo))
    assert gitsnap.snapshot(str(git_repo)).tree != gitsnap.head_tree(str(git_repo))


# -- checked_tree: a state-supplied object id must never reach argv unchecked ----------


def test_looks_like_object_id_is_the_cheap_shape_guard(git_repo: Path) -> None:
    assert gitsnap.looks_like_object_id("a" * 40)
    assert gitsnap.looks_like_object_id("0" * 64)
    assert gitsnap.looks_like_object_id(git(git_repo, "rev-parse", "HEAD^{tree}"))
    for bad in (
        "--output=/x",
        "-O",
        "HEAD",
        "main",
        "",
        "A" * 40,  # uppercase hex is not what git emits
        "a" * 39,
        "a" * 41,
        "deadbeef",
        "a" * 40 + "\n",
        None,
        40,
        ["a" * 40],
    ):
        assert not gitsnap.looks_like_object_id(bad), bad


# -- changed_paths_strict ------------------------------------------------------


def _trees(repo: Path) -> tuple[str, str]:
    return git(repo, "rev-parse", "HEAD^{tree}"), gitsnap.snapshot(str(repo)).tree


# --------------------------------------------------------------------------
# info/exclude
# --------------------------------------------------------------------------


def test_exclude_path_follows_git_rather_than_guessing_it(git_repo: Path, tmp_path: Path) -> None:
    """A linked worktree's ``.git`` is a file, so ``<repo>/.git/info/exclude`` does not exist.

    Hand-building that path would point every linked worktree at a file that does not exist,
    so the run directory's exclude entry would never be written where git reads it.
    """
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", "--detach", str(linked))
    assert (linked / ".git").is_file(), "a linked worktree's .git is a file, not a directory"

    resolved = gitsnap.exclude_path(str(linked))

    assert resolved is not None
    assert resolved.parent.name == "info"
    assert not str(resolved).startswith(str(linked / ".git" / "info"))


def test_an_excluded_file_is_invisible_to_every_tree_this_module_builds(git_repo: Path) -> None:
    """``snapshot`` stages with ``git add -A``, which obeys ``info/exclude``: one line in a file
    outside the worktree hides a real file from every tree. Pinned because the run's own
    ``.arl/`` entry relies on exactly this, and anything else listed there is unreviewed.
    """
    baseline = gitsnap.snapshot(str(git_repo)).tree
    (git_repo / "backdoor.py").write_text("payload\n")
    assert gitsnap.snapshot(str(git_repo)).tree != baseline

    exclude = gitsnap.exclude_path(str(git_repo))
    assert exclude is not None
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as handle:
        handle.write("backdoor.py\n")

    assert gitsnap.snapshot(str(git_repo)).tree == baseline, "and the tree falls back to the baseline"
    assert (git_repo / "backdoor.py").read_text() == "payload\n", "while the file is still there"


# --------------------------------------------------------------------------
# Excluding paths from a snapshot
# --------------------------------------------------------------------------


def test_an_excluded_path_keeps_what_the_index_holds(git_repo: Path) -> None:
    """A nested root is left out of its container: its changes neither appear nor vanish."""
    (git_repo / "inner").mkdir()
    (git_repo / "inner" / "tracked.txt").write_text("committed\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "inner")
    (git_repo / "inner" / "tracked.txt").write_text("changed\n")
    (git_repo / "inner" / "new.txt").write_text("new\n")
    (git_repo / "outer.txt").write_text("outer\n")

    tree = gitsnap.snapshot(str(git_repo), exclude=["inner"]).tree

    listed = git(git_repo, "ls-tree", "-r", "--name-only", tree).splitlines()
    assert "outer.txt" in listed
    assert "inner/new.txt" not in listed
    assert git(git_repo, "show", f"{tree}:inner/tracked.txt") == "committed"


def test_an_unborn_nested_repository_can_be_excluded(git_repo: Path) -> None:
    """``add -A`` refuses an embedded repository with no commit; excluded, it is never asked."""
    nested = git_repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "x.txt").write_text("x\n")
    with pytest.raises(gitsnap.SnapshotError):
        gitsnap.snapshot(str(git_repo))
    assert gitsnap.snapshot(str(git_repo), exclude=["nested"]).tree


def test_an_excluded_path_git_already_ignores_does_not_fail_the_snapshot(git_repo: Path) -> None:
    (git_repo / ".git" / "info" / "exclude").write_text("/.arl/\n")
    (git_repo / ".arl").mkdir()
    (git_repo / ".arl" / "state.json").write_text("{}\n")
    tree = gitsnap.snapshot(str(git_repo), exclude=[".arl"]).tree
    assert ".arl/state.json" not in git(git_repo, "ls-tree", "-r", "--name-only", tree)


def test_an_excluded_name_is_never_read_as_a_glob(git_repo: Path) -> None:
    (git_repo / "a*").mkdir()
    (git_repo / "a*" / "x.txt").write_text("x\n")
    (git_repo / "abc.txt").write_text("abc\n")
    tree = gitsnap.snapshot(str(git_repo), exclude=["a*"]).tree
    listed = git(git_repo, "ls-tree", "-r", "--name-only", tree).splitlines()
    assert "abc.txt" in listed
    assert "a*/x.txt" not in listed
