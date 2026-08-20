"""Activation state: durability, privacy, locking, and agreement with the shell.

Phase 6 flips the entrypoint and must stay revertible mid-session, so a session written by
one implementation has to be loadable *and advanceable* by the other. That is asserted here
against the shell libraries that are still live, not against a reading of them.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SCRIPTS_DIR, bash_fixture

from ocrl import config as ocrl_config
from ocrl import paths, state
from ocrl.atomic import write_private_atomic
from ocrl.config import Config
from ocrl.errors import StateLoadError, UnsafePathError

WORKTREE = "/wt"
SESSION = "sess1"


@pytest.fixture
def state_env(clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Apply the isolated environment to this process too, since paths reads os.environ."""
    for key in list(os.environ):
        if key.startswith(("OCRL_", "XDG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in clean_env.items():
        monkeypatch.setenv(key, value)
    return clean_env


def default_config() -> Config:
    return ocrl_config.load("", {})


# -- the shell wrote it, Python must read it -------------------------------


def test_python_reads_what_bash_wrote(state_env: dict[str, str]) -> None:
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)

    st = state.State(WORKTREE, SESSION)
    assert st.load()

    assert st.get("status") == "ACTIVE"
    assert st.get("reason") == "bash wrote this"
    assert st.get("plan_path") == "/tmp/plan.md"
    assert st.get("pending_command") == 'git commit -m "héllo"'
    assert st.get_int("armed_at") == 1700000000
    assert st.get_int("phase") == 2
    assert st.get_int("report_seq") == 3
    assert st.get("allow_dirty") == "true"
    assert st.get("defer_pending") == "false"
    assert st.get_array("phases") == ["first phase", "second phase"]
    assert st.get_array("approved_trees") == ["tree0", "tree1"]
    assert st.phase_count() == 2
    assert st.phase_desc(1) == "first phase"
    assert st.tree_approved("tree1")
    assert not st.tree_approved("nope")


def test_the_activation_directory_is_the_same_one(state_env: dict[str, str]) -> None:
    """Both implementations must hash the worktree path to the same location."""
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)
    assert state.State(WORKTREE, SESSION).state_file.is_file()


# -- Python wrote it, the shell must read it -------------------------------


def test_bash_reads_what_python_wrote(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(
        status="ACTIVE",
        reason="python wrote this",
        session_id=SESSION,
        worktree=WORKTREE,
        armed_at=1700000000,
        allow_dirty=True,
        defer_pending=False,
        phases=["alpha", "beta", "gamma"],
        phase=2,
        pending_command='git commit -m "héllo"',
    )
    st.mark_tree_approved("treeB")
    st.mark_tree_approved("treeA")
    st.save()

    def bash_get(key: str) -> str:
        # `ocrl_get` renders through `jq -r`, which appends a newline.
        return bash_fixture(["state-get", WORKTREE, SESSION, key], env=state_env).stdout.rstrip("\n")

    assert bash_get("status") == "ACTIVE"
    assert bash_get("reason") == "python wrote this"
    assert bash_get("pending_command") == 'git commit -m "héllo"'
    assert bash_get("armed_at") == "1700000000"
    # Booleans must survive as booleans; the shell has a jq trap here and handles it.
    assert bash_get("allow_dirty") == "true"
    assert bash_get("defer_pending") == "false"
    assert bash_fixture(["phase-count", WORKTREE, SESSION], env=state_env).stdout.rstrip("\n") == "3"
    assert bash_fixture(["state-array", WORKTREE, SESSION, "approved_trees"], env=state_env).stdout.split() == [
        "treeA",
        "treeB",
    ]


def test_a_session_survives_a_round_trip_through_both(state_env: dict[str, str]) -> None:
    """Reverting phase 6 must safely resume a live session, so this goes both ways."""
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)

    st = state.State(WORKTREE, SESSION)
    assert st.load()
    st.update(status="NEEDS_HUMAN", reason="python escalated it", failures=4)
    st.mark_tree_approved("treeP")
    st.save()

    bash_fixture(["state-advance", WORKTREE, SESSION], env=state_env)

    st = state.State(WORKTREE, SESSION)
    assert st.load()
    assert st.get("status") == "RECONCILE"
    assert st.get("reason") == "bash advanced it"
    assert st.get_int("phase") == 3
    assert st.get_int("stop_blocks") == 2
    assert st.get_int("failures") == 4, "the Python mutation must survive the shell's save"
    # Both implementations deduplicate and sort (jq `unique`), so the order is exact.
    assert st.get_array("approved_trees") == ["tree0", "tree1", "tree2", "treeP"]


def test_the_serialised_document_is_byte_identical_to_the_shells(state_env: dict[str, str]) -> None:
    """Not required for interoperability, but it keeps state diffs readable across a revert."""
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)
    from_bash = state.State(WORKTREE, SESSION).state_file.read_text(encoding="utf-8")

    st = state.State(WORKTREE, SESSION)
    st.load()
    assert st.serialise() == from_bash


def test_a_fresh_document_has_the_same_schema_as_the_shells(state_env: dict[str, str]) -> None:
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)
    from_bash = json.loads(state.State(WORKTREE, SESSION).state_file.read_text(encoding="utf-8"))
    assert list(state.new_state_document()) == list(from_bash)


# -- durability ------------------------------------------------------------


def test_a_failed_save_leaves_the_previous_document_untouched(state_env: dict[str, str]) -> None:
    """The shell truncated the temp file and renamed anyway, publishing empty state."""
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ACTIVE", reason="the good one")
    st.save()
    before = st.state_file.read_bytes()

    # A value json cannot serialise: the failure lands before the rename, not after.
    st.data["phases"] = {1, 2, 3}
    with pytest.raises(TypeError):
        st.save()

    assert st.state_file.read_bytes() == before
    assert json.loads(st.state_file.read_text(encoding="utf-8"))["reason"] == "the good one"
    assert not list(st.act_dir.glob("state.json.tmp.*")), "a temp file was left behind"


def test_a_write_that_fails_midway_leaves_the_previous_document_untouched(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The test above only proves serialisation happens before any file is touched.

    This one proves the rename is genuinely atomic: the failure lands *after* a valid
    document has been serialised and the write is under way. A version that truncated the
    destination first would pass the other test and fail this one.
    """
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ACTIVE", reason="the good one")
    st.save()
    before = st.state_file.read_bytes()

    def boom(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    st.update(reason="the one that never lands")
    with pytest.raises(OSError, match="No space left"):
        st.save()

    assert st.state_file.read_bytes() == before
    assert json.loads(st.state_file.read_text(encoding="utf-8"))["reason"] == "the good one"
    assert not list(st.act_dir.glob("state.json.tmp.*")), "a temp file was left behind"


def test_the_pointer_is_written_atomically(state_env: dict[str, str]) -> None:
    """An interrupted pointer write leaves an empty pointer, which Rule 0 reads as
    "arming never executed" -- denying every later tool call."""
    state.pointer_write(SESSION, WORKTREE)
    assert state.pointer_read(SESSION) == WORKTREE
    pointer = paths.sessions_dir() / SESSION
    assert pointer.read_text(encoding="utf-8") == f"{WORKTREE}\n"
    assert not list(pointer.parent.glob("*.tmp.*"))


def test_pointer_read_answers_none_rather_than_raising(state_env: dict[str, str]) -> None:
    assert state.pointer_read("no-such-session") is None
    assert state.pointer_read("") is None


def test_pointer_clear_is_idempotent(state_env: dict[str, str]) -> None:
    state.pointer_write(SESSION, WORKTREE)
    state.pointer_clear(SESSION)
    state.pointer_clear(SESSION)
    assert state.pointer_read(SESSION) is None


# -- privacy ---------------------------------------------------------------


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_state_is_created_private(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.save()

    assert mode_of(st.state_file) == 0o600
    assert mode_of(st.act_dir) == 0o700
    assert mode_of(paths.state_root()) == 0o700


def test_an_existing_world_readable_root_is_tightened(state_env: dict[str, str]) -> None:
    """The migration case a fresh-install test would miss entirely.

    `os.makedirs(mode=...)` is ignored for a directory that already exists, so the port has
    to tighten actively rather than rely on creation mode.
    """
    st = state.State(WORKTREE, SESSION)
    st.act_dir.mkdir(parents=True)
    for directory in (paths.state_root(), st.act_dir):
        directory.chmod(0o755)
    st.state_file.write_text("{}\n")
    st.state_file.chmod(0o644)

    st.new()
    st.save()

    assert mode_of(st.state_file) == 0o600
    assert mode_of(st.act_dir) == 0o700
    assert mode_of(paths.state_root()) == 0o700


def test_the_pointer_directory_is_private(state_env: dict[str, str]) -> None:
    paths.sessions_dir().mkdir(parents=True)
    paths.sessions_dir().chmod(0o755)
    state.pointer_write(SESSION, WORKTREE)
    assert mode_of(paths.sessions_dir()) == 0o700
    assert mode_of(paths.sessions_dir() / SESSION) == 0o600


def test_tightening_stops_at_the_state_root(state_env: dict[str, str], tmp_path: Path) -> None:
    """The gate must never chmod a directory it does not own -- $HOME, for instance."""
    outside = paths.state_root().parent
    outside.mkdir(parents=True, exist_ok=True)
    outside.chmod(0o755)
    before = mode_of(outside)
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.save()
    assert mode_of(outside) == before


# -- concurrency -----------------------------------------------------------


def test_concurrent_mutations_do_not_lose_an_update(state_env: dict[str, str]) -> None:
    """Atomic rename prevents a torn file but not a lost update.

    A PostToolUse hook overlapping a user-run `defer` is a read-modify-write race, and the
    loser's change -- an escalation, or a cleared pending approval -- would simply vanish.
    """
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(failures=0)
    st.save()

    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from ocrl.state import State\n"
        f"st = State({WORKTREE!r}, {SESSION!r})\n"
        "with st.transaction():\n"
        "    st.update(failures=st.get_int('failures') + 1)\n"
    )
    workers = 12
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", program],
            env=state_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(workers)
    ]
    for proc in procs:
        _, err = proc.communicate()
        assert proc.returncode == 0, err.decode()

    st = state.State(WORKTREE, SESSION)
    assert st.load()
    assert st.get_int("failures") == workers


def test_a_transaction_reloads_before_mutating(state_env: dict[str, str]) -> None:
    """Otherwise a stale in-memory copy overwrites whatever landed in between."""
    first = state.State(WORKTREE, SESSION)
    first.new()
    first.update(status="ACTIVE", failures=0)
    first.save()

    stale = state.State(WORKTREE, SESSION)
    stale.load()

    other = state.State(WORKTREE, SESSION)
    other.load()
    other.update(status="NEEDS_HUMAN")
    other.save()

    with stale.transaction():
        stale.update(failures=1)

    reread = state.State(WORKTREE, SESSION)
    reread.load()
    assert reread.get("status") == "NEEDS_HUMAN"
    assert reread.get_int("failures") == 1


# -- session ids are path components, not paths ----------------------------

UNSAFE_SESSIONS = [
    "/etc/passwd",
    "../escaped",
    "../../escaped",
    "a/b",
    "..",
    ".",
    "",
    "with\0nul",
]


@pytest.mark.parametrize("session", UNSAFE_SESSIONS)
def test_an_unsafe_session_id_cannot_be_written(session: str, state_env: dict[str, str]) -> None:
    """`Path("/state/sessions") / "/etc/passwd"` is `/etc/passwd`.

    An absolute right-hand side silently discards the base, and `..` walks out of the state
    root, so an unchecked session id composes an arbitrary path.
    """
    with pytest.raises(UnsafePathError):
        state.pointer_write(session, WORKTREE)


@pytest.mark.parametrize("session", UNSAFE_SESSIONS)
def test_an_unsafe_session_id_cannot_delete_anything(session: str, state_env: dict[str, str]) -> None:
    """`pointer_clear` unlinks, so this is an arbitrary-file-delete primitive if unchecked."""
    with pytest.raises(UnsafePathError):
        state.pointer_clear(session)


@pytest.mark.parametrize("session", UNSAFE_SESSIONS)
def test_an_unsafe_session_id_cannot_bind_state(session: str, state_env: dict[str, str]) -> None:
    with pytest.raises(UnsafePathError):
        state.State(WORKTREE, session)


@pytest.mark.parametrize("session", UNSAFE_SESSIONS)
def test_an_unsafe_session_id_reads_as_no_pointer(session: str, state_env: dict[str, str]) -> None:
    """The hot path answers "no pointer", which takes the caller down the Rule 0 deny."""
    assert state.pointer_read(session) is None


def test_a_traversing_session_id_does_not_touch_the_target(state_env: dict[str, str], tmp_path: Path) -> None:
    """The end-to-end proof: nothing outside the state root is created or removed."""
    victim = tmp_path / "victim"
    victim.write_text("precious\n")
    depth = len(paths.sessions_dir().parts)
    escape = "/".join([".."] * depth) + str(victim)

    with pytest.raises(UnsafePathError):
        state.pointer_write(escape, WORKTREE)
    with pytest.raises(UnsafePathError):
        state.pointer_clear(escape)

    assert victim.read_text() == "precious\n"
    with pytest.raises(UnsafePathError):
        state.pointer_write(str(victim), WORKTREE)
    assert victim.read_text() == "precious\n"


# -- unusable state must not be mutated into an activation -----------------


def test_a_transaction_refuses_to_publish_over_missing_state(state_env: dict[str, str]) -> None:
    """Otherwise a caller setting status=ACTIVE manufactures an activation nobody armed."""
    st = state.State(WORKTREE, SESSION)
    with pytest.raises(StateLoadError), st.transaction():
        st.update(status="ACTIVE")
    assert not st.state_file.exists()


def test_a_transaction_refuses_to_publish_over_malformed_state(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    write_private_atomic(st.state_file, "{ not json", root=paths.state_root())

    with pytest.raises(StateLoadError), st.transaction():
        st.update(status="ACTIVE")

    assert st.state_file.read_text(encoding="utf-8") == "{ not json"


def test_an_explicit_create_transaction_may_start_fresh(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    with st.transaction(create=True):
        st.update(status="ARM_FAILED", reason="arming never ran")

    reread = state.State(WORKTREE, SESSION)
    assert reread.load()
    assert reread.get("status") == "ARM_FAILED"


# -- escalation must win ---------------------------------------------------


def test_escalation_reloads_so_a_concurrent_save_cannot_undo_it(state_env: dict[str, str]) -> None:
    """A stale in-memory copy saved directly lets a later write resurrect ACTIVE."""
    first = state.State(WORKTREE, SESSION)
    first.new()
    first.update(status="ACTIVE", failures=0)
    first.save()

    stale = state.State(WORKTREE, SESSION)
    stale.load()

    other = state.State(WORKTREE, SESSION)
    other.load()
    other.update(failures=9)
    other.save()

    stale.needs_human("the reviewer failed too often")

    reread = state.State(WORKTREE, SESSION)
    assert reread.load()
    assert reread.get("status") == "NEEDS_HUMAN"
    assert reread.get_int("failures") == 9, "the escalation must build on what is on disk"


def test_escalation_survives_concurrent_ordinary_mutations(state_env: dict[str, str]) -> None:
    """The escalation and a stream of ordinary updates must not be able to lose each other."""
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ACTIVE", defers=0)
    st.save()

    bump = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from ocrl.state import State\n"
        f"st = State({WORKTREE!r}, {SESSION!r})\n"
        "with st.transaction():\n"
        "    st.update(defers=st.get_int('defers') + 1)\n"
    )
    escalate = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from ocrl.state import State\n"
        f"st = State({WORKTREE!r}, {SESSION!r})\n"
        "st.needs_human('escalated under contention')\n"
    )
    programs = [bump] * 8 + [escalate] + [bump] * 8
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", program],
            env=state_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for program in programs
    ]
    for proc in procs:
        _, err = proc.communicate()
        assert proc.returncode == 0, err.decode()

    reread = state.State(WORKTREE, SESSION)
    assert reread.load()
    assert reread.get_int("defers") == 16, "an ordinary update was lost"
    assert reread.get("status") == "NEEDS_HUMAN", "the escalation was overwritten"


def test_escalation_works_when_state_is_missing(state_env: dict[str, str]) -> None:
    """An escalation must not be dropped because state is unreadable; it only ever denies."""
    st = state.State(WORKTREE, SESSION)
    st.arm_failed("arming never executed")

    reread = state.State(WORKTREE, SESSION)
    assert reread.load()
    assert reread.get("status") == "ARM_FAILED"


# -- privacy fails closed --------------------------------------------------


def test_a_symlinked_state_component_creates_nothing_outside(state_env: dict[str, str], tmp_path: Path) -> None:
    """Refusing after the fact is not refusing.

    `mkdir(parents=True)` follows a planted symlink and creates the whole chain on the far
    side of it *before* any check can run, so the assertion has to be that the target is
    completely untouched -- not merely that `state.json` is absent from it.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    root = paths.state_root()
    root.mkdir(parents=True)
    (root / "worktrees").symlink_to(outside)

    with pytest.raises(UnsafePathError, match="symlink"):
        state.State(WORKTREE, SESSION).save()

    assert list(outside.iterdir()) == [], f"the gate created {[p.name for p in outside.iterdir()]} outside the state root"


def test_a_symlinked_leaf_creates_nothing_outside(state_env: dict[str, str], tmp_path: Path) -> None:
    """The same, one level deeper: the activation directory itself is the symlink."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = paths.state_root()
    worktrees = root / "worktrees" / paths.sha256_hex(WORKTREE)
    worktrees.mkdir(parents=True)
    (worktrees / SESSION).symlink_to(outside)

    with pytest.raises(UnsafePathError, match="symlink"):
        state.State(WORKTREE, SESSION).save()

    assert list(outside.iterdir()) == []


def test_a_lockfile_cannot_be_taken_through_a_symlink(state_env: dict[str, str], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = paths.state_root()
    root.mkdir(parents=True)
    (root / "worktrees").symlink_to(outside)

    st = state.State(WORKTREE, SESSION)
    with pytest.raises(UnsafePathError, match="symlink"), st.transaction(create=True):
        pass  # pragma: no cover - the transaction never opens

    assert list(outside.iterdir()) == []


def test_a_chmod_failure_is_not_swallowed(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must not carry on writing state it cannot show is private."""
    st = state.State(WORKTREE, SESSION)
    st.act_dir.mkdir(parents=True)
    st.act_dir.chmod(0o755)

    def boom(fd: int, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fchmod", boom)
    st.new()
    with pytest.raises(PermissionError):
        st.save()


def test_a_stat_failure_is_not_swallowed(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    st = state.State(WORKTREE, SESSION)
    st.act_dir.mkdir(parents=True)

    def boom(fd: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "fstat", boom)
    st.new()
    with pytest.raises(PermissionError):
        st.save()


# -- rename durability -----------------------------------------------------


def test_the_directory_entry_is_fsynced_after_the_rename(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.replace` orders the contents but not the directory entry naming them.

    Without this, a crash just after `pointer_write` returned could restore the previous
    pointer, and a freshly armed worktree's hooks would read the old worktree.
    """
    synced: list[str] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        with contextlib.suppress(OSError):
            synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    state.pointer_write(SESSION, WORKTREE)

    assert "dir" in synced, "the containing directory was never fsynced"
    assert synced.index("file") < synced.index("dir"), "the contents must be durable first"


def test_a_directory_fsync_failure_is_reported(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = os.fsync

    def boom(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "I/O error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="I/O error"):
        state.pointer_write(SESSION, WORKTREE)


# -- derived status --------------------------------------------------------


def test_an_expired_activation_becomes_stale(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ACTIVE", armed_at=1_000_000)
    monkeypatch.setattr("ocrl.state.now", lambda: 1_000_000 + 25 * 3600)
    assert st.effective_status(default_config()) == "STALE"


def test_an_unexpired_activation_keeps_its_status(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ACTIVE", armed_at=1_000_000)
    monkeypatch.setattr("ocrl.state.now", lambda: 1_000_000 + 23 * 3600)
    assert st.effective_status(default_config()) == "ACTIVE"


@pytest.mark.parametrize("status", ["COMPLETE", "ARM_FAILED", "NEEDS_HUMAN"])
def test_terminal_statuses_ignore_the_ttl(status: str, state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status=status, armed_at=1)
    monkeypatch.setattr("ocrl.state.now", lambda: 10**9)
    assert st.effective_status(default_config()) == status


def test_a_zero_armed_at_never_goes_stale(state_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(status="ARMED", armed_at=0)
    monkeypatch.setattr("ocrl.state.now", lambda: 10**9)
    assert st.effective_status(default_config()) == "ARMED"


def test_effective_status_matches_the_shell(state_env: dict[str, str]) -> None:
    bash_fixture(["state-new", WORKTREE, SESSION], env=state_env)
    st = state.State(WORKTREE, SESSION)
    st.load()
    # armed_at is 1700000000, long past a 24 hour TTL by now.
    from_bash = bash_fixture(["effective-status", WORKTREE, SESSION, ""], env=state_env).stdout
    assert st.effective_status(default_config()) == from_bash == "STALE"


# -- unusable state --------------------------------------------------------


@pytest.mark.parametrize("content", ["", "not json", "[1,2]", '"a string"'])
def test_an_unusable_state_file_loads_as_false(content: str, state_env: dict[str, str]) -> None:
    """Callers deny on False; absence of state is never an opt-out (Rule 0)."""
    st = state.State(WORKTREE, SESSION)
    write_private_atomic(st.state_file, content, root=paths.state_root())
    assert st.load() is False
    assert st.data == {}


def test_a_missing_state_file_loads_as_false(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    assert st.exists() is False
    assert st.load() is False


# -- small accessors -------------------------------------------------------


def test_mark_tree_approved_is_sorted_and_deduplicated(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    for tree in ("c", "a", "b", "a"):
        st.mark_tree_approved(tree)
    assert st.get_array("approved_trees") == ["a", "b", "c"]


@pytest.mark.parametrize("n", [0, 3, 99, -1])
def test_phase_desc_out_of_range_is_empty(n: int, state_env: dict[str, str]) -> None:
    """Guarded explicitly: a bare negative index would wrap round to the last phase."""
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(phases=["one", "two"])
    assert st.phase_desc(n) == ""


def test_phase_desc_in_range(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.update(phases=["one", "two"])
    assert st.phase_desc(1) == "one"
    assert st.phase_desc(2) == "two"


def test_escalation_persists_immediately(state_env: dict[str, str]) -> None:
    st = state.State(WORKTREE, SESSION)
    st.new()
    st.needs_human("because")

    reread = state.State(WORKTREE, SESSION)
    assert reread.load()
    assert reread.get("status") == "NEEDS_HUMAN"
    assert reread.get("reason") == "because"
