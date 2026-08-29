"""Report storage and the text Claude is actually handed.

The thing under test is what survives: **every** ``FINDING`` line comes back inline, and
prose is the only part allowed to be cut. A finding trimmed for length is a finding that
never gets fixed, and the loop would then block forever on evidence the model was never
shown.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import git

from ocrl import config as ocrl_config
from ocrl import report, state
from ocrl.config import Config
from ocrl.reviewer import Review, Target
from ocrl.util import TRUNCATION_MARKER

SESSION = "repsess"

#: Reviewer output that is not valid UTF-8, which the report must keep byte for byte.
INVALID_UTF8 = b"tool output: \xff\xfe\n"


@pytest.fixture
def report_env(clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key in list(os.environ):
        if key.startswith(("OCRL_", "XDG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in clean_env.items():
        monkeypatch.setenv(key, value)
    return clean_env


@pytest.fixture
def act_dir(report_env: dict[str, str]) -> Path:
    st = state.State("/wt", SESSION)
    st.new()
    st.save()
    return st.act_dir


def a_target(scope: str = "phase", phase: int = 1, base: str = "b", head: str = "h") -> Target:
    return Target(repo="/wt", base=base, head=head, scope=scope, phase=phase)


def config_with(**overrides: object) -> Config:
    return Config({**ocrl_config.DEFAULTS, **overrides})


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


def test_the_filename_carries_the_verdict(act_dir: Path) -> None:
    review = a_review()
    path = report.store(review, a_target("phase", 2, "b", "h"), seq="007", act_dir=act_dir, config=config_with())

    assert path.name == "007-phase2-changes_required.md"
    assert review.report == str(path)


def test_a_final_review_is_labelled_final(act_dir: Path) -> None:
    path = report.store(a_review(verdict="APPROVED"), a_target("final", 3, "b", "h"), seq="001", act_dir=act_dir, config=config_with())
    assert path.name == "001-final-approved.md"


def test_a_verdictless_review_is_still_stored(act_dir: Path) -> None:
    """A report that cannot be named after a verdict is evidence, not something to drop."""
    path = report.store(Review(), a_target("phase", 1, "b", "h"), seq="001", act_dir=act_dir, config=config_with())
    assert path.name == "001-phase1-unknown.md"
    assert "**UNKNOWN**" in path.read_text()


def test_the_report_records_what_decided_it(act_dir: Path) -> None:
    review = a_review(error="the reviewer timed out after 900s")
    text = report.store(
        review, a_target(base="basetree", head="headtree"), seq="001", act_dir=act_dir, config=config_with(variant="thinking")
    ).read_text()

    assert "- verdict (recomputed by the gate): **CHANGES_REQUIRED**\n" in text
    assert "- base tree: `basetree`\n" in text
    assert "- head tree: `headtree`\n" in text
    assert "- model: `openai/gpt-5.6-sol` (variant `thinking`)\n" in text
    assert "- block_severity: `medium`\n" in text
    assert "- gate note: the reviewer timed out after 900s\n" in text
    assert "Returns success on a failed lookup" in text


def test_a_clean_review_says_so_rather_than_leaving_the_section_empty(act_dir: Path) -> None:
    text = report.store(Review(verdict="APPROVED"), a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()
    assert "## Blocking findings\n\n(none)\n" in text
    assert "- gate note:" not in text


def test_the_raw_reviewer_output_is_embedded(act_dir: Path, tmp_path: Path) -> None:
    raw = tmp_path / "reviewer.out"
    raw.write_text("everything the reviewer said\n")
    text = report.store(a_review(raw=str(raw)), a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()

    assert "## Raw reviewer output\n\n````\neverything the reviewer said\n\n````\n" in text


def test_output_that_is_not_valid_utf8_is_kept_byte_for_byte(act_dir: Path, tmp_path: Path) -> None:
    """One stray byte must not cost the whole report -- it is what a denial points at."""
    raw = tmp_path / "reviewer.out"
    raw.write_bytes(INVALID_UTF8)

    path = report.store(a_review(raw=str(raw)), a_target("phase", 1, "b", "h"), seq="001", act_dir=act_dir, config=config_with())
    assert INVALID_UTF8 in path.read_bytes()


def test_a_missing_raw_file_does_not_stop_the_report(act_dir: Path, tmp_path: Path) -> None:
    path = report.store(a_review(raw=str(tmp_path / "gone")), a_target(), seq="001", act_dir=act_dir, config=config_with())
    assert path.is_file()


# --------------------------------------------------------------------------
# Session continuity
# --------------------------------------------------------------------------


def test_a_continued_review_shows_its_session_and_round(act_dir: Path) -> None:
    review = a_review(session="ses_abc12345", round=2)
    text = report.store(review, a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()
    assert "- opencode session: `ses_abc12345` (round 2, continued)\n" in text


def test_a_fresh_sessions_first_round_is_not_called_continued(act_dir: Path) -> None:
    review = a_review(session="ses_abc12345", round=1)
    text = report.store(review, a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()
    assert "- opencode session: `ses_abc12345` (round 1)\n" in text
    assert "continued" not in text


def test_a_cold_review_shows_no_session_line(act_dir: Path) -> None:
    text = report.store(a_review(session=""), a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()
    assert "opencode session" not in text


def test_both_verdicts_are_rendered_when_a_continued_approval_was_cold_confirmed(act_dir: Path, tmp_path: Path) -> None:
    """The cold-approval invariant's report side: a reader must be able to tell the acted-on
    verdict apart from the round that triggered it.

    The heading is deliberately not "continued round": the invariant covers every round shown
    model-influenced context, and a *fresh* round attached ``prior-rounds.txt`` is cold-confirmed
    on exactly the same footing as a continued one (``reviewer.execute``)."""
    continued_raw = tmp_path / "continued.out"
    continued_raw.write_text("the continued round's own transcript\n")
    cold_raw = tmp_path / "cold.out"
    cold_raw.write_text("the cold confirmation's own transcript\n")

    continued = a_review(
        verdict="APPROVED",
        session="ses_abc12345",
        round=3,
        raw=str(continued_raw),
        findings="",
        all_findings="",
    )
    cold = a_review(
        verdict="CHANGES_REQUIRED",
        session="",
        round=0,
        raw=str(cold_raw),
        confirmed=continued,
    )

    text = report.store(cold, a_target(), seq="001", act_dir=act_dir, config=config_with()).read_text()

    assert "- verdict (recomputed by the gate): **CHANGES_REQUIRED**\n" in text
    assert "## Round with context (not the verdict acted on)" in text
    assert "## Cold confirmation (the verdict acted on)" in text
    assert "ses_abc12345" in text
    assert "(round 3, continued)" in text
    assert "the continued round's own transcript" in text
    assert "the cold confirmation's own transcript" in text
    assert "the verdict acted on" in text
    # The top-level session line is only for a single-invocation report -- the two-invocation
    # case tells the session story inside the labelled sections instead.
    assert text.split("## Round with context (not the verdict acted on)")[0].count("opencode session") == 0


def test_the_report_is_private(act_dir: Path) -> None:
    """Reports quote the diff and the plan; they are not world-readable."""
    path = report.store(a_review(), a_target("phase", 1, "b", "h"), seq="001", act_dir=act_dir, config=config_with())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


# --------------------------------------------------------------------------
# The denial message
# --------------------------------------------------------------------------


def test_the_reason_leads_with_the_headline_and_ends_with_the_instruction() -> None:
    text = report.reason(a_review(), "opencode-review-loop: changes required.", config=config_with())

    assert text.startswith("opencode-review-loop: changes required.\n")
    assert text.endswith("Verify and address the findings above, then commit again. The commit is gated until the review passes.\n")


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


def test_the_gate_note_is_carried_back() -> None:
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


def test_supersedes_lines_are_rendered_in_the_stored_report(act_dir: Path) -> None:
    review = a_review(supersedes="SUPERSEDES round=1 file=b.txt:2 | retracted after re-reading the caller\n", raw="raw text")
    report.store(review, a_target(), seq="001", act_dir=act_dir, config=config_with())
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


def store_seq(act_dir: Path, seq: str, verdict: str) -> Path:
    return report.store(Review(verdict=verdict), a_target(), seq=seq, act_dir=act_dir, config=config_with())


def test_reports_list_oldest_first(act_dir: Path) -> None:
    store_seq(act_dir, "002", "APPROVED")
    store_seq(act_dir, "001", "CHANGES_REQUIRED")

    assert report.list_reports(act_dir) == ["001-phase1-changes_required.md", "002-phase1-approved.md"]


def test_an_activation_with_no_reports_lists_nothing(act_dir: Path) -> None:
    assert report.list_reports(act_dir) == []
    assert report.render(act_dir) == "No reports have been produced for this activation yet.\n"


def test_the_newest_report_is_the_default(act_dir: Path) -> None:
    store_seq(act_dir, "001", "CHANGES_REQUIRED")
    store_seq(act_dir, "002", "APPROVED")

    assert "# OpenCode review 002 (phase1)" in report.render(act_dir)


def test_a_report_can_be_asked_for_by_number(act_dir: Path) -> None:
    store_seq(act_dir, "001", "CHANGES_REQUIRED")
    store_seq(act_dir, "002", "APPROVED")

    assert "# OpenCode review 001 (phase1)" in report.render(act_dir, 1)


def test_an_unknown_number_lists_what_there_is(act_dir: Path) -> None:
    store_seq(act_dir, "001", "APPROVED")
    text = report.render(act_dir, 9)

    assert text.startswith("No such report. Available:\n")
    assert "001-phase1-approved.md" in text


def test_rendering_a_report_never_touches_stdout(act_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 2: this runs under hooks whose stdout is the protocol response."""
    store_seq(act_dir, "001", "APPROVED")
    report.render(act_dir)
    report.reason(a_review(), "h", config=config_with())
    assert capsys.readouterr().out == ""


def test_the_report_listing_matches_the_shell(report_env: dict[str, str], act_dir: Path, git_repo: Path) -> None:
    """Both implementations must find the same files, since one may have written them."""
    store_seq(act_dir, "001", "APPROVED")
    listing = subprocess.run(
        ["find", str(act_dir / "reports"), "-maxdepth", "1", "-name", "*.md", "-printf", "%f\n"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert sorted(listing.stdout.split()) == report.list_reports(act_dir)
    assert git(git_repo, "status", "--porcelain") == "", "reports never land in the repository under review"


# --------------------------------------------------------------------------
# The clarify hint
# --------------------------------------------------------------------------


def a_state(*, clarifications: int) -> state.State:
    """The activation the ``act_dir`` fixture created, with the clarify counter set."""
    st = state.State("/wt", SESSION)
    st.load()
    st.update(clarifications=clarifications)
    return st


def test_the_clarify_hint_names_the_remaining_budget(act_dir: Path) -> None:
    hint = report.clarify_hint(state=a_state(clarifications=1), config=config_with(max_clarifications=3))
    assert "Clarifications left: 2 of 3." in hint
    assert 'clarify --question "..."' in hint
    assert hint.endswith("3.")


def test_the_clarify_hint_is_a_single_line(act_dir: Path) -> None:
    """It is appended as its own line; a hint with newlines of its own would break that up."""
    assert "\n" not in report.clarify_hint(state=a_state(clarifications=0), config=config_with())


def test_the_clarify_hint_is_empty_once_the_allowance_is_spent(act_dir: Path) -> None:
    config = config_with(max_clarifications=2)
    assert report.clarify_hint(state=a_state(clarifications=2), config=config) == ""
    # A counter past the limit (a lowered `max_clarifications` on a resumed activation) must
    # not wrap round to a positive "left".
    assert report.clarify_hint(state=a_state(clarifications=5), config=config) == ""
    assert report.clarify_hint(state=a_state(clarifications=0), config=config_with(max_clarifications=0)) == ""


def test_with_clarify_hint_leaves_a_headline_alone_when_the_allowance_is_spent(act_dir: Path) -> None:
    config = config_with(max_clarifications=1)
    assert report.with_clarify_hint("headline", state=a_state(clarifications=1), config=config) == "headline"
    assert report.with_clarify_hint("headline", state=a_state(clarifications=0), config=config).startswith("headline\n\nIf a finding")


def test_the_clarify_hint_names_the_entrypoint_the_gate_accepts(act_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugins/ocrl")
    hint = report.clarify_hint(state=a_state(clarifications=0), config=config_with())
    assert '`/plugins/ocrl/scripts/ocrl.sh clarify --question "..."`' in hint


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


def test_the_stored_report_has_a_deferred_section_only_when_there_is_one(act_dir: Path) -> None:
    approved = a_review(verdict="APPROVED", findings="", deferred=DEFERRED_LINE)
    text = report.render_report(approved, a_target(), seq="001", config=config_with())
    assert "## Deferred findings" in text
    assert DEFERRED_LINE in text
    assert "- late_block_severity: `high`" in text

    plain = report.render_report(a_review(), a_target(), seq="001", config=config_with())
    assert "## Deferred findings" not in plain


def test_a_final_report_does_not_claim_a_late_rule(act_dir: Path) -> None:
    text = report.render_report(a_review(), a_target(scope="final"), seq="001", config=config_with())
    assert "late_block_severity" not in text


def test_the_reason_carries_deferred_lines_on_a_blocking_round() -> None:
    text = report.reason(a_review(deferred=DEFERRED_LINE), "headline", config=config_with())
    assert "Deferred findings" in text
    assert DEFERRED_LINE in text
    assert text.index("Blocking findings") < text.index("Deferred findings") < text.index("Reviewer prose")
