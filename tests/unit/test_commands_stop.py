"""``gate-stop`` -- the backstop that decides whether a turn may end.

Two shapes of answer, and every test here is about which one comes out:

- ``{"decision":"block"}`` sends the turn back, and is counted so a wedged loop escalates
  rather than blocking forever;
- ``{"systemMessage":…}`` (or nothing at all) lets the turn end -- which is **not** an
  approval, and the tests assert the messages say so.

The one transition that disarms the mode is ``COMPLETE``. By default (``final_review=false``)
it is reached with no cumulative review at all, on the strength of every phase having gone
through the per-commit gate or the unreviewed-work sweep alone -- neither of which is always a
model review (an unchanged, already-approved or ignore_globs-matched tree passes either one
without a call); with ``final_review=true`` it is reachable only through an approving final
cumulative review that nothing invalidated while it ran. Tests that exercise the reviewed path
set ``OCRL_FINAL_REVIEW=true`` explicitly so their names stay true.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import BOOTSTRAP, git, run_bootstrap, run_hook
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_posttool import COMMIT, gated_commit
from test_commands_pretool import SESSION, active, active_until, arm, patch_state, payload
from test_commands_races import _SETTLE, activation_lock, reviewer_stub

from ocrl import commands as commands_module
from ocrl import config as config_module
from ocrl.commands import completion
from ocrl.state import State


def stop(repo: Path, env: dict[str, str], **kwargs: object) -> dict[str, object]:
    """Run the Stop gate and return its parsed response (``{}`` for zero bytes)."""
    proc = run_hook("gate-stop", payload(repo, **kwargs), cwd=repo, env=env)  # type: ignore[arg-type]
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout:
        return {}
    document: dict[str, object] = json.loads(proc.stdout)
    return document


def blocked(response: dict[str, object]) -> str:
    assert response.get("decision") == "block", response
    return str(response["reason"])


def ended(response: dict[str, object]) -> str:
    assert "decision" not in response, response
    return str(response.get("systemMessage", ""))


def committed_phase(repo: Path, env: dict[str, str], text: str = "work\n") -> None:
    """One whole phase: gate, commit, confirm."""
    gated_commit(repo, env, text)
    proc = run_hook("confirm-commit", payload(repo, command=COMMIT), cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------
# Rule 0
# --------------------------------------------------------------------------


def test_no_session_id_ends_the_turn_saying_nothing_was_reviewed(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Nothing can be recorded without a key, so the turn ends -- explicitly not approved."""
    message = ended(stop(git_repo, clean_env, session=""))

    assert "cannot identify this activation" in message
    assert "not an approval" in message


def test_no_pointer_records_arm_failed_and_blocks(git_repo: Path, clean_env: dict[str, str]) -> None:
    reason = blocked(stop(git_repo, clean_env))

    assert "arming never ran" in reason
    assert read_state(clean_env, git_repo, SESSION)["status"] == "ARM_FAILED"


def test_the_unstarted_arm_block_is_counted_rather_than_endless(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Without counting, the same message repeats on every turn end until the host cap hits."""
    env = {**clean_env, "OCRL_MAX_STOP_BLOCKS": "1"}
    blocked(stop(git_repo, env))

    message = ended(stop(git_repo, env))

    assert "STALLED" in message
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


# --------------------------------------------------------------------------
# Status branches
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["COMPLETE", "DISARMED", "RESUMED"])
def test_a_finished_activation_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status=status)

    assert stop(git_repo, env) == {}


def test_needs_human_ends_the_turn_without_approving_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="NEEDS_HUMAN", reason="a reviewer failure")

    message = ended(stop(git_repo, env))

    assert "still in NEEDS_HUMAN" in message
    assert "not reviewed to completion" in message


def test_an_unfrozen_phase_list_blocks_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    reason = blocked(stop(git_repo, env))

    assert "the phase list has not been frozen" in reason
    assert "set-phases" in reason


def test_an_unfinished_reconcile_blocks_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RECONCILE", reason="a commit diverged", bad_commit_parent="abc123")

    reason = blocked(stop(git_repo, env))

    assert "the reconcile is unfinished" in reason
    assert "git reset --soft abc123" in reason


def test_an_expired_activation_blocks_rather_than_disarming(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, armed_at=1)

    reason = blocked(stop(git_repo, env))

    assert "past ttl_hours" in reason


def test_arm_failed_blocks_and_names_the_reason(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_hook("gate-stop", payload(git_repo), cwd=git_repo, env=env)
    assert proc.returncode == 0
    patch_state(env, git_repo, reason="the plan path does not resolve")

    reason = blocked(stop(git_repo, env))

    assert "arming failed" in reason
    assert "the plan path does not resolve" in reason


# --------------------------------------------------------------------------
# Defer
# --------------------------------------------------------------------------


def test_a_deferred_turn_may_end_once(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, defer_pending=True)

    message = ended(stop(git_repo, env))

    assert "deferred once at your request" in message
    assert read_state(env, git_repo, SESSION)["defer_pending"] is False

    # And exactly once: the next turn end is gated again.
    assert blocked(stop(git_repo, env))


# --------------------------------------------------------------------------
# The sweep, the outstanding phases, and the final review
# --------------------------------------------------------------------------


def test_uncommitted_work_is_reviewed_before_the_turn_may_end(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Work that never reached a commit still gets reviewed; a blocking review blocks."""
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")

    reason = blocked(stop(git_repo, env))

    assert "uncommitted work that OpenCode requires changes to" in reason
    assert "Returns success on a failed lookup" in reason


def test_a_failed_sweep_review_is_never_an_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FAKE_MODE="nonzero")
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")

    reason = blocked(stop(git_repo, env))

    assert "the review of the uncommitted work failed" in reason


def test_a_generation_bump_during_the_sweep_discards_the_approval(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A same-session ``resume`` that changes the model or plan mid-sweep bumps
    ``activation_generation``; the sweep's approval, decided against the *old* generation, must
    not be trusted for the new one -- otherwise a later turn could see the tree already marked
    approved and skip reviewing it again, completing under a scope no review ever actually
    covered.

    The reviewer stand-in bumps ``activation_generation`` directly, standing in
    deterministically for a concurrent same-session ``resume`` landing mid-review.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    approved_before = read_state(env, git_repo, SESSION)["approved_trees"]
    (git_repo / "unreviewed.txt").write_text("never gated\n")
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    script = tmp_path / "generation-bumping-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["activation_generation"] = int(d.get("activation_generation") or 0) + 1\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'Looks fine.\\n\\n'\n"
        "printf '<<<OCRL-FINDINGS>>>\\n'\n"
        "printf 'VERDICT APPROVED\\n'\n"
        "printf '<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    reason = blocked(stop(git_repo, env))

    assert "the activation changed while the unreviewed-work sweep was running" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["approved_trees"] == approved_before, "the tree with unreviewed.txt must not have been added"


def test_a_sweep_finding_the_activation_resumed_mid_review_does_not_mutate_it(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A cross-session ``resume`` retiring this activation into ``RESUMED`` mid-sweep must end
    the turn quietly, through ``_ended``, and must not write to the now-retired ``state.json``
    at all -- not even a content-identical resave. AGENTS.md is explicit that a retired
    activation directory is never mutated again.

    The reviewer stand-in writes ``RESUMED`` directly, then snapshots the file's bytes to a
    marker immediately afterward -- standing in deterministically for the moment a real
    cross-session ``resume`` would have retired this session. If anything downstream still
    writes the document, the marker and the file diverge.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    marker = tmp_path / "snapshot-after-resume.json"
    script = tmp_path / "resuming-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        f"m = pathlib.Path({str(marker)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "RESUMED"\n'
        'd["resumed_into"] = "some-other-session"\n'
        'd["reason"] = "retired by resume into session some-other-session"\n'
        "p.write_text(json.dumps(d))\n"
        "m.write_text(p.read_text())\n"
        "PY\n"
        "printf 'Looks fine.\\n\\n'\n"
        "printf '<<<OCRL-FINDINGS>>>\\n'\n"
        "printf 'VERDICT APPROVED\\n'\n"
        "printf '<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    response = stop(git_repo, env)

    assert "decision" not in response, response
    assert state_path.read_text() == marker.read_text(), "the retired activation must not be rewritten at all"


def test_a_completion_refusal_finding_the_activation_resumed_does_not_mutate_it(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The same guarantee as the sweep's, for the completion-refusal path: a cross-session
    ``resume`` retiring this activation into ``RESUMED`` while the final review runs must end
    the turn quietly and never write to the now-retired ``state.json``.
    """
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    marker = tmp_path / "snapshot-after-resume.json"
    script = tmp_path / "resuming-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        f"m = pathlib.Path({str(marker)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "RESUMED"\n'
        'd["resumed_into"] = "some-other-session"\n'
        'd["reason"] = "retired by resume into session some-other-session"\n'
        "p.write_text(json.dumps(d))\n"
        "m.write_text(p.read_text())\n"
        "PY\n"
        "printf 'Looks fine.\\n\\n'\n"
        "printf '<<<OCRL-FINDINGS>>>\\n'\n"
        "printf 'VERDICT APPROVED\\n'\n"
        "printf '<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    response = stop(git_repo, env)

    assert "decision" not in response, response
    assert state_path.read_text() == marker.read_text(), "the retired activation must not be rewritten at all"


def test_outstanding_phases_block_the_turn(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)

    reason = blocked(stop(git_repo, env))

    assert "phases 2..2 are still outstanding" in reason
    assert "phase two" in reason


def test_a_dirty_worktree_blocks_before_the_final_review(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    (git_repo / "left-over.txt").write_text("uncommitted\n")

    reason = blocked(stop(git_repo, env))

    assert "the worktree is not clean" in reason
    assert "left-over.txt" in reason


def test_an_approving_final_review_completes_the_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")


# --------------------------------------------------------------------------
# The pause target
# --------------------------------------------------------------------------


def test_stop_blocks_below_the_pause_target_exactly_as_without_one(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Below the target, a pause changes nothing: the outstanding-phases block is unchanged."""
    env = armed_env(clean_env)
    active_until(git_repo, tmp_path, env, 2, "one", "two", "three")

    reason = blocked(stop(git_repo, env))

    assert "phases 1..3 are still outstanding" in reason
    assert "one" in reason


def test_stop_pauses_at_the_target_without_the_final_review(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active_until(git_repo, tmp_path, env, 2, "one", "two", "three")
    committed_phase(git_repo, env, "phase one work\n")
    committed_phase(git_repo, env, "phase two work\n")
    before = read_state(env, git_repo, SESSION)
    baseline, approved_before = before["baseline_tree"], before["approved_trees"]

    message = ended(stop(git_repo, env))

    assert "paused" in message
    assert "pause target (phase 2 of 3)" in message
    assert "Next up, phase 3 of 3" in message
    assert "NOT an approval of the whole plan" in message
    assert "resume --until M" in message
    assert "/opencode-review-loop:finish" in message

    document = read_state(env, git_repo, SESSION)
    # A pause must never reach COMPLETE, which disarms -- the reviewer here always approves,
    # so if `_final` had run despite the pause, this would be COMPLETE instead.
    assert document["status"] == "ACTIVE"
    assert document["final_done_tree"] == ""
    assert document["baseline_tree"] == baseline
    assert document["approved_trees"] == approved_before


def test_stop_runs_the_final_review_once_the_pause_target_is_reached_and_finish_is_requested(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """``finish_requested`` is what lets the user cut a paused plan short deliberately."""
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active_until(git_repo, tmp_path, env, 1, "one", "two")
    committed_phase(git_repo, env)
    patch_state(env, git_repo, finish_requested=True)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


def test_stop_behaves_as_unset_when_the_pause_target_equals_the_phase_count(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A target at or above the last phase is no pause at all -- the final review still runs."""
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active_until(git_repo, tmp_path, env, 1, "only phase")
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


# --------------------------------------------------------------------------
# The final_review skip path
# --------------------------------------------------------------------------


def test_disabled_final_review_completes_without_calling_the_reviewer(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Regression: at the default (``final_review=false``), COMPLETE is reached on the strength
    of the per-commit gate alone, and the reviewer is never invoked for a cumulative pass. Old
    code called ``_final`` unconditionally, so a broken reviewer command would block with
    ``FINAL_FAILED`` here instead of completing."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_REVIEWER_CMD"] = "/nonexistent/reviewer"

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert "final_review is disabled" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")


def test_a_malformed_status_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """State is not a trust boundary: an unrecognised status falls through ``_by_status``'s
    known-status checks unblocked, and must not then become a silent, unreviewed ``COMPLETE``
    just because the outstanding-phase and pause checks happened to pass too.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, status="BOGUS")

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    assert "NEEDS_HUMAN" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"


def test_empty_phases_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The structural half of the same guard: an empty ``phases`` list makes ``total`` zero, so
    the outstanding-phase check (``phase <= target``) no longer refuses any positive ``phase``
    at all -- the no-review path must still refuse rather than trust that shape.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, phases=[])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"


def test_a_phase_description_containing_a_newline_still_completes(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The frozen-evidence check must not have a blind spot that refuses valid activations.

    ``set-phases`` rejects only empty and whitespace-only descriptions, so one containing a
    newline is accepted and frozen as written. Validating by splitting the frozen file back
    into lines could never reconstruct such a list, and would refuse this activation forever
    even with every phase committed; comparing the exact serialisation has no such blind spot.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one\nwith a second line")
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert "final_review is disabled" in message
    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


@pytest.mark.parametrize("newline", ["\r\n", "\r"], ids=["crlf", "cr"])
def test_a_phase_description_containing_carriage_returns_still_completes(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    newline: str,
) -> None:
    """The same blind spot at one remove: text-mode reads apply universal-newline translation,
    so a frozen ``\\r\\n`` or ``\\r`` comes back as ``\\n`` and never matches the description
    that was actually frozen. Comparing raw bytes is what avoids refusing these forever."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, f"phase one{newline}with a second line")
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


def test_commit_refuses_when_ttl_shrinks_while_the_completion_is_pending(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Completion.commit()`` reloads config rather than trusting the hook-start copy, because
    ``fingerprint``'s effective-status half depends on ``ttl_hours``: an activation that became
    STALE while the completion was pending must not disarm. A regression to ``self.config``
    would leave the fingerprint reading ACTIVE and complete it anyway.

    ``armed_at`` is aged first, so the activation is comfortably inside the default 24h TTL at
    ``start()`` and outside a 1h TTL at ``commit()`` -- the shrink is the only thing that
    changes, and it is applied to the user config file exactly as ``config ttl_hours 1`` would.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, armed_at=int(time.time()) - 3 * 3600)

    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(git_repo)

    state = State(str(git_repo), SESSION)
    assert state.load()
    config = config_module.load(str(git_repo), overrides=state.data.get("overrides"))
    assert state.effective_status(config) == "ACTIVE", "must not be stale yet at start()"
    pending = completion.start(state, config=config, repo=str(git_repo))

    user_config = Path(env["XDG_CONFIG_HOME"]) / "opencode-review-loop" / "config.json"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text(json.dumps({"ttl_hours": 1}))

    with pytest.raises(commands_module.Refused, match="ACTIVE to STALE"):
        pending.commit(
            reviewed=git(git_repo, "rev-parse", "HEAD^{tree}"),
            reason="completed without a final cumulative review (final_review is disabled)",
            refuse_if_review_now_required=True,
        )

    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_a_forged_phase_counter_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The hole neither counting nor the frozen-list check can see, and the reason phase
    progress is proven against git rather than against the document.

    Two frozen phases, phase one genuinely committed, and then only the ``phase`` integer is
    moved to 3. ``phases`` is untouched, so ``phases_match_frozen()`` is satisfied; ``phase ==
    total + 1`` is satisfied; HEAD's tree is genuinely approved, so the sweep does not run
    either. Every check that reads ``state.json`` agrees the activation is finished, and phase
    two was never implemented. Only ``phase_commits`` -- one recorded commit where two are
    required -- still disagrees, and it disagrees because git, not the document, is what backs
    it.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    document = read_state(env, git_repo, SESSION)
    assert document["phase"] == 2
    recorded = document["phase_commits"]
    assert isinstance(recorded, list)
    assert len(recorded) == 1, "exactly one phase has actually been committed"
    patch_state(env, git_repo, phase=3)

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    after = read_state(env, git_repo, SESSION)
    assert after["status"] == "NEEDS_HUMAN"
    assert after["final_done_tree"] == ""


def test_a_phase_commit_no_longer_in_history_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The recorded SHAs are re-checked against git, not merely counted: a full-length
    ``phase_commits`` whose entries name commits unreachable from HEAD proves nothing."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, phase_commits=[{"phase": 1, "commit": "0" * 40}])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_a_truncated_phase_list_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The plausible shape an empty list does not cover, and the one counting alone cannot catch.

    Dropping the *tail* of ``phases`` after an earlier phase lands leaves ``phase ==
    len(phases) + 1`` perfectly satisfied -- two frozen phases, phase one committed, ``phases``
    truncated to one entry, and the activation now looks finished with phase two never
    implemented. Every count derived from the document agrees; only ``phases.frozen``, the
    evidence ``set-phases`` wrote and every review is shown, still disagrees.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    assert read_state(env, git_repo, SESSION)["phase"] == 2
    patch_state(env, git_repo, phases=["phase one"])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["final_done_tree"] == ""


def test_a_reused_phase_commit_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """report 024: the recorded SHAs have to form a chain, not a set of ancestors.

    Two frozen phases, phase one genuinely committed, and then phase one's own SHA recorded a
    second time under phase two. Every other check passes: the list is not truncated, the
    entries are numbered 1..2 with no gaps, and both SHAs really are ancestors of HEAD --
    because they are the same real commit. One phase's work would stand in for the whole plan.
    Distinct SHAs, plus strict ancestry between consecutive phases, is what refuses it.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    recorded = read_state(env, git_repo, SESSION)["phase_commits"]
    assert isinstance(recorded, list)
    first = recorded[0]
    assert isinstance(first, dict)
    patch_state(env, git_repo, phase=3, phase_commits=[first, {"phase": 2, "commit": first["commit"]}])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["final_done_tree"] == ""


@pytest.mark.parametrize("alias", ["HEAD", "main", "@"])
def test_a_symbolic_phase_commit_refuses_the_no_review_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], alias: str) -> None:
    """report 025: only canonical object IDs count, because only those compare as they resolve.

    A full SHA and ``HEAD`` are unequal as strings while naming one commit, so the distinctness
    check would pass and ``merge-base --is-ancestor`` -- which is reflexive -- would then let a
    single real phase commit prove two phases. Nothing symbolic reaches git at all.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    recorded = read_state(env, git_repo, SESSION)["phase_commits"]
    assert isinstance(recorded, list)
    patch_state(env, git_repo, phase=3, phase_commits=[recorded[0], {"phase": 2, "commit": alias}])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_empty_commits_cannot_carry_an_unimplemented_plan_to_completion(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """report 026: the hole distinct commit IDs alone leave open, and the worst one on this path.

    ``git commit --allow-empty`` produces a real, distinct, correctly-ordered commit that
    ``pretool`` waves through from ``last_approved_tree`` without calling the reviewer -- the
    tree did not change, so there is nothing to review. One per frozen phase and the ancestry
    chain is perfect while not one line of the plan was written and no model saw anything. What
    refuses it is that an empty commit does not *move* the tree, and a moved tree is the thing
    the gate cannot let past without a review.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    document = read_state(env, git_repo, SESSION)
    recorded = document["phase_commits"]
    assert isinstance(recorded, list)
    git(git_repo, "commit", "-q", "--allow-empty", "-m", "phase two, allegedly")
    empty = git(git_repo, "rev-parse", "HEAD")
    patch_state(env, git_repo, phase=3, phase_commits=[recorded[0], {"phase": 2, "commit": empty}])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["final_done_tree"] == ""


def test_an_activation_armed_on_an_unborn_head_still_completes(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """report 025: ``arm`` writes an empty ``activation_commit`` in a repository with no commits
    yet, and an empty one is not a missing one.

    Rejecting it outright would wedge every legitimate empty-repository activation at the very
    last step -- the phases all committed, the work all gated, and completion escalating to
    NEEDS_HUMAN on the absence of a commit that never existed. With no earlier commit to start
    after, phase 1's own commit is where the chain starts.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, activation_commit="")
    env["OCRL_REVIEWER_CMD"] = "/nonexistent/reviewer"

    message = ended(stop(git_repo, env))

    assert "COMPLETE" in message
    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


def test_the_activation_commit_cannot_stand_in_for_a_phase(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The chain starts strictly *after* the commit the activation was armed at: history that
    was already there when the plan started is not evidence that any phase was done."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    activation = read_state(env, git_repo, SESSION)["activation_commit"]
    patch_state(env, git_repo, phase_commits=[{"phase": 1, "commit": activation}])

    message = ended(stop(git_repo, env))

    assert "unexpected state" in message
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_commit_revalidates_phase_state_under_its_own_lock(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """report 017: the no-review structural check ``_review`` makes before deciding to skip is
    not itself part of ``fingerprint`` (which tracks activation identity and status
    transitions, not phase progress), so a concurrent rewrite of ``phase``/``phases`` landing
    between that decision and the write must still be caught -- under the same lock the write
    uses, not only by the earlier, unlocked check.

    Exercises ``Completion.commit()`` directly rather than through the full Stop-hook flow:
    ``_complete_without_review`` calls no reviewer and has no I/O between the structural check
    and the write, so there is no natural pause point to hook a subprocess race into. Mutating
    ``phases`` between ``start()`` and ``commit()`` stands in deterministically for a
    concurrent rewrite landing in that same, structurally unavoidable window.

    The in-process ``State``/``Completion`` calls below read paths from ``os.environ`` at call
    time, the same as any subprocess would from ``env`` -- ``monkeypatch`` makes the two match,
    the same technique ``test_reviewer.review_env`` and the cross-session resume races already
    use for this.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(git_repo)

    state = State(str(git_repo), SESSION)
    assert state.load()
    config = config_module.load(str(git_repo), overrides=state.data.get("overrides"))
    pending = completion.start(state, config=config, repo=str(git_repo))

    # Corrupt phases after `start()` -- and after the caller's own structural check would have
    # passed -- standing in for a concurrent rewrite landing in the window that check cannot
    # close on its own.
    patch_state(env, git_repo, phases=[])

    head_tree = git(git_repo, "rev-parse", "HEAD^{tree}")
    with pytest.raises(commands_module.Refused, match="no longer describes every phase as committed"):
        pending.commit(
            reviewed=head_tree,
            reason="completed without a final cumulative review (final_review is disabled)",
            refuse_if_review_now_required=True,
        )

    document = read_state(env, git_repo, SESSION)
    assert document["status"] != "COMPLETE"


def test_enabled_final_review_with_a_broken_reviewer_still_blocks(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: catches an inverted condition (``if final_review`` instead of ``if not final_review``)."""
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_REVIEWER_CMD"] = "/nonexistent/reviewer"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review failed" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_disabled_final_review_the_sweep_still_blocks(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: ``final_review`` only ever skips the cumulative review, never the per-commit sweep."""
    env = armed_env(clean_env, OCRL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env)
    (git_repo / "unreviewed.txt").write_text("never gated\n")

    reason = blocked(stop(git_repo, env))

    assert "uncommitted work that OpenCode requires changes to" in reason


def test_disabled_final_review_outstanding_phases_still_block(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: skipping the cumulative review does not skip demanding the rest of the plan."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)

    reason = blocked(stop(git_repo, env))

    assert "phases 2..2 are still outstanding" in reason


def test_disabled_final_review_a_pause_target_still_pauses(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Pin: a pause is still a pause -- it must never fall through to completion."""
    env = armed_env(clean_env)
    active_until(git_repo, tmp_path, env, 1, "one", "two")
    committed_phase(git_repo, env)

    message = ended(stop(git_repo, env))

    assert "paused" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "ACTIVE"
    assert document["final_done_tree"] == ""


def test_disabled_final_review_a_dirty_worktree_still_blocks(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    (git_repo / "left-over.txt").write_text("uncommitted\n")

    reason = blocked(stop(git_repo, env))

    assert "the worktree is not clean" in reason


def test_disabled_final_review_reconcile_still_blocks_and_never_completes(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Fact 3: ``_by_status`` blocks ``RECONCILE`` before ``_review`` is ever reached, so the
    skip path inside ``_review`` cannot bypass it -- true only by ordering, worth pinning
    explicitly rather than trusting the ordering never changes."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="RECONCILE", reason="a commit diverged", bad_commit_parent="abc123")

    reason = blocked(stop(git_repo, env))

    assert "the reconcile is unfinished" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_disabled_final_review_a_completed_activation_stays_silent_on_the_next_turn(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """Fact 4: ``_complete_without_review`` never writes ``approved_trees``, exactly like
    ``_final`` -- ``_ended``'s silence after completion already depends only on per-commit
    approvals and the sweep, never on whether a final review ran."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    ended(stop(git_repo, env))
    patch_state(env, git_repo, status="ACTIVE")

    assert stop(git_repo, env) == {}


def test_a_failed_finish_with_final_review_disabled_still_blocks_at_the_next_turn_end(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The most important test for this skip path. ``session.py::_prepare`` writes
    ``finish_requested=True`` *before* the review runs and does not clear it when the review
    returns ``CHANGES_REQUIRED`` -- ``finish`` just returns 1 with the mode still armed. A
    one-term implementation of the skip branch (``if not final_review:``, missing ``and not
    finish_requested``) passes every other test here and fails only this one: without the
    second term, a cumulative review that *found problems* becomes a silent ``COMPLETE`` one
    turn later."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_FAKE_MODE"] = "changes"

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)
    assert proc.returncode == 1, proc.stdout
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review found problems" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_stop_does_not_complete_unreviewed_when_finish_lands_during_the_sweep(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The skip-path decision must not be made from the ``finish_requested`` snapshot captured
    before the sweep.

    ``_sweep``'s own ``state.transaction()`` reloads the document under lock once its (possibly
    minutes-long) reviewer call returns, so a ``finish`` invoked concurrently during that call --
    which takes the same lock to record ``finish_requested=True`` before its own review even
    starts -- is already visible by the time the sweep returns. Deciding the skip branch from
    the pre-sweep snapshot instead of a fresh read afterwards would let an unreviewed completion
    silently pre-empt a ``finish`` the user is actively waiting on.

    The reviewer stub patches the state file mid-call, standing in deterministically for the
    concurrent ``finish`` process without the timing flakiness a real second process would add.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    # A raw commit, bypassing the per-commit gate: HEAD moves to a tree nobody approved, but
    # the worktree stays clean, so this triggers the sweep without also tripping NOT_CLEAN.
    (git_repo / "late.txt").write_text("landed without going through the gate\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "a raw commit")
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STATE"] = str(state_dir(env, git_repo, SESSION) / "state.json")
    env["OCRL_TEST_PATCH"] = json.dumps({"finish_requested": True})

    message = ended(stop(git_repo, env))

    assert "final_review is disabled" not in message, "must not complete unreviewed once finish_requested is true"
    assert "The final cumulative review of the whole activation" in message
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["reason"] == "final cumulative review approved"


def test_a_concurrent_finish_request_routes_stop_through_the_real_review_instead_of_skipping(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A ``finish`` landing before the skip decision is made routes Stop through the real
    cumulative review, never through the no-review path -- the point of reading
    ``finish_requested`` fresh, under lock, once, right after the sweep (see ``_review``).

    The test holds the activation lock first, so the worker is guaranteed to queue for that
    read -- then flips ``finish_requested`` while it is queued, standing in deterministically
    for a concurrent ``finish`` process without the timing flakiness a real second process
    would add. Once that read sees ``finish_requested=True``, the turn never attempts
    ``_complete_without_review`` at all; it runs ``_final``'s real review and completes on its
    approval, exactly as if the flag had been true from the very start of the turn.

    ``refuse_if_review_now_required``'s own re-check inside ``pending.commit()`` remains the
    backstop for a flip landing in the far narrower window between that read and the commit
    itself -- too narrow to interpose on deterministically from outside the process, the same
    class of irreducible sliver ``completion.py`` already documents for the worktree and for
    config.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    with activation_lock(env, git_repo, session=SESSION):
        worker = subprocess.Popen(
            [sys.executable, "-I", str(BOOTSTRAP), "gate-stop"],
            cwd=str(git_repo),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert worker.stdin is not None
        worker.stdin.write(json.dumps(payload(git_repo)))
        worker.stdin.close()
        worker.stdin = None  # closed already; leaving the attribute set has communicate() try to flush it again
        time.sleep(_SETTLE)  # the worker reaches the post-sweep finish_requested read and queues
        patch_state(env, git_repo, finish_requested=True)
        time.sleep(_SETTLE)

    stdout, stderr = worker.communicate()
    assert worker.returncode == 0, stderr

    response = json.loads(stdout) if stdout else {}
    assert "decision" not in response, response
    assert "The final cumulative review of the whole activation" in response.get("systemMessage", "")
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["reason"] == "final cumulative review approved"
    assert document["final_done_tree"] != ""


def test_a_concurrently_enabled_final_review_still_blocks_the_skip_path(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The other half of the same window: the user re-enables the safety net instead of
    invoking ``finish``. ``gate.config`` was loaded once, at hook start, before this turn ever
    queued for the lock -- reusing it at commit time would complete unreviewed on the strength
    of a value the user has since changed. ``commit()`` reloads ``final_review`` from disk
    under the same lock as the write instead of trusting ``gate.config``.

    Same technique as the ``finish_requested`` race above: the test holds the activation lock
    so the worker is guaranteed to queue for it, then writes a config file while it waits,
    standing in for a concurrent ``ocrl config final_review true``. The *user* config file is
    used rather than the repo one deliberately: the repo config lives inside the git worktree,
    so writing it would also dirty the tree and trip the (separate, pre-existing) worktree
    check first -- which would still block, just not for the reason this test exists to prove.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    user_config = Path(env["XDG_CONFIG_HOME"]) / "opencode-review-loop" / "config.json"
    user_config.parent.mkdir(parents=True, exist_ok=True)

    with activation_lock(env, git_repo, session=SESSION):
        worker = subprocess.Popen(
            [sys.executable, "-I", str(BOOTSTRAP), "gate-stop"],
            cwd=str(git_repo),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert worker.stdin is not None
        worker.stdin.write(json.dumps(payload(git_repo)))
        worker.stdin.close()
        worker.stdin = None  # closed already; leaving the attribute set has communicate() try to flush it again
        time.sleep(_SETTLE)  # the worker reaches `pending.commit()` and queues for the lock
        user_config.write_text(json.dumps({"final_review": True}))
        time.sleep(_SETTLE)

    stdout, stderr = worker.communicate()
    assert worker.returncode == 0, stderr

    response = json.loads(stdout)
    assert response.get("decision") == "block", response
    assert "`final_review` was enabled while this completion was pending" in response["reason"]
    document = read_state(env, git_repo, SESSION)
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""


def test_a_terminal_completion_landing_while_queued_for_the_lock_ends_the_turn_quietly(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The harmless twin of the two races above: the fingerprint moved because the activation
    already finished successfully by another route, not because anything is wrong.

    A concurrent ``finish`` (stood in for here by patching ``status`` directly, the same
    technique the races above use) can reach ``COMPLETE`` while this turn is still queued for
    the lock in ``pending.commit()``. That refuses too -- the fingerprint moved -- but
    ``_block_counted`` re-checks status on the same locked reload it already takes for its own
    accounting and, finding ``COMPLETE``, routes through ``_ended`` instead of counting a
    block: the turn ends quietly, with no findings to fix and no count against
    ``max_stop_blocks``, because HEAD's tree was already approved by the per-commit gate that
    landed it.

    This is deliberately the check placed on ``_block_counted``'s own reload, not a separate
    one taken right after ``pending.commit()`` refuses: both that write and ``_block_counted``'s
    write share the one activation lock, so whichever of the two reaches it first is
    authoritative and the other, on its own next reload, sees the truth -- there is no
    interleaving left in which a completed activation is missed. A check placed one lock
    acquisition earlier would only have moved this same race, not closed it.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    head_tree = git(git_repo, "rev-parse", "HEAD^{tree}")

    with activation_lock(env, git_repo, session=SESSION):
        worker = subprocess.Popen(
            [sys.executable, "-I", str(BOOTSTRAP), "gate-stop"],
            cwd=str(git_repo),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert worker.stdin is not None
        worker.stdin.write(json.dumps(payload(git_repo)))
        worker.stdin.close()
        worker.stdin = None  # closed already; leaving the attribute set has communicate() try to flush it again
        time.sleep(_SETTLE)  # the worker reaches `pending.commit()` and queues for the lock
        patch_state(env, git_repo, status="COMPLETE", reason="finished by a concurrent finish", final_done_tree=head_tree)
        time.sleep(_SETTLE)

    stdout, stderr = worker.communicate()
    assert worker.returncode == 0, stderr

    response = json.loads(stdout) if stdout else {}
    assert "decision" not in response, response
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["reason"] == "finished by a concurrent finish", "the winning completion's reason must survive, not be overwritten"


def test_a_concurrent_complete_does_not_suppress_this_turns_own_changes_required(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The COMPLETE short-circuit is scoped to a refused *completion*, not to every block.

    A ``CHANGES_REQUIRED`` verdict is this turn's own genuine finding, from the review it just
    ran -- a concurrent ``finish`` (or another Stop turn) writing ``COMPLETE`` by some other
    route while this one was reviewing does not un-produce it, and must not make it vanish.
    ``_final``'s ``CHANGES_REQUIRED`` branch calls ``_block_counted`` without
    ``after_completion_refusal``, so the reload there never even asks about ``COMPLETE``; it
    always reports the finding.

    The reviewer stand-in writes ``COMPLETE`` directly to ``state.json`` -- standing in for a
    concurrent completion landing while this review runs -- and still returns
    ``CHANGES_REQUIRED`` itself, deterministically, instead of racing a real second process.
    """
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    script = tmp_path / "concurrently-completing-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "COMPLETE"\n'
        'd["reason"] = "completed by a concurrent finish"\n'
        'd["final_done_tree"] = d.get("baseline_tree", "")\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'The error path is wrong.\\n\\n'\n"
        "printf '<<<OCRL-FINDINGS>>>\\n'\n"
        "printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\\n'\n"
        "printf 'VERDICT CHANGES_REQUIRED\\n'\n"
        "printf '<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review found problems" in reason
    assert "Returns success on a failed lookup" in reason
    # The concurrent write is not overwritten -- ending up with both facts on disk (a COMPLETE
    # reason from elsewhere, and a block just reported for this turn's own finding) is exactly
    # the point: this call must never have consulted status at all to decide whether to block.
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "COMPLETE"
    assert document["reason"] == "completed by a concurrent finish"


@pytest.mark.parametrize("garbage", ["not-a-boolean", 1, None], ids=["string", "int", "null"])
def test_a_malformed_finish_requested_refuses_rather_than_completes(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    garbage: object,
) -> None:
    """Only the literal ``False`` the schema writes counts as "not requested".

    A tampered or corrupted document is not the same thing as a genuine ``false``, and treating
    every value that merely fails to equal ``"true"`` as permission to skip the review would
    make garbage in the state document read as an approval to disarm without one.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, finish_requested=garbage)

    reason = blocked(stop(git_repo, env))

    assert "finish_requested is" in reason
    assert "not the boolean the schema writes" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""


def test_a_blocking_final_review_blocks_and_leaves_the_mode_armed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_FAKE_MODE"] = "changes"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review found problems" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_a_failed_final_review_is_never_an_approval(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    env["OCRL_FAKE_MODE"] = "nonzero"

    reason = blocked(stop(git_repo, env))

    assert "the final cumulative review failed" in reason
    assert read_state(env, git_repo, SESSION)["status"] != "COMPLETE"


def test_an_escalation_during_the_final_review_is_not_overwritten_by_it(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The one direction that must never happen: an approval landing on somebody's denial.

    The reviewer seam escalates the activation to ``NEEDS_HUMAN`` from *inside* the review,
    which is exactly the window a slow model call opens. Without the fingerprint check the
    approval that follows would overwrite it with ``COMPLETE`` and disarm the mode.
    """
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)

    escalate = tmp_path / "escalating-reviewer.sh"
    document_path = state_dir(env, git_repo, SESSION) / "state.json"
    escalate.write_text(
        "#!/usr/bin/env bash\n"
        f"python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(document_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "NEEDS_HUMAN"\n'
        'd["reason"] = "escalated while the review ran"\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'Fine.\\n\\n<<<OCRL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<OCRL-END>>>\\n'\n"
    )
    escalate.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(escalate)

    reason = blocked(stop(git_repo, env))

    assert "while completion was pending" in reason
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "NEEDS_HUMAN"
    assert document["final_done_tree"] == ""


def test_a_second_turn_end_on_an_already_reviewed_tree_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``final_done_tree`` is what stops the same tree being reviewed on every turn end."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    ended(stop(git_repo, env))
    patch_state(env, git_repo, status="ACTIVE")

    assert stop(git_repo, env) == {}


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------


def test_progress_resets_the_no_progress_count(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Only genuine stalls count, or a long correct session would escalate for being long."""
    env = armed_env(clean_env, OCRL_MAX_STOP_BLOCKS="3")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    committed_phase(git_repo, env)
    blocked(stop(git_repo, env))
    blocked(stop(git_repo, env))
    assert read_state(env, git_repo, SESSION)["stop_blocks"] == 2

    committed_phase(git_repo, env, "second phase of work\n")
    ended(stop(git_repo, env))

    assert read_state(env, git_repo, SESSION)["status"] == "COMPLETE"


def test_the_fail_closed_fallback_for_this_event_is_a_block(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A crash in the Stop gate must not read as "the turn is fine to end"."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    lock = state_dir(env, git_repo, SESSION) / "lock"
    lock.unlink(missing_ok=True)
    lock.symlink_to("/dev/null")

    reason = blocked(stop(git_repo, env))

    assert "internal error in the Stop gate" in reason
    assert "final review did not run" in reason


def test_unreadable_state_blocks_the_turn_instead_of_ending_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A live pointer with no readable state is the fail-open case, and it must not pass.

    The shell ended the turn silently here, which reports finished work as reviewed when
    nothing was. It escalates rather than merely blocking because a block has to be counted to
    be bounded, and there is no document to count in until one is written.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").unlink()

    reason = blocked(stop(git_repo, env))

    assert "could not be read" in reason
    assert "not approved" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"

    # Bounded: the next turn end takes the NEEDS_HUMAN branch and may end, still not approved.
    message = ended(stop(git_repo, env))
    assert "still in NEEDS_HUMAN" in message


def test_corrupt_state_blocks_the_turn_too(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (state_dir(env, git_repo, SESSION) / "state.json").write_text("[not, an, object]")

    reason = blocked(stop(git_repo, env))

    assert "could not be read" in reason


def test_a_version_conflict_blocks_without_overwriting_the_document(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A document from a build newer than this one is not "nothing to preserve".

    Unlike the two tests above, ``state.needs_human`` here refuses to write at all: escalating
    over a document this build cannot trust would silently replace whatever the newer build
    recorded with a fresh, blank ``NEEDS_HUMAN``. The turn must still block -- through the
    generic fail-closed path, since the ordinary escalation refused -- but the file on disk
    must be untouched.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    document = json.loads(state_path.read_text())
    document["version"] = 99
    state_path.write_text(json.dumps(document))
    before = state_path.read_bytes()

    reason = blocked(stop(git_repo, env))

    assert "internal error in the Stop gate" in reason
    assert state_path.read_bytes() == before, "a version conflict must never be overwritten, not even by an escalation"


def test_a_malformed_version_still_escalates_normally(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The contrast case: a malformed (not merely newer) ``version`` is ordinary corrupt state.

    No build of this gate has ever written ``"version": null`` -- unlike the integer-99 case
    above, there is nothing here worth preserving untouched. The ordinary escalation must
    still work, exactly as it does for a missing or corrupt state.json, so the user is never
    left with manual deletion as the only way out.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    document = json.loads(state_path.read_text())
    document["version"] = None
    state_path.write_text(json.dumps(document))

    reason = blocked(stop(git_repo, env))

    assert "could not be read" in reason
    assert read_state(env, git_repo, SESSION)["status"] == "NEEDS_HUMAN"


def test_a_late_escalation_does_not_reopen_a_mode_the_user_stopped(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Rule 4: a review failing may not undo a stop the user ran while it was running.

    The reviewer stand-in disarms the activation from inside the review -- the window in which
    the user actually runs ``/opencode-review-loop:stop`` -- and then fails. ``_block_counted``
    finds ``DISARMED`` on its locked reload and routes through ``_ended`` rather than counting
    the block: with the block limit at zero, escalating over ``DISARMED`` would turn it back
    into a state that denies every mutation, and writing `stop_blocks` into it at all would be
    mutating a mode the user already turned off. ``_ended``'s own message is empty here because
    the committed phase's tree was already approved before this turn began.
    """
    env = armed_env(clean_env, OCRL_MAX_STOP_BLOCKS="0", OCRL_FINAL_REVIEW="true")
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    state_path = state_dir(env, git_repo, SESSION) / "state.json"
    script = tmp_path / "stopping-final-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["status"] = "DISARMED"\n'
        'd["reason"] = "stopped by the user"\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "exit 3\n"
    )
    script.chmod(0o755)
    env["OCRL_REVIEWER_CMD"] = str(script)

    message = ended(stop(git_repo, env))

    assert message == ""
    document = read_state(env, git_repo, SESSION)
    assert document["status"] == "DISARMED"
    assert document["reason"] == "stopped by the user"
    assert document.get("stop_blocks", 0) == 0, "a retired activation must not be written to at all"


@pytest.mark.parametrize("status", ["DISARMED", "COMPLETE", "RESUMED"])
def test_a_turn_ending_on_unreviewed_work_tells_the_user(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
) -> None:
    """``systemMessage`` reaches the user, so relaying it is not the model's decision.

    This is the only place a Rule 4 escape surfaces: a Bash command that commits and then
    disarms leaves an unapproved HEAD under a mode that looks deliberately stopped, and the
    turn used to end in silence.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    patch_state(env, git_repo, status=status)

    message = ended(stop(git_repo, env))

    assert "no review ever approved" in message
    assert "without passing the review gate" in message


@pytest.mark.parametrize("status", ["DISARMED", "COMPLETE", "RESUMED"])
def test_a_turn_ending_cleanly_still_says_nothing(git_repo: Path, tmp_path: Path, clean_env: dict[str, str], status: str) -> None:
    """The warning must not fire for the ordinary way a session ends."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    committed_phase(git_repo, env)
    patch_state(env, git_repo, status=status)

    assert stop(git_repo, env) == {}


def test_an_unreadable_repository_is_reported_at_turn_end(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The Stop-gate half of the same suppression: empty must not read as "nothing to see"."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    (git_repo / "sneaked.txt").write_text("never gated\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "ungated")
    patch_state(env, git_repo, status="DISARMED")
    (git_repo / ".git" / "HEAD").unlink()

    message = ended(stop(git_repo, env))

    assert "could not be read" in message
    assert "says nothing about whether the history was reviewed" in message
