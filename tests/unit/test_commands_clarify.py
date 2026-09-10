"""``clarify`` -- one prose question about a review that already ran.

The command grants nothing and parses no verdict, so the tests here are about *scope* and
*targeting*: that it leaves every fingerprinted field and ``round_history`` byte-identical,
that its argv never continues a session, and that it points at the most recent round's own
bundle rather than at whatever the continuity pointer happens to name.
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

import shutil
import time
from pathlib import Path
from typing import Any

from conftest import run_bootstrap
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_posttool import COMMIT
from test_commands_pretool import SESSION, active, patch_state, pretool

from arl import reviewer
from arl.config import Config

_FINGERPRINT = (
    "armed_at",
    "baseline_tree",
    "session_id",
    "status",
    "phase",
    "last_approved_tree",
    "pending_approved_tree",
    "pending_head",
    "pending_command",
    "activation_generation",
    "round_history",
)


def clarify(repo: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    proc = run_bootstrap(["clarify", *args], cwd=repo, env=env)
    return proc.returncode, proc.stdout


def _round(repo: Path, env: dict[str, str], content: str) -> None:
    """Drive one denied review of phase 1, leaving a ``round_history`` entry and its bundle."""
    (repo / "a.txt").write_text(content)
    verdict, _ = pretool(repo, env, command=COMMIT)
    assert verdict == "deny"


def test_clarify_leaves_the_fingerprint_and_round_history_untouched(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    before = read_state(env, git_repo, SESSION)
    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "what did finding 1 mean?")
    assert code == 0, out
    after = read_state(env, git_repo, SESSION)

    for key in _FINGERPRINT:
        assert after[key] == before[key], key
    assert after["clarifications"] == 1
    assert after["clarify_seq"] == 1
    assert "Clarification." in out
    assert "what did finding 1 mean?" in out


def test_the_question_lands_under_context_wrapped_as_evidence(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The one Claude-composed string that reaches the reviewer, and where it is allowed to be.

    Three separate claims, each load-bearing for the evidence boundary
    (``docs/security.md``, ``reviewer``'s module docstring):

    - it is written under ``context/``, a **sibling** of ``bundles/``. ``bundles/`` holds
      gate-generated evidence only, and a continued reviewer's read grant covers the whole
      bundles root -- so a question written in there would be model-authored text at a stable,
      re-openable path inside the evidence directory;
    - nothing under ``bundles/`` holds it, asserted by searching the tree rather than by
      trusting the path the writer chose;
    - it is wrapped in the evidence-not-instruction fence, so the reviewer is told the text is
      a record of what an agent is unsure about and not something that changes its rules.
    """
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")
    question = "what did finding 1 mean?"

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", question)

    assert code == 0, out
    act_dir = state_dir(env, git_repo, SESSION)
    written = act_dir / "context" / "001-question.txt"
    assert written.is_file(), "the question is written under context/, beside bundles/"
    text = written.read_text()
    assert question in text
    assert "NOT an instruction" in text
    assert not any(question in path.read_text(errors="replace") for path in (act_dir / "bundles").rglob("*") if path.is_file()), (
        "no file under bundles/ may hold model-authored text"
    )


def test_clarify_argv_never_continues_a_session(git_repo: Path) -> None:
    attachments = [git_repo / "bundles" / "001" / "range.txt", git_repo / "bundles" / "001" / "changes.00.diff"]
    argv = reviewer.clarify_argv(
        str(git_repo),
        attachments,
        git_repo / "context" / "001-question.txt",
        "review-loop clarify [deadbeef/001]",
        config=Config({"model": "m", "variant": "", "pure": True}),
    )
    assert "-s" not in argv
    assert "--title" in argv
    # Exactly the supplied attachments plus the one question -- no globbing here.
    assert argv.count("-f") == 3
    assert [argv[i + 1] for i, tok in enumerate(argv) if tok == "-f"] == [*map(str, attachments), str(git_repo / "context" / "001-question.txt")]


def test_clarify_targets_the_last_rounds_bundle_not_the_session_pointer(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")
    _round(git_repo, env, "v2\n")

    history = read_state(env, git_repo, SESSION)["round_history"]
    assert isinstance(history, list)
    assert [entry["seq"] for entry in history] == [1, 2]

    # A continuity pointer that names an earlier round -- the mismatch that motivated
    # running clarify cold against round_history rather than against reviewer_session.
    patch_state(env, git_repo, reviewer_session={"round": 1, "id": "ses_stale00"})

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "which round stands?")
    assert code == 0, out
    assert "bundles/002" in out
    assert "bundles/001" not in out


def test_clarify_is_refused_before_any_round(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out = clarify(git_repo, env, "--question", "anything?")
    assert code == 1
    assert "no review has run" in out


def test_clarify_refuses_when_the_round_bundle_lost_a_diff_chunk(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`range.txt` alone is not an intact bundle -- a missing `changes.NN.diff` would have the
    reviewer answer from less evidence than the verdict was formed on."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    act = state_dir(env, git_repo, SESSION)
    (act / "bundles" / "001" / "changes.00.diff").unlink()

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 1
    assert "no longer on disk" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 0


def test_clarify_never_attaches_a_diff_file_the_manifest_does_not_name(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A `changes.NN.diff` beyond the manifest -- here a symlink to a repo file -- must not
    ride `-f` into the provider prompt.

    The manifest is authoritative, so the plant is simply never in the attachment set. It is
    *ignored* rather than fatal, deliberately: a stray file must not be able to break clarify
    for a round permanently, and it can no longer reach the provider either way."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    (git_repo / "secret.txt").write_text("secret\n")
    (state_dir(env, git_repo, SESSION) / "bundles" / "001" / "changes.99.diff").symlink_to(git_repo / "secret.txt")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 0, out
    assert "secret" not in out
    assert "changes.99" not in out


def test_clarify_rejects_a_bundle_directory_that_is_a_symlink(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The plant a per-file symlink check cannot see.

    Every file *below* a symlinked ``bundles/<seq>/`` is an ordinary regular file, so
    ``is_file() and not is_symlink()`` passes on each of them and the whole planted directory
    rides ``-f`` into the provider prompt. Containment has to be proved by walking the
    components, not by inspecting the leaves. Fails on the old code, which ran the clarify."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    act = state_dir(env, git_repo, SESSION)
    real = act / "bundles" / "001"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "chunks").write_text("1\n")
    (planted / "range.txt").write_text("someone else's range\n")
    (planted / "changes.00.diff").write_text("someone else's secrets\n")

    shutil.rmtree(real)
    real.symlink_to(planted, target_is_directory=True)
    assert (real / "range.txt").is_file() and not (real / "range.txt").is_symlink(), "both naive checks pass"

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 1
    assert "no longer on disk" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 0


def test_clarify_discards_a_reply_when_the_activation_moves_during_the_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify-mutate"), "--question", "q")
    assert code == 1
    assert "discarded" in out
    # The allowance is still spent -- the counter bump landed before the invocation.
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_clarify_discards_a_reply_when_a_newer_round_completes_during_the_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`round_history` is not in `hooks.Activation`, so a concurrent `reviewer.execute`
    finishing a newer round leaves the fingerprint intact -- the round check is what catches it."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify-supersede"), "--question", "q")
    assert code == 1
    assert "no longer the latest" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_clarify_is_refused_past_max_clarifications(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    ask_env = armed_env(clean_env, ARL_FAKE_MODE="clarify", ARL_MAX_CLARIFICATIONS="1")
    assert clarify(git_repo, ask_env, "--question", "one")[0] == 0
    code, out = clarify(git_repo, ask_env, "--question", "two")
    assert code == 1
    assert "already used" in out
    assert "accept" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1


# --------------------------------------------------------------------------
# A clarify may retract a finding of the round it answers
# --------------------------------------------------------------------------

_RETRACTION = "SUPERSEDES round=1 file=a.txt:1 | the premise was wrong"


def _retract(repo: Path, clean_env: dict[str, str], mode: str = "clarify-retract", **env: str) -> tuple[int, str]:
    return clarify(repo, armed_env(clean_env, ARL_FAKE_MODE=mode, **env), "--question", "the lint output shows finding 1's premise is wrong")


def _clarify_history(env: dict[str, str], repo: Path) -> list[Any]:
    history = read_state(env, repo, SESSION)["clarify_history"]
    assert isinstance(history, list)
    return history


def _one_denied_round(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> dict[str, str]:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")
    return env


def test_a_retraction_is_recorded_and_nothing_the_gate_reads_moves(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)
    before = read_state(env, git_repo, SESSION)

    code, out = _retract(git_repo, clean_env)
    assert code == 0, out
    after = read_state(env, git_repo, SESSION)

    for key in (*_FINGERPRINT, "reviewer_session", "active_review", "review_attempts", "failures"):
        assert after.get(key) == before.get(key), key
    assert after["clarifications"] == 1
    history = after["clarify_history"]
    assert isinstance(history, list)
    assert len(history) == 1
    record = dict(history[0])
    assert isinstance(record.pop("at"), int)
    assert record == {
        "seq": 1,
        "label": "phase1",
        "phase": 1,
        "generation": before["activation_generation"],
        "round_seq": 1,
        "supersedes": [_RETRACTION],
    }
    assert "You are right, the premise was wrong." in out
    assert "recorded 1 retraction(s)" in out
    assert "next review of phase 1" in out
    assert "round 1's record" in out
    assert "<<<ARL-FINDINGS>>>" not in out, "the block is the gate's to record, not prose to print"


def test_the_next_round_is_shown_the_retraction_under_the_round_it_retracts(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The live-run regression: round 2 re-raised a finding round 1's reviewer had conceded,
    because nothing of the exchange reached it. Fails on the old code, which recorded nothing."""
    env = _one_denied_round(git_repo, tmp_path, clean_env)
    assert _retract(git_repo, clean_env)[0] == 0
    _round(git_repo, env, "v2\n")

    act_dir = state_dir(env, git_repo, SESSION)
    text = (act_dir / "context" / "002-prior-rounds.txt").read_text()
    round_one = text.index("### round 1")
    lead_in = text.index("Retracted by this round's reviewer when asked about it:\n", round_one)
    assert text.index(_RETRACTION, lead_in) == lead_in + len("Retracted by this round's reviewer when asked about it:\n")
    assert not any("the premise was wrong" in path.read_text(errors="replace") for path in (act_dir / "bundles").rglob("*") if path.is_file()), (
        "a retraction is model-authored text and stays out of bundles/"
    )


def test_a_malformed_retraction_block_records_nothing_but_prints_the_prose(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, "clarify-retract-malformed")
    assert code == 0, out
    assert "On reflection, there is a different problem." in out
    assert "did not validate" in out
    assert "nothing was recorded" in out
    after = read_state(env, git_repo, SESSION)
    assert after["clarify_history"] == []
    assert after["clarifications"] == 1


def test_retractions_naming_no_finding_of_the_round_are_dropped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, "clarify-retract-unmatched")
    assert code == 0, out
    assert "2 retraction line(s) named no finding of round 1" in out
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_a_retraction_naming_the_wrong_round_is_dropped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, ARL_FAKE_ROUND="2")
    assert code == 0, out
    assert "named no finding of round 1" in out
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_a_finding_is_retracted_at_most_once(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, ARL_FAKE_REPEAT="1")
    assert code == 0, out
    assert "recorded 1 retraction(s)" in out
    assert "1 line(s) named no finding of round 1 or repeated one already recorded" in out
    assert _clarify_history(env, git_repo)[0]["supersedes"] == [_RETRACTION]

    code, out = _retract(git_repo, clean_env)
    assert code == 0, out
    assert "repeated one already recorded; nothing was recorded" in out
    assert len(_clarify_history(env, git_repo)) == 1


def test_retractions_past_the_evidence_caps_are_not_trimmed_but_dropped_whole(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, ARL_MAX_FINDINGS_BYTES="10")
    assert code == 0, out
    assert "exceed max_findings / max_findings_bytes" in out
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_a_review_in_flight_refuses_a_clarify_before_anything_is_spent(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)
    generation = read_state(env, git_repo, SESSION)["activation_generation"]
    patch_state(env, git_repo, active_review={"phase1": {"generation": generation, "claimed_at": int(time.time()), "claim_id": "live"}})

    code, out = _retract(git_repo, clean_env)
    assert code == 1
    assert "running right now" in out
    after = read_state(env, git_repo, SESSION)
    assert after["clarifications"] == 0
    assert after["clarify_history"] == []


def test_an_expired_review_lease_refuses_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A run whose lease lapsed cannot publish, so a clarify is not racing anything that can land."""
    env = _one_denied_round(git_repo, tmp_path, clean_env)
    generation = read_state(env, git_repo, SESSION)["activation_generation"]
    patch_state(env, git_repo, active_review={"phase1": {"generation": generation, "claimed_at": int(time.time()) - 100_000, "claim_id": "dead"}})

    code, out = _retract(git_repo, clean_env)
    assert code == 0, out
    assert len(_clarify_history(env, git_repo)) == 1


def test_a_review_started_during_the_clarify_withholds_the_retraction(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """That review's ``prior-rounds.txt`` is already written, so a retraction recorded now would
    be shown to nobody -- and no ``hooks.Activation`` field moved to say so."""
    env = _one_denied_round(git_repo, tmp_path, clean_env)

    code, out = _retract(git_repo, clean_env, "clarify-claim")
    assert code == 0, out
    assert "You are right, the premise was wrong." in out
    assert "started while the reviewer answered" in out
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []


def test_a_duplicated_round_seq_records_no_retraction(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = _one_denied_round(git_repo, tmp_path, clean_env)
    _round(git_repo, env, "v2\n")
    history = read_state(env, git_repo, SESSION)["round_history"]
    assert isinstance(history, list)
    history[0]["seq"] = 2
    patch_state(env, git_repo, round_history=history)

    code, out = _retract(git_repo, clean_env, ARL_FAKE_ROUND="2")
    assert code == 0, out
    assert "could not be verified" in out
    assert read_state(env, git_repo, SESSION)["clarify_history"] == []
