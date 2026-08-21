"""The reviewer half of the gate: bundle, invocation, and contract parsing.

The property asserted throughout, and the reason this file is long: **Rule 1**. No reviewer
output and no operational failure produces ``APPROVED``. Every broken shape in
``tests/fixtures/fake-reviewer.sh`` is driven through the parser and the full ``execute``
path, and the verdict is asserted, never merely "not a crash".
"""

from __future__ import annotations

import json
import os
import random
import stat
import subprocess
import time
from pathlib import Path

import pytest
from conftest import FAKE_REVIEWER, git, git_status_ignored

from ocrl import config as ocrl_config
from ocrl import gitsnap, report, reviewer, state
from ocrl.config import Config
from ocrl.reviewer import BundleError, BundleTooLarge, Invocation, Review, ReviewerFailed, Target

SESSION = "revsess"

#: What the ANSI-stripping test expects once the escapes are gone.
PLAIN_VERDICT = b"VERDICT APPROVED\n"

#: Every stand-in reviewer mode, with the verdict the gate must reach. Nothing here is
#: ``APPROVED`` unless the reviewer both said so and left no actionable finding behind.
MODE_VERDICTS = [
    ("approve", "APPROVED"),
    ("approve-with-nit", "APPROVED"),
    ("changes", "CHANGES_REQUIRED"),
    ("approve-with-critical", "CHANGES_REQUIRED"),
    ("critical-nonactionable", "APPROVED"),
    ("malformed", "OP_FAILURE"),
    ("no-verdict", "OP_FAILURE"),
    ("empty", "OP_FAILURE"),
    ("big-prose", "CHANGES_REQUIRED"),
    ("many", "CHANGES_REQUIRED"),
    # Blocks the contract does not allow. Each carries the reviewer's own APPROVED, and
    # each of them used to get it.
    ("bad-actionable", "OP_FAILURE"),
    ("bad-severity", "OP_FAILURE"),
    ("mangled-finding", "OP_FAILURE"),
    ("stray-end", "OP_FAILURE"),
    ("two-blocks", "OP_FAILURE"),
    ("two-verdicts", "OP_FAILURE"),
    ("chatty-block", "OP_FAILURE"),
    ("inline-start-marker", "OP_FAILURE"),
    ("suffixed-end-marker", "OP_FAILURE"),
    ("nul-byte", "OP_FAILURE"),
]


@pytest.fixture
def review_env(clean_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Apply the isolated environment to this process too, since paths reads os.environ."""
    for key in list(os.environ):
        if key.startswith(("OCRL_", "XDG_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in clean_env.items():
        monkeypatch.setenv(key, value)
    return clean_env


def config_with(**overrides: object) -> Config:
    return Config({**ocrl_config.DEFAULTS, **overrides})


@pytest.fixture
def activation(review_env: dict[str, str], git_repo: Path) -> state.State:
    """An armed activation for the scratch repository, with two frozen phases."""
    st = state.State(str(git_repo), SESSION)
    st.new()
    st.update(
        status="ACTIVE",
        session_id=SESSION,
        worktree=str(git_repo),
        phases=["first phase", "second phase"],
        phase=1,
        activation_commit=git(git_repo, "rev-parse", "HEAD"),
        baseline_tree=git(git_repo, "rev-parse", "HEAD^{tree}"),
    )
    st.save()
    (st.act_dir / "plan.frozen.md").write_text("# The frozen plan\n\nDo the thing.\n")
    return st


def fake_reviewer_output(tmp_path: Path, mode: str, **env: str) -> Path:
    """Run the stand-in reviewer and keep its output, as ``invoke`` would have."""
    out = tmp_path / f"reviewer-{mode}.out"
    with out.open("wb") as sink:
        subprocess.run(
            [str(FAKE_REVIEWER), str(tmp_path), "prompt"],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env={**os.environ, "OCRL_FAKE_MODE": mode, **env},
            check=False,
        )
    return out


def dirty(repo: Path, text: str = "phase one\n") -> str:
    (repo / "a.txt").write_text(text)
    return gitsnap.snapshot(str(repo)).tree


# --------------------------------------------------------------------------
# Permission
# --------------------------------------------------------------------------


def test_the_permission_document_denies_everything_but_reading(tmp_path: Path) -> None:
    document = json.loads(reviewer.permission(tmp_path))

    assert document["*"] == "deny"
    assert document["read"] == "allow"
    assert document["external_directory"]["*"] == "deny"
    assert document["external_directory"][f"{tmp_path}/**"] == "allow"
    assert "write" not in document
    assert "bash" not in document


def test_the_broad_external_deny_is_written_before_the_bundle_allow(tmp_path: Path) -> None:
    """Patterns are last-match-wins, so the order of these two keys is the policy."""
    external = reviewer.permission(tmp_path).split('"external_directory":', 1)[1]
    assert external.index('"*":"deny"') < external.index(f'"{tmp_path}/**":"allow"')


# --------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------


def test_argv_carries_the_bundle_as_attachments(tmp_path: Path) -> None:
    (tmp_path / "range.txt").write_text("r")
    (tmp_path / "changes.00.diff").write_text("a")
    (tmp_path / "changes.01.diff").write_text("b")

    argv = reviewer.review_argv("/repo", tmp_path, "a title", config=config_with())

    assert argv[:2] == ["--pure", "--dir"]
    assert argv[2] == "/repo"
    assert "--title" in argv
    attachments = [argv[i + 1] for i, item in enumerate(argv) if item == "-f"]
    assert attachments == [
        str(tmp_path / "range.txt"),
        str(tmp_path / "changes.00.diff"),
        str(tmp_path / "changes.01.diff"),
    ]


def test_argv_never_contains_the_prompt(tmp_path: Path) -> None:
    """``-f`` is a yargs array option: a trailing prompt would be read as an attachment."""
    argv = reviewer.review_argv("/repo", tmp_path, "review-loop phase 1", config=config_with())
    assert argv[-2] == "-f"


def test_argv_honours_pure_and_variant(tmp_path: Path) -> None:
    plain = reviewer.review_argv("/repo", tmp_path, "t", config=config_with(pure=False))
    assert "--pure" not in plain

    varied = reviewer.review_argv("/repo", tmp_path, "t", config=config_with(variant="thinking"))
    assert varied[varied.index("--variant") + 1] == "thinking"
    assert "--variant" not in plain


def test_verify_output_is_attached_last(tmp_path: Path) -> None:
    (tmp_path / "range.txt").write_text("r")
    (tmp_path / "changes.00.diff").write_text("a")
    (tmp_path / "verify.txt").write_text("v")

    argv = reviewer.review_argv("/repo", tmp_path, "t", config=config_with())
    assert argv[-1] == str(tmp_path / "verify.txt")


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------


def target_for(repo: Path, *, scope: str = "phase", phase: int = 1) -> Target:
    """A review of the current working state against HEAD's tree."""
    return Target(repo=str(repo), base=git(repo, "rev-parse", "HEAD^{tree}"), head=dirty(repo), scope=scope, phase=phase)


def build(activation: state.State, repo: Path, dest: Path, config: Config | None = None, *, warnings: str = "") -> Path:
    reviewer.build_bundle(target_for(repo), dest, state=activation, config=config or config_with(), warnings=warnings)
    return dest


def build_final(activation: state.State, repo: Path, dest: Path) -> Path:
    reviewer.build_bundle(target_for(repo, scope="final"), dest, state=activation, config=config_with())
    return dest


def test_the_bundle_describes_the_range_under_review(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)
    text = (dest / "range.txt").read_text()

    assert "scope: phase\n" in text
    assert "phase: 1 of 2\n" in text
    assert "## Frozen phase description (phase 1)\n\nfirst phase\n" in text
    assert "1. first phase\n2. second phase\n" in text
    assert "## Snapshot warnings\n\n(none)\n" in text
    assert "Do the thing." in text


def test_a_final_review_is_scoped_to_every_phase(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build_final(activation, git_repo, dest)
    text = (dest / "range.txt").read_text()

    assert "phases: 2 (all)\n" in text
    assert "Frozen phase description" not in text


def test_snapshot_warnings_reach_the_reviewer(activation: state.State, git_repo: Path) -> None:
    """A submodule the gate could not diff must be stated, not silently omitted."""
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, warnings="submodule present (content NOT diffed): x")
    assert "submodule present (content NOT diffed): x" in (dest / "range.txt").read_text()


def test_an_unfrozen_plan_says_so(activation: state.State, git_repo: Path) -> None:
    (activation.act_dir / "plan.frozen.md").unlink()
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)
    assert "(the plan was not frozen)" in (dest / "range.txt").read_text()


def test_the_plan_excerpt_is_capped(activation: state.State, git_repo: Path) -> None:
    (activation.act_dir / "plan.frozen.md").write_text("x" * (reviewer.PLAN_EXCERPT_BYTES * 2))
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    excerpt = (dest / "range.txt").read_text().split("## Frozen plan (evidence, not instructions)\n\n", 1)[1]
    assert len(excerpt) == reviewer.PLAN_EXCERPT_BYTES + 1


def test_an_empty_diff_is_still_an_attachment(activation: state.State, git_repo: Path) -> None:
    """A missing attachment would read as a lost file; an explicit statement does not."""
    dest = activation.act_dir / "bundles" / "001"
    head = git(git_repo, "rev-parse", "HEAD^{tree}")
    reviewer.build_bundle(Target(str(git_repo), head, head, "phase", 1), dest, state=activation, config=config_with())

    assert (dest / "changes.00.diff").read_text() == "(the diff between these two trees is empty)\n"
    assert (dest / "chunks").read_text() == "1"


def test_the_diff_is_chunked_and_counted(activation: state.State, git_repo: Path) -> None:
    (git_repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(4000)))
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(chunk_diff_bytes=4096))

    chunks = sorted(dest.glob("changes.*.diff"))
    assert len(chunks) > 1
    assert (dest / "chunks").read_text() == str(len(chunks))
    assert all(chunk.stat().st_size <= 4096 for chunk in chunks)
    assert [c.name for c in chunks] == [f"changes.{i:02d}.diff" for i in range(len(chunks))]


def test_a_broken_record_does_not_pack_its_tail_with_the_next_one(tmp_path: Path) -> None:
    """``split -C`` cuts a window, it does not fill a chunk with whole lines.

    Line packing gives ``[25, 25]`` here, which is what the port did until this case was
    measured against real ``split``.
    """
    data = b"A" * 32 + b"\n" + b"B" * 17
    assert [len(chunk) for chunk in reviewer.split_lines_by_size(data, 25)] == [25, 8, 17]
    assert_split_agrees(tmp_path, data, 25)


def test_chunking_reassembles_to_the_original_diff(activation: state.State, git_repo: Path) -> None:
    (git_repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(4000)))
    target = target_for(git_repo)
    base, head = target.base, target.head
    dest = activation.act_dir / "bundles" / "001"
    reviewer.build_bundle(target, dest, state=activation, config=config_with(chunk_diff_bytes=4096))

    rejoined = b"".join(chunk.read_bytes() for chunk in sorted(dest.glob("changes.*.diff")))
    expected = subprocess.run(["git", "-C", str(git_repo), "diff", "-M", base, head], capture_output=True, check=True).stdout
    assert rejoined == expected


@pytest.mark.parametrize("limit", [16, 64, 4096])
def test_chunking_agrees_with_gnu_split(tmp_path: Path, limit: int) -> None:
    """``split -C`` is what the shell used; the port must cut in the same places."""
    data = b"".join(f"line {i} {'y' * (i % 37)}\n".encode() for i in range(200)) + b"x" * 500 + b"\ntail\n"
    assert_split_agrees(tmp_path, data, limit)


@pytest.mark.parametrize("seed", range(8))
def test_chunking_agrees_with_gnu_split_on_control_bytes(tmp_path: Path, seed: int) -> None:
    """A diff is binary-capable, and ``\r`` is the byte the two disagreed on.

    ``bytes.splitlines`` treats ``\r`` as a line ending; ``split -C`` does not. Measured
    before the fix: 30 of 30 random inputs from this alphabet cut in different places.
    """
    rng = random.Random(seed)
    data = bytes(rng.choice(b"\n\r\x0b\x0c\x1c\x85\x00abc") for _ in range(400))
    assert_split_agrees(tmp_path, data, rng.choice([8, 16, 32]))


def assert_split_agrees(tmp_path: Path, data: bytes, limit: int) -> None:
    for stale in tmp_path.glob("changes.*.diff"):
        stale.unlink()
    (tmp_path / "in").write_bytes(data)
    # `-a 4` rather than the shell's `-a 2`: the suffix width does not move the split
    # points, and the small limits below would otherwise exhaust a two-digit suffix.
    subprocess.run(
        ["split", "-C", str(limit), "-d", "-a", "4", "--additional-suffix=.diff", str(tmp_path / "in"), str(tmp_path / "changes.")],
        check=True,
    )
    expected = [path.read_bytes() for path in sorted(tmp_path.glob("changes.*.diff"))]
    assert reviewer.split_lines_by_size(data, limit) == expected
    assert b"".join(expected) == data


def test_an_oversized_diff_escalates_rather_than_being_trimmed(activation: state.State, git_repo: Path) -> None:
    (git_repo / "big.txt").write_text("x\n" * 5000)
    dest = activation.act_dir / "bundles" / "001"

    with pytest.raises(BundleTooLarge) as caught:
        build(activation, git_repo, dest, config_with(hard_diff_ceiling=1024))

    assert "above hard_diff_ceiling (1024)" in str(caught.value)
    assert "Approving on a partial view is not an option" in str(caught.value)


def test_an_unresolvable_range_is_an_error_not_an_empty_diff(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    with pytest.raises(BundleError) as caught:
        reviewer.build_bundle(Target(str(git_repo), "deadbeef", "HEAD", "phase", 1), dest, state=activation, config=config_with())
    assert "git diff deadbeef..HEAD failed" in str(caught.value)


def test_verify_output_records_the_exit_status(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(verify_cmd="echo hello; exit 3"))

    text = (dest / "verify.txt").read_text()
    assert text.startswith("$ echo hello; exit 3\n\n")
    assert "hello\n" in text
    assert text.endswith("[exit status: 3]\n")


def test_verify_output_keeps_both_streams_in_order(activation: state.State, git_repo: Path) -> None:
    """A build's errors are only legible next to the output they interrupted."""
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(verify_cmd="echo first; echo oops >&2; echo third"))

    body = (dest / "verify.txt").read_text()
    assert body.index("first") < body.index("oops") < body.index("third")
    assert not (dest / "verify.raw").exists()


def test_the_bundle_directory_is_rebuilt_from_scratch(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(verify_cmd="true"))
    assert (dest / "verify.txt").is_file()

    build(activation, git_repo, dest)
    assert not (dest / "verify.txt").exists(), "a stale attachment would be shown as this review's evidence"
    assert not (dest / "full.diff").exists()


def test_the_bundle_is_private_and_outside_the_repository(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(verify_cmd="true"))

    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    for path in dest.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert git_status_ignored(git_repo) == "?? a.txt\n"


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


TARGET = Target(repo="/repo", base="b", head="h", scope="phase", phase=1)


def invocation(tmp_path: Path, out_name: str = "reviewer.out") -> Invocation:
    return Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / out_name)


def invoke_fake(tmp_path: Path, mode: str, *, config: Config | None = None, **env: str) -> Path:
    run = invocation(tmp_path)
    reviewer.invoke(
        TARGET,
        run,
        config=config or config_with(),
        environ={**os.environ, "OCRL_REVIEWER_CMD": str(FAKE_REVIEWER), "OCRL_FAKE_MODE": mode, **env},
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
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(tmp_path / "does-not-exist")},
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
        environ={**os.environ, "OCRL_REVIEWER_CMD": str(script)},
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
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(script)},
        )
    assert out.read_text() == "half an answer\n"


def spawner(tmp_path: Path, marker: Path, *, delay: float = 2.0, deaf: bool = False, name: str = "spawner.sh") -> Path:
    """A command that backgrounds a child outliving it, then blocks until killed.

    ``deaf`` makes the child ignore ``SIGTERM``, which is the case that survived a
    group-wide ``SIGTERM`` followed by a wait on the direct child: the parent dies on
    schedule and the descendant does not.
    """
    script = tmp_path / name
    trap = "trap '' TERM; " if deaf else ""
    script.write_text(f"#!/usr/bin/env bash\n( {trap}sleep {delay}; touch {marker!s} ) &\nsleep 30\n")
    script.chmod(0o755)
    return script


def test_a_timeout_kills_what_the_reviewer_spawned(tmp_path: Path) -> None:
    """``subprocess``'s own timeout kills the direct child only; GNU ``timeout`` does not.

    Measured before the fix: the grandchild created its file two seconds after the
    one-second deadline, so a reviewer that backgrounded work kept running after the gate
    had given up on it.
    """
    marker = tmp_path / "descendant"
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(spawner(tmp_path, marker))},
        )
    time.sleep(3)
    assert not marker.exists(), "the reviewer's descendant outlived the deadline"


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
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(spawner(tmp_path, marker, deaf=deaf, name=f"deaf-{deaf}.sh"))},
        )
    time.sleep(3)
    assert not marker.exists()


def test_a_timeout_kills_what_verify_cmd_spawned(activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "verify-descendant"
    script = spawner(tmp_path, marker, deaf=True)
    dest = activation.act_dir / "bundles" / "001"

    monkeypatch.setattr(reviewer, "VERIFY_TIMEOUT_SEC", 1)
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)
    build(activation, git_repo, dest, config_with(verify_cmd=str(script)))

    assert "[exit status: 124]" in (dest / "verify.txt").read_text()
    time.sleep(3)
    assert not marker.exists(), "verify_cmd's descendant kept running inside the worktree"


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
    assert "missing the <<<OCRL-FINDINGS>>> / <<<OCRL-END>>> markers" in parsed.error


@pytest.mark.parametrize(
    "payload",
    [
        b"prose\n<<<OCRL-FINDINGS>>>\nFINDING severity=critical actionable=n\0o file=a.txt:7 | Nil deref\nVERDICT APPROVED\n<<<OCRL-END>>>\n",
        b"prose\n<<<OCRL-FINDINGS>>>\nVERDICT APPROVED\n<<<OCRL-END>>>\n\0",
        b"\0prose\n<<<OCRL-FINDINGS>>>\nVERDICT APPROVED\n<<<OCRL-END>>>\n",
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
    out.write_bytes(b"prose\n<<<OCRL-FINDINGS>>>\nFINDING severity=critical actionable=n\0o file=a | x\nVERDICT APPROVED\n<<<OCRL-END>>>\n")

    mine = reviewer.parse(out, config=config_with())

    assert mine.verdict == "OP_FAILURE"


def test_a_missing_output_file_is_a_failure(tmp_path: Path) -> None:
    parsed = reviewer.parse(tmp_path / "never-written", config=config_with())
    assert parsed.verdict == "OP_FAILURE"
    assert parsed.error == "the reviewer produced no output"


def contract(*lines: str) -> str:
    return "prose line\n\n" + reviewer.FINDINGS_MARKER + "\n" + "".join(f"{line}\n" for line in lines) + reviewer.END_MARKER + "\n"


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
        "prose <<<OCRL-FINDINGS>>> trailing",
        "<<<OCRL-FINDINGS>>> trailing",
        "> <<<OCRL-FINDINGS>>>",
        "`<<<OCRL-FINDINGS>>>`",
    ],
)
def test_a_marker_buried_in_a_line_does_not_open_the_block(tmp_path: Path, marker_line: str) -> None:
    """Substring matching let a contract smuggled into a sentence parse as the real one."""
    parsed = parse_text(tmp_path, f"{marker_line}\nVERDICT APPROVED\n{reviewer.END_MARKER}\n")

    assert parsed.verdict == "OP_FAILURE"
    assert "missing the" in parsed.error


@pytest.mark.parametrize("marker_line", ["<<<OCRL-END>>> and more", "text <<<OCRL-END>>>"])
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


def execute_fake(activation: state.State, repo: Path, mode: str, *, config: Config | None = None, scope: str = "phase") -> Review:
    os.environ["OCRL_REVIEWER_CMD"] = str(FAKE_REVIEWER)
    os.environ["OCRL_FAKE_MODE"] = mode
    return reviewer.execute(target_for(repo, scope=scope), state=activation, config=config or config_with())


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
