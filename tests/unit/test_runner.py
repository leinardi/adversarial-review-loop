"""Running an external reviewer: the bounded runner, timeouts, and failure classification.

No reviewer output and no operational failure produces ``APPROVED``.
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
import signal
import stat
import time
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, config_with
from reviewer_common import PLAIN_VERDICT, assert_descendant_never_ran, bundle_with_diff, invocation, spawner

from arl import cli, contract, harness, runner
from arl.config import Config
from arl.contract import Target
from arl.runner import Invocation, ReviewerFailed

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


TARGET = Target(scope="phase", phase=1, launch_dir="/launch")


def invoke_fake(tmp_path: Path, mode: str, *, config: Config | None = None, **env: str) -> Path:
    run = invocation(tmp_path)
    runner.invoke(
        TARGET,
        run,
        config=config or config_with(),
        environ={**os.environ, "ARL_REVIEWER_CMD": str(FAKE_REVIEWER), "ARL_FAKE_MODE": mode, **env},
    )
    return run.out_path


def test_the_reviewer_seam_receives_the_bundle(tmp_path: Path) -> None:
    out = invoke_fake(tmp_path, "echo-bundle")
    assert contract.FINDINGS_MARKER in out.read_text()


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
        runner.invoke(
            TARGET,
            Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=out),
            config=config_with(),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(tmp_path / "does-not-exist")},
        )
    assert str(caught.value) == "the reviewer exited with status 127"


def test_terminal_escapes_are_stripped(tmp_path: Path) -> None:
    script = tmp_path / "ansi.sh"
    script.write_text("#!/usr/bin/env bash\nprintf '\\033[1;32mVERDICT\\033[0m APPROVED\\n'\n")
    script.chmod(0o755)

    out = tmp_path / "reviewer.out"
    runner.invoke(
        TARGET,
        Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=out),
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
        runner.invoke(
            TARGET,
            Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=out),
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
        runner.invoke(
            TARGET,
            Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
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
    monkeypatch.setattr(runner, "KILL_GRACE_SEC", 0.2)
    marker = tmp_path / f"deaf-{deaf}"
    started = time.monotonic()
    with pytest.raises(ReviewerFailed):
        runner.invoke(
            TARGET,
            Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "ARL_REVIEWER_CMD": str(spawner(tmp_path, marker, deaf=deaf, name=f"deaf-{deaf}.sh"))},
        )
    assert_descendant_never_ran(marker, started=started)


def test_an_engine_that_is_terminated_takes_its_reviewer_with_it(tmp_path: Path) -> None:
    """The mod kills the engine with SIGTERM when the person interrupts an external review; the
    reviewer runs in a session of its own and must not outlive the engine that owns it."""
    marker = tmp_path / "orphan"

    def terminated(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGALRM, terminated)
    started = time.monotonic()
    signal.alarm(1)
    try:
        with pytest.raises(SystemExit):
            runner.invoke(
                TARGET,
                Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
                config=config_with(timeout_sec=30),
                environ={**os.environ, "ARL_REVIEWER_CMD": str(spawner(tmp_path, marker))},
            )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert_descendant_never_ran(marker, started=started)


def test_the_bootstrap_makes_sigterm_unwind_the_engine(tmp_path: Path) -> None:
    """Default SIGTERM ends Python without unwinding, which would skip the kill above."""
    previous = signal.getsignal(signal.SIGTERM)
    try:
        cli.end_on_sigterm()
        with pytest.raises(SystemExit) as ended:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert ended.value.code == 128 + signal.SIGTERM


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
    assert caught.value.status in runner._TIMEOUT_STATUSES


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
    assert runner.classify_op_failure(exc, out) == kind


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
    assert runner.classify_op_failure(exc, out) == "transient"


@pytest.mark.parametrize("text", ["command not found\n", "permission denied\n", ""])
def test_output_with_no_rate_limit_phrase_stays_operational(tmp_path: Path, text: str) -> None:
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert runner.classify_op_failure(exc, out) == "operational"


def test_the_rate_limit_signal_is_only_read_from_the_head_of_the_output(tmp_path: Path) -> None:
    """ "Bounded" -- a signal past the head is not scanned for, so a large transcript is never
    read in full just to classify a failure."""
    out = tmp_path / "o"
    out.write_text(("x" * runner._TRANSIENT_OUTPUT_HEAD_BYTES) + "rate limit exceeded\n")
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert runner.classify_op_failure(exc, out) == "operational"


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
    assert runner.classify_op_failure(exc, out) == "transient"


@pytest.mark.parametrize("text", ["rateXlimit exceeded\n", "notarratelimiter\n", "arbitraryrate_limitingtoken\n", "rate limitation for this month\n"])
def test_the_rate_limit_pattern_does_not_glue_across_unrelated_characters(tmp_path: Path, text: str) -> None:
    """A bare ``.?`` between "rate" and "limit" would also match "rateXlimit" or catch the

    phrase glued onto an unrelated identifier -- only a real word, bounded and separated by
    nothing, a space, a hyphen or an underscore, counts.
    """
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert runner.classify_op_failure(exc, out) == "operational"


@pytest.mark.parametrize("text", ["rate_limit_exceeded\n", "RATE-LIMIT hit\n", "ratelimit reached\n"])
def test_every_documented_rate_limit_separator_still_matches(tmp_path: Path, text: str) -> None:
    out = tmp_path / "o"
    out.write_text(text)
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert runner.classify_op_failure(exc, out) == "transient"


def test_a_missing_out_path_falls_back_to_operational(tmp_path: Path) -> None:
    exc = ReviewerFailed("the reviewer exited with status 1", status=1)
    assert runner.classify_op_failure(exc, tmp_path / "never-written") == "operational"


def test_a_bundle_without_a_diff_is_never_handed_to_a_reviewer(tmp_path: Path) -> None:
    """A prompt naming a bundle with no diff would be judged on nothing, and an empty
    judgement reads as an approval: the launch is refused before anything runs."""
    run = invocation(tmp_path)
    for chunk in run.bundle_dir.glob("changes.*.diff"):
        chunk.unlink()
    with pytest.raises(runner.LaunchError, match=r"holds no changes"):
        runner.invoke(TARGET, run, config=config_with(), environ={**os.environ, "ARL_REVIEWER_CMD": str(FAKE_REVIEWER)})
    assert not run.out_path.exists()


def test_a_command_that_cannot_be_composed_is_a_launch_error_and_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A harness refusing to compose the command is a failure to launch, not a reviewer answer."""

    def refuse(_self: object, _spec: harness.ReviewSpec) -> harness.Command:
        raise harness.PayloadError("the permission document could not be written")

    monkeypatch.delenv("ARL_REVIEWER_CMD", raising=False)
    cfg = config_with(harness="opencode")
    monkeypatch.setattr(type(harness.selected(cfg)), "review_command", refuse)
    run = invocation(tmp_path)
    run.prompt_file.write_text("Review the change.\n")
    with pytest.raises(runner.LaunchError, match="permission document"):
        runner.invoke(TARGET, run, config=cfg)
    assert not run.out_path.exists()


def test_an_unreadable_prompt_is_a_launch_error_not_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARL_REVIEWER_CMD", raising=False)
    run = invocation(tmp_path)
    with pytest.raises(runner.LaunchError, match="could not be read"):
        runner.invoke(TARGET, run, config=config_with(harness="opencode"))
