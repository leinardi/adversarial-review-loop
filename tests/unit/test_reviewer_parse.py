"""Invocation, failure classification, and contract parsing.

The property asserted throughout: **Rule 1**. No reviewer output and no operational
failure produces ``APPROVED``.
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

import os
import stat
import time
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, config_with, git_status_ignored
from reviewer_common import (
    MODE_VERDICTS,
    PLAIN_VERDICT,
    assert_descendant_never_ran,
    build,
    contract,
    dirty,
    execute_fake,
    fake_reviewer_output,
    invocation,
    spawner,
    target_for,
)

from arl import report, reviewer, state
from arl.config import Config
from arl.reviewer import Invocation, Review, ReviewerFailed, Target

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")

# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


TARGET = Target(repo="/repo", base="b", head="h", scope="phase", phase=1)


def invoke_fake(tmp_path: Path, mode: str, *, config: Config | None = None, **env: str) -> Path:
    run = invocation(tmp_path)
    reviewer.invoke(
        TARGET,
        run,
        config=config or config_with(),
        environ={**os.environ, "ARL_REVIEWER_CMD": str(FAKE_REVIEWER), "ARL_FAKE_MODE": mode, **env},
    )
    return run.out_path


def test_the_reviewer_seam_receives_the_bundle(tmp_path: Path) -> None:
    out = invoke_fake(tmp_path, "echo-bundle")
    assert reviewer.FINDINGS_MARKER in out.read_text()


def test_a_nonzero_reviewer_exit_is_a_failure(tmp_path: Path) -> None:
    with pytest.raises(ReviewerFailed) as caught:
        invoke_fake(tmp_path, "nonzero")
    assert str(caught.value) == "the reviewer exited with status 3"


def test_a_slow_reviewer_times_out(tmp_path: Path) -> None:
    with pytest.raises(ReviewerFailed) as caught:
        invoke_fake(tmp_path, "slow", config=config_with(timeout_sec=1))
    assert str(caught.value) == "the reviewer timed out after 1s"


def test_a_missing_reviewer_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    out = tmp_path / "reviewer.out"
    with pytest.raises(ReviewerFailed) as caught:
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=out),
            config=config_with(),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(tmp_path / "does-not-exist")},
        )
    assert str(caught.value) == "the reviewer exited with status 127"


def test_terminal_escapes_are_stripped(tmp_path: Path) -> None:
    script = tmp_path / "ansi.sh"
    script.write_text("#!/usr/bin/env bash\nprintf '\\033[1;32mVERDICT\\033[0m APPROVED\\n'\n")
    script.chmod(0o755)

    out = tmp_path / "reviewer.out"
    reviewer.invoke(
        TARGET,
        Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=out),
        config=config_with(),
        environ={**os.environ, "ARL_REVIEWER_CMD": str(script)},
    )
    assert out.read_bytes() == PLAIN_VERDICT


def test_the_raw_output_is_private(tmp_path: Path) -> None:
    out = invoke_fake(tmp_path, "approve")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_a_timed_out_reviewers_partial_output_is_still_kept(tmp_path: Path) -> None:
    """It is evidence for the report, and the verdict is decided by the exception."""
    script = tmp_path / "partial.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'half an answer\\n'\nsleep 30\n")
    script.chmod(0o755)

    out = tmp_path / "reviewer.out"
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=out),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(script)},
        )
    assert out.read_text() == "half an answer\n"


def test_a_timeout_kills_what_the_reviewer_spawned(tmp_path: Path) -> None:
    """``subprocess``'s own timeout kills the direct child only; GNU ``timeout`` does not.

    Measured before the fix: the grandchild created its file two seconds after the
    one-second deadline, so a reviewer that backgrounded work kept running after the gate
    had given up on it.
    """
    marker = tmp_path / "descendant"
    started = time.monotonic()
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(spawner(tmp_path, marker))},
        )
    assert_descendant_never_ran(marker, started=started)


@pytest.mark.parametrize("deaf", [False, True])
def test_a_timeout_kills_a_descendant_that_ignores_sigterm(tmp_path: Path, deaf: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM then SIGKILL, to the group, whatever the direct child did in between.

    The grace is shortened so the descendant's own delay outlasts it. A descendant that
    ignores SIGTERM and finishes its work *within* the grace is not prevented -- that window
    is the price of letting a build tear itself down, and ``timeout``, which never escalates
    at all, gives such a process the rest of time.
    """
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)
    marker = tmp_path / f"deaf-{deaf}"
    started = time.monotonic()
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(spawner(tmp_path, marker, deaf=deaf, name=f"deaf-{deaf}.sh"))},
        )
    assert_descendant_never_ran(marker, started=started)


def test_a_timeout_kills_what_verify_cmd_spawned(activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "verify-descendant"
    script = spawner(tmp_path, marker, deaf=True)
    dest = activation.act_dir / "bundles" / "001"

    monkeypatch.setattr(reviewer, "VERIFY_TIMEOUT_SEC", 1)
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)
    started = time.monotonic()
    build(activation, git_repo, dest, config_with(verify_cmd=str(script)))

    assert "[exit status: 124]" in (dest / "verify.txt").read_text()
    assert_descendant_never_ran(marker, started=started)


# --------------------------------------------------------------------------
# Phase 6: failure classification
# --------------------------------------------------------------------------


def test_reviewer_failed_carries_the_exit_status(tmp_path: Path) -> None:
    with pytest.raises(ReviewerFailed) as caught:
        invoke_fake(tmp_path, "nonzero")
    assert caught.value.status == 3


def test_a_timeout_carries_its_own_exit_status(tmp_path: Path) -> None:
    with pytest.raises(ReviewerFailed) as caught:
        invoke_fake(tmp_path, "slow", config=config_with(timeout_sec=1))
    assert caught.value.status in reviewer._TIMEOUT_STATUSES


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (124, "transient"),  # `timeout`'s own SIGTERM status
        (137, "transient"),  # `timeout`'s own SIGKILL status
        (126, "operational"),  # not executable
        (127, "operational"),  # no `opencode` on PATH
        (1, "operational"),  # a generic non-zero exit, no rate-limit signal in its output
    ],
)
def test_classify_op_failure_is_an_allow_list_not_a_catch_all(tmp_path: Path, status: int, kind: str) -> None:
    """A bad ``--model``/``--variant`` and an expired credential all exit non-zero the same

    way ``126``/``127`` do -- five attempts against any of them must burn the ordinary
    ``failures`` budget, not the transient one, or a missing binary alone would exhaust
    ``max_transient_failures`` on nothing but dead waiting.
    """
    out = tmp_path / "o"
    out.write_text("some ordinary CLI error text, no known signal in it\n")
    exc = ReviewerFailed(f"the reviewer exited with status {status}", status=status)
    assert reviewer._classify_op_failure(exc, out) == kind


@pytest.mark.parametrize(
    "text",
    [
        "Error: rate limit exceeded, please retry later\n",
        "429 Too Many Requests\n",
        "quota exceeded for this model\n",
        "usage limit reached, try again tomorrow\n",
        "Rate-Limited: backing off\n",
    ],
)
def test_a_matched_rate_limit_signal_is_transient_even_on_a_plain_exit(tmp_path: Path, text: str) -> None:
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "transient"


@pytest.mark.parametrize("text", ["command not found\n", "permission denied\n", ""])
def test_output_with_no_rate_limit_phrase_stays_operational(tmp_path: Path, text: str) -> None:
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "operational"


def test_the_rate_limit_signal_is_only_read_from_the_head_of_the_output(tmp_path: Path) -> None:
    """ "Bounded" -- a signal past the head is not scanned for, so a large transcript is never
    read in full just to classify a failure."""
    out = tmp_path / "o"
    out.write_text(("x" * reviewer._TRANSIENT_OUTPUT_HEAD_BYTES) + "rate limit exceeded\n")
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "operational"


def test_classify_op_failure_never_reads_the_whole_file_into_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit can still have written an unbounded amount to ``out_path`` before it

    did; the classifier must read only the bounded head from disk, never load the whole file
    first and slice it after the fact.
    """
    out = tmp_path / "o"
    out.write_text("rate limit exceeded\n")

    def forbidden(self: Path) -> bytes:
        raise AssertionError("_classify_op_failure must not call Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "transient"


@pytest.mark.parametrize("text", ["rateXlimit exceeded\n", "notarratelimiter\n", "arbitraryrate_limitingtoken\n", "rate limitation for this month\n"])
def test_the_rate_limit_pattern_does_not_glue_across_unrelated_characters(tmp_path: Path, text: str) -> None:
    """A bare ``.?`` between "rate" and "limit" would also match "rateXlimit" or catch the

    phrase glued onto an unrelated identifier -- only a real word, bounded and separated by
    nothing, a space, a hyphen or an underscore, counts.
    """
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "operational"


@pytest.mark.parametrize("text", ["rate_limit_exceeded\n", "RATE-LIMIT hit\n", "ratelimit reached\n"])
def test_every_documented_rate_limit_separator_still_matches(tmp_path: Path, text: str) -> None:
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, out) == "transient"


def test_a_missing_out_path_falls_back_to_operational(tmp_path: Path) -> None:
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert reviewer._classify_op_failure(exc, tmp_path / "never-written") == "operational"


def test_run_invocation_classifies_a_timeout_as_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(FAKE_REVIEWER))
    monkeypatch.setenv("ARL_FAKE_MODE", "slow")
    review, invoked = reviewer._run_invocation(TARGET, invocation(tmp_path), config=config_with(timeout_sec=1))
    assert invoked is False
    assert review.verdict == "OP_FAILURE"
    assert review.kind == "transient"


def test_run_invocation_classifies_a_plain_nonzero_exit_as_operational(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_REVIEWER_CMD", str(FAKE_REVIEWER))
    monkeypatch.setenv("ARL_FAKE_MODE", "nonzero")
    review, invoked = reviewer._run_invocation(TARGET, invocation(tmp_path), config=config_with())
    assert invoked is False
    assert review.verdict == "OP_FAILURE"
    assert review.kind == "operational"


def test_execute_classifies_a_bundle_error_as_kind_bundle(activation: state.State, git_repo: Path) -> None:
    target = Target(repo=str(git_repo), base="deadbeef", head=dirty(git_repo), scope="phase", phase=1)
    review = reviewer.execute(target, state=activation, config=config_with())
    assert review.verdict == "OP_FAILURE"
    assert review.kind == "bundle"


# --------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "verdict"), MODE_VERDICTS)
def test_each_reviewer_shape_reaches_the_right_verdict(tmp_path: Path, mode: str, verdict: str) -> None:
    out = fake_reviewer_output(tmp_path, mode)
    assert reviewer.parse(out, config=config_with()).verdict == verdict


@pytest.mark.parametrize(("mode", "verdict"), MODE_VERDICTS)
def test_no_reviewer_shape_produces_an_approval_it_did_not_earn(tmp_path: Path, mode: str, verdict: str) -> None:
    """Rule 1, stated as its own assertion so a future change cannot quietly relax it."""
    parsed = reviewer.parse(fake_reviewer_output(tmp_path, mode), config=config_with())
    if verdict != "APPROVED":
        assert parsed.verdict != "APPROVED"
        assert parsed.error or parsed.findings


def test_missing_markers_are_a_failure(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_text("VERDICT APPROVED\n")
    parsed = reviewer.parse(out, config=config_with())
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
    parsed = reviewer.parse(out, config=config_with())

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer output contains a NUL byte, so the contract cannot be validated"


def test_a_nul_byte_inside_a_finding_line_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_bytes(b"prose\n<<<ARL-FINDINGS>>>\nFINDING severity=critical actionable=n\0o file=a | x\nVERDICT APPROVED\n<<<ARL-END>>>\n")

    mine = reviewer.parse(out, config=config_with())

    assert mine.verdict == "OP_FAILURE"


def test_a_missing_output_file_is_a_failure(tmp_path: Path) -> None:
    parsed = reviewer.parse(tmp_path / "never-written", config=config_with())
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer produced no output"
    assert parsed.kind == "contract"


def test_missing_markers_carry_kind_contract(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_text("VERDICT APPROVED\n")
    assert reviewer.parse(out, config=config_with()).kind == "contract"


def test_a_nul_refusal_carries_kind_contract(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_bytes(b"\0prose\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n")
    assert reviewer.parse(out, config=config_with()).kind == "contract"


def test_an_unrecognised_verdict_carries_kind_contract(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("VERDICT MAYBE"))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.kind == "contract"


def test_output_that_is_not_valid_utf8_is_refused(tmp_path: Path) -> None:
    """``_decode`` is ``surrogateescape``; a lone surrogate that reached ``round_history``
    could not be encoded when ``state.json`` is saved and would crash the whole review. A
    UTF-8 text protocol that is not valid UTF-8 fails the contract, like a NUL byte."""
    out = tmp_path / "o"
    out.write_bytes(b"prose\n<<<ARL-FINDINGS>>>\nFINDING severity=high actionable=yes file=a | bad \xff byte\nVERDICT APPROVED\n<<<ARL-END>>>\n")

    parsed = reviewer.parse(out, config=config_with())

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer output is not valid UTF-8, so the contract cannot be validated"


def test_a_non_utf8_review_appends_no_round_history_and_still_reports(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    script = tmp_path / "bad-utf8-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'p\\n\\n<<<ARL-FINDINGS>>>\\n'\n"
        r"printf 'FINDING severity=high actionable=yes file=a | \xff\n'" + "\n"
        "printf 'VERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    os.environ["ARL_REVIEWER_CMD"] = str(script)

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert review.verdict == "OP_FAILURE"
    assert activation.get_array_of_dicts("round_history") == []
    assert Path(review.report).is_file()


def parse_text(tmp_path: Path, text: str, config: Config | None = None) -> Review:
    out = tmp_path / "o"
    out.write_text(text)
    return reviewer.parse(out, config=config or config_with())


def test_an_unlabelled_severity_is_a_contract_failure(tmp_path: Path) -> None:
    """Omitting the field is not a way under the threshold, and not a finding to drop."""
    parsed = parse_text(tmp_path, contract("FINDING actionable=yes file=a.txt:1 | No severity given", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error.startswith("the reviewer emitted a line the contract does not allow: FINDING actionable=yes")


@pytest.mark.parametrize("severity", ["spicy", "CRITICAL", "sev5", ""])
def test_a_severity_outside_the_documented_set_is_a_contract_failure(tmp_path: Path, severity: str) -> None:
    parsed = parse_text(tmp_path, contract(f"FINDING severity={severity} actionable=yes file=a.txt:1 | Odd label", "VERDICT APPROVED"))
    assert parsed.verdict == "OP_FAILURE"


def test_an_actionable_finding_blocks(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("FINDING severity=high actionable=yes file=a | x", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


@pytest.mark.parametrize("value", ["YES", "Yes", "true", "1", "maybe", "unknown", ""])
def test_an_actionable_field_the_gate_cannot_read_never_approves(tmp_path: Path, value: str) -> None:
    """The gate cannot tell a typo from a finding it failed to understand (Rule 1).

    Every one of these used to be read as "not actionable", so a ``critical`` finding was
    dropped and the reviewer's own ``APPROVED`` stood.
    """
    parsed = parse_text(tmp_path, contract(f"FINDING severity=critical actionable={value} file=a | x", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.findings == "" and parsed.all_findings == ""


def test_actionable_no_is_recorded_without_blocking(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("FINDING severity=critical actionable=no file=a | x", "VERDICT APPROVED"))
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
    parsed = parse_text(tmp_path, contract(line, "VERDICT APPROVED"))
    if line.endswith("severity=low"):
        assert parsed.verdict == "CHANGES_REQUIRED"
    else:
        assert parsed.verdict == "OP_FAILURE"


def test_a_path_with_spaces_is_still_a_finding(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("FINDING severity=high actionable=yes file=my file.txt:1 | x", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


def test_a_line_the_contract_does_not_allow_fails_the_review(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("Nothing worth reporting, honestly.", "VERDICT APPROVED"))

    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted a line the contract does not allow: Nothing worth reporting, honestly."


def test_the_echoed_line_is_bounded(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("z" * 5000, "VERDICT APPROVED"))
    assert len(parsed.error) < 200


def test_blank_lines_inside_the_block_are_allowed(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("", "   ", "VERDICT APPROVED"))
    assert parsed.verdict == "APPROVED"


def test_a_stray_end_marker_above_the_block_never_approves(tmp_path: Path) -> None:
    """The sed range took the first opening marker, so findings above it simply vanished."""
    text = (
        "prose\n"
        f"{reviewer.END_MARKER}\n"
        "FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref\n"
        f"{reviewer.FINDINGS_MARKER}\n"
        "VERDICT APPROVED\n"
        f"{reviewer.END_MARKER}\n"
    )
    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "OP_FAILURE"
    assert "exactly one" in parsed.error


def test_two_marker_blocks_never_approve(tmp_path: Path) -> None:
    text = contract("FINDING severity=critical actionable=yes file=a | boom") + contract("VERDICT APPROVED")
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
    parsed = parse_text(tmp_path, f"{marker_line}\nVERDICT APPROVED\n{reviewer.END_MARKER}\n")

    assert parsed.verdict == "OP_FAILURE"
    assert "missing the" in parsed.error


@pytest.mark.parametrize("marker_line", ["<<<ARL-END>>> and more", "text <<<ARL-END>>>"])
def test_a_buried_end_marker_does_not_close_the_block(tmp_path: Path, marker_line: str) -> None:
    parsed = parse_text(tmp_path, f"prose\n{reviewer.FINDINGS_MARKER}\nVERDICT APPROVED\n{marker_line}\n")
    assert parsed.verdict == "OP_FAILURE"


@pytest.mark.parametrize("pad", ["", "  ", "\t"])
def test_surrounding_whitespace_on_a_marker_is_tolerated(tmp_path: Path, pad: str) -> None:
    parsed = parse_text(tmp_path, f"prose\n{pad}{reviewer.FINDINGS_MARKER}{pad}\nVERDICT APPROVED\n{pad}{reviewer.END_MARKER}{pad}\n")
    assert parsed.verdict == "APPROVED"


def test_one_line_holding_both_markers_never_approves(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, f"prose\n{reviewer.FINDINGS_MARKER} {reviewer.END_MARKER}\nVERDICT APPROVED\n")
    assert parsed.verdict == "OP_FAILURE"


def test_the_threshold_is_applied(tmp_path: Path) -> None:
    text = contract(
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
    text = contract(
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
    text = contract(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="hihg"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert "severity=low" in parsed.findings


def test_an_actionable_low_finding_is_recorded_but_does_not_block_at_the_default_threshold(tmp_path: Path) -> None:
    """``block_severity`` defaults to ``medium``: an actionable ``low`` finding is real
    evidence, kept in ``all_findings``, but it no longer meets the threshold on its own."""
    text = contract(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "APPROVED"
    assert parsed.findings == ""
    assert "severity=low" in parsed.all_findings


def test_an_actionable_low_finding_still_blocks_when_the_threshold_is_lowered(tmp_path: Path) -> None:
    text = contract(
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
    text = contract(
        "FINDING severity=low actionable=yes file=a.txt:1 | Could be named better",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text)

    assert parsed.verdict == "APPROVED"


def test_an_unrecognised_verdict_is_a_failure(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("VERDICT MAYBE"))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted an unrecognised verdict: MAYBE"


@pytest.mark.parametrize("line", ["VERDICT APPROVED", "  VERDICT: APPROVED", "VERDICT:APPROVED", "VERDICT   APPROVED   "])
def test_the_verdict_line_is_read_the_way_the_shell_read_it(tmp_path: Path, line: str) -> None:
    assert parse_text(tmp_path, contract(line)).verdict == "APPROVED"


@pytest.mark.parametrize(
    "verdicts",
    [("VERDICT APPROVED", "VERDICT CHANGES_REQUIRED"), ("VERDICT CHANGES_REQUIRED", "VERDICT APPROVED")],
)
def test_a_second_verdict_line_fails_the_review(tmp_path: Path, verdicts: tuple[str, str]) -> None:
    """Last-wins let a trailing APPROVED overrule the reviewer's own CHANGES_REQUIRED."""
    parsed = parse_text(tmp_path, contract(*verdicts))
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer emitted more than one VERDICT line"


def test_the_findings_cap_escalates_instead_of_trimming(tmp_path: Path) -> None:
    lines = [f"FINDING severity=low actionable=no file=a:{i} | finding {i}" for i in range(6)]
    parsed = parse_text(tmp_path, contract(*lines, "VERDICT APPROVED"), config_with(max_findings=5))

    assert parsed.verdict == "NEEDS_HUMAN"
    assert "above max_findings (5)" in parsed.error
    assert parsed.all_findings.count("FINDING") == 6, "the list is kept, not trimmed"


def test_the_findings_byte_cap_escalates(tmp_path: Path) -> None:
    parsed = parse_text(
        tmp_path, contract("FINDING severity=low actionable=no file=a | " + "x" * 500, "VERDICT APPROVED"), config_with(max_findings_bytes=100)
    )
    assert parsed.verdict == "NEEDS_HUMAN"
    assert "above max_findings_bytes (100)" in parsed.error


def test_prose_stops_at_the_marker(tmp_path: Path) -> None:
    parsed = parse_text(tmp_path, contract("VERDICT APPROVED"))
    assert parsed.prose == "prose line"


def test_a_carriage_return_does_not_split_a_finding(tmp_path: Path) -> None:
    """``grep``/``sed`` break on ``\n`` alone; ``str.splitlines`` also breaks on ``\r``."""
    parsed = parse_text(tmp_path, contract("FINDING severity=high actionable=yes file=a | x\ry", "VERDICT APPROVED"))
    assert parsed.verdict == "CHANGES_REQUIRED"


# --------------------------------------------------------------------------
# One full review
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "verdict"), MODE_VERDICTS)
def test_a_full_review_reaches_the_same_verdict_end_to_end(activation: state.State, git_repo: Path, mode: str, verdict: str) -> None:
    assert execute_fake(activation, git_repo, mode).verdict == verdict


def test_a_full_review_stores_a_report_and_bumps_the_sequence(activation: state.State, git_repo: Path) -> None:
    review = execute_fake(activation, git_repo, "changes")

    assert activation.get_int("report_seq") == 1
    assert Path(review.report).is_file()
    assert Path(review.report).name == "001-phase1-changes_required.md"
    assert Path(review.raw).read_text().count(reviewer.FINDINGS_MARKER) == 1
    assert "Returns success on a failed lookup" in review.findings


def test_consecutive_reviews_do_not_overwrite_each_others_report(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    execute_fake(activation, git_repo, "approve")

    assert activation.get_int("report_seq") == 2
    assert report.list_reports(activation.act_dir) == ["001-phase1-changes_required.md", "002-phase1-approved.md"]


def test_a_failed_reviewer_still_leaves_its_evidence(activation: state.State, git_repo: Path) -> None:
    review = execute_fake(activation, git_repo, "nonzero")

    assert review.verdict == "OP_FAILURE"
    assert review.error == "the reviewer exited with status 3"
    assert Path(review.report).is_file()
    assert "boom" in Path(review.report).read_text(), "the raw output is what a failure is diagnosed from"


def test_an_oversized_diff_escalates_the_whole_review(activation: state.State, git_repo: Path) -> None:
    (git_repo / "big.txt").write_text("x\n" * 5000)
    review = execute_fake(activation, git_repo, "approve", config=config_with(hard_diff_ceiling=1024))

    assert review.verdict == "NEEDS_HUMAN"
    assert "above hard_diff_ceiling" in review.error
    assert review.report == "", "there is no review to report on"


def test_a_final_review_names_itself_as_such(activation: state.State, git_repo: Path) -> None:
    review = execute_fake(activation, git_repo, "approve", scope="final")
    assert Path(review.report).name == "001-final-approved.md"


def test_a_review_writes_nothing_into_the_repository(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "approve")
    assert git_status_ignored(git_repo) == "?? a.txt\n"


# --------------------------------------------------------------------------
# parse_clarify: a clarify reply and its retraction block
# --------------------------------------------------------------------------

_RETRACTION = "SUPERSEDES round=1 file=a.txt:1 | the premise was wrong"


def test_a_clarify_reply_with_no_markers_is_all_prose() -> None:
    text = "Just an answer.\nOn two lines.\n"
    assert reviewer.parse_clarify(text) == reviewer.ClarifyReply(prose=text)


def test_a_clarify_retraction_block_is_split_from_the_prose_above_it() -> None:
    reply = reviewer.parse_clarify(f"You are right.\n\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n\n<<<ARL-END>>>\n")
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
    reply = reviewer.parse_clarify(f"Prose above.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n{block_line}\n<<<ARL-END>>>\n")
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
    reply = reviewer.parse_clarify(text)
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
    reply = reviewer.parse_clarify(text)
    assert reply.supersedes == ()
    assert "must end the reply" in reply.problem
    assert reply.prose == text, "the trailing text is shown, not hidden"


def test_blank_lines_after_a_clarify_block_are_allowed() -> None:
    reply = reviewer.parse_clarify(f"You are right.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\n<<<ARL-END>>>\n\n  \n\t\n")
    assert reply.supersedes == (_RETRACTION,)
    assert reply.problem == ""


def test_an_empty_clarify_block_yields_no_retraction() -> None:
    reply = reviewer.parse_clarify("Prose.\n<<<ARL-FINDINGS>>>\n\n<<<ARL-END>>>\n")
    assert reply.supersedes == ()
    assert "no SUPERSEDES" in reply.problem
    assert reply.prose == "Prose."


def test_a_clarify_block_carrying_a_nul_byte_yields_no_retraction() -> None:
    reply = reviewer.parse_clarify(f"Prose.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}\0\n<<<ARL-END>>>\n")
    assert reply.supersedes == ()
    assert "NUL" in reply.problem


def test_a_clarify_block_that_is_not_valid_utf8_yields_no_retraction() -> None:
    """``_decode`` keeps invalid bytes as lone surrogates, and one reaching ``clarify_history``
    could not be encoded when ``state.json`` is saved."""
    raw = f"Prose.\n<<<ARL-FINDINGS>>>\n{_RETRACTION}".encode() + b"\xff\n<<<ARL-END>>>\n"
    reply = reviewer.parse_clarify(raw.decode("utf-8", "surrogateescape"))
    assert reply.supersedes == ()
    assert "UTF-8" in reply.problem
