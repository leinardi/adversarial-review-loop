"""The run document: stages, approvals, tickets, and ``next_action``'s continue-or-yield answer."""

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
from collections.abc import Callable
from pathlib import Path

import pytest

from arl.errors import StateLoadError
from arl.state import RunError, RunState, locked_state, new_document, next_action, run_paths

TREES = {"ws": "a" * 40, "api": "b" * 40}


def make_run(tmp_path: Path, *, phases: int = 2, final_review: bool = False) -> RunState:
    run = RunState(run_paths(tmp_path), new_document(plan_source="plan.md", final_review=final_review))
    run.data["roots"] = [
        {"alias": alias, "path": str(tmp_path / alias), "kind": "dir", "baseline": "set", "start_tree": tree, "approved_tree": tree, "proposed": ""}
        for alias, tree in TREES.items()
    ]
    if phases:
        run.set_phases([f"phase {n}" for n in range(1, phases + 1)], replan=False)
    return run


def moved(n: int) -> dict[str, str]:
    return {alias: f"{n:x}".rjust(40, "c") for alias in TREES}


def assert_continues(run: RunState, *, stage: str, phase: int = 0, says: str = "") -> str:
    answer = next_action(run)
    assert answer["action"] == "continue", answer
    assert (answer["stage"], answer["phase"]) == (stage, phase)
    assert says in answer["prompt"]
    token: str = answer["token"]
    return token


def assert_yields(run: RunState, reason: str) -> None:
    answer = next_action(run)
    assert (answer["action"], answer["reason"]) == ("yield", reason), answer


# --------------------------------------------------------------------------
# Continuing
# --------------------------------------------------------------------------


def test_a_run_without_phases_is_prompted_to_set_them(tmp_path: Path) -> None:
    assert_continues(make_run(tmp_path, phases=0), stage="plan", says="set_phases")


def test_the_same_state_is_never_prompted_twice(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    first = assert_continues(run, stage="phase", phase=1, says="phase 1 of 2")
    assert_yields(run, "unchanged")
    assert_yields(run, "unchanged")

    run.record_changes_required()
    second = assert_continues(run, stage="phase", phase=1, says="asked for changes")
    assert second != first
    assert_yields(run, "unchanged")

    run.approve(moved(1), by="review")
    assert_continues(run, stage="phase", phase=2, says="Commit the approved phase")


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda run: run.pause(), "paused"),
        (lambda run: run.stop(), "stopped"),
        (lambda run: run.wait_for_user("stall", "five rounds"), "waiting"),
        (lambda run: run.wait_for_user("failures", "reviewer failed twice"), "waiting"),
        (lambda run: run.wait_for_user("oversized", "a 40 MiB file"), "waiting"),
        (lambda run: run.issue_ticket({"review_id": "r1"}), "review-pending"),
    ],
    ids=["paused", "stopped", "stalled", "failed", "oversized", "ticket-pending"],
)
def test_the_loop_yields_whenever_the_user_or_a_review_is_awaited(tmp_path: Path, setup: Callable[[RunState], None], reason: str) -> None:
    run = make_run(tmp_path)
    setup(run)
    assert_yields(run, reason)
    assert run.data["last_continuation"] == "", "a yield must not spend the token"


def test_an_unknown_baseline_yields_and_names_the_root(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.data["roots"][1]["baseline"] = "unknown"
    assert next_action(run) == {"action": "yield", "reason": "baseline-unknown", "roots": ["api"]}


def test_without_a_final_review_the_last_approval_completes_the_run(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.approve(moved(1), by="review")
    run.approve(moved(2), by="review")
    assert (run.status, run.stage()) == ("complete", "done")
    assert_yields(run, "complete")


def test_with_a_final_review_the_run_completes_only_on_the_final_approval(tmp_path: Path) -> None:
    run = make_run(tmp_path, final_review=True)
    run.approve(moved(1), by="review")
    assert_continues(run, stage="phase", phase=2)
    run.approve(moved(2), by="review")

    assert run.status == "active"
    assert_continues(run, stage="final", says="Commit the approved phase")
    assert_yields(run, "unchanged")

    run.record_changes_required()
    assert_continues(run, stage="final", says="Fix the cumulative findings")
    assert run.status == "active"

    run.approve(moved(3), by="review")
    assert (run.status, run.data["final_approved"]) == ("complete", True)
    assert_yields(run, "complete")


def test_finish_requires_the_final_review(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.approve(moved(1), by="review")
    run.request_final()
    run.approve(moved(2), by="review")
    assert run.stage() == "final"
    assert run.status == "active"


# --------------------------------------------------------------------------
# Approvals and tickets
# --------------------------------------------------------------------------


def test_an_approval_missing_a_root_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    before = json.dumps(run.data, sort_keys=True)
    with pytest.raises(RunError) as refused:
        run.approve({"ws": "d" * 40}, by="review")
    assert refused.value.code == "missing-tree"
    assert json.dumps(run.data, sort_keys=True) == before


def test_nothing_is_approved_before_there_are_phases(tmp_path: Path) -> None:
    with pytest.raises(RunError) as refused:
        make_run(tmp_path, phases=0).approve(moved(1), by="user")
    assert refused.value.code == "nothing-to-approve"


def test_an_approval_retires_the_ticket_and_clears_the_wait(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.record_changes_required()
    run.issue_ticket({"review_id": "r2"})
    run.wait_for_user("stall", "x")
    run.approve(moved(1), by="review")
    assert run.data["pending_ticket"] is None
    assert run.data["last_ticket"] == {"review_id": "r2", "status": "approved"}
    assert run.data["waiting"] is None
    assert run.data["rounds"] == 0
    assert run.data["approvals"][-1]["rounds"] == 1


def test_a_new_ticket_supersedes_the_pending_one(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.issue_ticket({"review_id": "r1"})
    run.issue_ticket({"review_id": "r2"})
    assert run.data["last_ticket"] == {"review_id": "r1", "status": "superseded"}
    assert run.data["pending_ticket"] == {"review_id": "r2"}


def test_stop_invalidates_the_pending_ticket(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    run.issue_ticket({"review_id": "r1"})
    run.stop()
    assert run.data["last_ticket"] == {"review_id": "r1", "status": "stopped"}


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_a_saved_run_loads_back_identically(tmp_path: Path) -> None:
    run = make_run(tmp_path)
    with locked_state(run.paths) as absent:
        assert absent is None
        run.save()
    loaded = RunState.load(run.paths)
    assert loaded is not None
    assert loaded.data == run.data


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "not json",
        '{"version": 3}',
        "MISSING:approvals",
        "WRONG:rounds",
        "STATUS:bogus",
        "PHASES:[1]",
        "ROOT:{}",
        'ROOT:{"alias": "a", "path": "/a", "kind": "svn", "baseline": "set", "start_tree": "t", "approved_tree": "t"}',
        'ROOT:{"alias": "a", "path": "/a", "kind": "git", "baseline": "garbage", "start_tree": "t", "approved_tree": "t"}',
        'ROOT:{"alias": "a", "path": "/a", "kind": "git", "baseline": "set", "start_tree": "t", "approved_tree": ""}',
        'ROOT:{"alias": "a", "path": "/a", "kind": "git", "baseline": "set", "start_tree": "t"}',
    ],
)
def test_a_malformed_document_is_refused_whole(tmp_path: Path, document: str) -> None:
    run = make_run(tmp_path)
    data = dict(run.data)
    kind, _, value = document.partition(":")
    if kind == "MISSING":
        del data[value]
        document = json.dumps(data)
    elif kind == "WRONG":
        data[value] = True  # a bool is not a count
        document = json.dumps(data)
    elif kind == "STATUS":
        document = json.dumps(data | {"status": value})
    elif kind == "PHASES":
        document = json.dumps(data | {"phases": json.loads(value)})
    elif kind == "ROOT":
        document = json.dumps(data | {"roots": [json.loads(value)]})
    run.paths.run_dir.mkdir()
    run.paths.state_file.write_text(document)
    with pytest.raises(StateLoadError):
        RunState.load(run.paths)


@pytest.mark.parametrize("decision", ["stall", "needs-human"])
@pytest.mark.parametrize("later", ["failures", "oversized"])
def test_a_wait_that_clears_itself_never_replaces_a_users_decision(tmp_path: Path, decision: str, later: str) -> None:
    run = make_run(tmp_path)
    run.wait_for_user(decision, "x")
    run.wait_for_user(later, "y")
    run.clear_wait(later)
    assert run.data["waiting"] == {"kind": decision, "detail": "x"}
    assert run.pending_decision() == {"kind": decision, "detail": "x"}
