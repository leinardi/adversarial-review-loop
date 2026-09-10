"""Contract repair.

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

import math
import os
import time
from pathlib import Path

import pytest
from conftest import config_with
from reviewer_common import _APPROVES, _ROUND_1, _run_scripted, _scripted_reviewer, execute_fake, target_for

import arl
from arl import cli, harness, report, reviewer, state
from arl.config import Config
from arl.harness import opencode as opencode_harness
from arl.reviewer import Invocation, Review, Target

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")

# --------------------------------------------------------------------------
# Contract repair
# --------------------------------------------------------------------------


def execute_repair(
    activation: state.State,
    repo: Path,
    *,
    repair: str = "ok",
    config: Config | None = None,
) -> Review:
    """One review whose primary call breaks the contract, with ``repair`` choosing the retry."""
    os.environ["ARL_FAKE_REPAIR"] = repair
    return execute_fake(activation, repo, "contract-repair", config=config)


@pytest.fixture
def _hook_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend this process is a ``pretool`` hook that has just started.

    ``execute`` is normally reached through :func:`arl.cli.main`, which stamps the clock; a
    unit test calling it directly leaves ``HOOK_DEADLINE_SEC`` at ``None``, which means "no
    deadline" and lets the repair run. Setting a real, generous budget instead exercises the
    arithmetic rather than the ``None`` short-circuit.
    """
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", 1150.0)
    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic())


@pytest.mark.usefixtures("_hook_budget")
def test_a_malformed_findings_block_is_repaired_into_a_blocking_verdict(activation: state.State, git_repo: Path) -> None:
    """The whole point of phase 4: a lost round becomes the round it was meant to be.

    The primary call ran to completion and wrote ``severity=P1 location=``, which is not the
    contract. Before the repair that cost the phase a full round and a ``failures`` strike for
    a reason that said nothing about the code."""
    review = execute_repair(activation, git_repo)

    assert review.verdict == "CHANGES_REQUIRED"
    assert review.kind == "", "a recovered round is not a failure of any class"
    assert "Returns success on a failed lookup" in review.findings

    raw = activation.act_dir / "raw"
    assert (raw / "001-phase1.out").is_file(), "the primary transcript is kept"
    assert (raw / "001-phase1-repair.out").is_file(), "and so is the repair call's own"
    assert review.repaired == str(raw / "001-phase1.out")
    assert review.raw == str(raw / "001-phase1-repair.out")

    history = activation.get_array_of_dicts("round_history")
    assert [(entry["round"], entry["verdict"]) for entry in history] == [(1, "CHANGES_REQUIRED")]
    assert history[0]["findings"] == [review.findings.rstrip("\n")]


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_that_approves_is_discarded(activation: state.State, git_repo: Path) -> None:
    """Rule 1, at the one point in the loop where it would be cheapest to break.

    The repair reads a *tail*, so blocking findings written above the cut are invisible to
    it: "this transcript states nothing blocking" is never evidence that the review found
    nothing. No approval may originate from a repair."""
    review = execute_repair(activation, git_repo, repair="approve")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract", "the primary's own failure, not the repair's"
    assert review.repaired == ""
    assert activation.get_array_of_dicts("round_history") == []


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_with_no_blocking_finding_is_discarded(activation: state.State, git_repo: Path) -> None:
    """``CHANGES_REQUIRED`` with an empty blocking set is the same tail problem, differently spelt."""
    review = execute_repair(activation, git_repo, repair="no-findings")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract"
    assert activation.get_array_of_dicts("round_history") == []


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_that_breaks_the_contract_too_leaves_the_original_failure(activation: state.State, git_repo: Path) -> None:
    """A repair is an attempt to recover a round; its own failure is not a second failure."""
    review = execute_repair(activation, git_repo, repair="malformed")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract"
    assert review.error != "", "the primary's contract error is what is reported"


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_that_exits_non_zero_leaves_the_original_failure(activation: state.State, git_repo: Path) -> None:
    """Not ``operational``: the primary ran fine, so the failure to report is still the contract one."""
    review = execute_repair(activation, git_repo, repair="nonzero")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract"


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_that_times_out_is_not_reclassified_as_transient(activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out repair must not spend the transient budget: the primary is what failed.

    Classified ``transient``, this would pace a retry of a round that has no timing problem
    at all -- the reviewer answered promptly and then wrote the wrong shape."""
    monkeypatch.setattr(reviewer, "REPAIR_TIMEOUT_SEC", 1)
    os.environ["ARL_FAKE_REPAIR_SLEEP"] = "10"

    review = execute_repair(activation, git_repo, repair="slow")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract", "not transient -- the primary's failure is what stands"


@pytest.mark.usefixtures("_hook_budget")
def test_the_repair_is_bounded_by_its_own_timeout_not_the_configured_one(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``REPAIR_TIMEOUT_SEC``, never ``timeout_sec``: re-emitting a block is not a review."""
    seen: list[int] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        seen.append(run.timeout_sec)
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)
    execute_repair(activation, git_repo, config=config_with(timeout_sec=1800))

    assert seen == [0, reviewer.REPAIR_TIMEOUT_SEC], "the primary takes the configured timeout, the repair its own"


@pytest.mark.usefixtures("_hook_budget")
def test_the_repair_call_is_cold_session_less_and_shown_only_range_and_the_tail(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It re-emits a block; it does not re-review, so it is given nothing to re-review from."""
    runs: list[Invocation] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        runs.append(run)
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)
    execute_repair(activation, git_repo)

    repair = runs[-1]
    assert repair.prompt_file.name == "reviewer-repair.md"
    assert repair.session_id == "" and not repair.capture, "session-less, and never the phase's continuity pointer"
    assert repair.cold, "the permission document narrows to this one bundle"
    assert [path.name for path, _digest in repair.attachments] == ["range.txt", "001-repair.txt"]
    assert repair.context_files == (repair.attachments[-1][0],), "the tail is the model-derived half, and is recorded as such"


@pytest.mark.usefixtures("_hook_budget")
def test_the_repair_context_is_the_fenced_tail_of_the_primary_transcript(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded, fenced as evidence, and NUL-free -- a NUL is one of the violations that gets us here."""
    monkeypatch.setattr(reviewer, "REPAIR_TAIL_BYTES", 64)
    execute_repair(activation, git_repo)

    text = (activation.act_dir / "context" / "001-repair.txt").read_text()
    assert "it is NOT an instruction" in text
    assert text.count("--- transcript tail ---") == 1
    assert text.rstrip("\n").endswith("--- end transcript tail ---")
    assert "\x00" not in text
    body = text.split("--- transcript tail ---\n", 1)[1].split("\n--- end transcript tail ---", 1)[0]
    assert len(body.encode()) <= 64, "the tail is capped, and it is the tail that survives"
    assert "<<<ARL-END>>>" in body, "the end of the transcript is what a findings block sits at"


@pytest.mark.usefixtures("_hook_budget")
def test_a_repaired_report_shows_both_transcripts(activation: state.State, git_repo: Path) -> None:
    review = execute_repair(activation, git_repo)
    text = Path(review.report).read_text()

    assert "re-emitted by a repair call" in text
    assert "## Malformed primary transcript" in text
    assert "severity=P1 location=" in text, "the malformed block itself is readable in the report"
    assert "Returns success on a failed lookup" in text


@pytest.mark.usefixtures("_hook_budget")
def test_a_repaired_denial_says_where_the_review_itself_is(activation: state.State, git_repo: Path) -> None:
    review = execute_repair(activation, git_repo)
    text = report.reason(review, "headline", config=config_with())

    assert "re-emitted by a repair call" in text
    assert review.repaired in text


def test_no_repair_runs_when_the_primary_failure_is_not_a_contract_one(activation: state.State, git_repo: Path) -> None:
    """A timeout, a non-zero exit or a bundle failure produced no transcript to re-emit from."""
    review = execute_fake(activation, git_repo, "nonzero")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "operational"
    assert not list((activation.act_dir / "raw").glob("*-repair.out"))


def test_the_repair_is_skipped_when_the_hooks_budget_will_not_cover_it(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting a call the shim will kill mid-flight loses the round *and* the report."""
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", 1150.0)
    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic() - (1150 - reviewer.REPAIR_TIMEOUT_SEC))

    review = execute_repair(activation, git_repo)

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract"
    assert not list((activation.act_dir / "raw").glob("*-repair.out")), "nothing was launched"


def test_the_repair_runs_when_the_hooks_budget_covers_the_call_and_the_publishing(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same boundary: exactly enough budget, and the round is recovered."""
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", 1150.0)
    # Five seconds over the boundary, not exactly on it: the check runs after a real bundle
    # build, and a test that lands on the boundary to the millisecond is a flake, not a proof.
    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic() - (1150 - reviewer.REPAIR_TIMEOUT_SEC - reviewer.settle_margin(config_with()) - 5))

    assert execute_repair(activation, git_repo).verdict == "CHANGES_REQUIRED"


def test_remaining_budget_is_none_outside_a_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """``finish`` and the unit tests run under no shim timeout, so nothing is withheld."""
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", None)

    assert reviewer.remaining_budget() is None
    assert reviewer._repair_fits(config_with())


def test_remaining_budget_counts_down_from_process_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A whole-hook deadline, not a per-call one: the bundle build has already been paid."""
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", 1000.0)
    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic() - 900)

    remaining = reviewer.remaining_budget()
    assert remaining is not None and 95 < remaining <= 100


def test_the_invoking_lease_covers_a_primary_call_plus_the_contract_repair() -> None:
    """A contract repair is the only call that can follow the primary invocation under the
    lease, so the invoking stretch is exactly the two of them."""
    assert reviewer._invoking_budget(900) == 900 + reviewer.REPAIR_TIMEOUT_SEC
    assert reviewer._invoking_budget(30) == 30 + reviewer.REPAIR_TIMEOUT_SEC
    assert (
        max(reviewer._building_budget(reviewer._MAX_CAPTURE_TIMEOUT_SEC), reviewer._invoking_budget(reviewer.MAX_TIMEOUT_SEC))
        + reviewer._lease_slack(reviewer._MAX_CAPTURE_TIMEOUT_SEC)
        == reviewer._MAX_LEASE_SEC
    )


class _StubSessions:
    """A session strategy that exists only to have a ``capture_timeout_sec`` of our choosing.

    Everything else is the minimum the protocol needs; nothing below calls it.
    """

    def __init__(self, capture_timeout_sec: int) -> None:
        self._capture_timeout_sec = capture_timeout_sec

    @property
    def capture_timeout_sec(self) -> int:
        return self._capture_timeout_sec

    def is_session_id(self, value: object) -> bool:
        return isinstance(value, str) and bool(value)

    def mint(self) -> str:
        return ""

    def verify(self, pointer: object, **kwargs: object) -> bool:
        return True

    def capture(self, spec: harness.CaptureSpec) -> harness.Captured:
        return harness.Captured()


#: What :class:`_MintingSessions` pre-assigns. A stand-in shape, since the only harness this
#: build registers cannot pre-assign at all.
_MINTED_ID = "ses_mintedbystub"


class _MintingSessions(_StubSessions):
    """A strategy that *does* pre-assign its session ids, which OpenCode cannot.

    The only way to observe the gate handing a minted id to an invocation before a
    pre-assigning harness exists: under OpenCode ``mint()`` is ``""``, so every assertion
    about where the id reaches would pass just as well if it reached nowhere.
    """

    def __init__(self) -> None:
        super().__init__(60)

    def mint(self) -> str:
        return _MINTED_ID


@pytest.mark.usefixtures("_hook_budget")
def test_every_fresh_invocation_carries_the_id_its_harness_minted(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just the primary round: the contract repair too.

    **Fails on a ``_repair_contract`` that leaves ``new_session_id`` defaulted.** It is as
    session-less as a first round, so a harness that pre-assigns has to be able to name it;
    leaving it empty forces that harness to mint outside :func:`reviewer._mint_session`, which
    is exactly the seam this split exists to keep whole. The id is safe to carry precisely
    because the call is ``capture=False`` and it names a *new, empty* session -- never a
    resume, which ``tests/unit/test_harness.py`` asserts no harness can spell.
    """
    monkeypatch.setattr(reviewer, "_sessions", lambda _config: _MintingSessions())
    seen: list[Invocation] = []
    real = reviewer._run_invocation

    def spy(tgt: Target, run: Invocation, *, config: Config, scope: reviewer.LateScope | None = None) -> tuple[Review, bool]:
        seen.append(run)
        return real(tgt, run, config=config, scope=scope)

    monkeypatch.setattr(reviewer, "_run_invocation", spy)

    _run_scripted(activation, git_repo, tmp_path, "round1", _ROUND_1)
    _scripted_reviewer(tmp_path, "round2", _APPROVES)
    reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    execute_repair(activation, git_repo)
    assert [run for run in seen if not run.allow_supersedes], "a contract repair really did run"

    for run in seen:
        assert run.session_id == "", "every round here starts fresh"
        assert run.new_session_id == _MINTED_ID, f"{run.out_path.name} was launched without the minted id"


@pytest.mark.parametrize("delta", [0, 1000])
def test_the_lease_and_the_repair_reserve_track_the_harnesss_own_capture_window(delta: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both budgets are sized from the configured strategy, not from a fixed number.

    **Fails on a hard-coded 60-second session-list allowance**, which is what these were
    before the harness split and what a reader cannot tell apart by testing OpenCode alone --
    OpenCode's own window *is* 60. A harness that captures without a subprocess needs none of
    it, and a lease padded for a call that never happens is a lease an abandoned claim is
    honoured far past anything real; a reserve padded the same way skips repairs there is
    room for, losing a recoverable round each time.

    The reserve's assertion is exact rather than merely monotonic, because it is the one the
    shim's kill window depends on: every other term in it is fixed.
    """
    config = config_with(timeout_sec=900)
    monkeypatch.setattr(reviewer, "_sessions", lambda _config: _StubSessions(delta))

    assert reviewer._capture_timeout(config) == delta
    assert reviewer.settle_margin(config) == delta + 2 * math.ceil(reviewer.KILL_GRACE_SEC) + reviewer._PUBLISH_BUDGET_SEC

    # The lease grows with it too -- strictly, once the building stretch dominates.
    monkeypatch.setattr(reviewer, "_sessions", lambda _config: _StubSessions(delta + 10_000))
    wider = reviewer._active_review_reclaim_after(config)
    monkeypatch.setattr(reviewer, "_sessions", lambda _config: _StubSessions(delta))
    assert wider > reviewer._active_review_reclaim_after(config), "a slower harness must get a longer lease"


def test_the_repair_prompts_fallback_finding_is_a_line_the_gate_can_parse() -> None:
    """The prompt tells the reviewer what to emit when the tail states no findings; that
    line has to survive ``_FINDING_RE``, or the instruction guarantees a second failure."""
    fallback = "FINDING severity=high actionable=yes file=- | review transcript incomplete"
    assert fallback in arl.prompt_path("reviewer-repair").read_text()
    assert reviewer._FINDING_RE.match(fallback) is not None


@pytest.mark.usefixtures("_hook_budget")
def test_a_repair_that_emits_a_supersedes_fails_the_contract(activation: state.State, git_repo: Path) -> None:
    """The repair has no earlier round to reverse, so a reversal from it would be invented.

    It is shown a truncated tail of one transcript and nothing else, and
    ``prompts/reviewer-repair.md`` says so. Recording such a line is not inert: ``_record_round``
    stores it and ``oscillation`` counts reversals as one of the two signals that escalate a
    phase to ``NEEDS_HUMAN``, so a repair could manufacture a stall out of nothing. The whole
    block is refused rather than the line dropped -- a block written against the instructions
    the call was given earns no more trust in its remaining lines."""
    review = execute_repair(activation, git_repo, repair="supersedes")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract", "the primary's failure stands; nothing was recovered"
    assert review.supersedes == ""
    assert activation.get_array_of_dicts("round_history") == [], "no fabricated reversal reaches the history"


@pytest.mark.usefixtures("_hook_budget")
def test_an_ordinary_phase_round_may_still_supersede(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """The repair's restriction is ANDed with the target's, so it narrows that call and no other.

    The reversal channel is how a reviewer retracts a finding it no longer stands behind, and
    ``oscillation`` reads it to tell convergence from a stall -- closing it for every phase
    round while closing it for the repair would be a real regression, and an invisible one."""
    script = tmp_path / "superseding-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Round two.\\n\\n<<<ARL-FINDINGS>>>\\n"
        "FINDING severity=high actionable=yes file=a.txt:1 | still there\\n"
        "SUPERSEDES round=1 file=a.txt:9 | the null case cannot occur here\\n"
        "VERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    os.environ["ARL_REVIEWER_CMD"] = str(script)

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert review.verdict == "CHANGES_REQUIRED"
    assert "the null case cannot occur here" in review.supersedes
    assert activation.get_array_of_dicts("round_history")[0]["supersedes"] == [review.supersedes.rstrip("\n")]


def test_the_settlement_reserve_covers_every_deadline_after_a_repair() -> None:
    """``SETTLE_MARGIN_SEC`` is derived, and this pins the derivation to the steps it covers.

    The largest of them is ``_settle_pointer`` capturing a *fresh* round's session, which is a
    full ``SESSION_LIST_TIMEOUT_SEC`` listing -- and both that and the repair itself pay a
    SIGTERM-to-SIGKILL grace on top of their own deadline when they expire (``_kill_group``
    waits it out). A flat 60 covered none of that and lost the report the reserve exists to
    protect."""
    config = config_with()
    worst_case_deadlines = reviewer._capture_timeout(config) + 2 * reviewer.KILL_GRACE_SEC

    assert worst_case_deadlines <= reviewer.settle_margin(config)
    assert reviewer.settle_margin(config) - worst_case_deadlines >= reviewer._PUBLISH_BUDGET_SEC - 1, (
        "and there is still room left for the publishing the deadlines do not bound"
    )


def test_the_whole_post_repair_path_still_publishes_when_both_deadlines_expire(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst case the reserve is sized for, driven end to end with the clocks shrunk.

    A fresh round whose repair times out *and* whose session-capture listing then times out is
    the longest path out of ``execute``. Nothing beyond the two bounded calls and the flat
    publish allowance may sit on it -- so this asserts the report and the claim release
    actually happen after both expire, which is what a shim kill in between would have cost."""
    monkeypatch.setattr(reviewer, "REPAIR_TIMEOUT_SEC", 1)
    monkeypatch.setattr(opencode_harness, "SESSION_LIST_TIMEOUT_SEC", 1)
    hanging_list = tmp_path / "hanging-session-list.sh"
    hanging_list.write_text("#!/usr/bin/env bash\nsleep 30\n")
    hanging_list.chmod(0o755)
    os.environ["ARL_SESSION_LIST_CMD"] = str(hanging_list)
    os.environ["ARL_FAKE_REPAIR_SLEEP"] = "10"

    review = execute_repair(activation, git_repo, repair="slow")

    assert review.verdict == "OP_FAILURE"
    assert review.kind == "contract"
    assert Path(review.report).is_file(), "the report survived both expiries"
    assert activation.data.get("active_review") in (None, {}), "and the active-review slot was released"


def test_the_repair_reserve_is_what_the_budget_check_actually_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """One boundary, stated once: the check is ``remaining >= repair + reserve``."""
    needed = reviewer.REPAIR_TIMEOUT_SEC + reviewer.settle_margin(config_with())
    # A second either side of the boundary rather than exactly on it: the clock is real, and a
    # test that lands on it to the microsecond is a flake, not a proof.
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", float(needed + 1))
    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic())
    assert reviewer._repair_fits(config_with())

    monkeypatch.setattr(cli, "HOOK_STARTED", time.monotonic() - 2)
    assert not reviewer._repair_fits(config_with())
