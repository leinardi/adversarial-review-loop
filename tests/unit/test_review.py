"""The review pipeline: ``review prepare`` / ``record`` / ``run-external``, repair, stalls,
failure budgets, ``clarify``, the final review and the guards around them.

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

from arl import review as review_module
from arl import state

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

    outcome = review(engine, ws, answers, finding("web/src/main.py:1", severity="medium"), "VERDICT APPROVED")

    assert outcome["verdict"] == "APPROVED"
    assert "web/src/main.py:1" in outcome["message"], "a deferred finding is still shown"


def test_a_retraction_hits_only_the_root_it_names(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    edit(ws / "web" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), finding("web/src/main.py:1"), "VERDICT CHANGES_REQUIRED")

    asked = engine(ws, "clarify", "prepare", stdin="Is the api finding right? api/src/main.py never returns.")
    assert asked["ok"], asked
    bundle_dir = Path(asked["bundle_dir"])
    assert "file=web/src/main.py:1" in (bundle_dir / "review.md").read_text(), "the fresh reviewer is shown the review"
    assert "review.md" in Path(asked["prompt_file"]).read_text()
    reply = answers / "clarify.md"
    reply.write_text(
        "The api finding was wrong.\n\n<<<ARL-FINDINGS>>>\nSUPERSEDES round=1 file=api/src/main.py:1 | the function returns\n<<<ARL-END>>>\n"
    )
    recorded = engine(ws, "clarify", "record", str(asked["round_seq"]), str(asked["question"]), "--answer", str(reply))
    assert len(recorded["retracted"]) == 1
    assert recorded["reply"].startswith("The api finding was wrong.")

    edit(ws / "web" / "src" / "main.py", "print('fixed')\n")
    prior = (Path(prepare(engine, ws)["bundle_dir"]) / "prior-rounds.txt").read_text()
    retraction = prior.index("Retracted by this round's reviewer")
    assert "file=api/src/main.py:1" in prior[retraction:]
    assert "file=web/src/main.py:1" not in prior[retraction:]


def test_clarify_has_a_budget_per_round(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_MAX_CLARIFICATIONS", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="why?")
    reply = answers / "reply.md"
    reply.write_text("Because.\n")
    engine(ws, "clarify", "record", str(asked["round_seq"]), str(asked["question"]), "--answer", str(reply))
    assert engine(ws, "clarify", "prepare", stdin="and why else?")["code"] == "clarify-budget"


def test_a_clarify_reply_about_a_superseded_round_is_refused(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="why?")
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    reply = answers / "reply.md"
    reply.write_text("x\n")
    refused = engine(ws, "clarify", "record", str(asked["round_seq"]), str(asked["question"]), "--answer", str(reply))
    assert refused["code"] == "stale-clarify"


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


def test_clarify_run_external_asks_and_records(ws: Path, engine: Engine, answers: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    script = scripted_reviewer(tmp_path, 'cat "$1"/question-1.txt >/dev/null || exit 9\nprintf "It means the value is wrong.\\n"')
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(script))

    outcome = engine(ws, "clarify", "run-external", stdin="What does the finding mean?")

    assert outcome["reply"] == "It means the value is wrong.\n"
    assert outcome["retracted"] == []


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


def test_a_question_whose_reply_never_came_back_still_spends_the_budget(
    ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARL_MAX_CLARIFICATIONS", "1")
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    assert engine(ws, "clarify", "prepare", stdin="why?")["ok"]
    assert engine(ws, "clarify", "prepare", stdin="why, again?")["code"] == "clarify-budget"


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


def test_a_clarify_question_about_a_later_round_sees_the_earlier_rounds(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    review(engine, ws, answers, finding("web/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="Why did the two rounds disagree?")
    assert f"- `{Path(asked['bundle_dir']) / 'prior-rounds.txt'}`" in Path(asked["prompt_file"]).read_text()


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


def test_a_clarify_of_the_final_review_keeps_no_retraction(ws: Path, engine: Engine, answers: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_FINAL_REVIEW", "true")
    started(engine, ws, phases=1)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, *APPROVE)
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="Is that right?")
    reply = answers / "reply.md"
    reply.write_text("No.\n\n<<<ARL-FINDINGS>>>\nSUPERSEDES round=1 file=api/src/main.py:1 | wrong\n<<<ARL-END>>>\n")
    recorded = engine(ws, "clarify", "record", str(asked["round_seq"]), str(asked["question"]), "--answer", str(reply))
    assert recorded["retracted"] == []


def test_run_external_refuses_a_harness_this_build_does_not_implement(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    ticket = prepare(engine, ws)
    monkeypatch.setenv("ARL_HARNESS", "opencod")
    assert engine(ws, "review", "run-external", ticket["review_id"])["code"] == "unknown-harness"
    assert engine(ws, "clarify", "run-external", stdin="why?")["code"] == "unknown-harness"


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


def test_a_clarify_reply_for_another_run_is_refused(ws: Path, engine: Engine, answers: Path) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(1)\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="why?")
    reply = answers / "reply.md"
    reply.write_text("x\n")
    args = ["clarify", "record", str(asked["round_seq"]), str(asked["question"]), "--answer", str(reply)]
    assert engine(ws, *args, "--run-id", "another-run")["code"] == "stale-clarify"
    assert engine(ws, *args, "--run-id", asked["run_id"])["ok"]


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


def test_an_unset_model_is_passed_on_empty_not_as_its_display_text(ws: Path, engine: Engine) -> None:
    """``model`` is what the band shows ("the session's default"); the spawn must get nothing."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    ticket = prepare(engine, ws)
    assert ticket["reviewer_model"] == ""
    assert ticket["model"] != ""


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


def test_clarify_prepare_tells_the_mod_how_to_run_the_subagent(ws: Path, engine: Engine, answers: Path) -> None:
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"model": "sonnet"}))
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print('api 2')\n")
    review(engine, ws, answers, finding("api/src/main.py:1"), "VERDICT CHANGES_REQUIRED")
    asked = engine(ws, "clarify", "prepare", stdin="Why?")
    assert (asked["reviewer_model"], asked["timeout_sec"]) == ("sonnet", 900)


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
    assert "stop, and tell them" in outcome["message"]


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
