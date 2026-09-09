"""The cross-cutting hook helpers in ``arl.commands.hooks``: the end-state record."""

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

import subprocess
from pathlib import Path
from typing import Any

import pytest

from arl import state
from arl.commands import hooks

HEAD_ID = "a" * 40
TREE_ID = "b" * 40


def end_state_of(**fields: Any) -> hooks.EndState:
    """``hooks.end_state`` over a document carrying exactly ``fields``."""
    st = state.State("/wt", "sess")
    st.data = state.new_state_document()
    st.data.update(fields)
    return hooks.end_state(st)


def legacy_end_state() -> hooks.EndState:
    """A document written before the end-state record existed: the key is *absent*."""
    st = state.State("/wt", "sess")
    st.data = state.new_state_document()
    del st.data["ended_capture"]
    del st.data["ended_head"]
    del st.data["ended_tree"]
    del st.data["ended_at"]
    return hooks.end_state(st)


# -- reading a well-formed record ----------------------------------------------------


def test_a_recorded_capture_reads_back_whole() -> None:
    read = end_state_of(ended_capture="recorded", ended_head=HEAD_ID, ended_tree=TREE_ID, ended_at=1_757_000_000)
    assert read == hooks.EndState(capture="recorded", head=HEAD_ID, tree=TREE_ID, at=1_757_000_000, recorded=True, malformed=False)


@pytest.mark.parametrize("capture", ["unborn", "unreadable"])
def test_a_failed_capture_is_recorded_rather_than_absent(capture: str) -> None:
    """A capture that *failed* stays reportable instead of degrading into "nothing to see"."""
    read = end_state_of(ended_capture=capture, ended_at=1_757_000_000)
    assert read.recorded is True
    assert read.malformed is False
    assert read.capture == capture
    assert (read.head, read.tree) == ("", "")


# -- the legacy signal ---------------------------------------------------------------


def test_an_absent_capture_is_legacy_silence_not_corruption() -> None:
    read = legacy_end_state()
    assert read.recorded is False
    assert read.malformed is False
    assert read.capture == ""


def test_an_empty_capture_on_a_live_document_is_legacy_silence_too() -> None:
    """A freshly armed document carries ``ended_capture: ""``; nothing has ended there."""
    read = end_state_of()
    assert read.recorded is False
    assert read.malformed is False


# -- corruption ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "capture",
    [
        "disarmed",
        "RECORDED",
        0,
        False,
        [],  # unhashable: `x in frozenset` would raise before the membership test decided
        ["recorded"],
        {},
        {"recorded": True},
    ],
)
def test_a_capture_this_gate_never_writes_is_malformed(capture: object) -> None:
    """Corruption is *reported*, never raised.

    A ``TypeError`` out of the set membership would unwind through the hook's fail-closed
    guard, which reports a crash rather than the tampering this function exists to name --
    so the type is checked before the value is hashed.
    """
    read = end_state_of(ended_capture=capture, ended_at=1_757_000_000)
    assert read.malformed is True
    assert read.recorded is False


@pytest.mark.parametrize(
    "ended_at",
    [
        True,  # `get_int` would coerce this to 1 -- read the raw document, not the accessor
        0,
        -1,
        10**12,
        "1757000000",
        None,
        [1_757_000_000],
    ],
)
def test_an_ended_at_no_clock_could_have_written_is_malformed(ended_at: object) -> None:
    read = end_state_of(ended_capture="unreadable", ended_at=ended_at)
    assert read.malformed is True


@pytest.mark.parametrize(
    ("head", "tree"),
    [
        (HEAD_ID, ""),  # a *partial* record is malformed, not silence
        ("", TREE_ID),
        (HEAD_ID, "--output=/tmp/pwned"),
        ("--output=/tmp/pwned", TREE_ID),
        (HEAD_ID, "c" * 39),
        (HEAD_ID, "c" * 64),  # sha1 and sha256 cannot both have come from one repository
        (HEAD_ID, None),
    ],
)
def test_a_recorded_capture_needs_two_matching_object_ids(head: object, tree: object) -> None:
    read = end_state_of(ended_capture="recorded", ended_head=head, ended_tree=tree, ended_at=1_757_000_000)
    assert read.malformed is True
    assert read.recorded is False


def test_a_matched_pair_of_sha256_ids_is_accepted() -> None:
    """``looks_like_object_id`` accepts 64 hex, and a sha256 repository is a real one."""
    read = end_state_of(ended_capture="recorded", ended_head="a" * 64, ended_tree="b" * 64, ended_at=1_757_000_000)
    assert read.recorded is True
    assert read.malformed is False


@pytest.mark.parametrize("capture", ["unborn", "unreadable"])
def test_a_failed_capture_carrying_an_id_was_edited(capture: str) -> None:
    """``ended_evidence`` cannot emit these combinations, so finding one means tampering.

    Reading it as benign unborn evidence would let an editor silence a real report.
    """
    read = end_state_of(ended_capture=capture, ended_head=HEAD_ID, ended_tree=TREE_ID, ended_at=1_757_000_000)
    assert read.malformed is True


# -- capturing -----------------------------------------------------------------------


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    return work


def test_ended_evidence_records_head_and_its_own_tree(repo: Path) -> None:
    (repo / "f.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()

    evidence = hooks.ended_evidence(str(repo))

    assert evidence["ended_capture"] == "recorded"
    assert evidence["ended_head"] == head
    assert evidence["ended_tree"] == tree
    assert isinstance(evidence["ended_at"], int)
    assert evidence["ended_at"] > 0


def test_ended_evidence_on_an_empty_repository_records_unborn(repo: Path) -> None:
    evidence = hooks.ended_evidence(str(repo))
    assert evidence["ended_capture"] == "unborn"
    assert evidence["ended_head"] == ""
    assert evidence["ended_tree"] == ""


def test_ended_evidence_records_unreadable_rather_than_raising(repo: Path) -> None:
    """Nothing here may fail the transition: a raise would abandon a completion.

    Also the reason the record is *stronger* than the check it replaces -- breaking ``.git``
    after the stop can no longer suppress a report that is already on disk.
    """
    (repo / "f.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    (repo / ".git" / "HEAD").unlink()

    evidence = hooks.ended_evidence(str(repo))

    assert evidence["ended_capture"] == "unreadable"
    assert evidence["ended_head"] == ""
    assert evidence["ended_tree"] == ""


def test_a_captured_record_reads_back_through_end_state(repo: Path) -> None:
    """The writer's output and the reader's validation agree -- no round-trip surprise."""
    (repo / "f.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")

    read = end_state_of(**hooks.ended_evidence(str(repo)))

    assert read.recorded is True
    assert read.malformed is False
    assert read.capture == "recorded"
