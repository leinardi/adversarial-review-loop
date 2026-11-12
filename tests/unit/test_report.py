"""Report storage and the text Claude is actually handed.

The thing under test is what survives: **every** ``FINDING`` line comes back inline, and
prose is the only part allowed to be cut. A finding trimmed for length is a finding that
never gets fixed, and the loop would then block forever on evidence the model was never
shown.
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

import stat
import subprocess
from pathlib import Path

import pytest
from conftest import config_with, git

from arl import harness, report
from arl.contract import Review, RootRange, Target
from arl.util import TRUNCATION_MARKER

#: Reviewer output that is not valid UTF-8, which the report must keep byte for byte.
INVALID_UTF8 = b"tool output: \xff\xfe\n"


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    return tmp_path / "launch" / ".arl" / "reports"


def a_target(scope: str = "phase", phase: int = 1, base: str = "b", head: str = "h") -> Target:
    roots = (RootRange(alias="ws", path="/launch/ws", kind="git", base=base, head=head),)
    return Target(scope=scope, phase=phase, roots=roots, launch_dir="/launch")


def a_review(**overrides: object) -> Review:
    review = Review(
        verdict="CHANGES_REQUIRED",
        findings="FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n",
        all_findings=(
            "FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n"
            "FINDING severity=low actionable=no file=b.txt:2 | Could be named better\n"
        ),
        prose="The error path is wrong.",
    )
    for key, value in overrides.items():
        setattr(review, key, value)
    return review


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_the_filename_carries_the_verdict(reports_dir: Path) -> None:
    review = a_review()
    path = report.store(review, a_target("phase", 2, "b", "h"), seq="007", reports_dir=reports_dir, config=config_with())

    assert path.name == "007-phase2-changes_required.md"
    assert review.report == str(path)


def test_a_final_review_is_labelled_final(reports_dir: Path) -> None:
    path = report.store(a_review(verdict="APPROVED"), a_target("final", 3, "b", "h"), seq="001", reports_dir=reports_dir, config=config_with())
    assert path.name == "001-final-approved.md"


def test_a_verdictless_review_is_still_stored(reports_dir: Path) -> None:
    """A report that cannot be named after a verdict is evidence, not something to drop."""
    path = report.store(Review(), a_target("phase", 1, "b", "h"), seq="001", reports_dir=reports_dir, config=config_with())
    assert path.name == "001-phase1-unknown.md"
    assert "**UNKNOWN**" in path.read_text()


def test_the_report_records_what_decided_it(reports_dir: Path) -> None:
    review = a_review(error="the reviewer timed out after 900s")
    text = report.store(
        review, a_target(base="basetree", head="headtree"), seq="001", reports_dir=reports_dir, config=config_with(variant="thinking")
    ).read_text()

    assert "- verdict (recomputed by the engine): **CHANGES_REQUIRED**\n" in text
    assert "- root `ws` (/launch/ws): `basetree` -> `headtree`\n" in text
    assert "- harness: `subagent`\n" in text
    assert "- model: `the session's default` (variant `thinking`)\n" in text
    assert "- block_severity: `medium`\n" in text
    assert "- engine note: the reviewer timed out after 900s\n" in text
    assert "Returns success on a failed lookup" in text


def test_a_clean_review_says_so_rather_than_leaving_the_section_empty(reports_dir: Path) -> None:
    text = report.store(Review(verdict="APPROVED"), a_target(), seq="001", reports_dir=reports_dir, config=config_with()).read_text()
    assert "## Blocking findings\n\n(none)\n" in text
    assert "- engine note:" not in text


def test_the_raw_reviewer_output_is_embedded(reports_dir: Path, tmp_path: Path) -> None:
    raw = tmp_path / "reviewer.out"
    raw.write_text("everything the reviewer said\n")
    text = report.store(a_review(raw=str(raw)), a_target(), seq="001", reports_dir=reports_dir, config=config_with()).read_text()

    assert "## Raw reviewer output\n\n````\neverything the reviewer said\n\n````\n" in text


def test_output_that_is_not_valid_utf8_is_kept_byte_for_byte(reports_dir: Path, tmp_path: Path) -> None:
    """One stray byte must not cost the whole report -- it is what a denial points at."""
    raw = tmp_path / "reviewer.out"
    raw.write_bytes(INVALID_UTF8)

    path = report.store(a_review(raw=str(raw)), a_target("phase", 1, "b", "h"), seq="001", reports_dir=reports_dir, config=config_with())
    assert INVALID_UTF8 in path.read_bytes()


def test_a_missing_raw_file_does_not_stop_the_report(reports_dir: Path, tmp_path: Path) -> None:
    path = report.store(a_review(raw=str(tmp_path / "gone")), a_target(), seq="001", reports_dir=reports_dir, config=config_with())
    assert path.is_file()


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


def test_the_report_is_private(reports_dir: Path) -> None:
    """Reports quote the diff and the plan; they are not world-readable."""
    path = report.store(a_review(), a_target("phase", 1, "b", "h"), seq="001", reports_dir=reports_dir, config=config_with())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


# --------------------------------------------------------------------------
# The denial message
# --------------------------------------------------------------------------


def test_the_reason_leads_with_the_headline_and_ends_with_the_instruction() -> None:
    text = report.reason(a_review(), "adversarial-review-loop: changes required.", config=config_with())

    assert text.startswith("adversarial-review-loop: changes required.\n")
    assert text.endswith("Verify and address the findings above, then ask for the review again.\n")


def test_every_blocking_finding_is_quoted_inline() -> None:
    text = report.reason(a_review(), "h", config=config_with())

    assert "Blocking findings (actionable, severity >= medium) -- every one must be resolved:" in text
    assert "Returns success on a failed lookup" in text
    assert "All findings reported (non-blocking ones included, for context):" in text
    assert "Could be named better" in text


def test_the_full_set_is_not_repeated_when_it_is_the_blocking_set() -> None:
    review = a_review()
    review.all_findings = review.findings
    assert "All findings reported" not in report.reason(review, "h", config=config_with())


def test_the_engine_note_is_carried_back() -> None:
    text = report.reason(a_review(error="the reviewer produced no output"), "h", config=config_with())
    assert "\nGate note: the reviewer produced no output\n" in text


def test_prose_truncates_but_findings_never_do() -> None:
    review = a_review(prose="padding. " * 5000)
    text = report.reason(review, "h", config=config_with(max_reason_bytes=2000))

    assert TRUNCATION_MARKER.format(limit=2000) in text
    assert "truncated at 2000 bytes" in text
    assert "Returns success on a failed lookup" in text
    assert "Could be named better" in text


def test_short_prose_is_left_alone() -> None:
    assert "truncated at" not in report.reason(a_review(), "h", config=config_with())


def test_supersedes_lines_are_carried_back_in_the_reason() -> None:
    review = a_review(supersedes="SUPERSEDES round=1 file=b.txt:2 | round 1 was wrong about this\n")
    text = report.reason(review, "h", config=config_with())
    assert "Reversals of earlier rounds (SUPERSEDES lines)" in text
    assert "round 1 was wrong about this" in text


def test_no_supersedes_section_when_the_review_has_none() -> None:
    assert "SUPERSEDES" not in report.reason(a_review(), "h", config=config_with())


def test_supersedes_lines_are_rendered_in_the_stored_report(reports_dir: Path) -> None:
    review = a_review(supersedes="SUPERSEDES round=1 file=b.txt:2 | retracted after re-reading the caller\n", raw="raw text")
    report.store(review, a_target(), seq="001", reports_dir=reports_dir, config=config_with())
    body = Path(review.report).read_text()
    assert "Reversals of earlier rounds (SUPERSEDES)" in body
    assert "retracted after re-reading the caller" in body


def test_oscillating_points_are_carried_back_in_the_reason() -> None:
    review = a_review(oscillating="- `loop.py` (severity medium): reversed 2 time(s) via SUPERSEDES -- raised in round(s) with seq 1, 2, 3\n")
    text = report.reason(review, "h", config=config_with())
    assert "Oscillating points" in text
    assert "not a new finding" in text
    assert "loop.py" in text


def test_no_oscillating_section_when_the_review_has_none() -> None:
    assert "Oscillating" not in report.reason(a_review(), "h", config=config_with())


def test_the_report_path_is_offered_when_there_is_one() -> None:
    assert "\nFull report: /state/001.md\n" in report.reason(a_review(report="/state/001.md"), "h", config=config_with())
    assert "Full report:" not in report.reason(a_review(), "h", config=config_with())


# --------------------------------------------------------------------------
# Listing and printing
# --------------------------------------------------------------------------


def store_seq(reports_dir: Path, seq: str, verdict: str) -> Path:
    return report.store(Review(verdict=verdict), a_target(), seq=seq, reports_dir=reports_dir, config=config_with())


def test_reports_list_oldest_first(reports_dir: Path) -> None:
    store_seq(reports_dir, "002", "APPROVED")
    store_seq(reports_dir, "001", "CHANGES_REQUIRED")

    assert report.list_reports(reports_dir) == ["001-phase1-changes_required.md", "002-phase1-approved.md"]


def test_a_run_with_no_reports_lists_nothing(reports_dir: Path) -> None:
    assert report.list_reports(reports_dir) == []
    assert report.render(reports_dir) == "No reports have been produced for this run yet.\n"


def test_the_newest_report_is_the_default(reports_dir: Path) -> None:
    store_seq(reports_dir, "001", "CHANGES_REQUIRED")
    store_seq(reports_dir, "002", "APPROVED")

    assert "# Review 002 (phase1)" in report.render(reports_dir)


def test_a_report_can_be_asked_for_by_number(reports_dir: Path) -> None:
    store_seq(reports_dir, "001", "CHANGES_REQUIRED")
    store_seq(reports_dir, "002", "APPROVED")

    assert "# Review 001 (phase1)" in report.render(reports_dir, 1)


def test_an_unknown_number_lists_what_there_is(reports_dir: Path) -> None:
    store_seq(reports_dir, "001", "APPROVED")
    text = report.render(reports_dir, 9)

    assert text.startswith("No such report. Available:\n")
    assert "001-phase1-approved.md" in text


def test_rendering_a_report_never_touches_stdout(reports_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2: this runs under hooks whose stdout is the protocol response."""
    store_seq(reports_dir, "001", "APPROVED")
    report.render(reports_dir)
    report.reason(a_review(), "h", config=config_with())
    assert capsys.readouterr().out == ""


def test_the_report_listing_matches_the_shell(reports_dir: Path, git_repo: Path) -> None:
    """Both implementations must find the same files, since one may have written them."""
    store_seq(reports_dir, "001", "APPROVED")
    # `-exec basename {} \;` rather than `-printf %f`: the latter is a GNU extension that BSD
    # find (macOS) does not have, and this test only needs the leaf names.
    listing = subprocess.run(
        ["find", str(reports_dir), "-maxdepth", "1", "-name", "*.md", "-exec", "basename", "{}", ";"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert sorted(listing.stdout.split()) == report.list_reports(reports_dir)
    assert git(git_repo, "status", "--porcelain") == "", "reports never land in the repository under review"


# --------------------------------------------------------------------------
# Deferred findings
# --------------------------------------------------------------------------

DEFERRED_LINE = "FINDING severity=medium actionable=yes file=untouched.py:3 | new medium in an untouched file\n"


def test_deferred_text_is_empty_when_nothing_was_deferred() -> None:
    assert report.deferred_text(a_review(), what="commit") == ""


def test_deferred_text_names_what_was_approved_and_quotes_every_line() -> None:
    text = report.deferred_text(a_review(deferred=DEFERRED_LINE), what="commit")
    assert text.startswith("Deferred findings")
    assert "did not block this commit" in text
    assert "if this phase is reviewed again they will block" in text
    assert text.endswith(DEFERRED_LINE)


def test_the_stored_report_has_a_deferred_section_only_when_there_is_one(reports_dir: Path) -> None:
    approved = a_review(verdict="APPROVED", findings="", deferred=DEFERRED_LINE)
    text = report.render_report(approved, a_target(), seq="001", config=config_with())
    assert "## Deferred findings" in text
    assert DEFERRED_LINE in text
    assert "- late_block_severity: `high`" in text

    plain = report.render_report(a_review(), a_target(), seq="001", config=config_with())
    assert "## Deferred findings" not in plain


def test_a_final_report_does_not_claim_a_late_rule(reports_dir: Path) -> None:
    text = report.render_report(a_review(), a_target(scope="final"), seq="001", config=config_with())
    assert "late_block_severity" not in text


def test_the_reason_carries_deferred_lines_on_a_blocking_round() -> None:
    text = report.reason(a_review(deferred=DEFERRED_LINE), "headline", config=config_with())
    assert "Deferred findings" in text
    assert DEFERRED_LINE in text
    assert text.index("Blocking findings") < text.index("Deferred findings") < text.index("Reviewer prose")


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def test_a_report_says_what_the_round_cost(reports_dir: Path) -> None:
    """The figures were always in the CLI's output; `_reduce_transcript` moved them into an
    `.envelope` nobody reads. A round that costs several dollars should say so in its report."""
    review = Review(
        verdict="APPROVED",
        usage=harness.Usage(
            turns=50, cache_creation_tokens=189352, cache_read_tokens=5649958, output_tokens=31659, cost_usd=5.576844, duration_ms=460123
        ),
    )
    text = report.store(review, a_target(), seq="001", reports_dir=reports_dir, config=config_with()).read_text()

    assert "- cost: $5.58" in text
    assert "50 turns" in text
    assert "189k new + 5,650k cached input" in text, "the ratio that explains the bill has to be readable at a glance"
    assert "32k output" in text
    assert "460s" in text


def test_a_harness_that_reports_no_cost_gets_no_cost_line(reports_dir: Path) -> None:
    """An OpenCode review, a run under the test seam and every failed run land here. A `$0.00`
    would claim the review was free, when what is true is that its cost was never reported."""
    text = report.store(Review(verdict="APPROVED"), a_target(), seq="001", reports_dir=reports_dir, config=config_with()).read_text()

    assert "- cost:" not in text


def test_a_partially_reported_cost_prints_only_what_was_reported(reports_dir: Path) -> None:
    review = Review(verdict="APPROVED", usage=harness.Usage(turns=12))
    text = report.store(review, a_target(), seq="001", reports_dir=reports_dir, config=config_with()).read_text()

    assert "- cost: 12 turns\n" in text
    assert "$" not in text.split("- cost:")[1].split("\n")[0]
