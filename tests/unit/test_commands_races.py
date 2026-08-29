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
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import BOOTSTRAP, git, run_bootstrap
from test_commands_arm import armed_env, plan_file, read_state, state_dir
from test_commands_session import arm, set_phases

from ocrl.commands import resume

#: Long enough for every spawned process to reach the lock, short enough not to drag the
#: suite. The assertions do not depend on it: they are invariants that must hold either way.
#: It is the ceiling ``settle`` waits up to, and the flat wait wherever the kernel cannot say
#: who is queued.
_SETTLE = 1.5

#: Blocked ``flock`` requests appear here as ``->`` continuation lines naming the waiter's
#: pid and the file's device/inode. Linux only; elsewhere ``settle`` falls back to sleeping.
_PROC_LOCKS = Path("/proc/locks")


def _waiters_on(lock: Path) -> set[int]:
    """Pids currently *blocked* acquiring a lock on ``lock``, per the kernel.

    Returns an empty set on any system that cannot answer, which the caller reads as "no
    information" rather than "nobody is waiting".
    """
    try:
        info = lock.stat()
        locks = _PROC_LOCKS.read_text()
    except OSError:
        return set()
    inode = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    waiting: set[int] = set()
    for line in locks.splitlines():
        fields = line.split()
        # "1: -> FLOCK ADVISORY WRITE <pid> <maj:min:ino> <start> <end>"; a granted lock has
        # no "->" and is one field shorter.
        if len(fields) < 8 or fields[1] != "->":
            continue
        if fields[6] == inode:
            waiting.add(int(fields[5]))
    return waiting


def settle(workers: list[subprocess.Popen[str]], env: dict[str, str], repo: Path, session: str = "s1") -> None:
    """Return once every worker is queued on the activation lock, or after ``_SETTLE``.

    A flat ``sleep(_SETTLE)`` was paid in full by every test that used it, and still only
    guessed. Asking the kernel who is blocked on the lock is both exact and, in the ordinary
    case, ~30x faster. The deadline is kept because a worker that never reaches the lock is
    exactly what some of these tests are about: the unfixed code being caught doing its work
    *before* queueing must not hang the suite, it must proceed and fail the assertion.
    """
    lock = state_dir(env, repo, session) / "lock"
    if not _PROC_LOCKS.exists():
        time.sleep(_SETTLE)
        return
    deadline = time.monotonic() + _SETTLE
    while time.monotonic() < deadline:
        pending = {worker.pid for worker in workers if worker.poll() is None}
        if not pending - _waiters_on(lock):
            # Every live worker is queued. The kernel records the wait at the moment of the
            # blocking call, so there is no window left between "seen waiting" and "waiting".
            return
        time.sleep(0.01)


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
        settle(workers, env, git_repo)

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
        settle(workers, env, git_repo)

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
        settle(workers, env, git_repo)

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
    assert f"tree {reviewed} was about to be recorded as complete" in proc.stdout
    assert "content changed in the meantime" in proc.stdout
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
    assert "re-armed while completion was pending" in proc.stdout
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
        settle([worker], env, git_repo)  # completion reaches the lock (and, unfixed, snapshots first)
        (git_repo / "late.txt").write_text("unreviewed content\n")
        # Not shortened: this one is the window the *unfixed* code needs to snapshot the
        # worktree it never locked. Waiting it out is what makes the regression observable.
        time.sleep(_SETTLE)

    code = worker.wait()
    stdout, _ = worker.communicate()

    assert code == 1, stdout
    assert "content changed in the meantime" in stdout
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
    """The guards must not be a blanket refusal: the ordinary path still completes.

    ``final_review=true`` here only makes the intent explicit -- ``finish`` always invokes the
    reviewer regardless of the key; see Phase 5's pins for that guarantee itself.
    """
    env = armed_env(clean_env, OCRL_FINAL_REVIEW="true")
    armed_and_committed(git_repo, tmp_path, env)
    env["OCRL_REVIEWER_CMD"] = str(reviewer_stub(tmp_path))

    proc = run_bootstrap(["finish"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert "opencode-review-loop: COMPLETE." in proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "COMPLETE"
    assert document["final_done_tree"] == git(git_repo, "rev-parse", "HEAD^{tree}")


# --------------------------------------------------------------------------
# resume: same-session plan-revision decision
# --------------------------------------------------------------------------


def test_concurrent_same_session_resumes_do_not_duplicate_a_revision(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Two same-session resumes can both decide "changed" against the same predecessor
    revision before either takes the lock. The second, reloading a document that already
    carries the first's write, must recognise the plan now matches the active revision and
    record nothing -- not append a second, duplicate revision for the identical change."""
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(git_repo, env, "one")
    plan.write_text("# plan\n\nphase one, edited\n")

    with activation_lock(env, git_repo):
        workers = [spawn(["resume", "--session", "s1"], cwd=git_repo, env=env) for _ in range(8)]
        settle(workers, env, git_repo)

    results = [(worker.wait(), *worker.communicate()) for worker in workers]
    assert all(code == 0 for code, _out, _err in results), results

    document = read_state(env, git_repo, "s1")
    revisions = document["plan_revisions"]
    assert isinstance(revisions, list)
    # Revision 0 (recorded at arm) plus exactly one new revision for the edit -- not one per
    # worker that happened to decide "changed" before the lock.
    assert len(revisions) == 2, revisions
    assert (state_dir(env, git_repo, "s1") / revisions[1]["file"]).read_text() == "# plan\n\nphase one, edited\n"


def test_mixed_same_and_cross_session_resumes_do_not_duplicate_a_revision(
    git_repo: Path, tmp_path: Path, clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same duplicate-revision race, but between a same-session resume (writes in place)
    and a cross-session one (retires the predecessor first): both can decide "changed" against
    the same starting revision before either takes the predecessor's lock. If the same-session
    write lands first, the cross-session resume's ``_retire`` must see it and record nothing
    new for the successor -- not append a second, duplicate revision on top of it.

    Forced deterministically rather than hoped for from scheduling, and **without** any
    production-code rendezvous (a runtime hook that pauses a real resume for an
    environment-selected duration, keyed off an environment variable nothing validates, is a
    Rule 3 and reliability hazard of its own -- see the module docstring for what this suite
    already avoids doing with `activation_lock`/`spawn` instead). This drives ``resume.run``
    in-process, in a background thread, and monkeypatches ``resume._decide_revision`` for the
    duration of this test only, to pause the cross-session resume's first (pre-lock) decision
    until the same-session resume -- run to completion on the main thread in between -- has
    published. Same technique ``test_reviewer.review_env`` already uses to make ``os.environ``
    match an isolated env dict for in-process calls.
    """
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(git_repo, env, "one")
    plan.write_text("# plan\n\nphase one, edited\n")

    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(git_repo)

    real_decide_revision = resume._decide_revision
    first_call_seen = threading.Event()
    release_first_call = threading.Event()

    def barriered_decide_revision(state: object, *, explicit_plan: str | None) -> object:
        # The very first call across both threads is necessarily the cross-session resume's
        # pre-lock one: it is started first, and the same-session resume below is not started
        # until this call has already paused here.
        is_first_call = not first_call_seen.is_set()
        first_call_seen.set()
        result = real_decide_revision(state, explicit_plan=explicit_plan)  # type: ignore[arg-type]
        if is_first_call:
            assert release_first_call.wait(timeout=10), "same-session resume never released the barrier"
        return result

    monkeypatch.setattr(resume, "_decide_revision", barriered_decide_revision)

    cross_result: list[int] = []
    cross_thread = threading.Thread(target=lambda: cross_result.append(resume.run(["--session", "s2"])))
    cross_thread.start()
    assert first_call_seen.wait(timeout=10), "cross-session resume never reached its pre-lock decision"

    # The cross-session resume has now decided "changed" against the original revision and is
    # blocked before taking "s1"'s lock. Run the same-session resume to completion here, on the
    # main thread -- its own decision is made and published fresh, against that same original
    # revision.
    same_code = resume.run(["--session", "s1"])
    assert same_code == 0
    same_session_revisions = read_state(env, git_repo, "s1")["plan_revisions"]
    assert isinstance(same_session_revisions, list)
    assert len(same_session_revisions) == 2, same_session_revisions

    # Release the cross-session resume: `_retire` must now see the same-session write.
    release_first_call.set()
    cross_thread.join(timeout=10)
    assert not cross_thread.is_alive()
    assert cross_result == [0], cross_result

    document = read_state(env, git_repo, "s2")
    revisions = document["plan_revisions"]
    assert isinstance(revisions, list)
    # Revision 0 (arm) plus exactly the one new revision the same-session resume already
    # published -- not a second, duplicate one for the identical edit.
    assert len(revisions) == 2, revisions
    assert (state_dir(env, git_repo, "s2") / revisions[1]["file"]).read_text() == "# plan\n\nphase one, edited\n"


# --------------------------------------------------------------------------
# resume: the activation overlay
# --------------------------------------------------------------------------


def test_concurrent_resumes_never_store_an_overlay_neither_of_them_probed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Two same-session resumes each probe their own pre-lock overlay, then both write.

    **Fails on a resume that merges the overlay at write time, however it merges.** One call
    switches the harness, another sets a model; whichever composing rule the writes follow, the
    pair that lands is one neither call ran ``_check_reviewer`` against -- a model only the old
    harness reports, now paired with the new one, so every later review fails for an
    operational reason. Composing under the lock does not fix that; it *is* that. Taking only
    the first writer's overlay and refusing the rest is what keeps "the reviewer was checked"
    true of whatever ends up stored.

    So the property is not "everyone succeeds": it is that the stored overlay is always exactly
    one call's, the losers are refused before writing anything, and no mixture ever appears.
    Driven through ``activation_lock``/``settle`` like the revision race above, so every worker
    is held past its pre-lock read before any may write.
    """
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(git_repo, env, "one")
    assert read_state(env, git_repo, "s1")["overrides"] == {"harness": "opencode"}

    switch = ["resume", "--session", "s1", "--args", "--harness claude-code"]
    remodel = ["resume", "--session", "s1", "--args", "--model vendor/other"]
    with activation_lock(env, git_repo):
        workers = [spawn(switch if index % 2 == 0 else remodel, cwd=git_repo, env=env) for index in range(8)]
        settle(workers, env, git_repo)

    results = [(worker.wait(), *worker.communicate()) for worker in workers]
    assert any(code == 0 for code, _out, _err in results), results
    for code, out, _err in results:
        if code != 0:
            assert "changed while this resume was preparing" in out, out
            assert "the live activation was left untouched" in out, "a refused resume must write nothing"

    overrides = read_state(env, git_repo, "s1")["overrides"]
    # Exactly one call's overlay -- never {"harness": "claude-code", "model": "vendor/other"},
    # the combination that would have reached the reviewer unvalidated.
    assert overrides in ({"harness": "claude-code"}, {"harness": "opencode", "model": "vendor/other"}), overrides


def test_a_refused_concurrent_resume_succeeds_when_run_again(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The refusal is a retry instruction, not a wedge: the second run probes the combination
    against the overlay that is now on disk, and composes with it."""
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    set_phases(git_repo, env, "one")

    assert run_bootstrap(["resume", "--session", "s1", "--args", "--harness claude-code"], cwd=git_repo, env=env).returncode == 0
    proc = run_bootstrap(["resume", "--session", "s1", "--args", "--model vendor/other"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["overrides"] == {"harness": "claude-code", "model": "vendor/other"}
