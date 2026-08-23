"""``arm`` is where Rule 0 is established: a gate that cannot prove it is running denies.

Every test here drives the real CLI through ``scripts/ocrl-bootstrap.py``, because the thing
under test is not "does the function return the right value" but "does an activation exist on
disk afterwards, and does it say the right thing". A failure that is not persisted is
indistinguishable from a session that was never armed, and the hooks would then deny with the
wrong reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, git, run_bootstrap

from ocrl import paths
from ocrl.atomic import locked as _real_locked
from ocrl.commands import arm


def armed_env(clean_env: dict[str, str], **extra: str) -> dict[str, str]:
    """A clean environment with the reviewer seam in place.

    ``OCRL_REVIEWER_CMD`` is what stops ``arm`` probing a real ``opencode``; the probe itself
    is tested separately, with the seam deliberately absent.
    """
    env = {**clean_env, "OCRL_REVIEWER_CMD": str(FAKE_REVIEWER)}
    env.update(extra)
    return env


def state_dir(env: dict[str, str], repo: Path, session: str) -> Path:
    root = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop"
    return root / "worktrees" / paths.sha256_hex(str(repo)) / session


def read_state(env: dict[str, str], repo: Path, session: str) -> dict[str, object]:
    document: dict[str, object] = json.loads((state_dir(env, repo, session) / "state.json").read_text())
    return document


def plan_file(tmp_path: Path, text: str = "# plan\n\nphase one\n") -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text(text)
    return plan


# --------------------------------------------------------------------------
# Argument splitting
# --------------------------------------------------------------------------
#
# `split_args` is the only thing standing between `$ARGUMENTS` -- substituted into the skill
# body unescaped, see "The argument channel is not escaped" in AGENTS.md -- and the plan path
# `arm` actually opens, so its split points are a direct spec, not an implementation detail.
# This corpus and its expected splits used to be asserted differentially, against the shell
# port `ocrl_split_args` was translated from; that reference was retired in Phase 8, so the
# split points below are now the specification.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ("", [])),
        ("   ", ("", [])),
        ("plan.md", ("plan.md", [])),
        ("  plan.md  ", ("plan.md", [])),
        ("plan.md --allow-dirty", ("plan.md", ["--allow-dirty"])),
        ("plan.md   --allow-dirty", ("plan.md", ["--allow-dirty"])),
        ("--allow-dirty", ("", ["--allow-dirty"])),
        (" --allow-dirty ", ("", ["--allow-dirty"])),
        ("my plans/phase one.md", ("my plans/phase one.md", [])),
        ("my plans/phase one.md --allow-dirty", ("my plans/phase one.md", ["--allow-dirty"])),
        ("plan.md --nonsense", ("plan.md", ["--nonsense"])),
        # The retired oddity: only "--" starts a flag boundary now, so a single dash is just
        # more of the plan, and "the last word is the flag" is gone entirely.
        pytest.param("a -x b", ("a -x b", []), id="single-dash-is-not-a-flag-boundary"),
        ("-x", ("-x", [])),
        ("plan.md -", ("plan.md -", [])),
        ("plan.md --allow-dirty extra", ("plan.md", ["--allow-dirty", "extra"])),
        ("~/plan.md --allow-dirty", ("~/plan.md", ["--allow-dirty"])),
        ("plan.md\t--allow-dirty", ("plan.md", ["--allow-dirty"])),
        ("plan.md --until 2", ("plan.md", ["--until", "2"])),
        ("plan.md --model gpt-x --variant fast", ("plan.md", ["--model", "gpt-x", "--variant", "fast"])),
        # A path may legitimately contain more than one run of whitespace; that must survive
        # verbatim, not collapse to a single space, or a file that really exists there stops
        # resolving the moment a flag is added alongside it.
        pytest.param("my  plans/plan.md --until 2", ("my  plans/plan.md", ["--until", "2"]), id="irregular-whitespace-preserved"),
        pytest.param("my\tplans/plan.md --allow-dirty", ("my\tplans/plan.md", ["--allow-dirty"]), id="internal-tab-preserved"),
    ],
)
def test_split_args(raw: str, expected: tuple[str, list[str]]) -> None:
    """Pins the split point: the plan is every token up to the first one starting with ``--``.

    Semantic validation of the flag tokens (unknown names, missing values) happens later, in
    ``_parse_flags`` -- this is only about where the plan ends and the flags begin.
    """
    assert arm.split_args(raw) == expected


# --------------------------------------------------------------------------
# The armed path
# --------------------------------------------------------------------------


def test_arm_freezes_the_plan_and_records_the_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "**opencode-review-loop is ARMED for this worktree.**" in proc.stdout
    assert "Phases are not set yet" in proc.stdout

    document = read_state(env, git_repo, "s1")
    head_tree = git(git_repo, "rev-parse", "HEAD^{tree}")
    assert document["status"] == "ARMED"
    assert document["session_id"] == "s1"
    assert document["worktree"] == str(git_repo)
    assert document["plan_path"] == str(plan)
    assert document["baseline_tree"] == head_tree
    assert document["last_approved_tree"] == head_tree
    assert document["activation_commit"] == git(git_repo, "rev-parse", "HEAD")
    assert document["approved_trees"] == [head_tree]
    assert document["allow_dirty"] is False

    frozen = state_dir(env, git_repo, "s1") / "plan.frozen.md"
    assert frozen.read_text() == plan.read_text()

    revisions = document["plan_revisions"]
    assert isinstance(revisions, list)
    assert len(revisions) == 1
    assert revisions[0]["phase"] == 1
    assert revisions[0]["file"] == "plan.frozen.md"
    assert revisions[0]["sha256"] == hashlib.sha256(frozen.read_bytes()).hexdigest()


def test_arm_writes_both_pointers(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The session pointer is what the hooks read; the worktree pointer is what ``status`` reads."""
    env = armed_env(clean_env)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    root = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop"
    assert (root / "sessions" / "s1").read_text() == f"{git_repo}\n"
    assert (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").read_text() == "s1\n"


def test_everything_arming_writes_is_private(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The frozen plan and the state document are the user's, and nobody else's."""
    env = armed_env(clean_env)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    act = state_dir(env, git_repo, "s1")
    assert act.stat().st_mode & 0o777 == 0o700
    for name in ("state.json", "plan.frozen.md"):
        assert (act / name).stat().st_mode & 0o777 == 0o600


def test_arm_expands_a_leading_tilde(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    home = Path(env["HOME"])
    (home / "plan.md").write_text("# plan\n")

    proc = run_bootstrap(["arm", "--session", "s1", "--args", "~/plan.md"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["plan_path"] == str(home / "plan.md")


def test_re_arming_starts_a_fresh_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A second arming must not inherit the first one's approvals.

    ``approved_trees`` is what lets a commit through without a review. Carrying it across
    activations would approve, for a new plan, a tree reviewed against the old one.
    """
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)

    act = state_dir(env, git_repo, "s1")
    document = json.loads((act / "state.json").read_text())
    document["approved_trees"] = [*document["approved_trees"], "deadbeef"]
    document["phases"] = ["one"]
    document["phase"] = 4
    (act / "state.json").write_text(json.dumps(document))

    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)

    fresh = read_state(env, git_repo, "s1")
    assert "deadbeef" not in fresh["approved_trees"]  # type: ignore[operator]
    assert fresh["phases"] == []
    assert fresh["phase"] == 1


def test_re_arming_refuses_a_version_conflict_without_touching_the_directory(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A newer build's activation must not be destroyed by an older build's re-arm.

    ``_freeze_plan`` writes ``plan.frozen.md`` before ``state.transaction`` ever takes the
    lock, so a check that only lived inside the transaction would let this exact overwrite
    happen and only refuse to save the (unrelated) state document afterwards -- the frozen
    plan, which is the evidence every past review was run against, would already be gone.
    This asserts the whole activation directory, byte for byte, to catch that.
    """
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)

    act = state_dir(env, git_repo, "s1")
    document = json.loads((act / "state.json").read_text())
    document["version"] = 99
    (act / "state.json").write_text(json.dumps(document))

    before = {p.relative_to(act): p.read_bytes() for p in act.rglob("*") if p.is_file()}

    new_plan = plan_file(tmp_path, text="# a completely different plan\n\nnot the same at all\n")
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(new_plan)], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "ARMING REFUSED" in proc.stdout
    assert "version 99" in proc.stdout
    assert str(act) in proc.stdout

    after = {p.relative_to(act): p.read_bytes() for p in act.rglob("*") if p.is_file()}
    assert after == before, "the version-99 activation directory must be byte-for-byte unchanged"


def test_a_concurrent_newer_arm_landing_between_the_check_and_the_freeze_is_still_caught(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run``'s pre-check is unlocked and is a fast exit, not the guarantee.

    Simulates the race directly, in-process: a "concurrent newer build" plants a version-99
    activation, complete with its own frozen plan, at the exact moment this arm is about to
    take the activation lock -- i.e. *after* ``run``'s own unlocked check already read "no
    conflict". Only the recheck ``_arm`` repeats *inside* the lock, immediately before
    freezing, can catch a race landing in that window; a version-conflict check that lived
    only in ``run`` would freeze right over the newer build's plan before ever finding out.
    """
    env = armed_env(clean_env)
    # Unlike run_bootstrap, this drives arm.run() in-process so the monkeypatch below can
    # take effect -- and in-process means os.environ is overlaid, not replaced, so any
    # OCRL_*/XDG_* the host happens to carry has to be cleared first (test_state.py's
    # state_env fixture does the same, for the same reason).
    for key in list(os.environ):
        if key.startswith(("OCRL_", "XDG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(git_repo)

    plan = plan_file(tmp_path)
    assert arm.run(["--session", "s1", "--plan", str(plan)]) == 0

    act = state_dir(env, git_repo, "s1")
    concurrent_plan = b"# planted by a concurrent newer build, mid-race\n"

    def racing_locked(lock_path: Path, *, root: Path):  # type: ignore[no-untyped-def]
        # The instant this arm is about to take the lock -- after its own unlocked check
        # already passed -- a "different, newer process" wins the race and lands first.
        document = json.loads((act / "state.json").read_text())
        document["version"] = 99
        (act / "state.json").write_text(json.dumps(document))
        (act / "plan.frozen.md").write_bytes(concurrent_plan)
        return _real_locked(lock_path, root=root)

    monkeypatch.setattr(arm, "locked", racing_locked)

    new_plan = plan_file(tmp_path, text="# a different plan, from the old build that lost the race\n")
    rc = arm.run(["--session", "s1", "--plan", str(new_plan)])

    assert rc == 1
    assert (act / "plan.frozen.md").read_bytes() == concurrent_plan, "the concurrently-armed newer build's plan must survive"
    assert json.loads((act / "state.json").read_text())["version"] == 99


# --------------------------------------------------------------------------
# Refusals -- every one of which must be persisted
# --------------------------------------------------------------------------


def test_a_missing_session_id_records_nothing_and_says_so(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The session id *is* the key state is filed under, so this failure cannot be recorded."""
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "ARMING FAILED" in proc.stdout
    assert "no session id was supplied" in proc.stdout
    assert not (Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "worktrees").exists()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param([], "no plan path was supplied", id="no-plan"),
        pytest.param(["--plan", "does-not-exist.md"], "does not resolve to an existing regular file", id="missing-file"),
        pytest.param(["--args", 'x"; id; echo "'], "characters that are not safe", id="injection-shaped"),
        pytest.param(["--args", "plan.md --nonsense"], 'unrecognised flag "--nonsense"', id="unknown-flag"),
    ],
)
def test_a_refusal_is_persisted_as_arm_failed(
    git_repo: Path,
    clean_env: dict[str, str],
    argv: list[str],
    expected: str,
) -> None:
    """Rule 0: the next tool call has to be able to read *why* it is being denied."""
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", *argv], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "**opencode-review-loop: ARMING FAILED" in proc.stdout
    assert expected in proc.stdout

    document = read_state(env, git_repo, "s1")
    assert document["status"] == "ARM_FAILED"
    assert expected in str(document["reason"])
    assert document["session_id"] == "s1"
    assert document["worktree"] == str(git_repo)
    # Both pointers, so the failure is findable from a hook *and* from `status`.
    root = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop"
    assert (root / "sessions" / "s1").read_text() == f"{git_repo}\n"
    assert (root / "worktrees" / paths.sha256_hex(str(git_repo)) / "latest").read_text() == "s1\n"


def test_an_unreadable_plan_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything, so the check cannot be observed")
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    plan.chmod(0o000)

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the plan file is not readable" in proc.stdout


def test_a_dirty_worktree_is_refused_unless_allowed(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = plan_file(tmp_path)
    (git_repo / "untracked.txt").write_text("work in progress\n")

    refused = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    assert refused.returncode == 1
    assert "the worktree is dirty" in refused.stdout
    assert "untracked.txt" in refused.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"

    allowed = run_bootstrap(["arm", "--session", "s2", "--args", f"{plan} --allow-dirty"], cwd=git_repo, env=env)
    assert allowed.returncode == 0, allowed.stdout
    document = read_state(env, git_repo, "s2")
    assert document["status"] == "ARMED"
    assert document["allow_dirty"] is True


def test_allow_dirty_can_also_come_from_config(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, OCRL_ALLOW_DIRTY="true")
    (git_repo / "untracked.txt").write_text("work in progress\n")

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["allow_dirty"] is True


def test_arming_outside_a_repository_is_refused(tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=elsewhere, env=env)

    assert proc.returncode == 1
    assert "is not inside a git repository" in proc.stdout
    assert read_state(env, elsewhere, "s1")["status"] == "ARM_FAILED"


# --------------------------------------------------------------------------
# The reviewer reachability probe
# --------------------------------------------------------------------------


def _path_without_opencode(tmp_path: Path) -> str:
    """A PATH carrying git -- which arming needs -- and nothing else."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("git", "bash"):
        found = shutil.which(tool)
        assert found, f"{tool} is required to run these tests"
        target = bindir / tool
        if not target.exists():
            target.symlink_to(found)
    return str(bindir)


def test_arming_without_opencode_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Arming with an unreachable reviewer would make every commit fail for the wrong reason."""
    env = {**clean_env, "PATH": _path_without_opencode(tmp_path)}
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "the `opencode` binary is not on PATH" in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"


def test_a_model_opencode_does_not_report_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/other-model\\n'\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir), "OCRL_MODEL": "vendor/wanted-model"}

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert 'the configured model "vendor/wanted-model" is not among the models OpenCode reports' in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"


def test_a_silent_opencode_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Empty output is a failure to answer, and a failure is never permission (Rule 1)."""
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "could not list OpenCode models" in proc.stdout


def test_a_reported_model_is_accepted(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The probe must not be a rubber stamp: the matching model has to arm."""
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/other\\nvendor/wanted\\n'\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir), "OCRL_MODEL": "vendor/wanted"}

    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARMED"


# --------------------------------------------------------------------------
# --until, --model, --variant
# --------------------------------------------------------------------------


def test_an_unrecognised_flag_is_refused(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --nope"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert 'unrecognised flag "--nope"' in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"


@pytest.mark.parametrize("flag", ["--until", "--model", "--variant"])
def test_a_value_flag_with_nothing_after_it_is_refused(flag: str, git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} {flag}"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert f'"{flag}" requires a value' in proc.stdout


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5", "01x"])
def test_an_invalid_until_value_is_refused(raw: str, git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --until {raw}"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert f'--until "{raw}" is not a positive integer' in proc.stdout


@pytest.mark.parametrize("raw", ["0", "all"])
def test_until_zero_or_all_means_no_target(raw: str, git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --until {raw}"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["stop_after_phase"] == 0


def test_a_positive_until_is_persisted(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --until 3"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["stop_after_phase"] == 3


def test_a_plan_path_with_irregular_whitespace_still_resolves_alongside_a_flag(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """A regression on the split point itself: normalising the plan's whitespace when a flag
    follows would make a file that really exists at that path stop resolving."""
    env = armed_env(clean_env)
    plan = tmp_path / "my  plan.md"
    plan.write_text("# plan\n\nphase one\n")

    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan} --until 2"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["plan_path"] == str(plan)
    assert read_state(env, git_repo, "s1")["stop_after_phase"] == 2


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("²", "unicode-superscript-digit"),
        ("1" * 5000, "oversized-digit-string"),
    ],
)
def test_an_untypeable_until_value_is_refused_not_a_crash(raw: str, label: str, git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``str.isdigit()`` accepts Unicode digits ``int()`` cannot parse, and an oversized digit
    string hits Python's own conversion limit -- both must become ``_ArmFailure``, not an
    unhandled crash that never persists why arming failed (Rule 0)."""
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --until {raw}"], cwd=git_repo, env=env)

    assert proc.returncode == 1, label
    assert "is not a positive integer" in proc.stdout, label
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED", label


def test_a_value_flag_cannot_swallow_the_next_flag_as_its_value(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``--variant --allow-dirty`` must refuse, not silently arm with variant="--allow-dirty"."""
    env = armed_env(clean_env)
    proc = run_bootstrap(
        ["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --variant --allow-dirty"],
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 1
    assert '"--variant" requires a value' in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"


def test_the_armed_banner_reflects_the_environment_not_the_override_alone(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The environment can itself outrank a --model override; the banner must say so too.

    Both the reviewer probe and the persisted ``overrides`` already used the fully-resolved
    config -- this pins the one place that used to compute its own, wrong answer.
    """
    env = armed_env(clean_env, OCRL_MODEL="vendor/env-wins")
    proc = run_bootstrap(
        ["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --model vendor/flag-loses"],
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout
    assert "vendor/env-wins" in proc.stdout
    assert "vendor/flag-loses" not in proc.stdout
    # The override is still recorded in state -- it is only not what actually runs here.
    assert read_state(env, git_repo, "s1")["overrides"] == {"model": "vendor/flag-loses"}


def test_an_out_of_range_until_is_clamped_with_a_warning_once_phases_are_frozen(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
) -> None:
    """The upper bound cannot be checked at arm time: phases do not exist yet."""
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --until 99"], cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["stop_after_phase"] == 99

    proc = run_bootstrap(["set-phases", "--session", "s1", "--phase", "one", "--phase", "two"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert "pause target (phase 99) is beyond the 2 phases just frozen; clamped to 2" in proc.stdout
    assert read_state(env, git_repo, "s1")["stop_after_phase"] == 2


def test_model_and_variant_are_persisted_as_overrides(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(
        ["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --model vendor/x --variant fast"],
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["overrides"] == {"model": "vendor/x", "variant": "fast"}
    assert "vendor/x" in proc.stdout
    assert "fast" in proc.stdout


def test_no_model_or_variant_flag_leaves_overrides_empty(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stdout
    assert read_state(env, git_repo, "s1")["overrides"] == {}


def test_a_model_override_is_probed_instead_of_the_stored_default(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``--model`` must be validated against itself -- probing the stored model would pass
    on the strength of a model this run will not use.

    The stored default comes from the repo config, not ``OCRL_MODEL``: the environment
    would otherwise beat the ``--model`` override in the merge order (Phase 1's
    ``defaults < user < repo < activation overrides < env``), which would make this test
    assert the wrong thing rather than exercise the reordering it is named for.
    """
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/stored\\n'\n")
    fake.chmod(0o755)
    (git_repo / ".opencode-review-loop.json").write_text('{"model": "vendor/stored"}')
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "repo config")
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(
        ["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --model vendor/not-reported"],
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 1
    assert 'the configured model "vendor/not-reported" is not among the models OpenCode reports' in proc.stdout
    assert read_state(env, git_repo, "s1")["status"] == "ARM_FAILED"
    # The stored default was never even the question -- nothing about it appears here.
    assert "vendor/stored" not in proc.stdout


def test_a_model_override_that_is_reported_arms(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/stored\\nvendor/override\\n'\n")
    fake.chmod(0o755)
    (git_repo / ".opencode-review-loop.json").write_text('{"model": "vendor/stored"}')
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "repo config")
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(
        ["arm", "--session", "s1", "--args", f"{plan_file(tmp_path)} --model vendor/override"],
        cwd=git_repo,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout
    document = read_state(env, git_repo, "s1")
    assert document["status"] == "ARMED"
    assert document["overrides"] == {"model": "vendor/override"}
