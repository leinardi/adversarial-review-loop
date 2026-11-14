"""The benchmark harness's offline half: transcript analysis and seeded-defect scoring."""

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
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import bench

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bench"
SEEDS = Path(__file__).resolve().parents[1] / "bench" / "seeds"
STATE: dict[str, Any] = {"roots": [{"alias": "case", "path": "/case"}]}

DEFECT_A = {"id": "A", "file": "scripts/arl/review.py", "line": 153, "symbols": ["_next_turn"]}
DEFECT_B = {"id": "B", "file": "scripts/arl/review.py", "line": 156, "symbols": ["_resume"]}


def entry(*findings: str) -> dict[str, Any]:
    return {"label": "phase1", "verdict": "CHANGES_REQUIRED", "findings": list(findings)}


def finding(severity: str, file: str, text: str, *, actionable: str = "yes") -> str:
    return f"FINDING severity={severity} actionable={actionable} file=case/{file} | {text}"


# --- analyze -----------------------------------------------------------------------------------


def test_analyze_counts_a_split_response_once() -> None:
    row = bench.analyze_transcript(FIXTURES / "project" / "0001-session" / "subagents" / "agent-a1.jsonl")
    assert row["requests"] == 3
    assert row["tool_calls"] == 3
    assert row["max_calls_per_turn"] == 2
    assert row["single_call_turns"] == 1
    assert (row["bundle_reads"], row["repo_reads"], row["repeat_reads"]) == (1, 2, 1)
    assert (row["tok_in"], row["tok_cw"], row["tok_cr"], row["tok_out"]) == (7, 12500, 31000, 1200)
    assert row["final_context"] == 15501
    # 7 x 1 + 12500 x 1.25 + 31000 x 0.1 + 1200 x 5 = 24732
    assert row["cost_units_k"] == round(24732 / 1000, 1)


def test_analyze_case_keeps_only_reviewers_and_joins_their_round() -> None:
    state = json.loads((FIXTURES / "state.json").read_text(encoding="utf-8"))
    rows = bench.analyze_case(FIXTURES / "project", state, sessions=["0001-session"])
    assert len(rows) == 1
    assert rows[0]["description"] == "Review phase1, round 1"
    assert rows[0]["verdict"] == "CHANGES_REQUIRED"
    assert rows[0]["findings"] == {"high": 1, "low": 1}


def test_analyze_case_does_not_join_rounds_of_the_source_case() -> None:
    state = json.loads((FIXTURES / "state.json").read_text(encoding="utf-8"))
    rows = bench.analyze_case(FIXTURES / "project", state, history_offset=1, sessions=["0001-session"])
    assert "verdict" not in rows[0]


def test_analyze_case_reads_only_the_sessions_of_this_case() -> None:
    """A case name reused by a later matrix shares its transcript bucket with the earlier runs."""
    bucket = FIXTURES / "project"
    assert sum(row["requests"] for row in bench.analyze_case(bucket, None, sessions=["0001-session"])) == 3
    assert sum(row["requests"] for row in bench.analyze_case(bucket, None, sessions=["0002-session"])) == 2
    # Only a case made before sessions were recorded reads them all.
    assert sum(row["requests"] for row in bench.analyze_case(bucket, None)) == 5
    assert bench.analyze_case(bucket, None, sessions=[]) == []


def test_record_session_appends_a_new_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "c.bench.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    first, second = bench.record_session("c"), bench.record_session("c")
    assert first != second
    assert json.loads((tmp_path / "cases" / "c.bench.json").read_text(encoding="utf-8"))["sessions"] == [first, second]


# --- score, round 1 ----------------------------------------------------------------------------


def test_parse_finding_strips_the_alias_and_the_line() -> None:
    parsed = bench.parse_finding(0, finding("high", "scripts/arl/review.py:12", "x"), ["case"])
    assert parsed is not None
    assert (parsed.path, parsed.line, parsed.severity, parsed.actionable) == ("scripts/arl/review.py", 12, "high", True)
    nowhere = bench.parse_finding(1, "FINDING severity=low actionable=no file=- | general", ["case"])
    assert nowhere is not None
    assert (nowhere.path, nowhere.line) == (None, None)


def test_an_unrelated_finding_in_a_seeded_file_scores_nothing() -> None:
    result = bench.score_round1(STATE, entry(finding("high", "scripts/arl/review.py:700", "an unrelated bug in prepare")), [DEFECT_A])
    assert result["reported"] == 0
    assert result["blocking"] == 0
    assert len(result["unmatched_blocking"]) == 1


def test_a_symbol_names_the_defect_when_the_line_is_wrong() -> None:
    result = bench.score_round1(STATE, entry(finding("high", "scripts/arl/review.py:830", "_next_turn swaps the branches")), [DEFECT_A])
    assert (result["reported"], result["blocking"]) == (1, 1)
    assert result["unmatched_blocking"] == []


@pytest.mark.parametrize(
    ("severity", "actionable"),
    [("low", "yes"), ("high", "no")],
)
def test_a_low_or_unactionable_match_is_reported_but_not_blocking(severity: str, actionable: str) -> None:
    result = bench.score_round1(STATE, entry(finding(severity, "scripts/arl/review.py:153", "x", actionable=actionable)), [DEFECT_A])
    assert (result["reported"], result["blocking"]) == (1, 0)


def test_two_findings_on_one_defect_count_once() -> None:
    result = bench.score_round1(
        STATE,
        entry(finding("high", "scripts/arl/review.py:153", "x"), finding("medium", "scripts/arl/review.py:154", "x again")),
        [DEFECT_A],
    )
    assert (result["reported"], result["blocking"], result["of"]) == (1, 1, 1)
    # The second one named the same defect: a duplicate, not a finding that named nothing.
    assert result["unmatched_blocking"] == []


def test_one_finding_cannot_match_two_defects() -> None:
    # Line 154 is within the window of both A (153) and B (156): a symbol tells them apart.
    result = bench.score_round1(STATE, entry(finding("high", "scripts/arl/review.py:154", "_resume is off by one")), [DEFECT_A, DEFECT_B])
    assert (result["reported"], result["blocking"]) == (1, 1)
    assert [key for key, record in result["defects"].items() if record["reported"]] == ["B"]


def test_a_finding_named_by_its_line_alone_between_two_defects_is_ambiguous() -> None:
    result = bench.score_round1(STATE, entry(finding("high", "scripts/arl/review.py:154", "x")), [DEFECT_A, DEFECT_B])
    assert (result["reported"], result["blocking"]) == (0, 0)
    assert result["ambiguous"] == [{"finding": finding("high", "scripts/arl/review.py:154", "x"), "candidates": ["A", "B"]}]
    assert result["unmatched_blocking"] == []


def test_a_blocking_candidate_is_preferred() -> None:
    result = bench.score_round1(
        STATE,
        entry(finding("low", "scripts/arl/review.py:153", "x"), finding("high", "scripts/arl/review.py:830", "_next_turn")),
        [DEFECT_A],
    )
    assert result["defects"]["A"]["blocking"] is True


# --- score, round 2 ----------------------------------------------------------------------------

ROUND2 = [
    {"id": "O", "class": "open", "file": "scripts/arl/state.py", "line": 583, "symbols": ["resume_context"]},
    {"id": "R", "class": "regression", "file": "hooks/flow.ts", "line": 473, "symbols": ["dropRoot"]},
    {"id": "F", "class": "fixed", "file": "hooks/flow.ts", "line": 474, "symbols": ["'dropped'"]},
    {"id": "U", "class": "untouched-miss", "file": "scripts/arl/report.py", "line": 229, "symbols": ["_footer"]},
]


def classes(result: dict[str, Any]) -> dict[str, tuple[int, int]]:
    return {name: (record["blocking"], record["reported"]) for name, record in result["classes"].items()}


def test_round2_an_open_defect_found() -> None:
    result = bench.score_round2(STATE, entry(finding("medium", "scripts/arl/state.py:583", "still < done")), ROUND2)
    assert classes(result)["open"] == (1, 1)
    assert result["false_reraises"] == 0


def test_round2_a_regression_found_is_not_a_reraise_of_the_fix_beside_it() -> None:
    result = bench.score_round2(STATE, entry(finding("high", "hooks/flow.ts:474", "dropRoot now reads an engine error as kept")), ROUND2)
    assert classes(result)["regression"] == (1, 1)
    assert classes(result)["fixed"] == (0, 0)
    assert result["false_reraises"] == 0


def test_round2_a_regression_rated_low() -> None:
    result = bench.score_round2(STATE, entry(finding("low", "hooks/flow.ts:473", "dropRoot")), ROUND2)
    assert classes(result)["regression"] == (0, 1)


def test_round2_a_fixed_defect_reraised() -> None:
    result = bench.score_round2(
        STATE,
        entry(finding("high", "hooks/flow.ts:473", "dropRoot regression"), finding("high", "hooks/flow.ts:900", "still returns 'dropped'")),
        ROUND2,
    )
    assert classes(result)["fixed"] == (1, 1)
    assert (result["false_reraises"], result["false_reraises_blocking"]) == (1, 1)


def test_round2_an_untouched_miss_found() -> None:
    result = bench.score_round2(STATE, entry(finding("medium", "scripts/arl/report.py:229", "_footer reads deferred")), ROUND2)
    assert classes(result)["untouched-miss"] == (1, 1)


def test_round2_refuses_an_unknown_class() -> None:
    with pytest.raises(ValueError, match="unknown round-2 class"):
        bench.score_round2(STATE, entry(), [{"id": "X", "class": "maybe", "file": "a", "line": 1}])


# --- the shipped seeds -------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["medium", "large1"])
def test_shipped_truth_manifests_are_well_formed(phase: str) -> None:
    for name, required in ((f"{phase}.truth.json", set()), (f"{phase}.round2.truth.json", {"class"})):
        truth = json.loads((SEEDS / name).read_text(encoding="utf-8"))
        ids = [defect["id"] for defect in truth]
        assert len(ids) == len(set(ids)), name
        for defect in truth:
            assert {"id", "file", "line", "symbols", *required} <= defect.keys(), (name, defect["id"])
            assert defect["symbols"], (name, defect["id"])
            assert defect.get("class", "open") in bench.ROUND2_CLASSES
    assert (SEEDS / f"{phase}.patch").exists()
    assert (SEEDS / f"{phase}.round2.patch").exists()


def test_bench_dir_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    assert bench.bench_dir() == tmp_path


# --- round-2 cases -----------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _round1_case(cases: Path, name: str, verdict: str = "CHANGES_REQUIRED") -> Path:
    case = cases / name
    (case / ".arl").mkdir(parents=True)
    _git(case, "init", "-q")
    (case / "a.txt").write_text("one\n", encoding="utf-8")
    _git(case, "add", "a.txt")
    _git(case, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    state = {
        "pending_ticket": None,
        "roots": [{"alias": name, "path": str(case)}],
        "round_history": [
            {
                "label": "phase1",
                "verdict": verdict,
                "trees": {name: "abc"},
                "findings": [f"FINDING severity=high actionable=yes file={name}/a.txt:1 | x"],
            }
        ],
    }
    (case / ".arl" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return case


def test_round2_copies_the_case_renames_it_and_applies_the_fix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    source = _round1_case(tmp_path / "cases", "one")
    fix = tmp_path / "fix.patch"
    (source / "a.txt").write_text("two\n", encoding="utf-8")
    fix.write_text(_git(source, "diff"), encoding="utf-8")
    _git(source, "checkout", "-q", "--", "a.txt")

    new = bench.make_round2("p", "one", "two", fix=fix)

    assert (new / "a.txt").read_text(encoding="utf-8") == "two\n"
    assert (source / "a.txt").read_text(encoding="utf-8") == "one\n"
    text = (new / ".arl" / "state.json").read_text(encoding="utf-8")
    assert "one" not in text.replace("phase1", "")
    state = json.loads(text)
    assert state["roots"] == [{"alias": "two", "path": str(new)}]
    assert state["round_history"][0]["trees"] == {"two": "abc"}
    assert state["round_history"][0]["findings"] == ["FINDING severity=high actionable=yes file=two/a.txt:1 | x"]
    meta = json.loads((tmp_path / "cases" / "two.bench.json").read_text(encoding="utf-8"))
    assert (meta["round"], meta["history_offset"], meta["from"], meta["sessions"]) == (2, 1, "one", [])
    with pytest.raises(SystemExit, match="not a round-1 case"):
        bench.make_round2("p", "two", "three", fix=fix)


def test_round2_refuses_an_approved_round_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    _round1_case(tmp_path / "cases", "one", verdict="APPROVED")
    with pytest.raises(SystemExit, match="nothing to re-review"):
        bench.make_round2("p", "one", "two", fix=tmp_path / "none.patch")


def _commit_known(rev: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=bench.REPO, capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("phase", ["medium", "large1"])
def test_shipped_seed_patches_apply_to_their_commit(phase: str, tmp_path: Path) -> None:
    """The seed applies to the commit, and the round-2 fix to the seeded tree, without touching the checkout."""
    commit = json.loads((bench.BENCH / "phases.json").read_text(encoding="utf-8"))[phase]["commit"]
    if not _commit_known(commit):
        pytest.skip(f"{commit} is not in this clone")
    env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "index")}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=bench.REPO, env=env, check=True, capture_output=True)

    git("read-tree", commit)
    git("apply", "--cached", str(SEEDS / f"{phase}.patch"))
    git("apply", "--cached", str(SEEDS / f"{phase}.round2.patch"))


def _shipped(phase: str) -> list[dict[str, Any]]:
    return list(json.loads((SEEDS / f"{phase}.round2.truth.json").read_text(encoding="utf-8")))


def test_round2_shipped_a_fixed_defect_reraised_on_the_regressions_line_is_no_regression() -> None:
    """L1 (fixed) and LR (regression) share commands.py:259: re-raising L1 alone must not score LR."""
    reraise = finding("high", "scripts/arl/commands.py:259", "_require_untouched still compares now == start_tree, inverted")
    result = bench.score_round2(STATE, entry(reraise), _shipped("large1"))
    assert classes(result)["regression"] == (0, 0)
    assert classes(result)["fixed"] == (1, 1)
    assert (result["false_reraises"], result["false_reraises_blocking"]) == (1, 1)


def test_round2_shipped_the_regression_on_a_fixed_line_is_the_regression() -> None:
    caught = finding(
        "high", "scripts/arl/commands.py:259", "_require_untouched skips the check once here.exists() is false: a deleted root is dropped"
    )
    result = bench.score_round2(STATE, entry(caught), _shipped("large1"))
    assert classes(result)["regression"] == (1, 1)
    assert result["false_reraises"] == 0


def test_round2_shipped_a_finding_that_cannot_tell_the_two_apart_is_left_for_a_person() -> None:
    vague = finding("high", "scripts/arl/commands.py:259", "_require_untouched is wrong")
    result = bench.score_round2(STATE, entry(vague), _shipped("large1"))
    assert classes(result)["regression"] == (0, 0)
    assert result["false_reraises"] == 0
    assert result["ambiguous"] == [{"finding": vague, "candidates": ["LR", "L1"]}]


# --- case names --------------------------------------------------------------------------------


def test_round2_refuses_to_replace_its_own_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    source = _round1_case(tmp_path / "cases", "one")
    with pytest.raises(SystemExit, match="name of its own"):
        bench.make_round2("p", "one", "one", fix=tmp_path / "none.patch", replace=True)
    assert (source / ".arl" / "state.json").exists()


@pytest.mark.parametrize("name", ["../escape", "/abs", ".", "..", "a/b", "", "-flag"])
def test_case_names_must_stay_inside_the_cases_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path / "bench"))
    _round1_case(tmp_path / "bench" / "cases", "one")
    (tmp_path / "escape").mkdir()
    with pytest.raises(SystemExit, match="not a plain case name"):
        bench.make_round2("p", "one", name, fix=tmp_path / "none.patch", replace=True)
    with pytest.raises(SystemExit, match="not a plain case name"):
        bench.make_round2("p", name, "two", fix=tmp_path / "none.patch", replace=True)
    assert (tmp_path / "escape").is_dir()


def test_a_symlinked_case_name_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path / "bench"))
    _round1_case(tmp_path / "bench" / "cases", "one")
    (tmp_path / "outside").mkdir()
    (tmp_path / "bench" / "cases" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(SystemExit, match="not a plain case name"):
        bench.make_round2("p", "one", "link", fix=tmp_path / "none.patch", replace=True)
    assert (tmp_path / "outside").is_dir()


def test_round2_refuses_an_existing_destination_unless_replacing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    source = _round1_case(tmp_path / "cases", "one")
    fix = tmp_path / "fix.patch"
    (source / "a.txt").write_text("two\n", encoding="utf-8")
    fix.write_text(_git(source, "diff"), encoding="utf-8")
    _git(source, "checkout", "-q", "--", "a.txt")
    (tmp_path / "cases" / "two").mkdir()
    (tmp_path / "cases" / "two" / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="pass --replace"):
        bench.make_round2("p", "one", "two", fix=fix)
    assert (tmp_path / "cases" / "two" / "keep").exists()
    bench.make_round2("p", "one", "two", fix=fix, replace=True)
    assert not (tmp_path / "cases" / "two" / "keep").exists()


def test_round2_refuses_a_missing_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARL_BENCH_DIR", str(tmp_path))
    (tmp_path / "cases").mkdir()
    with pytest.raises(SystemExit, match="no recorded run"):
        bench.make_round2("p", "nothing", "two", fix=tmp_path / "none.patch")
