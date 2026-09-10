"""Late-round blocking scope (late_block_severity).

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
from pathlib import Path

import pytest
from conftest import config_with
from reviewer_common import execute_fake, target_for

from arl import config as arl_config
from arl import gitsnap, reviewer, state
from arl.reviewer import BundleError

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")

# --------------------------------------------------------------------------
# Late-round blocking scope (late_block_severity)
# --------------------------------------------------------------------------


def _scope(changed: tuple[str, ...] = (), prior: tuple[str, ...] = ()) -> reviewer.LateScope:
    return reviewer.LateScope(changed_paths=frozenset(changed), prior_files=frozenset(prior))


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
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config_with(), allow_supersedes=True)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_UNTOUCHED}\n"
    assert review.deferred == ""


def test_parse_with_a_scope_defers_a_new_medium_outside_the_changed_paths(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",), prior=("known.py:1", "known.py"))
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "APPROVED"
    assert review.findings == ""
    assert review.deferred == f"{MEDIUM_UNTOUCHED}\n"
    assert review.all_findings == f"{MEDIUM_UNTOUCHED}\n", "deferred is still reported and recorded"


def test_parse_with_a_scope_blocks_a_medium_in_a_changed_path(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_CHANGED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_CHANGED}\n"
    assert review.deferred == ""


def test_parse_with_a_scope_blocks_a_high_anywhere(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    review = reviewer.parse(_write_block(tmp_path, HIGH_UNTOUCHED), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{HIGH_UNTOUCHED}\n"


def test_parse_with_a_scope_blocks_a_medium_in_a_prior_round_file(tmp_path: Path) -> None:
    scope = _scope(changed=(), prior=("known.py:1", "known.py"))
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_PRIOR), config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.findings == f"{MEDIUM_PRIOR}\n"


def test_late_block_severity_medium_restores_the_ordinary_rule(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    config = config_with(late_block_severity="medium")
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_UNTOUCHED), config=config, allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.deferred == ""


def test_late_block_severity_below_block_severity_is_clamped_up(tmp_path: Path) -> None:
    """``late_block_severity`` can only defer, never widen: set below ``block_severity`` it
    reads as ``block_severity`` itself, so a *low* finding still does not block."""
    scope = _scope(changed=("changed.py",))
    config = config_with(block_severity="high", late_block_severity="low")
    assert arl_config.late_threshold_rank(config) == arl_config.threshold_rank("high")
    review = reviewer.parse(_write_block(tmp_path, MEDIUM_CHANGED), config=config, allow_supersedes=True, scope=scope)
    assert review.verdict == "APPROVED"
    assert review.deferred == "", "below block_severity is not deferred, it is simply non-blocking"


def test_the_reviewers_own_changes_required_still_wins_over_a_deferred_only_set(tmp_path: Path) -> None:
    scope = _scope(changed=("changed.py",))
    out = _write_block(tmp_path, MEDIUM_UNTOUCHED, verdict="CHANGES_REQUIRED")
    review = reviewer.parse(out, config=config_with(), allow_supersedes=True, scope=scope)
    assert review.verdict == "CHANGES_REQUIRED", "stricter wins; the scope narrows the gate's recomputation only"
    assert review.findings == ""
    assert review.deferred == f"{MEDIUM_UNTOUCHED}\n"


def test_a_scope_never_defers_a_finding_below_block_severity_into_the_deferred_set(tmp_path: Path) -> None:
    low = "FINDING severity=low actionable=yes file=untouched.py:1 | low"
    review = reviewer.parse(_write_block(tmp_path, low), config=config_with(), allow_supersedes=True, scope=_scope(changed=("x",)))
    assert review.verdict == "APPROVED"
    assert review.deferred == ""
    assert review.all_findings == f"{low}\n"


# -- late_scope: when a scope exists at all ----------------------------------


def test_round_one_has_no_scope(activation: state.State, git_repo: Path) -> None:
    assert reviewer.late_scope(target_for(git_repo), state=activation) is None


def test_a_final_review_has_no_scope(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    assert reviewer.late_scope(target_for(git_repo, scope="final"), state=activation) is None


def test_round_two_scope_holds_the_paths_changed_since_round_one_and_round_ones_files(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")  # a.txt:1, high
    (git_repo / "b.txt").write_text("second round\n")
    scope = reviewer.late_scope(target_for(git_repo), state=activation)
    assert scope is not None
    assert scope.changed_paths == frozenset({"b.txt"})
    assert scope.prior_files == frozenset({"a.txt:1", "a.txt"})


def test_prior_files_are_stored_with_a_leading_dot_slash_normalised_away(activation: state.State, git_repo: Path) -> None:
    """``covers`` normalises the value it is asked about, so the stored side must match."""
    os.environ["ARL_FAKE_FILE"] = "./README.md:4"  # `medium-file` emits this value verbatim
    execute_fake(activation, git_repo, "medium-file")
    (git_repo / "b.txt").write_text("second round\n")
    scope = reviewer.late_scope(target_for(git_repo), state=activation)
    assert scope is not None
    assert scope.prior_files == frozenset({"README.md:4", "README.md"})
    assert scope.covers("./README.md:4")


def test_round_two_scope_keeps_both_sides_of_a_rename(activation: state.State, git_repo: Path) -> None:
    (git_repo / "renamed-src.txt").write_text("x" * 200 + "\n")
    execute_fake(activation, git_repo, "approve")
    (git_repo / "renamed-src.txt").rename(git_repo / "renamed-dst.txt")
    scope = reviewer.late_scope(target_for(git_repo), state=activation)
    assert scope is not None
    assert {"renamed-src.txt", "renamed-dst.txt"} <= scope.changed_paths, "a reviewer may name either side"
    assert scope.covers("renamed-src.txt:1")
    assert scope.covers("renamed-dst.txt:1")


def test_a_malformed_round_history_entry_disables_the_scope(activation: state.State, git_repo: Path) -> None:
    """Dropping a bad line is right for display and wrong here: a missing path authorises a
    deferral, so any doubt about the history disables the scope entirely."""
    execute_fake(activation, git_repo, "changes")
    history = activation.get_array_of_dicts("round_history")
    history[0]["findings"] = ["FINDING severity=medium actionable=yes file=a.txt:1 | ok", "not a finding line"]
    activation.update(round_history=history)
    activation.save()
    (git_repo / "b.txt").write_text("second round\n")
    assert reviewer.late_scope(target_for(git_repo), state=activation) is None


@pytest.mark.parametrize("field", ["seq", "tree", "verdict", "findings"])
def test_a_tampered_round_history_field_disables_the_scope(activation: state.State, git_repo: Path, field: str) -> None:
    execute_fake(activation, git_repo, "changes")
    history = activation.get_array_of_dicts("round_history")
    history[0][field] = {"seq": "1", "tree": "not-an-id", "verdict": "OK", "findings": "not-a-list"}[field]
    activation.update(round_history=history)
    activation.save()
    (git_repo / "b.txt").write_text("second round\n")
    assert reviewer.late_scope(target_for(git_repo), state=activation) is None


def test_an_unresolvable_previous_tree_disables_the_scope(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    history = activation.get_array_of_dicts("round_history")
    history[0]["tree"] = "0" * 40
    activation.update(round_history=history)
    activation.save()
    (git_repo / "b.txt").write_text("second round\n")
    assert reviewer.late_scope(target_for(git_repo), state=activation) is None


def test_a_failed_changed_path_listing_is_a_bundle_error_not_a_scope(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")

    def broken(repo: str, base: str, head: str) -> frozenset[str]:
        raise gitsnap.ChangedPathsUnavailable("simulated git failure")

    monkeypatch.setattr(reviewer, "changed_paths_strict", broken)
    with pytest.raises(BundleError, match="simulated git failure"):
        reviewer.late_scope(target_for(git_repo), state=activation)


def test_a_scope_build_failure_is_an_op_failure_of_kind_bundle_never_an_approval(
    activation: state.State, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")

    def broken(repo: str, base: str, head: str) -> frozenset[str]:
        raise gitsnap.ChangedPathsUnavailable("simulated git failure")

    monkeypatch.setattr(reviewer, "changed_paths_strict", broken)
    review = execute_fake(activation, git_repo, "approve")
    assert review.verdict == "OP_FAILURE"
    assert review.kind == "bundle"
    assert "simulated git failure" in review.error
    assert len(activation.get_array_of_dicts("round_history")) == 1, "a refused review records no round"


# -- end to end through execute ----------------------------------------------


def test_round_one_blocks_a_new_medium_wherever_it_is(activation: state.State, git_repo: Path) -> None:
    os.environ["ARL_FAKE_FILE"] = "a.txt:1"
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.deferred == ""


def test_round_two_defers_a_new_medium_in_an_untouched_file(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")  # round 1: a.txt:1 high
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "README.md:4"  # neither changed since round 1 nor named before
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "APPROVED"
    assert review.findings == ""
    assert "README.md:4" in review.deferred
    entry = activation.get_array_of_dicts("round_history")[-1]
    assert entry["verdict"] == "APPROVED"
    assert any("README.md:4" in line for line in entry["findings"]), "deferred is recorded as evidence"
    assert "## Deferred findings" in Path(review.report).read_text()


def test_round_two_blocks_the_same_medium_in_a_touched_file(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "b.txt:1"
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED"
    assert "b.txt:1" in review.findings


def test_round_two_blocks_a_medium_in_a_file_round_one_named_at_another_line(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")  # a.txt:1
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "a.txt:9"
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED"


def test_round_two_blocks_a_high_in_an_untouched_file(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "approve")
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "README.md"
    review = execute_fake(activation, git_repo, "changes-file")  # high
    assert review.verdict == "CHANGES_REQUIRED"


def test_a_deferred_finding_blocks_the_next_review_of_the_same_phase(activation: state.State, git_repo: Path) -> None:
    """Deferral applies to one approval only: the recorded line puts its path in
    ``prior_files`` for every later round of this phase."""
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "README.md:4"
    assert execute_fake(activation, git_repo, "medium-file").verdict == "APPROVED"

    (git_repo / "b.txt").write_text("third round\n")
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED"
    assert "README.md:4" in review.findings


def test_round_two_with_late_block_severity_medium_blocks_as_before(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "README.md:4"
    review = execute_fake(activation, git_repo, "medium-file", config=config_with(late_block_severity="medium"))
    assert review.verdict == "CHANGES_REQUIRED"


def test_range_txt_discloses_the_blocking_rules_and_the_policy_round(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    first = (activation.act_dir / "bundles" / "001" / "range.txt").read_text()
    assert "## Blocking rules\n" in first
    assert "review round of this phase: 1\n" in first
    assert "late_block_severity: high\n" in first

    (git_repo / "b.txt").write_text("second round\n")
    execute_fake(activation, git_repo, "approve")
    second = (activation.act_dir / "bundles" / "002" / "range.txt").read_text()
    assert "review round of this phase: 2\n" in second
    assert "From round 2 on, a finding blocks only if its path is in *Changed since round 1*" in second


def test_range_txt_says_when_the_late_rule_is_off_for_a_later_round(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    history = activation.get_array_of_dicts("round_history")
    history[0]["findings"] = ["tampered"]
    activation.update(round_history=history)
    activation.save()
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "README.md:4"
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED", "no scope: the ordinary rule"
    text = (activation.act_dir / "bundles" / "002" / "range.txt").read_text()
    assert "The late-round rule is not in effect for this round" in text


def test_a_dot_slash_deferred_finding_still_blocks_the_next_review(activation: state.State, git_repo: Path) -> None:
    """Regression: deferral is one approval's grace whatever spelling the reviewer used.

    ``covers`` normalises a leading ``./`` on the finding side, so ``prior_files`` has to hold
    the normalised form too -- storing the raw ``./README.md:4`` made the very same finding
    miss its own recorded line on every later round and be deferred again, indefinitely."""
    execute_fake(activation, git_repo, "changes")
    (git_repo / "b.txt").write_text("second round\n")
    os.environ["ARL_FAKE_FILE"] = "./README.md:4"
    assert execute_fake(activation, git_repo, "medium-file").verdict == "APPROVED", "round 2 defers it once"

    (git_repo / "b.txt").write_text("third round\n")
    review = execute_fake(activation, git_repo, "medium-file")
    assert review.verdict == "CHANGES_REQUIRED", "round 3 must treat it as a known finding"
    assert "./README.md:4" in review.findings
    assert review.deferred == ""
