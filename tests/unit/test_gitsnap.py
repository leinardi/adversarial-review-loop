"""Snapshotting, mirrored from ``selftest.sh``'s snapshot section.

Beyond that section's verdicts, the properties the shell suite could not easily assert are
covered here: that the repository's real index is byte-identical afterwards, that nothing is
left inside the repository under review (Rule 3), and that the throwaway index is removed on
every path out.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import git, git_status_ignored

from ocrl import gitsnap
from ocrl.config import Config
from ocrl.gitsnap import SnapshotError

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


def test_a_worktree_holding_only_ignored_files_counts_as_clean(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("junk.txt\n")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-qm", "ignore")
    (git_repo / "junk.txt").write_text("noise\n")

    assert gitsnap.worktree_clean(str(git_repo)) is True


def test_a_modified_tracked_file_counts_as_dirty(git_repo: Path) -> None:
    (git_repo / "seed.txt").write_text("seed\nmodified\n")
    assert gitsnap.worktree_clean(str(git_repo)) is False


def test_staged_content_counts_as_dirty(git_repo: Path) -> None:
    """Staging is not a way to hide a change from the gate: the snapshot spans the index."""
    (git_repo / "new.txt").write_text("new\n")
    git(git_repo, "add", "new.txt")
    assert gitsnap.worktree_clean(str(git_repo)) is False


def test_nothing_is_oversized_in_a_small_repo(git_repo: Path) -> None:
    assert gitsnap.oversized(str(git_repo), 1_000_000) == []


def test_an_oversized_stageable_file_is_reported(git_repo: Path) -> None:
    (git_repo / "big.bin").write_bytes(b"a" * 2000)
    found = gitsnap.oversized(str(git_repo), 1000)

    assert [path for path, _ in found] == ["big.bin"]
    assert found[0][1] == 2000
    assert gitsnap.format_oversized(found) == "big.bin\t2000\n"


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


# -- Rule 3: the repository under review is not touched --------------------


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
    with pytest.raises(SnapshotError, match="could not seed the temporary index"):
        gitsnap.snapshot(str(not_a_repo))


def test_a_failed_snapshot_counts_as_dirty(tmp_path: Path) -> None:
    """`worktree_clean` answers False when it cannot tell -- the failure never grants anything."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert gitsnap.worktree_clean(str(not_a_repo)) is False


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


def test_the_dirty_summary_names_what_is_dirty(git_repo: Path) -> None:
    (git_repo / "seed.txt").write_text("seed\nmodified\n")
    (git_repo / "untracked.txt").write_text("x\n")
    summary = gitsnap.dirty_summary(str(git_repo))

    assert " M seed.txt" in summary
    assert "?? untracked.txt" in summary


def test_the_dirty_summary_is_bounded(git_repo: Path) -> None:
    for n in range(gitsnap.DIRTY_SUMMARY_LINES + 20):
        (git_repo / f"f{n:03d}.txt").write_text("x\n")
    assert len(gitsnap.dirty_summary(str(git_repo)).splitlines()) == gitsnap.DIRTY_SUMMARY_LINES


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


def test_changed_paths_lists_the_difference(two_trees: tuple[Path, str, str]) -> None:
    repo, base, head = two_trees
    assert gitsnap.changed_paths(str(repo), base, head) == ["CHANGELOG.md", "docs/guide.md", "src.py"]


def test_changed_paths_is_empty_for_an_unknown_tree(git_repo: Path) -> None:
    assert gitsnap.changed_paths(str(git_repo), "not-a-tree", "also-not") == []


def test_no_ignore_globs_means_no_skip(two_trees: tuple[Path, str, str]) -> None:
    repo, base, head = two_trees
    assert gitsnap.all_paths_ignored(str(repo), base, head, Config({"ignore_globs": []})) is False


def test_every_path_matching_a_glob_skips_the_review(two_trees: tuple[Path, str, str]) -> None:
    repo, base, head = two_trees
    config = Config({"ignore_globs": ["*.md", "*.py"]})
    assert gitsnap.all_paths_ignored(str(repo), base, head, config) is True


def test_one_unmatched_path_is_enough_to_require_a_review(two_trees: tuple[Path, str, str]) -> None:
    repo, base, head = two_trees
    config = Config({"ignore_globs": ["*.md"]})
    assert gitsnap.all_paths_ignored(str(repo), base, head, config) is False


def test_a_glob_crosses_directory_separators(two_trees: tuple[Path, str, str]) -> None:
    """`*` spans `/`, as it did under the shell's `[[ $p == $g ]]`.

    A stricter matcher would start reviewing what a user had deliberately excluded; a looser
    one would skip reviews they expected to happen.
    """
    repo, base, head = two_trees
    assert gitsnap.all_paths_ignored(str(repo), base, head, Config({"ignore_globs": ["*"]})) is True
    assert gitsnap.all_paths_ignored(str(repo), base, head, Config({"ignore_globs": ["docs/*", "*.md", "src.py"]})) is True


def test_a_negated_class_does_not_skip_the_review(two_trees: tuple[Path, str, str]) -> None:
    """`[^…]` negates, as it does in bash.

    Under `fnmatch` this set reads as `{'^', 's', 'r', 'c'}`, which matches `src.py`'s
    first character and -- with the other two paths covered -- would skip the review
    entirely. Under bash's rules it matches nothing here, so the review happens.
    """
    repo, base, head = two_trees
    config = Config({"ignore_globs": ["[^src]*", "*.md"]})
    assert gitsnap.all_paths_ignored(str(repo), base, head, config) is False


def test_an_extended_glob_does_not_skip_the_review(git_repo: Path) -> None:
    """A path named `@(a|b)`, ignored by `@(a|b)`, is still reviewed.

    Bash reads the pattern as "a or b" and does not match the literal path, so a matcher
    that took it literally would skip a review bash would have run.
    """
    base = gitsnap.head_tree(str(git_repo))
    (git_repo / "@(a|b)").write_text("x\n")
    head = gitsnap.snapshot(str(git_repo)).tree

    assert gitsnap.all_paths_ignored(str(git_repo), base, head, Config({"ignore_globs": ["@(a|b)"]})) is False


def test_a_non_ascii_path_is_not_decided_by_a_character_class(git_repo: Path) -> None:
    """Class membership beyond ASCII is a locale table, so the review happens instead."""
    base = gitsnap.head_tree(str(git_repo))
    (git_repo / "é").write_text("x\n")
    head = gitsnap.snapshot(str(git_repo)).tree

    assert gitsnap.all_paths_ignored(str(git_repo), base, head, Config({"ignore_globs": ["[^[:digit:]]"]})) is False
    # An ordinary pattern still covers it: only classes and ranges are refused.
    assert gitsnap.all_paths_ignored(str(git_repo), base, head, Config({"ignore_globs": ["*"]})) is True


def test_an_escaped_metacharacter_is_literal(git_repo: Path) -> None:
    r"""`a\*b` matches the literal `a*b`, not `a<anything>b` -- so it skips nothing else."""
    base = gitsnap.head_tree(str(git_repo))
    (git_repo / "axb").write_text("x\n")
    head = gitsnap.snapshot(str(git_repo)).tree

    assert gitsnap.all_paths_ignored(str(git_repo), base, head, Config({"ignore_globs": [r"a\*b"]})) is False
    assert gitsnap.all_paths_ignored(str(git_repo), base, head, Config({"ignore_globs": ["a?b"]})) is True


def test_an_identical_pair_of_trees_is_not_treated_as_ignored(git_repo: Path) -> None:
    """No changed paths means "nothing to skip", not "everything was ignorable"."""
    tree = gitsnap.head_tree(str(git_repo))
    assert gitsnap.all_paths_ignored(str(git_repo), tree, tree, Config({"ignore_globs": ["*"]})) is False


# -- submodules ------------------------------------------------------------


def test_a_submodule_is_declared_as_not_diffed(git_repo: Path, tmp_path: Path) -> None:
    """The reviewer never sees inside a submodule, so the snapshot has to say so."""
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    git(inner, "config", "user.email", "selftest@example.invalid")
    git(inner, "config", "user.name", "ocrl selftest")
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
