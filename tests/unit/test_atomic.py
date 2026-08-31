"""``write_atomic`` and ``write_private_atomic``: what each is, and is not, allowed to touch.

``write_private_atomic`` is exercised end to end by ``test_state.py`` already; what is pinned
here is narrower and specific to the two writers' contracts: ``write_atomic`` must never chmod
a directory and must never widen a file it replaces, and ``write_private_atomic`` must still
tighten everything under the root it is given.
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
import stat
from pathlib import Path

import pytest

from arl.atomic import read_verified_file, verified_file, write_atomic, write_private_atomic


def test_write_atomic_leaves_the_directory_mode_untouched(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    os.chmod(directory, 0o755)
    before = stat.S_IMODE(directory.stat().st_mode)

    write_atomic(directory / "config.json", "{}\n")

    after = stat.S_IMODE(directory.stat().st_mode)
    assert before == 0o755
    assert after == before


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_write_atomic_preserves_an_existing_files_mode_across_a_replace(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"old": true}\n')
    os.chmod(path, mode)
    old_umask = os.umask(0o022)
    try:
        write_atomic(path, '{"new": true}\n')
    finally:
        os.umask(old_umask)

    assert path.read_text() == '{"new": true}\n'
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_write_atomic_uses_the_umask_default_for_a_genuinely_new_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    old_umask = os.umask(0o022)
    try:
        write_atomic(path, "{}\n")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(path.stat().st_mode) == 0o666 & ~0o022


def test_write_private_atomic_still_tightens_everything_under_its_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    path = root / "worktrees" / "x" / "state.json"

    write_private_atomic(path, "{}\n", root=root)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


# --------------------------------------------------------------------------
# verified_file: the read-side counterpart, and what it refuses
# --------------------------------------------------------------------------


def test_verified_file_accepts_a_plain_file_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    path = root / "context" / "002-prior-rounds.txt"
    write_private_atomic(path, "history\n", root=root)

    assert verified_file(path, root=root)


def test_verified_file_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    root = tmp_path / "state"
    (root / "context").mkdir(parents=True)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    planted = root / "context" / "002-prior-rounds.txt"
    planted.symlink_to(secret)

    assert planted.is_file(), "the naive check passes -- which is exactly why it is not the check"
    assert not verified_file(planted, root=root)


def test_verified_file_refuses_a_file_below_a_symlinked_directory(tmp_path: Path) -> None:
    """The plant a per-file ``is_symlink()`` check cannot see: every component *below* the link
    is an ordinary regular file, so only walking the directories catches it."""
    root = tmp_path / "state"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "range.txt").write_text("someone else's file")
    (root / "bundles").symlink_to(elsewhere, target_is_directory=True)

    leaf = root / "bundles" / "range.txt"
    assert leaf.is_file() and not leaf.is_symlink(), "both naive checks pass"
    assert not verified_file(leaf, root=root)


def test_verified_file_refuses_paths_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    assert not verified_file(outside, root=root)
    assert not verified_file(root / ".." / "outside.txt", root=root)


def test_verified_file_refuses_directories_and_missing_paths(tmp_path: Path) -> None:
    root = tmp_path / "state"
    (root / "bundles").mkdir(parents=True)

    assert not verified_file(root / "bundles", root=root)
    assert not verified_file(root / "bundles" / "nothing.txt", root=root)
    assert not verified_file(root, root=root), "the root itself names no file"


# --------------------------------------------------------------------------
# read_verified_file: the check and the read are one operation
# --------------------------------------------------------------------------


def test_read_verified_file_returns_the_bytes_of_a_plain_file(tmp_path: Path) -> None:
    content = "history\n"
    root = tmp_path / "state"
    path = root / "context" / "002-prior-rounds.txt"
    write_private_atomic(path, content, root=root)

    assert read_verified_file(path, root=root) == content.encode()


def test_read_verified_file_refuses_everything_verified_file_refuses(tmp_path: Path) -> None:
    """Same containment rules, so the two cannot drift into disagreeing about one path."""
    root = tmp_path / "state"
    (root / "context").mkdir(parents=True)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    (root / "context" / "leaf-link.txt").symlink_to(secret)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "range.txt").write_text("someone else's file")
    (root / "bundles").symlink_to(elsewhere, target_is_directory=True)

    for candidate in (
        root / "context" / "leaf-link.txt",
        root / "bundles" / "range.txt",
        root / "context",
        root / "context" / "missing.txt",
        tmp_path / "id_rsa",
        root / ".." / "id_rsa",
    ):
        assert read_verified_file(candidate, root=root) is None, candidate
        assert not verified_file(candidate, root=root), candidate


def test_read_verified_file_leaks_no_descriptors(tmp_path: Path) -> None:
    """Every refusal path closes what it opened. A hook process that leaked one per review
    would run out long before anyone noticed why."""
    root = tmp_path / "state"
    (root / "context").mkdir(parents=True)
    good = root / "context" / "a.txt"
    good.write_text("x")
    (root / "context" / "link.txt").symlink_to(tmp_path / "absent")

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(200):
        read_verified_file(good, root=root)
        read_verified_file(root / "context", root=root)
        read_verified_file(root / "context" / "link.txt", root=root)
        read_verified_file(root / "context" / "missing.txt", root=root)

    assert len(os.listdir("/proc/self/fd")) == before
