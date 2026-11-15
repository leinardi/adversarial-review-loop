"""A root whose checkout left the branch its base was taken on: the reviewer and Claude are told.

The diff is between trees, so the review itself is unchanged; what changes is that a fix only the
old branch has, shown as removed, is explained instead of read as this phase's regression.
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

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from run_common import arl, git_at, run_env, ws

from arl import state

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any

APPROVE = "<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n"


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


def started(engine: Engine, launch: Path) -> None:
    assert engine(launch, "init", "--plan", str(launch / "plan.md"))["ok"]
    assert engine(launch, "set-phases", stdin=json.dumps(["phase 1", "phase 2"]))["ok"]


def range_text(launch: Path, ticket: dict[str, Any]) -> str:
    seq = int(ticket["review_id"].rsplit("-", 1)[1])
    return next((launch / ".arl" / "reviews").glob(f"*-{seq:03d}")).joinpath("bundle", "range.txt").read_text()


def record(engine: Engine, launch: Path, ticket: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    answer = tmp_path / f"answer-{ticket['review_id']}.md"
    answer.write_text(f"Read it.\n\n{APPROVE}")
    result: dict[str, Any] = engine(launch, "review", "record", ticket["review_id"], "--answer", str(answer))
    return result


def test_a_root_that_changed_branch_since_its_base_is_named_to_the_reviewer_and_claude(ws: Path, engine: Engine, tmp_path: Path) -> None:
    """**Fails on the old code**, which said nothing: the old branch's fixes read as removals."""
    api = ws / "api"
    git_at(api, "checkout", "-q", "-b", "fix/prereqs")
    (api / "src" / "main.py").write_text("print('fixed')\n")
    git_at(api, "commit", "-qam", "a fix only this branch has")
    started(engine, ws)
    run = state.RunState.load(state.run_paths(ws))
    assert run is not None
    assert {root["alias"]: root.get("approved_branch") for root in run.roots} == {"api": "fix/prereqs", "web": "main", "ws": ""}

    # The work goes on a new branch from main: the fix is not in it.
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    text = range_text(ws, ticket)
    assert "api: its base was taken on branch 'fix/prereqs', and it is now on branch 'feat/work'" in text
    assert "web:" not in text.partition("## Snapshot warnings")[2]

    outcome = record(engine, ws, ticket, tmp_path)
    assert "Note: api: its base was taken on branch 'fix/prereqs'" in outcome["message"]

    # Approved on the new branch: the next phase's base is on it, and nothing is said.
    (api / "src" / "new.py").write_text("print('newer')\n")
    second = engine(ws, "review", "prepare")
    assert second["ok"], second
    assert "its base was taken on branch" not in range_text(ws, second)


def test_a_detached_head_is_named_too(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    git_at(ws / "web", "checkout", "-q", "--detach")
    (ws / "web" / "src" / "main.py").write_text("print(2)\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "web: its base was taken on branch 'main', and it is now on a detached HEAD" in range_text(ws, ticket)


def test_a_run_stored_before_branches_were_recorded_says_nothing(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    run = state.RunState.load(state.run_paths(ws))
    assert run is not None
    with state.locked_state(run.paths) as locked:
        assert locked is not None
        for root in locked.roots:
            root.pop("approved_branch", None)
            root.pop("start_branch", None)
        locked.save()
    git_at(ws / "api", "checkout", "-q", "-b", "elsewhere")
    (ws / "api" / "src" / "main.py").write_text("print(2)\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "its base was taken on branch" not in range_text(ws, ticket)
