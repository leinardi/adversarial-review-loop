"""The reviewer output contract: the findings block, the recomputed verdict, the late-round
scope, ``SUPERSEDES`` and a clarify reply's retraction block.

No reviewer output the contract does not allow produces ``APPROVED``.
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

from pathlib import Path

import pytest
from conftest import config_with
from reviewer_common import MODE_VERDICTS, contract_text, fake_reviewer_output

from arl import config as arl_config
from arl import contract
from arl.config import Config
from arl.contract import Review

# --------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "verdict"), MODE_VERDICTS)
def test_each_reviewer_shape_reaches_the_right_verdict(tmp_path: Path, mode: str, verdict: str) -> None:
    out = fake_reviewer_output(tmp_path, mode)
    assert contract.parse(out, config=config_with()).verdict == verdict


@pytest.mark.parametrize(("mode", "verdict"), MODE_VERDICTS)
def test_no_reviewer_shape_produces_an_approval_it_did_not_earn(tmp_path: Path, mode: str, verdict: str) -> None:
    """Rule 1, stated as its own assertion so a future change cannot quietly relax it."""
    parsed = contract.parse(fake_reviewer_output(tmp_path, mode), config=config_with())
    if verdict != "APPROVED":
        assert parsed.verdict != "APPROVED"
        assert parsed.error or parsed.findings


def test_missing_markers_are_a_failure(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_text("VERDICT APPROVED\n")
    parsed = contract.parse(out, config=config_with())
    assert parsed.verdict == "OP_FAILURE"
    assert "missing the <<<ARL-FINDINGS>>> / <<<ARL-END>>> markers" in parsed.error


@pytest.mark.parametrize(
    "payload",
    [
        b"prose\n<<<ARL-FINDINGS>>>\nFINDING severity=critical actionable=n\0o file=a.txt:7 | Nil deref\nVERDICT APPROVED\n<<<ARL-END>>>\n",
        b"prose\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n\0",
        b"\0prose\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n",
    ],
)
def test_output_carrying_a_nul_byte_is_refused(tmp_path: Path, payload: bytes) -> None:
    """The shell cannot hold a NUL: command substitution deletes it.

    ``actionable=n\0o`` therefore reached the shell's validation as a valid, non-blocking
    ``actionable=no``, and the reviewer's own APPROVED stood over a critical finding. Python
    would have rejected the corrupted line on its own; the explicit refusal is what keeps
    the two gates agreeing about what a byte sequence means.
    """
    out = tmp_path / "o"
    out.write_bytes(payload)
    parsed = contract.parse(out, config=config_with())

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer output contains a NUL byte, so the contract cannot be validated"


def test_a_nul_byte_inside_a_finding_line_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_bytes(b"prose\n<<<ARL-FINDINGS>>>\nFINDING severity=critical actionable=n\0o file=a | x\nVERDICT APPROVED\n<<<ARL-END>>>\n")

    mine = contract.parse(out, config=config_with())

    assert mine.verdict == "OP_FAILURE"


def test_a_missing_output_file_is_a_failure(tmp_path: Path) -> None:
    parsed = contract.parse(tmp_path / "never-written", config=config_with())
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer produced no output"
    assert parsed.kind == "contract"


def test_missing_markers_carry_kind_contract(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_text("VERDICT APPROVED\n")
    assert contract.parse(out, config=config_with()).kind == "contract"


def test_a_nul_refusal_carries_kind_contract(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_bytes(b"\0prose\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n")
    assert contract.parse(out, config=config_with()).kind == "contract"


def test_an_unrecognised_verdict_carries_kind_contract(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("VERDICT MAYBE"))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.kind == "contract"


def test_output_that_is_not_valid_utf8_is_refused(tmp_path: Path) -> None:
    """``_decode`` is ``surrogateescape``; a lone surrogate that reached ``round_history``
    could not be encoded when ``state.json`` is saved and would crash the whole review. A
    UTF-8 text protocol that is not valid UTF-8 fails the contract, like a NUL byte."""
    out = tmp_path / "o"
    out.write_bytes(b"prose\n<<<ARL-FINDINGS>>>\nFINDING severity=high actionable=yes file=a | bad \xff byte\nVERDICT APPROVED\n<<<ARL-END>>>\n")

    parsed = contract.parse(out, config=config_with())

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer output is not valid UTF-8, so the contract cannot be validated"


def parse_text(tmp_path: Path, text: str, config: Config | None = None) -> Review:
    out = tmp_path / "o"
    out.write_text(text)
    return contract.parse(out, config=config or config_with())


def test_an_unlabelled_severity_is_a_contract_failure(tmp_path: Path) -> None:
    """Omitting the field is not a way under the threshold, and not a finding to drop."""
    parsed = parse_text(tmp_path, contract_text("FINDING actionable=yes file=a.txt:1 | No severity given", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error.startswith("the reviewer emitted a line the contract does not allow: FINDING actionable=yes")


@pytest.mark.parametrize("severity", ["spicy", "CRITICAL", "sev5", ""])
def test_a_severity_outside_the_documented_set_is_a_contract_failure(tmp_path: Path, severity: str) -> None:
    parsed = parse_text(tmp_path, contract_text(f"FINDING severity={severity} actionable=yes file=a.txt:1 | Odd label", "VERDICT APPROVED"))
    assert parsed.verdict == "OP_FAILURE"


def test_an_actionable_finding_blocks(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("FINDING severity=high actionable=yes file=a | x", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


@pytest.mark.parametrize("value", ["YES", "Yes", "true", "1", "maybe", "unknown", ""])
def test_an_actionable_field_the_gate_cannot_read_never_approves(tmp_path: Path, value: str) -> None:
    """The gate cannot tell a typo from a finding it failed to understand (Rule 1).

    Every one of these used to be read as "not actionable", so a ``critical`` finding was
    dropped and the reviewer's own ``APPROVED`` stood.
    """
    parsed = parse_text(tmp_path, contract_text(f"FINDING severity=critical actionable={value} file=a | x", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.findings == "" and parsed.all_findings == ""


def test_actionable_no_is_recorded_without_blocking(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("FINDING severity=critical actionable=no file=a | x", "VERDICT APPROVED"))
    assert parsed.verdict == "APPROVED"
    assert parsed.all_findings and not parsed.findings


@pytest.mark.parametrize(
    "line",
    [
        "FINDING severity=high actionable=yes file=a",
        "FINDING severity=high actionable=yes file=a |",
        "FINDING severity=high actionable=yes file= | x",
        "FINDING severity=high file=a | x",
        "FINDING: severity=high actionable=yes file=a | x",
        "finding severity=high actionable=yes file=a | x",
        "FINDING severity=high actionable=yes file=a | x extra=1 severity=low",
    ],
)
def test_only_the_documented_finding_shape_is_accepted(tmp_path: Path, line: str) -> None:
    """The last case is legal -- trailing text is detail -- and is here to pin that down."""
    parsed = parse_text(tmp_path, contract_text(line, "VERDICT APPROVED"))
    if line.endswith("severity=low"):
        assert parsed.verdict == "CHANGES_REQUIRED"
    else:
        assert parsed.verdict == "OP_FAILURE"


def test_a_path_with_spaces_is_still_a_finding(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("FINDING severity=high actionable=yes file=my file.txt:1 | x", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


def test_a_line_the_contract_does_not_allow_fails_the_review(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("Nothing worth reporting, honestly.", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted a line the contract does not allow: Nothing worth reporting, honestly."


def test_the_echoed_line_is_bounded(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("z" * 5000, "VERDICT APPROVED"))
    assert len(parsed.error) < 200


def test_blank_lines_inside_the_block_are_allowed(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("", "   ", "VERDICT APPROVED"))
    assert parsed.verdict == "APPROVED"


def test_a_stray_end_marker_above_the_block_never_approves(tmp_path: Path) -> None:
    """The sed range took the first opening marker, so findings above it simply vanished."""
    text = (
        "prose\n"
        f"{contract.END_MARKER}\n"
        "FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref\n"
        f"{contract.FINDINGS_MARKER}\n"
        "VERDICT APPROVED\n"
        f"{contract.END_MARKER}\n"
    )
    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "OP_FAILURE"
    assert "exactly one" in parsed.error


def test_two_marker_blocks_never_approve(tmp_path: Path) -> None:
    text = contract_text("FINDING severity=critical actionable=yes file=a | boom") + contract_text("VERDICT APPROVED")
    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "OP_FAILURE"
    assert "exactly one" in parsed.error


@pytest.mark.parametrize(
    "marker_line",
    [
        "prose <<<ARL-FINDINGS>>> trailing",
        "<<<ARL-FINDINGS>>> trailing",
        "> <<<ARL-FINDINGS>>>",
        "`<<<ARL-FINDINGS>>>`",
    ],
)
def test_a_marker_buried_in_a_line_does_not_open_the_block(tmp_path: Path, marker_line: str) -> None:
    """Substring matching let a contract smuggled into a sentence parse as the real one."""
    parsed = parse_text(tmp_path, f"{marker_line}\nVERDICT APPROVED\n{contract.END_MARKER}\n")

    assert parsed.verdict == "OP_FAILURE"
    assert "missing the" in parsed.error


@pytest.mark.parametrize("marker_line", ["<<<ARL-END>>> and more", "text <<<ARL-END>>>"])
def test_a_buried_end_marker_does_not_close_the_block(tmp_path: Path, marker_line: str) -> None:
    parsed = parse_text(tmp_path, f"prose\n{contract.FINDINGS_MARKER}\nVERDICT APPROVED\n{marker_line}\n")
    assert parsed.verdict == "OP_FAILURE"


@pytest.mark.parametrize("pad", ["", "  ", "\t"])
def test_surrounding_whitespace_on_a_marker_is_tolerated(tmp_path: Path, pad: str) -> None:
    parsed = parse_text(tmp_path, f"prose\n{pad}{contract.FINDINGS_MARKER}{pad}\nVERDICT APPROVED\n{pad}{contract.END_MARKER}{pad}\n")
    assert parsed.verdict == "APPROVED"


def test_one_line_holding_both_markers_never_approves(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, f"prose\n{contract.FINDINGS_MARKER} {contract.END_MARKER}\nVERDICT APPROVED\n")
    assert parsed.verdict == "OP_FAILURE"


def test_the_threshold_is_applied(tmp_path: Path) -> None:
    text = contract_text(
        "FINDING severity=medium actionable=yes file=a | below",
        "FINDING severity=high actionable=yes file=b | at",
        "VERDICT APPROVED",
    )
    parsed = parse_text(tmp_path, text, config_with(block_severity="high"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert parsed.findings == "FINDING severity=high actionable=yes file=b | at\n"
    assert parsed.all_findings.count("FINDING") == 2


def test_a_critical_block_severity_blocks_only_critical_findings(tmp_path: Path) -> None:
    """`critical` is a real fifth tier the reviewer contract's `FINDING` regex accepts, not a
    typo that should fall through `threshold_rank`'s unrecognised-value fallback (rank 1,
    which would block on everything instead of the critical-only threshold asked for)."""
    text = contract_text(
        "FINDING severity=high actionable=yes file=a | serious but not critical",
        "FINDING severity=critical actionable=yes file=b | critical",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="critical"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert parsed.findings == "FINDING severity=critical actionable=yes file=b | critical\n"


def test_an_unrecognised_block_severity_blocks_everything_rather_than_nothing(tmp_path: Path) -> None:
    """``severity_rank``'s "unrecognised ranks highest" rule is fail-*open* if it is applied
    to the threshold instead of the finding: an unknown ``block_severity`` would rank 5,
    above every real severity, so nothing would ever meet it and even a ``high`` actionable
    finding would sail through APPROVED. The threshold must use `threshold_rank`, whose
    fallback is the opposite direction (rank 1), so a typo'd or unrecognised threshold makes
    the gate block on everything rather than on nothing (Rule 1)."""
    text = contract_text(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="hihg"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert "severity=low" in parsed.findings


def test_an_actionable_low_finding_is_recorded_but_does_not_block_at_the_default_threshold(tmp_path: Path) -> None:
    """``block_severity`` defaults to ``medium``: an actionable ``low`` finding is real
    evidence, kept in ``all_findings``, but it no longer meets the threshold on its own."""
    text = contract_text(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "APPROVED"
    assert parsed.findings == ""
    assert "severity=low" in parsed.all_findings


def test_an_actionable_low_finding_still_blocks_when_the_threshold_is_lowered(tmp_path: Path) -> None:
    text = contract_text(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="low"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert "severity=low" in parsed.findings


def test_the_gate_actually_approves_an_actionable_low_finding_at_the_default_threshold(tmp_path: Path) -> None:
    """The regression this rubric change targets: a reviewer that emits an actionable ``low``
    finding alongside its own ``VERDICT APPROVED`` must have that verdict stand at the default
    threshold -- not merely have ``review.findings`` come back empty while some other path
    still forces ``CHANGES_REQUIRED``."""
    text = contract_text(
        "FINDING severity=low actionable=yes file=a.txt:1 | Could be named better",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "APPROVED"


def test_an_unrecognised_verdict_is_a_failure(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("VERDICT MAYBE"))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted an unrecognised verdict: MAYBE"


@pytest.mark.parametrize("line", ["VERDICT APPROVED", "  VERDICT: APPROVED", "VERDICT:APPROVED", "VERDICT   APPROVED   "])
def test_the_verdict_line_is_read_the_way_the_shell_read_it(tmp_path: Path, line: str) -> None:
    assert parse_text(tmp_path, contract_text(line)).verdict == "APPROVED"


@pytest.mark.parametrize(
    "verdicts",
    [("VERDICT APPROVED", "VERDICT CHANGES_REQUIRED"), ("VERDICT CHANGES_REQUIRED", "VERDICT APPROVED")],
)
def test_a_second_verdict_line_fails_the_review(tmp_path: Path, verdicts: tuple[str, str]) -> None:
    """Last-wins let a trailing APPROVED overrule the reviewer's own CHANGES_REQUIRED."""
    parsed = parse_text(tmp_path, contract_text(*verdicts))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted more than one VERDICT line"


def test_the_findings_cap_escalates_instead_of_trimming(tmp_path: Path) -> None:
    lines = [f"FINDING severity=low actionable=no file=a:{i} | finding {i}" for i in range(6)]
    parsed = parse_text(tmp_path, contract_text(*lines, "VERDICT APPROVED"), config_with(max_findings=5))

    assert parsed.verdict == "NEEDS_HUMAN"
    assert "above max_findings (5)" in parsed.error
    assert parsed.all_findings.count("FINDING") == 6, "the list is kept, not trimmed"


def test_the_findings_byte_cap_escalates(tmp_path: Path) -> None:
    parsed = parse_text(
        tmp_path, contract_text("FINDING severity=low actionable=no file=a | " + "x" * 500, "VERDICT APPROVED"), config_with(max_findings_bytes=100)
    )
    assert parsed.verdict == "NEEDS_HUMAN"
    assert "above max_findings_bytes (100)" in parsed.error


def test_prose_stops_at_the_marker(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract_text("VERDICT APPROVED"))
    assert parsed.prose == "prose line"


def test_a_carriage_return_does_not_split_a_finding(tmp_path: Path) -> None:
    """``grep``/``sed`` break on ``\n`` alone; ``str.splitlines`` also breaks on ``\r``."""
    parsed = parse_text(tmp_path, contract_text("FINDING severity=high actionable=yes file=a | x\ry", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


# --------------------------------------------------------------------------
# Late-round blocking scope (late_block_severity)
# --------------------------------------------------------------------------


def _scope(changed: tuple[str, ...] = (), prior: tuple[str, ...] = ()) -> contract.LateScope:
    return contract.LateScope(changed_paths=frozenset(changed), prior_files=frozenset(prior))


def test_scope_covers_a_changed_path_exactly_and_by_line_stripping() -> None:
    scope = _scope(changed=("src/a.py",))
    assert scope.covers("src/a.py")
    assert scope.covers("src/a.py:42")
    assert scope.covers("./src/a.py:42"), "a leading ./ is normalised on the finding side only"
    assert not scope.covers("src/b.py:42")
    assert not scope.covers("src/a.py.bak")


def test_scope_matches_a_changed_file_literally_named_with_a_colon_suffix() -> None:
    """A file called ``x:1`` matches exactly; the line-stripping fallback runs only after."""
    scope = _scope(changed=("x:1",))
    assert scope.covers("x:1")
    assert not scope.covers("x"), "the changed path is `x:1`, not `x`"
    assert scope.covers("x:1:7"), "`x:1` at line 7"


def test_scope_always_covers_a_finding_with_no_location() -> None:
    assert _scope().covers("-"), "a missing location must never widen a bypass"


def test_scope_covers_a_prior_round_file_exactly_and_line_stripped() -> None:
    scope = _scope(prior=("lib/x.go:10", "lib/x.go"))
    assert scope.covers("lib/x.go:10")
    assert scope.covers("lib/x.go:99"), "re-raised at another line of a file an earlier round named"
    assert scope.covers("lib/x.go")


def test_scope_matches_a_dot_slash_finding_against_its_normalised_prior_file() -> None:
    """Both sides go through the same normalisation, so a value always matches itself."""
    scope = _scope(prior=("README.md:4", "README.md"))
    assert scope.covers("./README.md:4")
    assert scope.covers("README.md:4")
    assert scope.covers("./README.md:9"), "another line of a file an earlier round named"


def _write_block(tmp_path: Path, *findings: str, verdict: str = "APPROVED") -> Path:
    out = tmp_path / "late.out"
    body = "prose\n\n<<<ARL-FINDINGS>>>\n" + "".join(f"{line}\n" for line in findings) + f"VERDICT {verdict}\n<<<ARL-END>>>\n"
    out.write_text(body)
    return out


MEDIUM_UNTOUCHED = "FINDING severity=medium actionable=yes file=untouched.py:3 | new medium in an untouched file"
MEDIUM_CHANGED = "FINDING severity=medium actionable=yes file=changed.py:3 | medium in a changed file"
HIGH_UNTOUCHED = "FINDING severity=high actionable=yes file=untouched.py:3 | high anywhere"
MEDIUM_PRIOR = "FINDING severity=medium actionable=yes file=known.py:77 | re-raised at another line"


def test_parse_without_a_scope_blocks_every_finding_at_or_above_block_severity(tmp_path: Path) -> None:
    review = contract.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config_with(), allow_supersedes=True)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_UNTOUCHED}\n"
    assert review.deferred == ""


def test_parse_with_a_scope_defers_a_new_medium_outside_the_changed_paths(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",), prior=("known.py:1", "known.py"))
    review = contract.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "APPROVED"
    assert review.findings == ""
    assert review.deferred == f"{MEDIUM_UNTOUCHED}\n"
    assert review.all_findings == f"{MEDIUM_UNTOUCHED}\n", "deferred is still reported and recorded"


def test_parse_with_a_scope_blocks_a_medium_in_a_changed_path(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    review = contract.parse(_write_block(tmp_path, MEDIUM_CHANGED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_CHANGED}\n"
    assert review.deferred == ""


def test_parse_with_a_scope_blocks_a_high_anywhere(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    review = contract.parse(_write_block(tmp_path, HIGH_UNTOUCHED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{HIGH_UNTOUCHED}\n"


def test_parse_with_a_scope_blocks_a_medium_in_a_prior_round_file(tmp_path: Path) -> None:
    scope = _scope(changed=(), prior=("known.py:1", "known.py"))
    review = contract.parse(_write_block(tmp_path, MEDIUM_PRIOR), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_PRIOR}\n"


def test_late_block_severity_medium_restores_the_ordinary_rule(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    config = config_with(late_block_severity="medium")
    review = contract.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config, allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.deferred == ""


def test_late_block_severity_below_block_severity_is_clamped_up(tmp_path: Path) -> None:
    """``late_block_severity`` can only defer, never widen: set below ``block_severity`` it
    reads as ``block_severity`` itself, so a *low* finding still does not block."""
    scope = _scope(changed=("changed.py",))
    config = config_with(block_severity="high", late_block_severity="low")
    assert arl_config.late_threshold_rank(config) == arl_config.threshold_rank("high")
    review = contract.parse(_write_block(tmp_path, MEDIUM_CHANGED), config=config, allow_supersedes=True, scope=scope)
    assert review.verdict == "APPROVED"
    assert review.deferred == "", "below block_severity is not deferred, it is simply non-blocking"


def test_the_reviewers_own_changes_required_still_wins_over_a_deferred_only_set(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    out = _write_block(tmp_path, MEDIUM_UNTOUCHED, verdict="CHANGES_REQUIRED")
    review = contract.parse(out, config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED", "stricter wins; the scope narrows the gate's recomputation only"
    assert review.findings == ""
    assert review.deferred == f"{MEDIUM_UNTOUCHED}\n"


def test_a_scope_never_defers_a_finding_below_block_severity_into_the_deferred_set(tmp_path: Path) -> None:
    low = "FINDING severity=low actionable=yes file=untouched.py:1 | low"
    review = contract.parse(_write_block(tmp_path, low), config=config_with(), allow_supersedes=True, scope=_scope(changed=("x",)))
    assert review.verdict == "APPROVED"
    assert review.deferred == ""
    assert review.all_findings == f"{low}\n"


# --------------------------------------------------------------------------
# SUPERSEDES
# --------------------------------------------------------------------------


SUPERSEDES_OK = [
    "SUPERSEDES round=1 file=a.txt:9 | the null case cannot occur here",
    "SUPERSEDES round=12 file=pkg/x.go | retracted after reading the caller",
    "SUPERSEDES round=3 file=- | an earlier round misread the frozen plan",
    "SUPERSEDES round=1 file=a b/c.txt:2 | a path with spaces is still a location",
]
SUPERSEDES_BAD = [
    "SUPERSEDES file=a.txt:9 | no round number",
    "SUPERSEDES round= file=a.txt:9 | empty round",
    "SUPERSEDES round=x file=a.txt:9 | non-numeric round",
    "SUPERSEDES round=1 | no file clause at all",
    "SUPERSEDES round=1 file=a.txt:9 |",
    "SUPERSEDES round=1 file=a.txt:9",
    "SUPERSEDES: round=1 file=a.txt:9 | a stray colon",
    "  SUPERSEDES round=1 file=a.txt:9 | leading whitespace",
    "SUPERSEDESX round=1 file=a.txt:9 | wrong keyword",
]


@pytest.mark.parametrize("line", SUPERSEDES_OK)
def test_the_supersedes_grammar_accepts_a_well_formed_line(line: str) -> None:
    assert contract._SUPERSEDES_RE.match(line) is not None


@pytest.mark.parametrize("line", SUPERSEDES_BAD)
def test_the_supersedes_grammar_rejects_a_malformed_line(line: str) -> None:
    assert contract._SUPERSEDES_RE.match(line) is None


def _block(*body: str) -> bytes:
    return ("Prose first.\n\n<<<ARL-FINDINGS>>>\n" + "".join(f"{line}\n" for line in body) + "<<<ARL-END>>>\n").encode()


def test_a_supersedes_line_is_recorded_and_never_clears_a_blocking_finding(tmp_path: Path) -> None:
    out = tmp_path / "r.out"
    out.write_bytes(
        _block(
            "FINDING severity=high actionable=yes file=a.txt:1 | still broken",
            "SUPERSEDES round=1 file=b.txt:2 | retracting a different, earlier finding",
            "VERDICT CHANGES_REQUIRED",
        )
    )
    review = contract.parse(out, config=config_with(), allow_supersedes=True)
    assert review.verdict == "CHANGES_REQUIRED"
    assert "still broken" in review.findings
    assert review.supersedes == "SUPERSEDES round=1 file=b.txt:2 | retracting a different, earlier finding\n"


def test_a_supersedes_line_alongside_approved_does_not_flip_the_verdict(tmp_path: Path) -> None:
    out = tmp_path / "r.out"
    out.write_bytes(_block("SUPERSEDES round=1 file=a.txt:1 | round 1 was wrong; this is fine now", "VERDICT APPROVED"))
    review = contract.parse(out, config=config_with(), allow_supersedes=True)
    assert review.verdict == "APPROVED"
    assert review.supersedes.startswith("SUPERSEDES round=1 ")


def test_a_near_miss_of_the_supersedes_grammar_is_still_a_contract_failure(tmp_path: Path) -> None:
    out = tmp_path / "r.out"
    out.write_bytes(_block("SUPERCEDES round=1 file=a.txt:1 | a typo in the keyword", "VERDICT APPROVED"))
    assert contract.parse(out, config=config_with(), allow_supersedes=True).verdict == "OP_FAILURE"


def test_a_supersedes_line_from_a_final_review_fails_the_contract(tmp_path: Path) -> None:
    """`reviewer-final.md` permits only FINDING and VERDICT -- SUPERSEDES is not scoped to it,
    so a final reviewer emitting one is an unrecognised line, not a silently-accepted one."""
    out = tmp_path / "r.out"
    out.write_bytes(_block("SUPERSEDES round=1 file=a.txt:1 | not allowed here", "VERDICT APPROVED"))
    assert contract.parse(out, config=config_with(), allow_supersedes=False).verdict == "OP_FAILURE"
    assert contract.parse(out, config=config_with()).verdict == "OP_FAILURE"


# --------------------------------------------------------------------------
# parse_clarify: a clarify reply and its retraction block
# --------------------------------------------------------------------------

_RETRACTION = "SUPERSEDES round=1 file=a.txt:1 | the premise was wrong"


def test_a_clarify_reply_with_no_markers_is_all_prose() -> None:
    text = "Just an answer.\nOn two lines.\n"
    assert contract.parse_clarify(text) == contract.ClarifyReply(prose=text)


def test_a_clarify_retraction_block_is_split_from_the_prose_above_it() -> None:
    reply = contract.parse_clarify(f"You are right.\n\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n\n<<<ARL-END>>>\n")
    assert reply.prose == "You are right."
    assert reply.supersedes == (_RETRACTION,)
    assert reply.problem == ""


@pytest.mark.parametrize(
    "block_line",
    [
        "FINDING severity=high actionable=yes file=b.txt:2 | a brand-new finding",
        "VERDICT APPROVED",
        "VERDICT CHANGES_REQUIRED",
        "SUPERSEDES round=1 file=a.txt:1",
        "prose inside the block",
    ],
)
def test_a_clarify_block_holding_anything_but_supersedes_yields_no_retraction(block_line: str) -> None:
    """A ``FINDING`` or a ``VERDICT`` in a clarify block is a re-review, which a clarify is not.
    The valid line beside it is not salvaged: a block that breaks the contract yields nothing."""
    reply = contract.parse_clarify(f"Prose above.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n{block_line}\n<<<ARL-END>>>\n")
    assert reply.supersedes == ()
    assert reply.problem
    assert reply.prose == "Prose above.", "the prose above a located block is still returned"


@pytest.mark.parametrize(
    "text",
    [
        f"<<<ARL-FINDINGS>>>\n{_RETRACTION}\n<<<ARL-END>>>\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n<<<ARL-END>>>\n",
        f"Prose.\n<<<ARL-END>>>\n{_RETRACTION}\n<<<ARL-FINDINGS>>>\n",
        f"Prose.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n",
        f"Prose.\n{_RETRACTION}\n<<<ARL-END>>>\n",
    ],
    ids=["two-blocks", "inverted", "no-end-marker", "no-start-marker"],
)
def test_a_clarify_block_that_cannot_be_located_yields_no_retraction(text: str) -> None:
    reply = contract.parse_clarify(text)
    assert reply.supersedes == ()
    assert reply.problem
    assert reply.prose == text, "with no block located, the whole reply is the prose"


@pytest.mark.parametrize(
    "trailing",
    [
        "On reflection, the finding stands.",
        "FINDING severity=high actionable=yes file=a.txt:1 | still wrong after all",
        "VERDICT CHANGES_REQUIRED",
        "SUPERSEDES round=1 file=b.txt:2 | one more, outside the block",
    ],
    ids=["prose", "finding", "verdict", "supersedes"],
)
def test_a_clarify_block_followed_by_text_yields_no_retraction(trailing: str) -> None:
    """The block must end the reply. Text after it can take the retraction back, so recording the
    block while dropping that text would report a retraction the reviewer no longer makes."""
    text = f"You are right.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n<<<ARL-END>>>\n\n{trailing}\n"
    reply = contract.parse_clarify(text)
    assert reply.supersedes == ()
    assert "must end the reply" in reply.problem
    assert reply.prose == text, "the trailing text is shown, not hidden"


def test_blank_lines_after_a_clarify_block_are_allowed() -> None:
    reply = contract.parse_clarify(f"You are right.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n<<<ARL-END>>>\n\n  \n\t\n")
    assert reply.supersedes == (_RETRACTION,)
    assert reply.problem == ""


def test_an_empty_clarify_block_yields_no_retraction() -> None:
    reply = contract.parse_clarify("Prose.\n<<<ARL-FINDINGS>>>\n\n<<<ARL-END>>>\n")
    assert reply.supersedes == ()
    assert "no SUPERSEDES" in reply.problem
    assert reply.prose == "Prose."


def test_a_clarify_block_carrying_a_nul_byte_yields_no_retraction() -> None:
    reply = contract.parse_clarify(f"Prose.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\0\n<<<ARL-END>>>\n")
    assert reply.supersedes == ()
    assert "NUL" in reply.problem


def test_a_clarify_block_that_is_not_valid_utf8_yields_no_retraction() -> None:
    """``_decode`` keeps invalid bytes as lone surrogates, and one reaching ``clarify_history``
    could not be encoded when ``state.json`` is saved."""
    raw = f"Prose.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}".encode() + b"\xff\n<<<ARL-END>>>\n"
    reply = contract.parse_clarify(raw.decode("utf-8", "surrogateescape"))
    assert reply.supersedes == ()
    assert "UTF-8" in reply.problem


def test_a_finding_detail_with_a_unicode_line_separator_stays_one_record(tmp_path: Path) -> None:
    """Records split on ``\\n`` only, never ``str.splitlines``: a valid detail carrying U+2028
    stays one finding instead of two fragments, the second of which breaks the contract."""
    out = tmp_path / "reviewer.out"
    out.write_text(
        "Look.\n\n<<<ARL-FINDINGS>>>\n"
        "FINDING severity=high actionable=yes file=a.txt:1 | first line\u2028second line\n"
        "VERDICT CHANGES_REQUIRED\n<<<ARL-END>>>\n",
        encoding="utf-8",
    )
    assert len(contract.records(out.read_text(encoding="utf-8"))) == 6
    review = contract.parse(out, config=config_with())
    assert review.verdict == "CHANGES_REQUIRED"
    assert len(contract.records(review.findings)) == 1, f"the U+2028 must not have split the record: {review.findings!r}"
    assert "\u2028second line" in review.findings
