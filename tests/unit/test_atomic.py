"""``write_atomic`` and ``write_private_atomic``: what each is, and is not, allowed to touch.

``write_private_atomic`` is exercised end to end by ``test_state.py`` already; what is pinned
here is narrower and specific to the two writers' contracts: ``write_atomic`` must never chmod
a directory and must never widen a file it replaces, and ``write_private_atomic`` must still
tighten everything under the root it is given.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ocrl.atomic import write_atomic, write_private_atomic


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
