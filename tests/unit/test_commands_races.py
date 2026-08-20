"""Concurrency regressions for the user-facing commands.

Every test here fails against the obvious implementation -- check the state you loaded, then
mutate inside a transaction -- because ``State.transaction`` *reloads* the document under the
lock. Anything decided before queueing for that lock was decided about a document that may no
longer exist.

The two shapes:

- **Lost update.** Concurrent callers each read the same starting value, each pass a limit
  check, and each write the same result. The allowance is spent once and granted N times.
- **Stale approval.** A decision that takes minutes (the final review) is applied to a
  document that changed while it ran, overwriting an escalation or approving a tree that is
  no longer on disk.

The lock is taken by the test itself where the race would otherwise be a matter of timing:
holding it while the processes start guarantees they all read the pre-lock state, which is
precisely the window being closed.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import BOOTSTRAP, git, run_bootstrap
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_session import arm, set_phases

#: Long enough for every spawned process to reach the lock, short enough not to drag the
#: suite. The assertions do not depend on it: they are invariants that must hold either way.
_SETTLE = 1.5


@contextmanager
def activation_lock(env: dict[str, str], repo: Path, session: str = "s1") -> Iterator[None]:
    """Hold the activation lock, so every process launched inside blocks on the transaction."""
    lock = state_dir(env, repo, session) / "lock"
    handle = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def spawn(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-I", str(BOOTSTRAP), *argv],
        cwd=str(cwd),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# --------------------------------------------------------------------------
# defer
# --------------------------------------------------------------------------


def test_concurrent_defers_cannot_overspend_the_allowance(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """One allowance, one success -- however many callers read ``defers: 0`` first."""
    env = armed_env(clean_env, OCRL_MAX_DEFERS="1")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")

    with activation_lock(env, git_repo):
        workers = [spawn(["defer", "--reason", f"worker {n}"], cwd=git_repo, env=env) for n in range(8)]
        time.sleep(_SETTLE)

    results = [(worker.wait(), *worker.communicate()) for worker in workers]
    successes = [result for result in results if result[0] == 0]
    refusals = [result for result in results if result[0] != 0]

    assert len(successes) == 1, [result[1:] for result in results]
    assert all("defers already used (limit 1)" in result[2] for result in refusals)
    # The counter and the number of granted defers must agree; the bug granted eight and
    # counted one.
    assert read_state(env, git_repo, "s1")["defers"] == len(successes)


def test_a_second_defer_still_succeeds_when_the_limit_allows_it(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Guard against fixing the race by refusing everything: the count must still advance."""
    env = armed_env(clean_env, OCRL_MAX_DEFERS="3")
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")

    with activation_lock(env, git_repo):
        workers = [spawn(["defer", "--reason", f"worker {n}"], cwd=git_repo, env=env) for n in range(5)]
        time.sleep(_SETTLE)

    codes = [worker.wait() for worker in workers]
    for worker in workers:
        worker.communicate()

    assert codes.count(0) == 3
    assert read_state(env, git_repo, "s1")["defers"] == 3


# --------------------------------------------------------------------------
# set-phases
# --------------------------------------------------------------------------


def test_concurrent_set_phases_freezes_exactly_one_list(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Frozen means frozen: the second caller must not rewrite the review's scope."""
    env = armed_env(clean_env)
    arm(git_repo, tmp_path, env)

    with activation_lock(env, git_repo):
        workers = [spawn(["set-phases", "--phase", f"list {n} phase"], cwd=git_repo, env=env) for n in range(20)]
        time.sleep(_SETTLE)

    results = [(worker.wait(), *worker.communicate()) for worker in workers]
    successes = [result for result in results if result[0] == 0]

    assert len(successes) == 1, [result[1:] for result in results]
    assert all("already frozen" in result[2] for result in results if result[0] != 0)

    document = read_state(env, git_repo, "s1")
    assert document["status"] == "ACTIVE"
    frozen = document["phases"]
    assert isinstance(frozen, list)
    assert len(frozen) == 1
    # The winner's output, the state document and the frozen file must all name the same list.
    assert f"  1. {frozen[0]}\n" in successes[0][1]
    assert (state_dir(env, git_repo, "s1") / "phases.frozen").read_text() == f"{frozen[0]}\n"


# --------------------------------------------------------------------------
# finish
# --------------------------------------------------------------------------


def reviewer_stub(tmp_path: Path) -> Path:
    """An approving reviewer that can also change the world while it "reviews".

    Stands in for the real thing taking minutes: whatever a slow review would have raced
    against, this one does deterministically, in the same window.
    """
    stub = tmp_path / "meddling-reviewer.py"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "state = os.environ.get('OCRL_TEST_STATE')\n"
        "if state:\n"
        "    with open(state) as handle:\n"
        "        document = json.load(handle)\n"
        "    document.update(json.loads(os.environ['OCRL_TEST_PATCH']))\n"
        "    with open(state, 'w') as handle:\n"
        "        json.dump(document, handle)\n"
        "touch = os.environ.get('OCRL_TEST_TOUCH')\n"
        "if touch:\n"
        "    with open(touch, 'w') as handle:\n"
        "        handle.write('appeared while the review was running\\n')\n"
        "started = os.environ.get('OCRL_TEST_STARTED')\n"
        "if started:\n"
        "    with open(started, 'w') as handle:\n"
        "        handle.write('reviewing\\n')\n"
        "go = os.environ.get('OCRL_TEST_GO')\n"
        "if go:\n"
        "    import time\n"
        "    for _ in range(1200):\n"
        "        if os.path.exists(go):\n"
        "            break\n"
        "        time.sleep(0.05)\n"
        "print('Reviewed the whole diff.')\n"
        "print()\n"
        "print('<<<OCRL-FINDINGS>>>')\n"
        "print('VERDICT APPROVED')\n"
        "print('<<<OCRL-END>>>')\n"
    )
    stub.chmod(0o755)
    return stub


def armed_and_committed(git_repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    arm(git_repo, tmp_path, env)
    set_phases(git_repo, env, "one")
    (git_repo / "work.txt").write_text("done\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "phase one")


def test_finish_does_not_overwrite_an_escalation_that_arrived_during_the_review(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """Rule 1 at its sharpest: an approval must never overwrite somebody else's NEEDS_HUMAN."""
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STATE"] = str(state_dir(env, git_repo, "s1") / "state.json")
    env["OCRL_TEST_PATCH"] = json.dumps({"status": "NEEDS_HUMAN", "reason": "concurrent gate escalation"})

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    assert "moved from ACTIVE to NEEDS_HUMAN" in proc.stdout
    assert "further commits are ungated" not in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "NEEDS_HUMAN"
    assert document["reason"] == "concurrent gate escalation"
    assert document["final_done_tree"] == ""


@pytest.mark.parametrize("status", ["ARM_FAILED", "DISARMED", "RECONCILE", "ARMED", "SOMETHING_ADDED_LATER"])
def test_finish_does_not_overwrite_any_transition(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    status: str,
) -> None:
    """*Any* change, not an enumerated set of denying ones.

    ``RECONCILE`` is here because it was missing from the deny-list this replaced -- an
    approving review overwrote "a commit diverged from the reviewed tree" with ``COMPLETE``.
    ``SOMETHING_ADDED_LATER`` stands in for the next status somebody adds and forgets to
    enumerate: an equality check covers it on the day it is written, a list does not.
    """
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STATE"] = str(state_dir(env, git_repo, "s1") / "state.json")
    env["OCRL_TEST_PATCH"] = json.dumps({"status": status, "reason": "someone else decided"})

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    assert f"moved from ACTIVE to {status}" in proc.stdout
    assert "further commits are ungated" not in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == status
    assert document["final_done_tree"] == ""


def test_finish_refuses_when_the_activation_goes_stale_under_a_long_review(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A TTL that expires mid-review makes the baseline untrustworthy, so it does not sign off.

    The stored status never changes here -- only the derived one does -- which is why the
    fingerprint carries both.
    """
    env = armed_env(clean_env, OCRL_TTL_HOURS="1")
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STATE"] = str(state_dir(env, git_repo, "s1") / "state.json")
    # Only the clock moves: same activation, same stored status, now past its TTL.
    env["OCRL_TEST_PATCH"] = json.dumps({"armed_at": 1})

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""


def test_finish_refuses_when_the_worktree_changes_during_the_review(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The approval names a tree; if that is not what is on disk, it approves nothing."""
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    reviewed = git(git_repo, "rev-parse", "HEAD^{tree}")
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_TOUCH"] = str(git_repo / "appeared.txt")

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    assert f"the final review approved tree {reviewed}" in proc.stdout
    assert "content changed while the review was running" in proc.stdout
    assert "further commits are ungated" not in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""


def test_finish_refuses_when_the_activation_is_replaced_during_the_review(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A re-arm mid-review means the approval belongs to a plan that is no longer active."""
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STATE"] = str(state_dir(env, git_repo, "s1") / "state.json")
    env["OCRL_TEST_PATCH"] = json.dumps({"armed_at": 99, "baseline_tree": "0" * 40, "status": "ARMED"})

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    assert "re-armed while the final review was running" in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "ARMED"
    assert document["final_done_tree"] == ""


def test_finish_refuses_when_the_worktree_changes_while_it_waits_for_the_lock(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The exact interleaving a pre-lock check cannot see.

    Ordered so that a verification performed *before* taking the activation lock would pass:

    1. the reviewer approves and parks, holding the turn open;
    2. this test takes the activation lock;
    3. the reviewer is released, so completion runs -- an unlocked check would snapshot a
       clean tree here, then queue for the lock;
    4. ``late.txt`` appears **while completion is queued**;
    5. the lock is released.

    With the checks inside the lock, the snapshot happens in step 5 and sees ``late.txt``.
    With them outside, the approval is written over a dirty worktree.
    """
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    env["OCRL_TEST_STARTED"] = str(tmp_path / "review-started")
    env["OCRL_TEST_GO"] = str(tmp_path / "review-go")

    worker = spawn(["finish"], cwd=git_repo, env=env)
    deadline = time.monotonic() + 60
    while not Path(env["OCRL_TEST_STARTED"]).exists():
        assert time.monotonic() < deadline, "the reviewer stub never started"
        time.sleep(0.02)

    with activation_lock(env, git_repo):
        Path(env["OCRL_TEST_GO"]).write_text("go\n")
        time.sleep(_SETTLE)  # completion reaches the lock (and, unfixed, snapshots first)
        (git_repo / "late.txt").write_text("unreviewed content\n")
        time.sleep(_SETTLE)

    code = worker.wait()
    stdout, _ = worker.communicate()

    assert code == 1, stdout
    assert "content changed while the review was running" in stdout
    assert "further commits are ungated" not in stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] != "COMPLETE"
    assert document["final_done_tree"] == ""
    assert (git_repo / "late.txt").exists()


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"armed_at": 1}, id="stale"),
        pytest.param({"status": "NEEDS_HUMAN", "reason": "escalated earlier"}, id="needs-human"),
        pytest.param({"status": "ARM_FAILED", "reason": "arming failed earlier"}, id="arm-failed"),
        pytest.param({"status": "DISARMED", "reason": "stopped by the user"}, id="disarmed"),
        pytest.param({"status": "COMPLETE", "reason": "already finished"}, id="complete"),
    ],
)
def test_finish_refuses_an_activation_that_was_already_unfinishable(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    patch: dict[str, object],
) -> None:
    """The blind spot the fingerprint cannot see: nothing *changes*, it was already so.

    ``STALE -> STALE`` compares equal, and so does ``NEEDS_HUMAN -> NEEDS_HUMAN``. Every gate
    in the loop blocks on these; completing one would sign off a baseline the loop has already
    said it cannot trust, and announce that commits are ungated.

    The reviewer must not even run: it is a model call spent on an activation whose answer
    cannot be applied.
    """
    env = armed_env(clean_env, OCRL_TTL_HOURS="1")
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))
    invoked = tmp_path / "reviewer-was-invoked"
    env["OCRL_TEST_STARTED"] = str(invoked)

    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document.update(patch)
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1, proc.stdout
    assert "running the final cumulative review" not in proc.stdout
    assert "further commits are ungated" not in proc.stdout
    assert not invoked.exists(), "the reviewer ran for an activation that cannot complete"

    after = json.loads(path.read_text())
    assert after["status"] == document["status"]
    assert after["final_done_tree"] == ""
    # `finish_requested` is what stops the Stop gate insisting on the outstanding phases, so
    # a refused finish must not record it either.
    # Absent on a fresh document, and it must still be absent: recording it is a state change
    # in its own right.
    assert after.get("finish_requested") == document.get("finish_requested")


def test_a_stale_finish_names_the_recovery_the_other_gates_name(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """One recovery contract across the loop: re-arm, or stop."""
    env = armed_env(clean_env, OCRL_TTL_HOURS="2")
    armed_and_committed(git_repo, tmp_path, env)
    path = state_dir(env, git_repo, "s1") / "state.json"
    document = json.loads(path.read_text())
    document["armed_at"] = 1
    path.write_text(json.dumps(document))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "past ttl_hours (2)" in proc.stdout
    assert "/opencode-review-loop:implement <plan.md>" in proc.stdout
    assert "/opencode-review-loop:stop" in proc.stdout


def test_finish_still_completes_when_nothing_interferes(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The guards must not be a blanket refusal: the ordinary path still completes."""
    env = armed_env(clean_env)
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert "opencode-review-loop: COMPLETE." in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")
