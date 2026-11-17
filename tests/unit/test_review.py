"""The review pipeline: ``review prepare`` / ``record`` / ``run-external``, repair, stalls,
failure budgets, dispositions, the final review and the guards around them.

Every review here runs through the engine's own commands, in process, against real repositories
and a plain folder. The reviewer's answer is a file the test writes, exactly what the mod hands
``review record`` after its subagent ran; ``run-external`` drives ``ARL_REVIEWER_CMD`` instead.
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
import shutil
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from run_common import arl, make_repo, run_env, ws

from arl import config, contract, harness, runner, state
from arl import review as review_module
from arl.errors import StateLoadError

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any

APPROVE = ("VERDICT APPROVED",)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


@pytest.fixture
def answers(tmp_path: Path) -> Path:
    directory = tmp_path / "answers"
    directory.mkdir()
    return directory


def started(engine: Engine, launch: Path, phases: int = 3) -> None:
    assert engine(launch, "init", "--plan", str(launch / "plan.md"))["ok"]
    assert engine(launch, "set-phases", stdin=json.dumps([f"phase {n}" for n in range(1, phases + 1)]))["ok"]


def write_answer(directory: Path, *lines: str, prose: str = "Read the whole bundle.") -> Path:
    path = directory / f"answer-{len(list(directory.iterdir())):03d}.md"
    path.write_text(f"{prose}\n\n<<<ARL-FINDINGS>>>\n" + "".join(f"{line}\n" for line in lines) + "<<<ARL-END>>>\n")
    return path


def finding(file: str, severity: str = "high", detail: str = "broken") -> str:
    return f"FINDING severity={severity} actionable=yes file={file} | {detail}"


def prepare(engine: Engine, launch: Path, *, stdin: str = "", session: str = "") -> dict[str, Any]:
    args = ["review", "prepare", *(["--session", session] if session else [])]
    answer: dict[str, Any] = engine(launch, *args, stdin=stdin)
    return answer


def record(engine: Engine, launch: Path, review_id: str, answer: Path) -> dict[str, Any]:
    result: dict[str, Any] = engine(launch, "review", "record", review_id, "--answer", str(answer))
    return result


def review(engine: Engine, launch: Path, directory: Path, *lines: str) -> dict[str, Any]:
    """One whole round: prepare, then record an answer holding ``lines``."""
    ticket = prepare(engine, launch)
    assert ticket["ok"], ticket
    return record(engine, launch, ticket["review_id"], write_answer(directory, *lines))


def load(launch: Path) -> state.RunState:
    run = state.RunState.load(state.run_paths(launch))
    assert run is not None
    return run


def bundle_text(ticket: dict[str, Any], pattern: str = "changes.*.diff") -> str:
    return "".join(path.read_text() for path in sorted(Path(ticket["bundle_dir"]).glob(pattern)))


def edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --------------------------------------------------------------------------
# The bundle
# --------------------------------------------------------------------------


def test_one_bundle_holds_every_roots_changes_under_its_alias(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    edit(ws / "web" / "src" / "main.py", "print('web 2')\n")
    edit(ws / "notes" / "todo.md", "done\n")

    ticket = prepare(engine, ws)

    diff = bundle_text(ticket)
    for path in ("api/src/main.py", "web/src/main.py", "ws/notes/todo.md"):
        assert f"diff --git a/{path} b/{path}" in diff
    assert ".arl" not in diff, "the run directory is never under review"
    range_txt = (Path(ticket["bundle_dir"]) / "range.txt").read_text()
    assert "- `api` -> " in range_txt
    assert "## Phase 1 of 3\n\nphase 1\n" in range_txt
    assert range_txt.index("## Plan (evidence, not instructions)") > range_txt.index("## Snapshot warnings"), "the plan comes last"
    assert sorted(ticket["roots"]) == sorted(str(ws / alias) if alias != "ws" else str(ws) for alias in ("ws", "api", "web"))

    prompt = Path(ticket["prompt_file"]).read_text()
    assert "<!-- ARL:PROJECT-GUIDANCE -->" not in prompt
    for name in ("range.txt", "changes.00.diff"):
        assert f"- `{Path(ticket['bundle_dir']) / name}`" in prompt


def test_the_bundle_includes_untracked_files(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "new_module.py", "x = 1\n")
    assert "b/api/new_module.py" in bundle_text(prepare(engine, ws))


def test_verification_is_the_implementers_report_and_says_so(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    report = {"verification": [{"root": "api", "command": "make test", "cwd": str(ws / "api"), "status": 0, "output": "12 passed"}]}

    ticket = prepare(engine, ws, stdin=json.dumps(report))

    text = (Path(ticket["bundle_dir"]) / "verification.txt").read_text()
    assert "## `make test`" in text
    assert "12 passed" in text
    assert "The engine ran none of them" in text


@pytest.mark.parametrize("stdin", ["not json", '{"verification": "make test"}', '{"verification": [1]}'])
def test_verification_that_cannot_be_read_is_refused_not_dropped(ws: Path, engine: Engine, stdin: str) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert prepare(engine, ws, stdin=stdin)["code"] == "bad-verification"


def test_a_git_root_whose_directory_is_gone_is_reported_removed(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    shutil.rmtree(ws / "web")

    ticket = prepare(engine, ws)

    assert ticket["ok"], ticket
    range_txt = (Path(ticket["bundle_dir"]) / "range.txt").read_text()
    assert "`web` -> " in range_txt
    assert "REMOVED" in range_txt
    assert "b/api/src/main.py" in bundle_text(ticket)


# --------------------------------------------------------------------------
# Recording an answer
# --------------------------------------------------------------------------


def test_an_approval_moves_every_root_and_the_next_phase_diffs_from_there(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('phase 1')\n")
    outcome = review(engine, ws, answers, *APPROVE)
    assert (outcome["verdict"], outcome["run"]["phase"]) == ("APPROVED", 2)
    assert Path(outcome["report"]).is_file()

    edit(ws / "web" / "src" / "main.py", "print('phase 2')\n")
    diff = bundle_text(prepare(engine, ws))
    assert "web/src/main.py" in diff
    assert "api/src/main.py" not in diff, "phase 1's approved change is not shown again"


def test_the_engine_recomputes_the_verdict_and_blocks_a_self_approving_review(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT APPROVED")
    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert outcome["run"]["phase"] == 1
    assert outcome["run"]["rounds"] == 1


def test_a_finding_whose_path_names_no_root_goes_to_repair(ws: Path, engine: Engine, answers: Path) -> None:
    """``file=src/main.py`` could be either repository's file; it cannot be counted, retracted or
    stalled on, so it is a contract failure like any other unreadable block."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding("src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert outcome["status"] == "repair"


def test_a_citation_past_the_end_of_the_file_keeps_the_finding_and_adds_a_note(ws: Path, engine: Engine, answers: Path) -> None:
    """A diff line number cited as a source line: the finding still blocks, with no repair call."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding("api/src/main.py:812"), finding("api/src/main.py:1", detail="inside"), "VERDICT CHANGES_REQUIRED")
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "CHANGES_REQUIRED")
    assert load(ws).data["round_history"][-1]["blocking"] == 2
    assert "Review note: api/src/main.py: cited line 812 is past the end of the file (1 lines); locate it by the description" in outcome["message"]
    assert "cited line 1 " not in outcome["message"]
    assert "- engine note: api/src/main.py: cited line 812" in Path(outcome["report"]).read_text()


def test_a_citation_with_thousands_of_digits_is_still_recorded(ws: Path, engine: Engine, answers: Path) -> None:
    """The citation check is advisory: it must never keep the round from being recorded."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding(f"api/src/main.py:{'9' * 5000}"), "VERDICT CHANGES_REQUIRED")
    assert (outcome["ok"], outcome["status"], outcome["verdict"]) == (True, "recorded", "CHANGES_REQUIRED")
    assert load(ws).data["round_history"][-1]["blocking"] == 1
    assert "api/src/main.py: cited a line number is past the end of the file (1 lines)" in outcome["message"]


def test_a_citation_past_the_end_of_a_deferred_finding_is_noted_on_the_approval(ws: Path, engine: Engine, answers: Path) -> None:
    edit(ws / "api" / "notes.md", "a\nb\n")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    # Round 2: a medium finding in a file no round raised and this round did not change is deferred.
    outcome = review(
        engine, ws, answers, finding("api/notes.md:400", severity="medium", detail="late"), "RESOLVED id=r1.1 | fixed", "VERDICT APPROVED"
    )
    assert outcome["verdict"] == "APPROVED", outcome
    assert "Review note: api/notes.md: cited line 400 is past the end of the file (2 lines)" in outcome["message"]


def test_no_verdict_with_a_blocking_finding_is_recorded_as_changes_required(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding("api/src/main.py:1"))
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "CHANGES_REQUIRED")
    assert "Review note: the reviewer gave no VERDICT line; its 1 blocking finding(s) make this CHANGES_REQUIRED" in outcome["message"]
    assert load(ws).data["round_history"][-1]["verdict"] == "CHANGES_REQUIRED"


@pytest.mark.parametrize("lines", [(), (finding("api/src/main.py:1", severity="low"),)])
def test_no_verdict_with_nothing_blocking_goes_to_repair_never_approves(ws: Path, engine: Engine, answers: Path, lines: tuple[str, ...]) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, *lines)
    assert outcome["status"] == "repair"
    assert load(ws).data["round_history"] == []


def test_the_same_answer_cannot_be_recorded_twice(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    answer = write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert record(engine, ws, ticket["review_id"], answer)["ok"]

    again = record(engine, ws, ticket["review_id"], answer)
    assert again["code"] == "stale-review"
    assert load(ws).data["rounds"] == 1, "the second recording changed nothing"


@pytest.mark.parametrize("interruption", ["stop", "replan", "init-new"])
def test_an_answer_for_a_review_the_run_moved_past_is_rejected(ws: Path, engine: Engine, answers: Path, interruption: str) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    if interruption == "stop":
        assert engine(ws, "stop")["ok"]
    elif interruption == "replan":
        assert engine(ws, "set-phases", "--replan", stdin='["other phase"]')["ok"]
    else:
        assert engine(ws, "init", "--plan", str(ws / "plan.md"), "--new")["ok"]

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))

    assert outcome["code"] == "stale-review"
    run = load(ws)
    assert run.data["approvals"] == []
    assert run.data["round_history"] == []


def test_a_root_that_changed_while_the_review_ran_makes_the_answer_stale(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    edit(ws / "web" / "src" / "main.py", "print('written during the review')\n")

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))

    assert outcome["code"] == "stale-review"
    run = load(ws)
    assert run.data["approvals"] == []
    assert run.data["pending_ticket"] is None


def test_an_interrupted_review_is_abandoned_on_resume_and_prepared_again(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    lost = prepare(engine, ws, session="dead-session")

    resumed = engine(ws, "resume", "--session", "new-session")
    assert resumed["ok"]
    assert record(engine, ws, lost["review_id"], write_answer(answers, *APPROVE))["code"] == "stale-review"

    again = prepare(engine, ws, session="new-session")
    assert again["review_id"] != lost["review_id"]
    assert record(engine, ws, again["review_id"], write_answer(answers, *APPROVE))["verdict"] == "APPROVED"


def test_a_review_still_running_in_the_resuming_session_is_kept(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws, session="s1")
    assert engine(ws, "pause")["ok"]
    assert engine(ws, "resume", "--session", "s1")["ok"]
    assert record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))["verdict"] == "APPROVED"


def test_an_interruption_after_an_approval_resumes_on_the_next_phase(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert review(engine, ws, answers, *APPROVE)["verdict"] == "APPROVED"

    resumed = engine(ws, "resume", "--session", "later")
    assert (resumed["run"]["stage"], resumed["run"]["phase"]) == ("phase", 2)
    action = engine(ws, "next-action")
    assert action["action"] == "continue"
    assert "phase 2 of 3" in action["prompt"]

    edit(ws / "web" / "src" / "main.py", "print(3)\n")
    diff = bundle_text(prepare(engine, ws))
    assert "web/src/main.py" in diff
    assert "api/src/main.py" not in diff


# --------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------


def test_a_malformed_answer_gets_one_repair_and_its_findings_are_recorded(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    broken = answers / "broken.md"
    broken.write_text("The lookup in api/src/main.py:1 is wrong.\n\n<<<ARL-FINDINGS>>>\nFINDING severity=P1 location=a | x\n<<<ARL-END>>>\n")

    repair = record(engine, ws, ticket["review_id"], broken)
    assert repair["status"] == "repair"
    assert repair["review_id"] == ticket["review_id"]
    tail = (Path(repair["bundle_dir"]) / "repair.txt").read_text()
    assert "is wrong" in tail
    assert "repair.txt" in Path(repair["prompt_file"]).read_text()

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert load(ws).data["round_history"][-1]["repaired"] is True


def test_a_repair_that_approves_is_a_failure_never_an_approval(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["status"] == "repair"

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))

    assert outcome["verdict"] == "OP_FAILURE"
    run = load(ws)
    assert run.data["approvals"] == []
    assert run.data["failures"] == 1


def test_a_second_malformed_answer_is_a_failure_not_another_repair(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["status"] == "repair"
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["verdict"] == "OP_FAILURE"


# --------------------------------------------------------------------------
# Failure budgets, stalls, the oversized guard, ignore_globs
# --------------------------------------------------------------------------


def test_failures_past_the_budget_make_the_run_wait_for_the_user(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FAILURES", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    for attempt in (1, 2):
        ticket = prepare(engine, ws)
        outcome = engine(ws, "review", "record", ticket["review_id"], "--failed", "operational", "--error", f"crash {attempt}")
        assert outcome["verdict"] == "OP_FAILURE"
    run = load(ws)
    assert run.data["waiting"]["kind"] == "failures"
    assert "crash 2" in run.data["waiting"]["detail"]
    assert engine(ws, "next-action")["reason"] == "waiting"


def test_an_approval_resets_the_failure_count(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    engine(ws, "review", "record", ticket["review_id"], "--failed", "transient")
    assert load(ws).data["transient_failures"] == 1
    review(engine, ws, answers, *APPROVE)
    assert load(ws).data["transient_failures"] == 0


def test_two_roots_with_the_same_relative_path_never_share_a_stall_anchor(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``api/src/main.py`` and ``web/src/main.py`` are different files. Were findings anchored on
    the path inside the root, alternating between them would read as one finding persisting."""
    monkeypatch.setenv("ARL_STALL_ROUNDS", "2")
    started(engine, ws)
    for n, alias in enumerate(("api", "web", "api", "web")):
        edit(ws / alias / "src" / "main.py", f"print({n})\n")
        outcome = review(engine, ws, answers, finding(f"{alias}/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
        assert outcome["verdict"] == "CHANGES_REQUIRED"
        assert load(ws).data["waiting"] is None, f"round {n + 1} stalled on a finding of another root"


def test_the_same_finding_persisting_stalls_the_run(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STALL_ROUNDS", "2")
    started(engine, ws)
    for n in range(2):
        edit(ws / "api" / "src" / "main.py", f"print({n})\n")
        outcome = review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert "persisted for 2 rounds" in outcome["message"]
    assert load(ws).data["waiting"]["kind"] == "stall"
    assert engine(ws, "next-action")["reason"] == "waiting"


@pytest.mark.parametrize("where", ["api/big.bin", "notes/big.bin"], ids=["git-root", "folder-root"])
def test_a_file_past_max_file_bytes_makes_the_run_wait(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    monkeypatch.setenv("ARL_MAX_FILE_BYTES", "1000")
    started(engine, ws)
    edit(ws / where, "x" * 2000)

    refused = prepare(engine, ws)

    assert refused["code"] == "oversized"
    assert "big.bin (2000 bytes)" in refused["error"]
    run = load(ws)
    assert run.data["waiting"]["kind"] == "oversized"
    assert run.data["pending_ticket"] is None


def test_a_large_file_the_root_ignores_is_not_oversized(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FILE_BYTES", "1000")
    started(engine, ws)
    edit(ws / "api" / ".gitignore", "*.bin\n")
    edit(ws / "api" / "big.bin", "x" * 2000)
    assert prepare(engine, ws)["ok"]


def test_a_stage_whose_every_change_matches_ignore_globs_is_approved_unreviewed(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_IGNORE_GLOBS", "*.md")
    started(engine, ws)
    edit(ws / "notes" / "todo.md", "done\n")
    edit(ws / "api" / "README.md", "api docs\n")

    outcome = prepare(engine, ws)

    assert (outcome["status"], outcome["verdict"]) == ("skipped", "APPROVED")
    run = load(ws)
    assert run.data["approvals"][-1]["by"] == "ignored"
    assert run.data["round_history"] == []


def test_one_change_outside_ignore_globs_is_reviewed(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_IGNORE_GLOBS", "*.md")
    started(engine, ws)
    edit(ws / "notes" / "todo.md", "done\n")
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert "review_id" in prepare(engine, ws)


def test_an_unchanged_stage_is_reviewed_not_skipped(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_IGNORE_GLOBS", "*")
    started(engine, ws)
    assert "review_id" in prepare(engine, ws)


# --------------------------------------------------------------------------
# Late rounds and retractions
# --------------------------------------------------------------------------


def test_round_two_sees_round_ones_findings_and_what_changed_since(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1", detail="wrong value"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "web" / "src" / "main.py", "print('fix')\n")

    ticket = prepare(engine, ws)

    assert ticket["round"] == 2
    prior = (Path(ticket["bundle_dir"]) / "prior-rounds.txt").read_text()
    assert "file=api/src/main.py:1 | wrong value" in prior
    assert "web/src/main.py" in (Path(ticket["bundle_dir"]) / "incremental.diff").read_text()
    range_txt = (Path(ticket["bundle_dir"]) / "range.txt").read_text()
    assert "## Changed since round 1\n\nweb/src/main.py\n" in range_txt


def test_a_new_medium_outside_the_changed_paths_is_deferred_in_round_two(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print('fixed')\n")

    outcome = review(engine, ws, answers, finding("web/src/main.py:1", severity="medium"), "RESOLVED id=r1.1 | fixed", "VERDICT APPROVED")

    assert outcome["verdict"] == "APPROVED"
    assert "web/src/main.py:1" in outcome["message"], "a deferred finding is still shown"


def round_one(engine: Engine, ws: Path, answers: Path, *lines: str) -> None:
    """Round 1 of phase 1, asking for changes with ``lines``, then a fix to review in round 2."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    assert review(engine, ws, answers, *lines, "VERDICT CHANGES_REQUIRED")["verdict"] == "CHANGES_REQUIRED"
    edit(ws / "api" / "src" / "main.py", "print('fixed')\n")


def test_a_later_approval_that_forgets_an_earlier_blocking_finding_goes_to_repair(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1", detail="one"), finding("web/src/main.py:1", detail="two"))
    ticket = prepare(engine, ws)
    repair = record(engine, ws, ticket["review_id"], write_answer(answers, "RESOLVED id=r1.1 | prints fixed", *APPROVE))
    assert repair["status"] == "repair"
    reason = (Path(repair["bundle_dir"]) / "repair-reason.txt").read_text()
    assert "- r1.2\n" in reason
    assert "r1.1" not in reason
    prompt = Path(repair["prompt_file"]).read_text()
    assert "repair-reason.txt" in prompt
    assert "prior-rounds.txt" in prompt
    assert load(ws).data["approvals"] == []
    assert load(ws).data["pending_ticket"]["repair_unaccounted"] == ["r1.2"]


@pytest.mark.parametrize(
    "accounting",
    [
        "RESOLVED id=r1.1 | api/src/main.py:1 prints fixed now",
        "SUPERSEDES round=1 file=api/src/main.py:1 | round 1 misread it",
    ],
)
def test_each_way_of_accounting_lets_a_later_approval_through(ws: Path, engine: Engine, answers: Path, accounting: str) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    outcome = review(engine, ws, answers, accounting, *APPROVE)
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "APPROVED"), outcome
    assert load(ws).data["round_history"][-1]["resolved"] == ([accounting] if accounting.startswith("RESOLVED") else [])


def test_resolved_lines_are_stored_and_reported(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    line = "RESOLVED id=r1.1 | api/src/main.py:1 prints fixed now"
    outcome = review(engine, ws, answers, line, *APPROVE)
    assert load(ws).data["round_history"][-1]["resolved"] == [line]
    report = Path(outcome["report"]).read_text()
    assert f"(RESOLVED)\n\nThe reviewer's statement, with its evidence; recorded, never read for the verdict.\n\n```\n{line}\n```" in report


def test_a_supersedes_of_another_round_or_file_does_not_account(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    outcome = review(
        engine, ws, answers, "SUPERSEDES round=2 file=api/src/main.py:1 | wrong round", "SUPERSEDES round=1 file=web/src/main.py | other", *APPROVE
    )
    assert outcome["status"] == "repair"


def test_a_round_asking_for_changes_needs_no_accounting(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"), finding("web/src/main.py:1"))
    outcome = review(engine, ws, answers, finding("web/src/main.py:1"), "RESOLVED id=r1.1 | fixed", "VERDICT CHANGES_REQUIRED")
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "CHANGES_REQUIRED")
    assert load(ws).data["round_history"][-1]["resolved"] == ["RESOLVED id=r1.1 | fixed"]


def test_a_resolved_id_naming_no_earlier_finding_is_a_contract_failure(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    ticket = prepare(engine, ws)
    answer = write_answer(answers, finding("api/src/main.py:1"), "RESOLVED id=r1.9 | no such finding", "VERDICT CHANGES_REQUIRED")
    assert record(engine, ws, ticket["review_id"], answer)["status"] == "repair"
    outcome = record(
        engine, ws, ticket["review_id"], write_answer(answers, "RESOLVED id=r2.1 | not yet", finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    )
    assert outcome["verdict"] == "OP_FAILURE"
    assert "RESOLVED id=r2.1 names no finding of an earlier round" in Path(outcome["report"]).read_text()


def test_resolved_in_round_1_is_a_contract_violation(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    assert review(engine, ws, answers, "RESOLVED id=r1.1 | nothing to resolve", *APPROVE)["status"] == "repair"


def test_resolved_in_a_final_review_is_a_contract_violation(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_FINAL_REVIEW", "true")
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, *APPROVE)
    assert review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")["verdict"] == "CHANGES_REQUIRED"
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert review(engine, ws, answers, "RESOLVED id=r1.1 | fixed", *APPROVE)["status"] == "repair"


@pytest.mark.parametrize(
    "earlier",
    [
        "FINDING severity=low actionable=yes file=api/src/main.py:1 | below block_severity",
        "FINDING severity=critical actionable=no file=api/src/main.py:1 | nothing to change",
    ],
)
def test_an_earlier_finding_that_did_not_block_needs_no_accounting(ws: Path, engine: Engine, answers: Path, earlier: str) -> None:
    round_one(engine, ws, answers, earlier, finding("web/src/main.py:1"))
    outcome = review(engine, ws, answers, "RESOLVED id=r1.2 | fixed", *APPROVE)
    assert outcome["verdict"] == "APPROVED", outcome


def test_an_approval_with_only_a_nit_on_the_earlier_blockers_path_is_a_contract_failure(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    for nit in (
        "FINDING severity=low actionable=yes file=api/src/main.py:3 | only a nit",
        "FINDING severity=high actionable=no file=api/src/main.py:1 | x",
    ):
        ticket = prepare(engine, ws)
        assert record(engine, ws, ticket["review_id"], write_answer(answers, nit, *APPROVE))["status"] == "repair"


def three_rounds(engine: Engine, ws: Path, answers: Path, *third: str) -> dict[str, Any]:
    """Round 1 blocks on r1.1 (api) and r1.2 (web); round 2 asks for changes on api only, never naming web."""
    round_one(engine, ws, answers, finding("api/src/main.py:1", detail="A"), finding("web/src/main.py:1", detail="B"))
    assert review(engine, ws, answers, finding("api/src/main.py:1", detail="A still"), "VERDICT CHANGES_REQUIRED")["verdict"] == "CHANGES_REQUIRED"
    edit(ws / "api" / "src" / "main.py", "print('again')\n")
    return review(engine, ws, answers, *third)


def test_a_finding_no_later_round_named_still_blocks_a_third_round_approval(ws: Path, engine: Engine, answers: Path) -> None:
    pending = three_rounds(engine, ws, answers, "RESOLVED id=r2.1 | fixed", *APPROVE)
    assert pending["status"] == "repair"
    reason = (Path(pending["bundle_dir"]) / "repair-reason.txt").read_text()
    assert "- r1.1\n- r1.2\n" in reason
    assert "r2.1" not in reason


def test_a_third_round_that_resolves_the_forgotten_finding_approves(ws: Path, engine: Engine, answers: Path) -> None:
    lines = ("RESOLVED id=r1.1 | fixed", "RESOLVED id=r2.1 | fixed", "RESOLVED id=r1.2 | checked web/src/main.py", *APPROVE)
    outcome = three_rounds(engine, ws, answers, *lines)
    assert outcome["verdict"] == "APPROVED", outcome


def test_a_finding_raised_again_in_a_middle_round_needs_both_ids_closed(ws: Path, engine: Engine, answers: Path) -> None:
    """Round 2 raises r1.1's defect again as r2.1: an approval closes both, not one."""
    outcome = three_rounds(engine, ws, answers, "RESOLVED id=r1.1 | fixed", "RESOLVED id=r1.2 | checked", *APPROVE)
    assert outcome["status"] == "repair"
    reason = (Path(outcome["bundle_dir"]) / "repair-reason.txt").read_text()
    assert "- r2.1\n" in reason
    assert "- r1.1\n" not in reason


def test_a_middle_rounds_resolved_closes_a_finding_for_good(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("web/src/main.py:1"))
    assert (
        review(engine, ws, answers, finding("api/src/main.py:1"), "RESOLVED id=r1.1 | fixed", "VERDICT CHANGES_REQUIRED")["verdict"]
        == "CHANGES_REQUIRED"
    )
    edit(ws / "api" / "src" / "main.py", "print('again')\n")
    assert review(engine, ws, answers, "RESOLVED id=r2.1 | fixed", *APPROVE)["verdict"] == "APPROVED"


def forgotten(engine: Engine, ws: Path, answers: Path) -> dict[str, Any]:
    """Round 2 approves with r1.2 never mentioned; answers the repair to run."""
    round_one(engine, ws, answers, finding("api/src/main.py:1", detail="one"), finding("web/src/main.py:1", detail="two"))
    ticket = prepare(engine, ws)
    nit = "FINDING severity=low actionable=no file=api/src/main.py:2 | naming"
    repair = record(engine, ws, ticket["review_id"], write_answer(answers, nit, "RESOLVED id=r1.1 | prints fixed", *APPROVE))
    assert repair["status"] == "repair"
    return {"review_id": ticket["review_id"], "nit": nit}


def test_a_repair_that_accounts_for_every_id_approves(ws: Path, engine: Engine, answers: Path) -> None:
    pending = forgotten(engine, ws, answers)
    lines = (pending["nit"], "RESOLVED id=r1.1 | prints fixed", "RESOLVED id=r1.2 | web/src/main.py is unchanged and was checked", *APPROVE)
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, *lines))
    assert outcome["verdict"] == "APPROVED", outcome
    entry = load(ws).data["round_history"][-1]
    assert entry["repaired"] is True
    assert len(entry["resolved"]) == 2


def test_a_repair_that_reraises_the_forgotten_finding_asks_for_changes(ws: Path, engine: Engine, answers: Path) -> None:
    pending = forgotten(engine, ws, answers)
    lines = (pending["nit"], "RESOLVED id=r1.1 | prints fixed", finding("web/src/main.py:1", detail="two"), "VERDICT CHANGES_REQUIRED")
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, *lines))
    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert "web/src/main.py:1 | two" in outcome["message"]


def test_a_repair_that_still_leaves_an_id_unaccounted_for_is_a_failure(ws: Path, engine: Engine, answers: Path) -> None:
    pending = forgotten(engine, ws, answers)
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, pending["nit"], "RESOLVED id=r1.1 | prints fixed", *APPROVE))
    assert outcome["verdict"] == "OP_FAILURE"
    run = load(ws)
    assert run.data["approvals"] == []
    assert run.data["failures"] == 1
    assert "r1.2" in Path(outcome["report"]).read_text()


def test_a_repair_that_drops_a_finding_of_the_review_it_re_emits_is_a_failure(ws: Path, engine: Engine, answers: Path) -> None:
    pending = forgotten(engine, ws, answers)
    lines = ("RESOLVED id=r1.1 | prints fixed", "RESOLVED id=r1.2 | checked", *APPROVE)
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, *lines))
    assert outcome["verdict"] == "OP_FAILURE"
    assert load(ws).data["approvals"] == []


def withdrawn_round(engine: Engine, ws: Path, answers: Path) -> dict[str, Any]:
    """Round 2 approves having withdrawn r1.1 and resolved r1.3, with r1.2 still owed: answers the repair."""
    round_one(
        engine, ws, answers, finding("api/src/main.py:1", detail="one"), finding("web/src/main.py:1", detail="two"), finding("api/src/main.py:9")
    )
    ticket = prepare(engine, ws)
    lines = ("SUPERSEDES round=1 file=api/src/main.py:1 | misread", "RESOLVED id=r1.3 | gone", *APPROVE)
    repair = record(engine, ws, ticket["review_id"], write_answer(answers, *lines))
    assert repair["status"] == "repair"
    assert "- r1.2\n" in (Path(repair["bundle_dir"]) / "repair-reason.txt").read_text()
    return {"review_id": ticket["review_id"]}


@pytest.mark.parametrize(
    ("lines", "words"),
    [
        (
            (
                "SUPERSEDES round=1 file=api/src/main.py:1 | misread",
                "RESOLVED id=r1.3 | gone",
                "SUPERSEDES round=1 file=web/src/main.py:1 | invented",
            ),
            "added a SUPERSEDES line",
        ),
        # Each still closes every id, so only the dropped line can fail it.
        (("RESOLVED id=r1.1 | instead of withdrawing", "RESOLVED id=r1.3 | gone", "RESOLVED id=r1.2 | checked"), "dropped a line"),
        (
            ("SUPERSEDES round=1 file=api/src/main.py:1 | misread", "RESOLVED id=r1.3 | gone, reworded", "RESOLVED id=r1.2 | checked"),
            "dropped a line",
        ),
    ],
)
def test_an_accounting_repair_may_not_rewrite_the_block_it_re_emits(
    ws: Path, engine: Engine, answers: Path, lines: tuple[str, ...], words: str
) -> None:
    pending = withdrawn_round(engine, ws, answers)
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, *lines, *APPROVE))
    assert outcome["verdict"] == "OP_FAILURE"
    assert words in Path(outcome["report"]).read_text()
    assert load(ws).data["approvals"] == []


def test_an_accounting_repair_asking_for_changes_may_not_add_a_withdrawal(ws: Path, engine: Engine, answers: Path) -> None:
    pending = withdrawn_round(engine, ws, answers)
    lines = (
        "SUPERSEDES round=1 file=api/src/main.py:1 | misread",
        "SUPERSEDES round=1 file=api/src/main.py:9 | invented",
        finding("web/src/main.py:1", detail="two"),
        "VERDICT CHANGES_REQUIRED",
    )
    outcome = record(engine, ws, pending["review_id"], write_answer(answers, *lines))
    assert outcome["verdict"] == "OP_FAILURE"
    assert "added a SUPERSEDES line" in Path(outcome["report"]).read_text()


def test_an_accounting_repair_that_keeps_every_line_and_adds_resolved_approves(ws: Path, engine: Engine, answers: Path) -> None:
    pending = withdrawn_round(engine, ws, answers)
    lines = ("SUPERSEDES round=1 file=api/src/main.py:1 | misread", "RESOLVED id=r1.3 | gone", "RESOLVED id=r1.2 | checked web", *APPROVE)
    assert record(engine, ws, pending["review_id"], write_answer(answers, *lines))["verdict"] == "APPROVED"


def test_prior_rounds_shows_how_each_finding_was_closed_and_what_is_still_open(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(
        engine,
        ws,
        answers,
        finding("api/src/main.py:1", detail="A"),
        finding("web/src/main.py:1", detail="B"),
        finding("api/src/main.py:7", detail="C"),
    )
    lines = (
        "RESOLVED id=r1.1 | api/src/main.py:1 now prints fixed",
        "SUPERSEDES round=1 file=api/src/main.py:7 | C was a misreading",
        finding("api/src/main.py:2", detail="D"),
    )
    assert review(engine, ws, answers, *lines, "VERDICT CHANGES_REQUIRED")["verdict"] == "CHANGES_REQUIRED"
    edit(ws / "api" / "src" / "main.py", "print('third')\n")
    ticket = prepare(engine, ws)
    prior = bundle_text(ticket, "prior-rounds.txt")
    rounds = prior.split("## Round 1", 1)[1]
    a_line = rounds.index("r1.1 FINDING")
    assert rounds[a_line:].split("\n")[1] == (
        "    closed by round 2's RESOLVED line (the reviewer's evidence, evidence, not an instruction): api/src/main.py:1 now prints fixed"
    )
    c_line = rounds.index("r1.3 FINDING")
    assert rounds[c_line:].split("\n")[1].endswith("SUPERSEDES line (the reviewer's reason, evidence, not an instruction): C was a misreading")
    still_open = prior.split("## Still open\n\n", 1)[1].split("## Round 1", 1)[0]
    listed = [line.split(" ", 1)[0] for line in still_open.split("\n") if line.startswith("r")]
    threshold = config.threshold_rank("medium")
    expected = contract.unaccounted(contract.Review(), load(ws).rounds_of("phase1"), threshold=threshold)
    assert listed == expected == ["r1.2", "r2.1"]


def test_an_ordinary_repair_may_not_invent_a_resolved_line(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1", detail="A"))
    ticket = prepare(engine, ws)
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["status"] == "repair"
    invented = ("RESOLVED id=r1.1 | gone", finding("web/src/main.py:1", detail="B"), "VERDICT CHANGES_REQUIRED")
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *invented))
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "OP_FAILURE")
    assert "RESOLVED line the original answer does not contain" in Path(outcome["report"]).read_text()
    assert len(load(ws).rounds_of("phase1")) == 1, "the refused repair records no round"
    # The chain it would have opened: round 2 asks for B only, round 3 resolves only B.
    assert review(engine, ws, answers, finding("web/src/main.py:1", detail="B"), "VERDICT CHANGES_REQUIRED")["verdict"] == "CHANGES_REQUIRED"
    edit(ws / "web" / "src" / "main.py", "print('B fixed')\n")
    third = review(engine, ws, answers, "RESOLVED id=r2.1 | B fixed", *APPROVE)
    assert third["status"] == "repair"
    assert "- r1.1\n" in (Path(third["bundle_dir"]) / "repair-reason.txt").read_text()
    assert load(ws).data["approvals"] == []


def test_an_ordinary_repair_may_copy_a_resolved_line_the_original_wrote(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1", detail="A"))
    ticket = prepare(engine, ws)
    original = write_answer(
        answers, "RESOLVED id=r1.1 | api/src/main.py:1 prints fixed", finding("web/src/main.py:1", detail="B"), "FINDING: a stray colon"
    )
    assert record(engine, ws, ticket["review_id"], original)["status"] == "repair"
    copied = ("RESOLVED id=r1.1 | api/src/main.py:1 prints fixed", finding("web/src/main.py:1", detail="B"), "VERDICT CHANGES_REQUIRED")
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *copied))
    assert outcome["verdict"] == "CHANGES_REQUIRED", outcome
    entry = load(ws).data["round_history"][-1]
    assert (entry["repaired"], entry["resolved"]) == (True, ["RESOLVED id=r1.1 | api/src/main.py:1 prints fixed"])


def test_a_repair_of_an_unreadable_answer_still_never_approves_with_resolved(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    ticket = prepare(engine, ws)
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["status"] == "repair"
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, "RESOLVED id=r1.1 | fixed", *APPROVE))
    assert outcome["verdict"] == "OP_FAILURE"
    assert load(ws).data["approvals"] == []


def test_a_round_recorded_before_resolved_existed_still_loads_and_renders(ws: Path, engine: Engine, answers: Path) -> None:
    round_one(engine, ws, answers, finding("api/src/main.py:1"))
    paths = state.run_paths(ws)
    document = json.loads(paths.state_file.read_text())
    del document["round_history"][0]["resolved"]
    paths.state_file.write_text(json.dumps(document))
    ticket = prepare(engine, ws)
    assert ticket["ok"], ticket
    assert "r1.1 FINDING" in bundle_text(ticket, "prior-rounds.txt")
    assert engine(ws, "stats")["ok"]
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "RESOLVED id=r1.1 | fixed", *APPROVE))["verdict"] == "APPROVED"


# --------------------------------------------------------------------------
# The final review
# --------------------------------------------------------------------------


def test_the_final_review_shows_every_phase_including_a_root_added_mid_run(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARL_FINAL_REVIEW", "true")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('phase 1')\n")
    assert review(engine, ws, answers, *APPROVE)["verdict"] == "APPROVED"

    elsewhere = ws.parent / "lib"
    assert engine(ws, "root-add", str(elsewhere))["ok"]
    edit(elsewhere / "lib.py", "def helper(): ...\n")
    assert review(engine, ws, answers, *APPROVE)["verdict"] == "APPROVED"

    edit(ws / "web" / "src" / "main.py", "print('phase 3')\n")
    assert review(engine, ws, answers, *APPROVE)["verdict"] == "APPROVED"
    assert load(ws).stage() == "final"

    ticket = prepare(engine, ws)
    assert ticket["label"] == "final"
    diff = bundle_text(ticket)
    for path in ("api/src/main.py", "lib/lib.py", "web/src/main.py"):
        assert f"b/{path}" in diff, f"{path} is missing from the cumulative diff"
    assert "Final integration review" in Path(ticket["prompt_file"]).read_text()

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))
    assert outcome["run"]["status"] == "complete"


def test_the_final_review_names_a_phase_no_review_approved(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws, phases=2)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert engine(ws, "accept")["ok"]
    assert engine(ws, "finish")["ok"]
    edit(ws / "web" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, *APPROVE)

    ticket = prepare(engine, ws)

    range_txt = (Path(ticket["bundle_dir"]) / "range.txt").read_text()
    assert "## Approved without an approving review\n\n- phase 1: accepted by the user after 1 round(s)" in range_txt


def test_a_final_review_may_not_retract(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_FINAL_REVIEW", "true")
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, *APPROVE)
    outcome = review(engine, ws, answers, "SUPERSEDES round=1 file=api/src/main.py:1 | no", *APPROVE)
    assert outcome["status"] == "repair"


# --------------------------------------------------------------------------
# The external reviewer
# --------------------------------------------------------------------------


def scripted_reviewer(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "reviewer.sh"
    script.write_text(f"#!/usr/bin/env bash\n{body}\n")
    script.chmod(0o755)
    return script


def test_run_external_runs_the_reviewer_on_the_bundle_and_records_it(
    ws: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = scripted_reviewer(
        tmp_path,
        'test -f "$1/range.txt" || exit 7\n'
        "printf 'Fine.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=low actionable=no file=api/src/main.py | nit\\nVERDICT APPROVED\\n<<<ARL-END>>>\\n'",
    )
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(script))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    outcome = engine(ws, "review", "run-external", ticket["review_id"])

    assert outcome["verdict"] == "APPROVED"
    assert "nit" in Path(outcome["report"]).read_text()


def test_run_external_repairs_once_then_records(ws: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = scripted_reviewer(
        tmp_path,
        'case "$2" in\n'
        "  *repair-prompt.md) printf '<<<ARL-FINDINGS>>>\\nFINDING severity=high actionable=yes file=api/src/main.py:1 | bad\\n"
        "VERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n' ;;\n"
        "  *) printf 'It is bad.\\n' ;;\n"
        "esac",
    )
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(script))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    outcome = engine(ws, "review", "run-external", ticket["review_id"])

    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert "re-emitted by a repair call" in Path(outcome["report"]).read_text()


@pytest.mark.parametrize("case", [("sleep 30", "transient"), ("exit 3", "operational")], ids=["timeout", "nonzero"])
def test_run_external_reports_a_failed_reviewer_as_a_failure(
    ws: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: tuple[str, str]
) -> None:
    body, kind = case
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(scripted_reviewer(tmp_path, body)))
    monkeypatch.setenv("ARL_TIMEOUT_SEC", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    outcome = engine(ws, "review", "run-external", ticket["review_id"])

    assert outcome["verdict"] == "OP_FAILURE"
    run = load(ws)
    assert run.data["transient_failures" if kind == "transient" else "failures"] == 1
    assert run.data["approvals"] == []


def test_a_failed_external_reviewer_keeps_its_output_in_the_report(ws: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Fails on a report whose raw output is empty for a failed run.** Measured on OpenCode
    2.2.7: every review exited 1 printing its usage text, and the reports said only "exited with
    status 1". What it printed is stored, and an approval inside it is still not read."""
    script = scripted_reviewer(
        tmp_path,
        "printf 'ERRORS\\n  Unrecognized flag: --pure\\n<<<ARL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<ARL-END>>>\\n'\nexit 1",
    )
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(script))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    outcome = engine(ws, "review", "run-external", ticket["review_id"])

    assert outcome["verdict"] == "OP_FAILURE"
    assert "Unrecognized flag: --pure" in Path(outcome["report"]).read_text()
    assert load(ws).data["approvals"] == []


def test_run_external_is_refused_for_the_subagent_harness(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    assert engine(ws, "review", "run-external", ticket["review_id"])["code"] == "not-external"


# --------------------------------------------------------------------------
# Project guidance
# --------------------------------------------------------------------------


def test_a_roots_review_skill_is_spliced_into_the_prompt_and_disclosed(run_env: Path, engine: Engine) -> None:
    top = run_env / "proj"
    make_repo(top, files={"app.py": "x = 1\n", ".agents/skills/adversarial-review/SKILL.md": "# Review\n\nCheck the locking.\n"})
    (top / "plan.md").write_text("# Plan\n")
    started(engine, top, phases=1)
    edit(top / "app.py", "x = 2\n")

    ticket = prepare(engine, top)

    prompt = Path(ticket["prompt_file"]).read_text()
    assert "Check the locking." in prompt
    assert prompt.index("Check the locking.") < prompt.index("## Output contract")
    assert "### The `adversarial-review` review skill of root `proj`" in prompt
    range_txt = (Path(ticket["bundle_dir"]) / "range.txt").read_text()
    assert "review skill `adversarial-review` of root proj" in range_txt


def test_a_resolved_oversized_wait_is_cleared_by_the_next_prepare(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FILE_BYTES", "1000")
    started(engine, ws)
    edit(ws / "api" / "big.bin", "x" * 2000)
    assert prepare(engine, ws)["code"] == "oversized"
    (ws / "api" / "big.bin").unlink()
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")

    assert load(ws).data["waiting"] is None
    assert engine(ws, "next-action")["action"] == "continue"


def test_a_review_that_answers_clears_the_failure_wait(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FAILURES", "0")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    engine(ws, "review", "record", ticket["review_id"], "--failed", "operational")
    assert load(ws).data["waiting"]["kind"] == "failures"

    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")

    assert load(ws).data["waiting"] is None


def test_no_review_is_prepared_while_a_stall_waits_for_the_user(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An approving review would end the stall: only the user does."""
    monkeypatch.setenv("ARL_STALL_ROUNDS", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert load(ws).data["waiting"]["kind"] == "stall"

    assert prepare(engine, ws)["code"] == "waiting"
    assert load(ws).data["waiting"]["kind"] == "stall"

    assert engine(ws, "resume")["ok"]
    assert "review_id" in prepare(engine, ws)


def test_only_the_roots_this_stage_changed_contribute_guidance(run_env: Path, engine: Engine) -> None:
    top = run_env / "proj"
    top.mkdir()
    for alias in ("api", "web"):
        make_repo(top / alias, files={"app.py": "x = 1\n", ".agents/skills/adversarial-review/SKILL.md": f"{alias} rules\n"})
    (top / "plan.md").write_text("# Plan\n")
    started(engine, top, phases=1)
    edit(top / "api" / "app.py", "x = 2\n")

    prompt = Path(prepare(engine, top)["prompt_file"]).read_text()

    assert "api rules" in prompt
    assert "web rules" not in prompt, "an unchanged root's skill is not this review's business"


def test_accept_cannot_get_past_an_oversized_file_and_says_what_will(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FILE_BYTES", "1000")
    started(engine, ws)
    edit(ws / "api" / "big.bin", "x" * 2000)
    refused = engine(ws, "accept")
    assert refused["code"] == "oversized"
    assert "remove the file(s), or ignore them" in refused["error"]
    assert load(ws).data["approvals"] == []


def test_an_engine_failure_while_recording_retires_the_ticket_and_counts(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    # A root that is no longer the top of its repository cannot be snapshotted.
    shutil.rmtree(ws / "api" / ".git")

    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))

    assert outcome["code"] == "record-failed"
    run = load(ws)
    assert run.data["pending_ticket"] is None
    assert run.data["failures"] == 1
    assert run.data["approvals"] == []


def test_a_usage_error_does_not_retire_the_ticket(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    with pytest.raises(state.RunError):
        review_module.record(state.run_paths(ws), {"review_id": ticket["review_id"]})
    assert load(ws).data["pending_ticket"]["review_id"] == ticket["review_id"]


def test_an_empty_answer_is_a_counted_failure_not_a_repair(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_FAILURES", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    for attempt in (1, 2):
        ticket = prepare(engine, ws)
        empty = answers / f"empty-{attempt}.md"
        empty.write_text("  \n")
        outcome = record(engine, ws, ticket["review_id"], empty)
        assert outcome["verdict"] == "OP_FAILURE", outcome
    run = load(ws)
    assert run.data["round_history"] == []
    assert run.data["waiting"]["kind"] == "failures"


def test_a_repair_that_recovers_only_the_incomplete_sentinel_is_the_original_failure(ws: Path, engine: Engine, answers: Path) -> None:
    """The repair prompt's own fallback block is not a review: recording it as a round would
    send Claude to fix a finding nobody made and spend no failure."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    prose = answers / "prose.md"
    prose.write_text("I looked at it.\n")
    assert record(engine, ws, ticket["review_id"], prose)["status"] == "repair"

    sentinel = write_answer(answers, "FINDING severity=high actionable=yes file=- | review transcript incomplete", "VERDICT CHANGES_REQUIRED")
    outcome = record(engine, ws, ticket["review_id"], sentinel)

    assert outcome["verdict"] == "OP_FAILURE"
    run = load(ws)
    assert run.data["round_history"] == []
    assert (run.data["rounds"], run.data["failures"]) == (0, 1)


def test_a_repair_with_no_blocking_finding_is_the_original_failure(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT MAYBE"))["status"] == "repair"
    assert record(engine, ws, ticket["review_id"], write_answer(answers, "VERDICT CHANGES_REQUIRED"))["verdict"] == "OP_FAILURE"


def test_a_replan_starts_the_final_reviews_history_over(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_FINAL_REVIEW", "true")
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, *APPROVE)
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert engine(ws, "set-phases", "--replan", stdin='["phase 2"]')["ok"]
    assert load(ws).stage() == "phase", "the replan reopens work the final review judged"
    edit(ws / "web" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, *APPROVE)

    final = prepare(engine, ws)

    assert (final["label"], final["round"]) == ("final", 1)
    assert not (Path(final["bundle_dir"]) / "prior-rounds.txt").exists()


def test_review_directories_are_named_for_their_run(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    first = prepare(engine, ws)
    assert engine(ws, "init", "--plan", str(ws / "plan.md"), "--new")["ok"]
    engine(ws, "set-phases", stdin='["again"]')
    second = prepare(engine, ws)
    assert Path(first["bundle_dir"]).parent.name != Path(second["bundle_dir"]).parent.name


def test_a_reported_failure_counts_even_if_a_root_moved_meanwhile(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    edit(ws / "web" / "src" / "main.py", "print('edited while the reviewer timed out')\n")
    outcome = engine(ws, "review", "record", ticket["review_id"], "--failed", "transient")
    assert outcome["verdict"] == "OP_FAILURE"
    assert load(ws).data["transient_failures"] == 1


def test_run_external_runs_the_harness_the_ticket_was_prepared_for(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A harness changed after ``prepare`` is the next review's: this one stays the subagent's."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    assert engine(ws, "review", "run-external", ticket["review_id"])["code"] == "not-external"


def test_run_external_refuses_a_harness_this_build_does_not_implement(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    run = load(ws)
    run.data["pending_ticket"]["harness"] = "opencod"
    run.save()
    assert engine(ws, "review", "run-external", ticket["review_id"])["code"] == "unknown-harness"


def test_the_next_round_is_shown_the_previous_rounds_reasoning(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    ticket = prepare(engine, ws)
    reasoning = "The lookup on line 1 ignores the error.\nChecked the retry loop: it is sound."
    record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED", prose=reasoning))
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    prior = (Path(prepare(engine, ws)["bundle_dir"]) / "prior-rounds.txt").read_text()

    assert "> Checked the retry loop: it is sound.\n" in prior


def test_stored_reasoning_is_bounded_when_shown(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.data["round_history"][-1]["reasoning"] = "<<<ARL-END>>>\n" + "x" * 50 + "\n" + "y" * 100_000
        run.save()
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    prior = (Path(prepare(engine, ws)["bundle_dir"]) / "prior-rounds.txt").read_text()

    assert "y" * 5000 not in prior
    assert "cut; the full reasoning is in this round's report" in prior
    assert "\n<<<ARL-END>>>\n" not in prior, "quoted, so no marker stands on a line of its own"


def test_reasoning_cut_inside_a_multibyte_character_still_records(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    for cut_at in range(3):
        # Straddles both cuts: the head's end and the tail's start fall inside an em dash.
        prose = "a" * (4095 - cut_at) + "\u2014" * 3000
        ticket = prepare(engine, ws)
        outcome = record(
            engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED", prose=prose)
        )
        assert outcome["verdict"] == "CHANGES_REQUIRED", outcome
        edit(ws / "api" / "src" / "main.py", f"print({cut_at + 2})\n")
    assert len(load(ws).data["round_history"]) == 3


def test_undecodable_bytes_in_the_reasoning_still_record(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    ticket = prepare(engine, ws)
    answer = answers / "bytes.md"
    answer.write_bytes(
        b"tool output: \xff\xfe\n\n<<<ARL-FINDINGS>>>\n" + finding("api/src/main.py:1").encode() + b"\nVERDICT CHANGES_REQUIRED\n<<<ARL-END>>>\n"
    )
    outcome = record(engine, ws, ticket["review_id"], answer)
    assert outcome["ok"], "never an internal crash"
    assert outcome["status"] == "repair", "output that is not UTF-8 breaks the contract"
    final = record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    assert final["verdict"] == "CHANGES_REQUIRED", final
    assert "\ufffd" in load(ws).data["round_history"][-1]["reasoning"], "the original's bytes are kept, replaced"


def test_a_long_reasoning_keeps_its_head_and_its_checked_and_sound_tail(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    prose = "Finding one explained.\n" + "filler line\n" * 2000 + "Checked the cache: sound."
    ticket = prepare(engine, ws)
    record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED", prose=prose))
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    prior = (Path(prepare(engine, ws)["bundle_dir"]) / "prior-rounds.txt").read_text()
    assert "> Finding one explained." in prior
    assert "> Checked the cache: sound." in prior
    assert "cut; the full reasoning is in this round's report" in prior


def test_the_round_being_fixed_keeps_its_findings_and_reasoning_after_many_rounds(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    for n in range(13):
        edit(ws / "api" / "src" / "main.py", f"print({n})\n")
        ticket = prepare(engine, ws)
        prose = f"Round {n} reasoning.\n" + "detail line\n" * 600
        record(engine, ws, ticket["review_id"], write_answer(answers, finding(f"api/src/f{n}.py:1"), "VERDICT CHANGES_REQUIRED", prose=prose))
    edit(ws / "api" / "src" / "main.py", "print('last')\n")
    prior = (Path(prepare(engine, ws)["bundle_dir"]) / "prior-rounds.txt").read_text()
    assert len(prior.encode()) <= 65536
    assert "file=api/src/f12.py:1" in prior
    assert "> Round 12 reasoning." in prior
    assert "file=api/src/f0.py:1" in prior, "every round's findings fit once older reasoning is left out"
    assert "> Round 0 reasoning." not in prior, "the oldest rounds give up their reasoning first"


def test_a_repaired_round_keeps_the_original_answers_reasoning(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    ticket = prepare(engine, ws)
    broken = answers / "broken.md"
    broken.write_text(
        "The lookup ignores errors. Checked the parser: sound.\n\n<<<ARL-FINDINGS>>>\nFINDING severity=P1 location=a | x\n<<<ARL-END>>>\n"
    )
    record(engine, ws, ticket["review_id"], broken)
    record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED", prose=""))
    assert load(ws).data["round_history"][-1]["reasoning"] == "The lookup ignores errors. Checked the parser: sound."


def test_keep_going_after_a_stall_gives_the_phase_a_fresh_window(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STALL_ROUNDS", "2")
    started(engine, ws)
    for n in range(2):
        edit(ws / "api" / "src" / "main.py", f"print({n})\n")
        review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert load(ws).data["waiting"]["kind"] == "stall"
    assert engine(ws, "resume")["ok"]

    edit(ws / "api" / "src" / "main.py", "print('a')\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert load(ws).data["waiting"] is None, "one more round is not a new stall"
    edit(ws / "api" / "src" / "main.py", "print('b')\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert load(ws).data["waiting"]["kind"] == "stall"


def test_a_root_registered_while_the_review_ran_makes_the_answer_stale(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    assert engine(ws, "root-add", str(ws.parent / "extra"))["ok"]

    assert record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))["code"] == "stale-review"
    assert load(ws).data["pending_ticket"] is None
    assert "review_id" in prepare(engine, ws)


# --------------------------------------------------------------------------
# What the mod needs to run the subagent, and what it reports back
# --------------------------------------------------------------------------


def test_prepare_tells_the_mod_how_to_run_the_subagent(ws: Path, engine: Engine) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "haiku", "timeout_sec": 120}))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    assert (ticket["reviewer_model"], ticket["timeout_sec"]) == ("haiku", 120)


def test_a_finding_kept_with_a_better_condition_still_blocks(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    first = finding("api/src/main.py:1", detail="prints the wrong value || done when: main prints 3")
    outcome = review(engine, ws, answers, first, "VERDICT CHANGES_REQUIRED")
    assert "  suggested check: main prints 3\n" in outcome["message"]
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    ticket = prepare(engine, ws)
    # The stored line, condition included, is what the next round is shown.
    assert first in bundle_text(ticket, "prior-rounds.txt")
    kept = finding("api/src/main.py:1", detail="prints 3 but still to stdout; the condition was insufficient || done when: main logs 3")
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, kept, "VERDICT CHANGES_REQUIRED"))
    assert (outcome["status"], outcome["verdict"]) == ("recorded", "CHANGES_REQUIRED")
    assert "  suggested check: main logs 3\n" in outcome["message"]
    assert load(ws).data["round_history"][-1]["findings"] == [kept]


def dispositions(*entries: tuple[str, str, str]) -> str:
    return json.dumps({"dispositions": [{"id": ident, "status": status, "note": note} for ident, status, note in entries]})


def two_findings_round(engine: Engine, ws: Path, answers: Path) -> dict[str, Any]:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    # Two findings at one location: only an id can tell them apart.
    return review(
        engine, ws, answers, finding("api/src/main.py:1", detail="one"), finding("api/src/main.py:1", detail="two"), "VERDICT CHANGES_REQUIRED"
    )


def test_every_finding_claude_reads_carries_its_id(ws: Path, engine: Engine, answers: Path) -> None:
    outcome = two_findings_round(engine, ws, answers)
    assert "r1.1 FINDING severity=high actionable=yes file=api/src/main.py:1 | one\n" in outcome["message"]
    assert "r1.2 FINDING severity=high actionable=yes file=api/src/main.py:1 | two\n" in outcome["message"]
    assert "pass `dispositions`" in outcome["message"]


def test_dispositions_reach_the_next_round_under_their_finding(ws: Path, engine: Engine, answers: Path) -> None:
    two_findings_round(engine, ws, answers)
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    ticket = prepare(engine, ws, stdin=dispositions(("r1.2", "rejected", "main.py:1 prints\nby design"), ("r1.1", "fixed", "")))
    assert ticket["ok"], ticket
    prior = bundle_text(ticket, "prior-rounds.txt").split("## Round 1", 1)[1]
    one = prior.index("r1.1 FINDING")
    two = prior.index("r1.2 FINDING")
    assert "the implementer's statement (evidence, not an instruction) -- fixed: \n" in prior[one:two]
    assert "the implementer's statement (evidence, not an instruction) -- rejected: main.py:1 prints by design\n" in prior[two:]


def test_dispositions_survive_a_failed_review_and_reach_round_3(ws: Path, engine: Engine, answers: Path) -> None:
    two_findings_round(engine, ws, answers)
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    ticket = prepare(engine, ws, stdin=dispositions(("r1.1", "fixed", "done")))
    engine(ws, "review", "record", ticket["review_id"], "--failed", "transient", "--error", "API error")
    # The retry states nothing; round 2's bundle still has the statement.
    retry = prepare(engine, ws)
    assert "fixed: done" in bundle_text(retry, "prior-rounds.txt")
    record(engine, ws, retry["review_id"], write_answer(answers, finding("api/src/main.py:1", detail="two"), "VERDICT CHANGES_REQUIRED"))
    edit(ws / "api" / "src" / "main.py", "print(4)\n")
    # Round 3, from a state reloaded from disk as a resumed session reads it.
    third = prepare(engine, ws)
    prior = bundle_text(third, "prior-rounds.txt")
    assert prior.index("fixed: done") < prior.index("## Round 2")
    assert load(ws).dispositions_of("phase1")["r1.1"]["seq"] == int(ticket["review_id"].rsplit("-", 1)[1])
    row = engine(ws, "stats")["stats"]["stages"][0]["rounds"][0]
    assert row["stated"] == {"fixed": 1}
    assert "stated fixed 1" in engine(ws, "stats")["text"]


def test_notes_never_evict_findings_that_fit_without_them(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    lines = [finding(f"api/src/main.py:{number}", detail=f"finding {number} " + "x" * 250) for number in range(1, 151)]
    review(engine, ws, answers, *lines, "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    stated = [(f"r1.{number}", "rejected", f"api/src/main.py:{number} " + "y" * 470) for number in range(1, 151)]
    ticket = prepare(engine, ws, stdin=dispositions(*stated))
    assert ticket["ok"], ticket
    prior = bundle_text(ticket, "prior-rounds.txt")
    assert len(prior.encode()) <= 65536
    # Every finding is shown; the notes take only the room the findings leave.
    assert all(f"r1.{number} {line}\n" in prior for number, line in enumerate(lines, start=1))
    shown = prior.count("the implementer's statement")
    assert 0 < shown < 150


def test_a_finding_holding_a_unicode_line_separator_is_recorded_with_its_id(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    line = finding("api/src/main.py:1", detail="splits\u2028here || done when: it does not")
    outcome = review(engine, ws, answers, line, finding("api/src/main.py:1", severity="low", detail="nit"), "VERDICT CHANGES_REQUIRED")
    assert (outcome["ok"], outcome["status"], outcome["verdict"]) == (True, "recorded", "CHANGES_REQUIRED")
    assert f"r1.1 {line}\n  suggested check: it does not\n" in outcome["message"]
    assert load(ws).data["round_history"][-1]["findings"][0] == line


@pytest.mark.parametrize(
    ("stdin", "words"),
    [
        (dispositions(("r1.9", "fixed", "")), "names no finding of round 1 (its ids: r1.1, r1.2)"),
        (dispositions(("r2.1", "fixed", "")), "names no finding of round 1"),
        (dispositions(("r1.1", "fixed", ""), ("r1.1", "rejected", "")), "finding r1.1 has more than one disposition"),
        (dispositions(("r1.1", "done", "")), "status must be one of fixed, rejected"),
        (json.dumps({"dispositions": [{"id": "r1.1", "status": "fixed", "note": 3}]}), "a disposition note must be text"),
        (json.dumps({"dispositions": [{"id": "r1.1", "status": "fixed", "note": "x" * 5000}]}), "at most 4096 bytes"),
        (json.dumps({"dispositions": {"r1.1": "fixed"}}), "must be a list of objects"),
        ("not json", "must be a list of objects"),
    ],
)
def test_a_malformed_disposition_is_refused_and_nothing_is_stated(ws: Path, engine: Engine, answers: Path, stdin: str, words: str) -> None:
    two_findings_round(engine, ws, answers)
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    refused = prepare(engine, ws, stdin=stdin)
    assert refused["ok"] is False
    assert refused["code"] in ("bad-dispositions", "bad-verification")
    assert words in refused["error"]
    assert load(ws).data["dispositions"] == []
    assert load(ws).data["pending_ticket"] is None


def test_dispositions_on_a_stages_first_round_are_ignored_with_a_note(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws, stdin=dispositions(("r1.1", "fixed", "carried over"), ("r1.2", "rejected", "")))
    assert ticket["ok"], ticket
    assert load(ws).data["dispositions"] == []
    bundle_dir = Path(ticket["bundle_dir"])
    shown = [path.read_text(errors="replace") for path in [*bundle_dir.iterdir(), Path(ticket["prompt_file"])] if path.is_file()]
    assert not any("carried over" in text or "ignored:" in text for text in shown)
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))
    assert outcome["verdict"] == "APPROVED"
    assert "Note: 2 disposition(s) were ignored: this stage had no earlier review for them to answer.\n" in outcome["message"]


def test_ignored_dispositions_are_noted_on_findings_too(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws, stdin=dispositions(("r1.1", "fixed", "")))
    outcome = record(engine, ws, ticket["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert "Note: 1 disposition(s) were ignored" in outcome["message"]
    assert load(ws).data["dispositions"] == []


def test_ignored_dispositions_are_noted_when_the_review_is_skipped(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_IGNORE_GLOBS", "*.md")
    started(engine, ws)
    edit(ws / "api" / "README.md", "api docs\n")
    outcome = prepare(engine, ws, stdin=dispositions(("r1.1", "fixed", "")))
    assert (outcome["status"], outcome["verdict"]) == ("skipped", "APPROVED")
    assert "Note: 1 disposition(s) were ignored" in outcome["message"]
    assert load(ws).data["dispositions"] == []


@pytest.mark.parametrize(
    ("stdin", "words"),
    [
        (dispositions(("r1.1", "done", "")), "status must be one of fixed, rejected"),
        (dispositions(("r1.1", "fixed", ""), ("r1.1", "fixed", "")), "finding r1.1 has more than one disposition"),
        (json.dumps({"dispositions": [{"id": "r1.1", "status": "fixed", "note": "x" * 5000}]}), "at most 4096 bytes"),
        (json.dumps({"dispositions": ["r1.1"]}), "must be a list of objects"),
    ],
)
def test_a_malformed_disposition_on_a_first_round_is_still_refused(ws: Path, engine: Engine, stdin: str, words: str) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    refused = prepare(engine, ws, stdin=stdin)
    assert (refused["ok"], refused["code"]) == (False, "bad-dispositions")
    assert words in refused["error"]


def test_round_2_gets_the_late_round_prompt_and_still_has_the_whole_diff(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    first = prepare(engine, ws)
    prompt = Path(first["prompt_file"]).read_text()
    assert prompt.startswith("# Phase review, round 1\n")
    assert "Read every one of these files, `range.txt` first:" in prompt
    record(engine, ws, first["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    second = prepare(engine, ws)
    prompt = Path(second["prompt_file"]).read_text()
    assert prompt.startswith("# Phase review, round 2 or later\n")
    assert "Read every one of these files" not in prompt
    assert "the review above says which to read in full:" in prompt
    assert "not to be read whole" in prompt and "ARL:" not in prompt
    # Rule 2: the complete diff is still in the bundle and named to the reviewer.
    chunks = sorted(Path(second["bundle_dir"]).glob("changes.*.diff"))
    assert chunks and all(f"- `{chunk}`" in prompt for chunk in chunks)
    assert "print(3)" in bundle_text(second)
    for name in ("prior-rounds.txt", "incremental.diff"):
        assert f"/{name}`" in prompt


def test_round_2_reads_the_whole_diff_when_the_increment_was_left_out(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase diff fits, the change since round 1 does not: round 2 cannot read only the increment."""
    monkeypatch.setenv("ARL_HARD_DIFF_CEILING", "2048")
    started(engine, ws)
    original = (ws / "api" / "src" / "main.py").read_text()
    edit(ws / "api" / "src" / "main.py", "x = 1\n" * 200)
    first = prepare(engine, ws)
    assert first["ok"], first
    record(engine, ws, first["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    # Round 2 restores a and changes b: the phase diff is small, the increment past the ceiling.
    edit(ws / "api" / "src" / "main.py", original)
    edit(ws / "api" / "src" / "other.py", "y = 2\n" * 100)
    second = prepare(engine, ws)
    assert second["ok"], second
    assert "incremental diff omitted" in bundle_text(second, "incremental.diff")
    prompt = Path(second["prompt_file"]).read_text()
    assert prompt.startswith("# Phase review, round 2 or later\n")
    assert "This round has no complete record of what changed since the previous round" in prompt
    assert "not to be read whole" not in prompt
    assert "ARL:" not in prompt
    assert "Read every one of these files, `range.txt` first:" in prompt
    range_txt = bundle_text(second, "range.txt")
    assert "## Changed since round 1: incomplete" in range_txt
    assert "incremental.diff is past hard_diff_ceiling (2048 bytes)" in range_txt


def test_the_bundle_inventory_leaves_what_to_read_to_the_prompt(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    range_txt = bundle_text(prepare(engine, ws), "range.txt")
    assert "Every file in the bundle directory. The review prompt says which to read in full." in range_txt
    assert "Read every file listed here" not in range_txt


def test_round_2_runs_on_late_model_and_says_so(ws: Path, engine: Engine, answers: Path) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "opus", "late_model": "sonnet"}))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    first = prepare(engine, ws)
    assert (first["round"], first["reviewer_model"], first["model"]) == (1, "opus", "opus")
    record(engine, ws, first["review_id"], write_answer(answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED"))
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    second = prepare(engine, ws)
    assert (second["round"], second["reviewer_model"], second["model"]) == (2, "sonnet", "sonnet")
    outcome = record(engine, ws, second["review_id"], write_answer(answers, "RESOLVED id=r1.1 | prints 3", *APPROVE))
    assert "- model: `sonnet`" in Path(outcome["report"]).read_text()
    assert [entry["model"] for entry in load(ws).data["round_history"]] == ["opus", "sonnet"]
    # The next phase starts at round 1 again.
    edit(ws / "api" / "src" / "main.py", "print(4)\n")
    assert prepare(engine, ws)["reviewer_model"] == "opus"


def test_round_2_runs_on_the_weak_tier_by_default_and_the_final_review_on_model(ws: Path, engine: Engine, answers: Path) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "opus", "final_review": True}))
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    second = prepare(engine, ws)
    assert (second["round"], second["reviewer_model"], second["model"]) == (2, "sonnet", "weak (sonnet)")
    outcome = record(engine, ws, second["review_id"], write_answer(answers, "RESOLVED id=r1.1 | prints 3", *APPROVE))
    assert "- model: `weak (sonnet)`" in Path(outcome["report"]).read_text()
    assert engine(ws, "stats")["stats"]["stages"][0]["rounds"][1]["model"] == "weak (sonnet)"
    final = prepare(engine, ws)
    assert (final["label"], final["reviewer_model"]) == ("final", "opus")


def test_an_empty_late_model_keeps_model_on_round_2(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "opus"}))
    monkeypatch.setenv("ARL_LATE_MODEL", "")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    assert prepare(engine, ws)["reviewer_model"] == "opus"


def test_the_final_review_runs_on_model(ws: Path, engine: Engine, answers: Path) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "opus", "late_model": "sonnet", "final_review": True}))
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    assert review(engine, ws, answers, "RESOLVED id=r1.1 | prints 3", *APPROVE)["verdict"] == "APPROVED"
    final = prepare(engine, ws)
    assert (final["label"], final["reviewer_model"]) == ("final", "opus")


def test_an_unset_model_is_passed_on_as_its_id_not_its_display_text(ws: Path, engine: Engine) -> None:
    """``model`` is what the band shows ("strong (opus)"); the spawn gets the alias alone."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    assert (ticket["reviewer_model"], ticket["model"]) == ("opus", "strong (opus)")


def test_the_usage_the_mod_reports_reaches_the_report(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    usage = json.dumps({"input_tokens": 10, "output_tokens": 2600, "cache_read_tokens": 40000, "cache_creation_tokens": 3000, "duration_ms": 61000})
    result = engine(ws, "review", "record", ticket["review_id"], "--answer", str(write_answer(answers, *APPROVE)), "--usage", usage)
    assert result["verdict"] == "APPROVED"
    assert "- cost: 3k new + 40k cached input — 3k output — 61s" in Path(result["report"]).read_text()


@pytest.mark.parametrize("usage", ["not json", "[1, 2]", '{"output_tokens": -1, "turns": true}', '{"output_tokens": "many"}'])
def test_usage_the_engine_cannot_read_is_dropped_never_a_failure(ws: Path, engine: Engine, answers: Path, usage: str) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    result = engine(ws, "review", "record", ticket["review_id"], "--answer", str(write_answer(answers, *APPROVE)), "--usage", usage)
    assert result["verdict"] == "APPROVED"
    assert "- cost:" not in Path(result["report"]).read_text()


def test_a_failed_round_keeps_what_it_cost(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    result = engine(ws, "review", "record", ticket["review_id"], "--failed", "transient", "--usage", '{"output_tokens": 900}')
    assert result["verdict"] == "OP_FAILURE"
    assert "900 output" in Path(result["report"]).read_text()


def test_an_interrupted_review_is_abandoned_without_counting_a_failure(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws, session="s1")
    assert engine(ws, "review", "abandon", ticket["review_id"])["abandoned"] is True
    run = load(ws)
    assert run.data["pending_ticket"] is None
    assert (run.data["failures"], run.data["transient_failures"]) == (0, 0)
    assert engine(ws, "next-action")["action"] == "continue", "the loop is no longer held on a review nobody runs"
    late = record(engine, ws, ticket["review_id"], write_answer(answers, *APPROVE))
    assert late["code"] == "stale-review"


def test_abandoning_a_review_that_is_not_pending_changes_nothing(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    assert engine(ws, "review", "abandon", "someone-else")["abandoned"] is False
    assert load(ws).data["pending_ticket"]["review_id"] == ticket["review_id"]


def test_an_unknown_harness_is_refused_before_any_ticket_is_issued(ws: Path, engine: Engine) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"harness": "claude-code"}))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    assert prepare(engine, ws)["code"] == "unknown-harness"
    assert load(ws).data["pending_ticket"] is None


# --------------------------------------------------------------------------
# Measured in the interactive checks (STEP0 Session K)
# --------------------------------------------------------------------------


def test_a_paused_run_still_reviews_what_claude_asks_for(ws: Path, engine: Engine, answers: Path) -> None:
    """**Fails on a prepare that requires an active run.** Pausing stops the loop prompting
    Claude after the turn; measured live, it also refused the review Claude called in that same
    turn, leaving a finished phase unreviewed until the person resumed."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert engine(ws, "pause")["ok"]

    outcome = review(engine, ws, answers, "VERDICT APPROVED")

    assert outcome["verdict"] == "APPROVED"
    assert load(ws).status == "paused", "a review does not unpause the run"
    assert engine(ws, "next-action")["action"] == "yield"
    # Measured live: without this, Claude read the approval as "go on" and finished the plan.
    assert "The user paused the run" in outcome["message"]
    assert "do not start the next phase" in outcome["message"]


def test_an_active_run_is_not_told_it_is_paused(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert "paused" not in review(engine, ws, answers, "VERDICT APPROVED")["message"]


def test_with_auto_compact_an_approval_tells_claude_to_end_its_turn(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Fails on an approval that says nothing about the turn.** The loop compacts only between
    turns, and Claude goes on with the next phase in the same turn: measured live, no approval but
    one the person gave by command ever compacted."""
    monkeypatch.setenv("ARL_AUTO_COMPACT", "true")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    outcome = review(engine, ws, answers, "VERDICT APPROVED")

    assert "end your turn without starting the next stage" in outcome["message"]


def test_without_auto_compact_an_approval_says_nothing_about_the_turn(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert "end your turn" not in review(engine, ws, answers, "VERDICT APPROVED")["message"]


def test_an_approval_names_the_git_repositories_to_commit(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Fails on "commit each changed git repository" alone.** Measured live: Claude, launched in a
    plain folder, said there was no git repository to commit while a root beside it was one."""
    monkeypatch.setenv("ARL_AUTO_COMPACT", "true")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    message = review(engine, ws, answers, "VERDICT APPROVED")["message"]

    assert f"the run's git repositories: api at {ws / 'api'}" in message
    assert f"the run's git repositories: api at {ws / 'api'}" in engine(ws, "next-action")["prompt"]


def test_accepting_under_auto_compact_tells_claude_to_end_its_turn(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """**Fails on an accept that answers no next turn.** The user accepts from the stall dialog in
    the middle of Claude's turn: "go on with the next phase" left the compaction for no turn's end."""
    monkeypatch.setenv("ARL_AUTO_COMPACT", "true")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    assert "end your turn without starting the next stage" in engine(ws, "accept")["next"]


def test_the_approval_that_completes_the_run_says_so(ws: Path, engine: Engine, answers: Path) -> None:
    """**Fails on the old code**, which said only "approved the change". Measured live: Claude then
    waited for the loop to ask for the plan's after-the-run commit, which the loop never does."""
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")

    outcome = review(engine, ws, answers, "VERDICT APPROVED")

    assert outcome["run"]["stage"] == "done"
    assert "the run is complete, and the loop asks for nothing more" in outcome["message"]
    assert "Anything the plan says comes after the run is yours to do now" in outcome["message"]
    assert f"api at {ws / 'api'}" in outcome["message"]


def test_accepting_the_last_stage_says_the_run_is_complete(ws: Path, engine: Engine) -> None:
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert "the run is complete, and the loop asks for nothing more" in engine(ws, "accept")["next"]


def test_accepting_without_auto_compact_answers_no_next_turn(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    answer = engine(ws, "accept")
    assert answer["next"] == ""
    # The mod's own accept texts name the repositories from this.
    assert answer["repositories"] == f" (the run's git repositories: api at {ws / 'api'}, web at {ws / 'web'})"


def test_a_stalled_round_does_not_tell_claude_to_address_the_findings(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Fails on the findings footer.** The run waits for the user, whose choice the mod appends:
    "ask for the review again" before it would contradict a stop."""
    monkeypatch.setenv("ARL_STALL_ROUNDS", "2")
    started(engine, ws)
    for n in range(2):
        edit(ws / "api" / "src" / "main.py", f"print({n})\n")
        outcome = review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")

    assert "ask for the review again" not in outcome["message"]
    assert "waits for the user's decision" in outcome["message"]


def test_a_failed_review_does_not_ask_claude_to_address_findings_it_does_not_have(ws: Path, engine: Engine) -> None:
    """Measured live: a timed-out review ended "Verify and address the findings above"."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    outcome = engine(ws, "review", "record", ticket["review_id"], "--failed", "transient", "--error", "the reviewer did not answer within 10 s")

    assert outcome["verdict"] == "OP_FAILURE"
    assert "address the findings above" not in outcome["message"]
    assert "no findings to address" in outcome["message"]


def test_resume_tells_a_new_session_what_the_run_is(ws: Path, engine: Engine) -> None:
    """**Fails on a resume that answers no context.** Measured live: a new session resumed with
    only "implement phase 2 of 4: title", and went looking for the plan in the wrong repository."""
    started(engine, ws, phases=2)

    context = engine(ws, "resume")["context"]

    assert str(ws / "plan.md") in context
    assert str(ws / ".arl" / "plan.md") in context
    assert "1. phase 1; 2. phase 2" in context
    assert "add_root" in context and "call review" in context
    for root in load(ws).roots:
        assert root["path"] in context


def test_the_next_phase_prompt_does_not_claim_the_work_is_undone(ws: Path, engine: Engine, answers: Path) -> None:
    """A resume or an accept prompts after the commit or the work may already have happened:
    measured live, "Commit … then implement phase 3" came with phase 3 already written."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, "VERDICT APPROVED")

    prompt = engine(ws, "next-action")["prompt"]

    assert "if that is not done yet" in prompt
    assert "implement (or finish) phase 2 of 3" in prompt


def test_changes_required_with_only_prose_points_claude_at_the_prose(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, "VERDICT CHANGES_REQUIRED")
    assert outcome["verdict"] == "CHANGES_REQUIRED"
    assert "no findings to address" not in outcome["message"]
    assert "reviewer's prose above" in outcome["message"]


def test_a_paused_run_is_not_told_the_loop_will_prompt_it(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_AUTO_COMPACT", "true")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert engine(ws, "pause")["ok"]
    assert "end your turn" not in review(engine, ws, answers, "VERDICT APPROVED")["message"]


def test_a_review_handed_to_the_user_tells_claude_to_stop_not_to_retry(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``NEEDS_HUMAN`` round (here the findings ceiling) leaves the run waiting: the next
    ``review`` is refused, so "call review again" would send Claude into a refusal."""
    monkeypatch.setenv("ARL_MAX_FINDINGS", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    outcome = review(engine, ws, answers, finding("api/src/main.py:1"), finding("api/src/main.py:2"), "VERDICT CHANGES_REQUIRED")
    assert outcome["verdict"] == "NEEDS_HUMAN"
    assert "Call review again" not in outcome["message"]
    assert "waits for the user's decision" in outcome["message"]


def test_a_paused_run_is_told_to_stop_after_an_approval_no_reviewer_gave(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every change matched ``ignore_globs``: the approval comes from ``prepare``, not ``record``,
    and must say the run is paused all the same."""
    monkeypatch.setenv("ARL_IGNORE_GLOBS", "*.md")
    started(engine, ws)
    edit(ws / "notes" / "todo.md", "done\n")
    assert engine(ws, "pause")["ok"]

    outcome = prepare(engine, ws)

    assert outcome["status"] == "skipped"
    assert "The user paused the run" in outcome["message"]


def test_a_paused_review_that_waits_for_the_user_leaves_the_instruction_to_their_choice(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mod asks the user and tells Claude what they chose: a "stop" from the pause before it
    would contradict an "accept" or a "keep going"."""
    monkeypatch.setenv("ARL_MAX_FINDINGS", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    assert engine(ws, "pause")["ok"]

    outcome = review(engine, ws, answers, finding("api/src/main.py:1"), finding("api/src/main.py:2"), "VERDICT CHANGES_REQUIRED")

    assert outcome["verdict"] == "NEEDS_HUMAN"
    assert "The user paused the run" not in outcome["message"]


# --------------------------------------------------------------------------
# What each round cost, for /arl-stats
# --------------------------------------------------------------------------

USAGE = {"turns": 6, "input_tokens": 10, "cache_creation_tokens": 2000, "cache_read_tokens": 30000, "output_tokens": 400, "duration_ms": 9000}


def test_a_recorded_round_keeps_what_it_cost_who_ran_it_and_what_blocked(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    answer = write_answer(answers, finding("api/src/main.py:1"), finding("api/src/main.py:1", "low"), "VERDICT CHANGES_REQUIRED")
    usage = json.dumps({**USAGE, "model": "claude-opus-5-5"})
    assert engine(ws, "review", "record", ticket["review_id"], "--answer", str(answer), "--usage", usage)["verdict"] == "CHANGES_REQUIRED"

    entry = load(ws).data["round_history"][-1]
    assert entry["usage"] == {**USAGE, "cost_usd": None}
    assert (entry["model"], entry["harness"], entry["round"]) == ("claude-opus-5-5", "subagent", 1)
    assert (entry["blocking"], entry["deferred"]) == (1, 0)


def test_without_a_reported_model_a_round_names_the_configured_one(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MODEL", "sonnet")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, *APPROVE)
    entry = load(ws).data["round_history"][-1]
    assert (entry["model"], entry["usage"]) == ("sonnet", None)


def test_a_failed_round_is_counted_apart_with_its_cost_and_records_no_round(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    outcome = engine(ws, "review", "record", ticket["review_id"], "--failed", "transient", "--error", "API error", "--usage", json.dumps(USAGE))
    assert outcome["verdict"] == "OP_FAILURE"

    run = load(ws)
    assert run.data["round_history"] == []
    [failure] = run.data["failure_history"]
    assert (failure["verdict"], failure["kind"], failure["error"], failure["round"]) == ("OP_FAILURE", "transient", "API error", 1)
    assert failure["usage"]["turns"] == 6
    # Still a failure, never an approval.
    assert run.data["approvals"] == []


def test_run_external_records_the_usage_of_the_review_and_its_repair(
    ws: Path, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Fails on the old code**, which dropped what ``runner.invoke`` returned: an external round
    recorded no usage at all."""
    calls: list[Path] = []

    def invoke(_target: object, run: Any, *, config: object, environ: object = None) -> harness.Usage:
        calls.append(run.out_path)
        if len(calls) == 1:
            run.out_path.write_text("It is bad.\n")
        else:
            run.out_path.write_text(
                "<<<ARL-FINDINGS>>>\nFINDING severity=high actionable=yes file=api/src/main.py:1 | bad\nVERDICT CHANGES_REQUIRED\n<<<ARL-END>>>\n"
            )
        return harness.Usage(turns=3, input_tokens=100, cost_usd=0.25)

    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setattr(runner, "check_model", lambda _config: None)
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    assert engine(ws, "review", "run-external", ticket["review_id"])["verdict"] == "CHANGES_REQUIRED"

    assert len(calls) == 2
    entry = load(ws).data["round_history"][-1]
    assert entry["harness"] == "opencode"
    assert (entry["usage"]["turns"], entry["usage"]["input_tokens"], entry["usage"]["cost_usd"]) == (6, 200, 0.5)
    assert entry["usage"]["output_tokens"] is None


def test_a_state_written_before_failure_history_existed_still_loads(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    paths = state.run_paths(ws)
    document = json.loads(paths.state_file.read_text())
    del document["failure_history"]
    paths.state_file.write_text(json.dumps(document))
    assert load(ws).data["failure_history"] == []
    document["failure_history"] = "nope"
    paths.state_file.write_text(json.dumps(document))
    with pytest.raises(StateLoadError, match="failure_history"):
        state.RunState.load(paths)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"cost_usd": 1.5, "turns": 2}', (1.5, 2)),
        ('{"cost_usd": -1, "turns": true}', None),
        ('{"cost_usd": "free"}', None),
    ],
)
def test_reported_usage_reads_a_cost_only_when_it_is_a_number(raw: str, expected: tuple[float, int] | None) -> None:
    usage = review_module.reported_usage(raw)
    assert (usage.cost_usd, usage.turns) == expected if usage is not None else expected is None


def test_reported_model_is_display_text_or_nothing() -> None:
    assert review_module.reported_model('{"model": "opus"}') == "opus"
    assert review_module.reported_model('{"model": 3}') == ""
    assert review_module.reported_model("not json") == ""
    assert len(review_module.reported_model(json.dumps({"model": "x" * 500}))) == 120


@pytest.mark.parametrize("failure", ["launch", "reviewer"])
def test_a_repair_that_fails_keeps_the_cost_of_the_review_before_it(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """**Fails on the old code**, whose failure exits recorded no usage: the malformed review's
    requests were lost from the stats once its repair failed."""
    calls: list[Path] = []

    def invoke(_target: object, run: Any, *, config: object, environ: object = None) -> harness.Usage:
        calls.append(run.out_path)
        if len(calls) == 2:
            if failure == "launch":
                raise runner.LaunchError("opencode: not found")
            raise runner.ReviewerFailed("exited with status 3")
        run.out_path.write_text("It is bad.\n")
        return harness.Usage(turns=3, input_tokens=100)

    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setattr(runner, "check_model", lambda _config: None)
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)

    assert engine(ws, "review", "run-external", ticket["review_id"])["verdict"] == "OP_FAILURE"

    [failed] = load(ws).data["failure_history"]
    assert (failed["usage"]["turns"], failed["usage"]["input_tokens"]) == (3, 100)


@pytest.mark.parametrize("cost", ["1e309", "Infinity", "-Infinity", "NaN"])
def test_a_cost_that_is_not_finite_is_not_read(cost: str) -> None:
    usage = review_module.reported_usage(f'{{"cost_usd": {cost}, "turns": 1}}')
    assert usage is not None
    assert (usage.cost_usd, usage.turns) == (None, 1)
