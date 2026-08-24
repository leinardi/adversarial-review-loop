"""The reviewer half of the gate: bundle, invocation, and contract parsing.

The property asserted throughout, and the reason this file is long: **Rule 1**. No reviewer
output and no operational failure produces ``APPROVED``. Every broken shape in
``tests/fixtures/fake-reviewer.sh`` is driven through the parser and the full ``execute``
path, and the verdict is asserted, never merely "not a crash".
"""

from __future__ import annotations

import hashlib
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
from ocrl.commands import hooks
from ocrl.config import Config
from ocrl.reviewer import BundleError, BundleTooLarge, Invocation, Review, ReviewerFailed, Target
from ocrl.util import now as ocrl_now

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


@pytest.fixture(autouse=True)
def _short_kill_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the SIGTERM-to-SIGKILL grace for every test in this file.

    The escalation is what is under test, not how long the gate is willing to wait for a
    build to tear itself down: production keeps the full ``KILL_GRACE_SEC``, and the tests
    that assert a descendant died still assert exactly that. Left at 2s it was paid, in real
    seconds, by every timeout test here.
    """
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)


#: How long the spawner's descendant waits before touching its marker, and how long past
#: that a test waits before declaring it never did.
DESCENDANT_DELAY_SEC = 2.0
DESCENDANT_MARGIN_SEC = 1.0


def assert_descendant_never_ran(marker: Path, *, started: float) -> None:
    """Wait until the descendant's own deadline is well past, then require its silence.

    Timed from when the reviewer was *launched*, not from when the kill returned: what has
    to elapse is the descendant's ``sleep``, and sleeping a flat interval afterwards paid for
    the timeout and the kill grace twice over.
    """
    remaining = started + DESCENDANT_DELAY_SEC + DESCENDANT_MARGIN_SEC - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    assert not marker.exists(), "a descendant of the reviewer outlived the deadline"


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
    bundle_dir = tmp_path / "bundles" / "003"
    document = json.loads(reviewer.permission(bundle_dir))

    assert document["*"] == "deny"
    assert document["read"] == "allow"
    assert document["external_directory"]["*"] == "deny"
    # Widened to the bundles root -- a continued reviewer re-opens paths it remembers from an
    # earlier round's bundle -- but no further: see `test_permission_denies_the_activation_dir`.
    assert document["external_directory"][f"{bundle_dir.parent}/**"] == "allow"
    assert "write" not in document
    assert "bash" not in document


def test_permission_denies_the_activation_dir_and_state_json(tmp_path: Path) -> None:
    """The bundles root, not the activation directory -- which also holds ``state.json``,
    ``plan.frozen.md`` and the reports -- is what a continued reviewer may read."""
    act_dir = tmp_path / "activation"
    bundle_dir = act_dir / "bundles" / "003"
    document = json.loads(reviewer.permission(bundle_dir))

    external = document["external_directory"]
    assert f"{bundle_dir.parent}/**" in external
    assert f"{act_dir}/**" not in external
    assert str(act_dir / "state.json") not in external


def test_the_broad_external_deny_is_written_before_the_bundle_allow(tmp_path: Path) -> None:
    """Patterns are last-match-wins, so the order of these two keys is the policy."""
    bundle_dir = tmp_path / "bundles" / "003"
    external = reviewer.permission(bundle_dir).split('"external_directory":', 1)[1]
    assert external.index('"*":"deny"') < external.index(f'"{bundle_dir.parent}/**":"allow"')


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


def test_plan_revisions_are_attached_in_ascending_order(tmp_path: Path) -> None:
    (tmp_path / "range.txt").write_text("r")
    (tmp_path / "plan.rev1.md").write_text("one")
    (tmp_path / "plan.rev0.md").write_text("zero")
    (tmp_path / "plan.rev2.md").write_text("two")

    argv = reviewer.review_argv("/repo", tmp_path, "t", config=config_with())
    attachments = [argv[i + 1] for i, item in enumerate(argv) if item == "-f"]
    assert attachments == [
        str(tmp_path / "range.txt"),
        str(tmp_path / "plan.rev0.md"),
        str(tmp_path / "plan.rev1.md"),
        str(tmp_path / "plan.rev2.md"),
    ]


def test_plan_revisions_sort_numerically_not_lexically(tmp_path: Path) -> None:
    """``rev10`` must not precede ``rev2`` -- a plain lexical sort would put it there."""
    (tmp_path / "range.txt").write_text("r")
    for index in range(11):
        (tmp_path / f"plan.rev{index}.md").write_text(str(index))

    argv = reviewer.review_argv("/repo", tmp_path, "t", config=config_with())
    revision_attachments = [argv[i + 1] for i, item in enumerate(argv) if item == "-f" and "plan.rev" in argv[i + 1]]
    assert len(revision_attachments) == 11
    assert revision_attachments == [str(tmp_path / f"plan.rev{index}.md") for index in range(11)]


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


def test_a_missing_frozen_plan_escalates_rather_than_being_substituted(activation: state.State, git_repo: Path) -> None:
    """Phase 4: a missing/corrupted plan revision is a hard failure, never a placeholder."""
    (activation.act_dir / "plan.frozen.md").unlink()
    dest = activation.act_dir / "bundles" / "001"

    with pytest.raises(reviewer.PlanEvidenceCorrupted) as caught:
        build(activation, git_repo, dest)
    assert "missing" in str(caught.value)


def test_the_plan_excerpt_is_capped(activation: state.State, git_repo: Path) -> None:
    (activation.act_dir / "plan.frozen.md").write_text("x" * (reviewer.PLAN_EXCERPT_BYTES * 2))
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    excerpt = (dest / "range.txt").read_text().split("## Frozen plan (evidence, not instructions)\n\n", 1)[1]
    assert len(excerpt) == reviewer.PLAN_EXCERPT_BYTES + 1


# --------------------------------------------------------------------------
# Plan revisions
# --------------------------------------------------------------------------


def add_revision(activation: state.State, index: int, text: str) -> None:
    """Record one more plan revision, exactly the shape ``resume`` writes."""
    filename = "plan.frozen.md" if index == 0 else f"plan.rev{index}.md"
    (activation.act_dir / filename).write_text(text)
    revisions = list(activation.data.get("plan_revisions") or [])
    revisions.append({"at": index, "phase": index + 1, "sha256": hashlib.sha256(text.encode()).hexdigest(), "file": filename})
    activation.data["plan_revisions"] = revisions
    activation.save()


def test_an_unrevised_plan_has_no_disclosure_section(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)
    assert "## Plan revisions" not in (dest / "range.txt").read_text()


def test_a_revised_plan_is_disclosed_and_every_revision_attached(activation: state.State, git_repo: Path) -> None:
    add_revision(activation, 0, "revision zero\n")
    add_revision(activation, 1, "revision one\n")
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "## Plan revisions" in text
    assert "see plan.rev0.md" in text
    assert "see plan.rev1.md" in text
    # The active (last) revision is what "## Frozen plan" shows.
    assert "revision one" in text.split("## Frozen plan (evidence, not instructions)\n\n", 1)[1]

    assert (dest / "plan.rev0.md").read_text() == "revision zero\n"
    assert (dest / "plan.rev1.md").read_text() == "revision one\n"

    # The hop between the two is disclosed inline too, purely for orientation.
    assert "### revision 0 -> revision 1" in text
    assert "-revision zero" in text
    assert "+revision one" in text


def test_a_truncated_revision_attachment_is_disclosed_not_claimed_complete(activation: state.State, git_repo: Path) -> None:
    """``build_bundle`` caps each attachment at ``PLAN_EXCERPT_BYTES`` -- the disclosure must
    say so and mark the revision it actually cut, never claim every attachment is "in full"
    while one of them was silently truncated."""
    oversized = "x" * (reviewer.PLAN_EXCERPT_BYTES + 100)
    add_revision(activation, 0, "revision zero\n")
    add_revision(activation, 1, oversized)
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "attached in full" not in text
    assert f"capped at {reviewer.PLAN_EXCERPT_BYTES} bytes" in text
    # revision 0 fits and is not marked; revision 1 does not and must be.
    assert "see plan.rev0.md\n" in text
    assert f"see plan.rev1.md -- TRUNCATED at {reviewer.PLAN_EXCERPT_BYTES} bytes" in text

    assert (dest / "plan.rev1.md").stat().st_size == reviewer.PLAN_EXCERPT_BYTES


def test_the_first_revision_hop_diff_is_not_off_by_one(activation: state.State, git_repo: Path) -> None:
    """Three revisions must produce exactly two hops: 0->1 and 1->2, not one, not three."""
    add_revision(activation, 0, "zero\n")
    add_revision(activation, 1, "one\n")
    add_revision(activation, 2, "two\n")
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert text.count("### revision") == 2
    assert "### revision 0 -> revision 1" in text
    assert "### revision 1 -> revision 2" in text


def test_revision_attachments_are_capped(activation: state.State, git_repo: Path) -> None:
    oversized = "x" * (reviewer.PLAN_EXCERPT_BYTES * 2)
    add_revision(activation, 0, oversized)
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    assert (dest / "plan.rev0.md").stat().st_size == reviewer.PLAN_EXCERPT_BYTES


def test_an_oversized_revision_diff_is_omitted_not_truncated(activation: state.State, git_repo: Path) -> None:
    add_revision(activation, 0, "a\n" * 20000)
    add_revision(activation, 1, "b\n" * 20000)
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "diff omitted" in text
    assert f"past {reviewer.PLAN_REVISION_DIFF_BYTES} bytes" in text
    assert "for the full text" not in text
    # The full text is still the attachment's job, not range.txt's.
    assert (dest / "plan.rev0.md").read_text() == "a\n" * 20000


def test_the_diff_omitted_message_does_not_claim_the_attachments_are_complete(activation: state.State, git_repo: Path) -> None:
    """A revision large enough to have its diff omitted is frequently the same one whose
    *attachment* was truncated too (both caps apply to the same oversized content). The
    message pointing at the attachments must not promise "the full text" one section after
    the revision list already marked that same file as cut -- see the constant's own comment."""
    oversized_a = "a\n" * 40000  # past both PLAN_REVISION_DIFF_INPUT_CEILING and PLAN_EXCERPT_BYTES
    oversized_b = "b\n" * 40000
    add_revision(activation, 0, oversized_a)
    add_revision(activation, 1, oversized_b)
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "diff omitted" in text
    assert "for the full text" not in text
    assert f"capped at {reviewer.PLAN_EXCERPT_BYTES} bytes" in text
    # Both attachments really are truncated here, so the marked revision list and the diff
    # message's disclaimer agree with what is actually on disk.
    assert (dest / "plan.rev0.md").stat().st_size == reviewer.PLAN_EXCERPT_BYTES
    assert (dest / "plan.rev1.md").stat().st_size == reviewer.PLAN_EXCERPT_BYTES
    assert "see plan.rev0.md -- TRUNCATED" in text
    assert "see plan.rev1.md -- TRUNCATED" in text


def test_a_modest_edit_in_a_sizeable_plan_still_produces_a_diff(activation: state.State, git_repo: Path) -> None:
    """A one-line change inside two plans a good deal larger than the *output* cap must not be
    omitted just because of that: the input ceiling is deliberately looser than the output cap,
    precisely so a small, useful diff still gets through."""
    lines = [f"line {n}\n" for n in range(3000)]
    before = "".join(lines)
    assert reviewer.PLAN_REVISION_DIFF_BYTES < len(before.encode()) < reviewer.PLAN_REVISION_DIFF_INPUT_CEILING
    lines[1500] = "line 1500, edited\n"
    after = "".join(lines)
    assert len(after.encode()) < reviewer.PLAN_REVISION_DIFF_INPUT_CEILING

    add_revision(activation, 0, before)
    add_revision(activation, 1, after)
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "diff omitted" not in text
    assert "-line 1500\n" in text
    assert "+line 1500, edited\n" in text


def test_a_tampered_revision_escalates_rather_than_being_used(activation: state.State, git_repo: Path) -> None:
    add_revision(activation, 0, "revision zero\n")
    (activation.act_dir / "plan.frozen.md").write_text("tampered\n")
    dest = activation.act_dir / "bundles" / "001"

    with pytest.raises(reviewer.PlanEvidenceCorrupted) as caught:
        build(activation, git_repo, dest)
    assert "no longer matches the hash" in str(caught.value)


def test_a_tampered_revision_escalates_the_whole_review(activation: state.State, git_repo: Path) -> None:
    add_revision(activation, 0, "revision zero\n")
    (activation.act_dir / "plan.frozen.md").write_text("tampered\n")

    review = execute_fake(activation, git_repo, "approve")
    assert review.verdict == "NEEDS_HUMAN"
    assert "no longer matches the hash" in review.error


def test_a_non_object_plan_revisions_entry_escalates_rather_than_crashing(activation: state.State, git_repo: Path) -> None:
    """A malformed ``plan_revisions`` entry -- not even an object -- must still be reported as
    ``PlanEvidenceCorrupted``, not an uncontrolled ``AttributeError``/``ValueError`` caught only
    by whatever generic guard happens to be above it."""
    activation.data["plan_revisions"] = ["not-an-object"]
    activation.save()
    dest = activation.act_dir / "bundles" / "001"

    with pytest.raises(reviewer.PlanEvidenceCorrupted) as caught:
        build(activation, git_repo, dest)
    assert "not an object" in str(caught.value)


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
    started = time.monotonic()
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(spawner(tmp_path, marker))},
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
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)
    marker = tmp_path / f"deaf-{deaf}"
    started = time.monotonic()
    with pytest.raises(ReviewerFailed):
        reviewer.invoke(
            TARGET,
            Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / "reviewer.out"),
            config=config_with(timeout_sec=1),
            environ={**os.environ, "OCRL_REVIEWER_CMD": str(spawner(tmp_path, marker, deaf=deaf, name=f"deaf-{deaf}.sh"))},
        )
    assert_descendant_never_ran(marker, started=started)


def test_a_timeout_kills_what_verify_cmd_spawned(activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "verify-descendant"
    script = spawner(tmp_path, marker, deaf=True)
    dest = activation.act_dir / "bundles" / "001"

    monkeypatch.setattr(reviewer, "VERIFY_TIMEOUT_SEC", 1)
    monkeypatch.setattr(reviewer, "KILL_GRACE_SEC", 0.2)
    started = time.monotonic()
    build(activation, git_repo, dest, config_with(verify_cmd=str(script)))

    assert "[exit status: 124]" in (dest / "verify.txt").read_text()
    assert_descendant_never_ran(marker, started=started)


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


def test_a_critical_block_severity_blocks_only_critical_findings(tmp_path: Path) -> None:
    """`critical` is a real fifth tier the reviewer contract's `FINDING` regex accepts, not a
    typo that should fall through `threshold_rank`'s unrecognised-value fallback (rank 1,
    which would block on everything instead of the critical-only threshold asked for)."""
    text = contract(
        "FINDING severity=high actionable=yes file=a | serious but not critical",
        "FINDING severity=critical actionable=yes file=b | critical",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="critical"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert parsed.findings == "FINDING severity=critical actionable=yes file=b | critical\n"


def test_an_unrecognised_block_severity_blocks_everything_rather_than_nothing(tmp_path: Path) -> None:
    """``severity_rank``'s "unrecognised ranks highest" rule is fail-*open* if it is applied
    to the threshold instead of the finding: an unknown ``block_severity`` would rank 5,
    above every real severity, so nothing would ever meet it and even a ``high`` actionable
    finding would sail through APPROVED. The threshold must use `threshold_rank`, whose
    fallback is the opposite direction (rank 1), so a typo'd or unrecognised threshold makes
    the gate block on everything rather than on nothing (Rule 1)."""
    text = contract(
        "FINDING severity=low actionable=yes file=a | trivial-looking",
        "VERDICT APPROVED",
    )

    parsed = parse_text(tmp_path, text, config_with(block_severity="hihg"))

    assert parsed.verdict == "CHANGES_REQUIRED"
    assert "severity=low" in parsed.findings


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


# --------------------------------------------------------------------------
# Session continuity
# --------------------------------------------------------------------------


def continuity_reviewer(tmp_path: Path) -> Path:
    """Approves iff told it is continuing a session, via ``OCRL_SESSION_ID`` -- the env hook
    ``invoke`` sets on the stub path when ``run.session_id`` is non-empty. Drives the
    cold-approval invariant deterministically: the continued round approves, the cold
    confirmation (which never carries a session id) does not.
    """
    script = tmp_path / "continuity-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${OCRL_SESSION_ID:-}" ]; then\n'
        "    printf 'Continuing.\\n\\n<<<OCRL-FINDINGS>>>\\nVERDICT APPROVED\\n<<<OCRL-END>>>\\n'\n"
        "else\n"
        "    printf 'Fresh or cold.\\n\\n<<<OCRL-FINDINGS>>>\\n"
        "FINDING severity=high actionable=yes file=a.txt:1 | still there\\n"
        "VERDICT CHANGES_REQUIRED\\n<<<OCRL-END>>>\\n'\n"
        "fi\n"
    )
    script.chmod(0o755)
    return script


def session_list_script(tmp_path: Path, rows: list[dict[str, object]], *, name: str = "session-list.sh") -> Path:
    """A stand-in for ``opencode session list --format json``, wired via ``OCRL_SESSION_LIST_CMD``."""
    script = tmp_path / name
    script.write_text(f"#!/usr/bin/env bash\ncat <<'JSON'\n{json.dumps(rows)}\nJSON\n")
    script.chmod(0o755)
    return script


def generation_bumping_reviewer(tmp_path: Path, state_path: Path) -> Path:
    """Bumps ``activation_generation`` from inside the reviewer, then answers -- stands in for
    a concurrent ``resume --replan`` landing while this review's slow work runs."""
    script = tmp_path / "gen-bump-reviewer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"p = pathlib.Path({str(state_path)!r})\n"
        "d = json.loads(p.read_text())\n"
        'd["activation_generation"] = d.get("activation_generation", 0) + 1\n'
        "p.write_text(json.dumps(d))\n"
        "PY\n"
        "printf 'Fine.\\n\\n<<<OCRL-FINDINGS>>>\\nVERDICT CHANGES_REQUIRED\\n<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    return script


def _future_ms() -> int:
    """A ``created`` timestamp guaranteed to be >= any ``started_ms`` computed during a test."""
    return int(time.time() * 1000) + 10_000_000


def stored_pointer(
    *, session_id: str = "ses_deadbeef01", label: str = "phase1", revisions: int = 0, generation: int = 0, round_number: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "label": label,
        "id": session_id,
        "title": "review-loop phase 1 [aaaaaaaa/001]",
        "created": 1234567890000,
        "revisions": revisions,
        "generation": generation,
        "round": round_number,
        "claimed_at": "",
        "claim_id": "",
        **extra,
    }


def matching_row(pointer: dict[str, object], repo: Path) -> dict[str, object]:
    return {"id": pointer["id"], "title": pointer["title"], "created": pointer["created"], "directory": str(repo)}


# -- the shared isolation helper --------------------------------------------


def test_isolation_argv_and_env_cannot_drift_between_invoke_and_session_list(git_repo: Path) -> None:
    pure_on = config_with(pure=True, disable_project_config=True)
    pure_off = config_with(pure=False, disable_project_config=False)

    assert reviewer._isolation_argv(pure_on) == ["--pure"]
    assert reviewer._isolation_argv(pure_off) == []
    assert reviewer._isolation_env(pure_on, {})["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in reviewer._isolation_env(pure_off, {})

    # `review_argv` (invoke's real path) is built from exactly this helper's output.
    invoke_argv = reviewer.review_argv("/repo", Path("/bundle"), "t", config=pure_on)
    assert invoke_argv[: len(reviewer._isolation_argv(pure_on))] == reviewer._isolation_argv(pure_on)
    plain_argv = reviewer.review_argv("/repo", Path("/bundle"), "t", config=pure_off)
    assert "--pure" not in plain_argv


# -- argv: -s vs --title -----------------------------------------------------


def test_argv_carries_s_when_continuing_and_title_when_fresh(tmp_path: Path) -> None:
    (tmp_path / "range.txt").write_text("r")
    fresh = reviewer.review_argv("/repo", tmp_path, "a title", config=config_with())
    assert "--title" in fresh
    assert "-s" not in fresh

    continued = reviewer.review_argv("/repo", tmp_path, "a title", config=config_with(), session_id="ses_abc12345")
    assert "-s" in continued
    assert continued[continued.index("-s") + 1] == "ses_abc12345"
    assert "--title" not in continued


# -- session_ref: structural resets ------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(label="phase2"), id="label-change"),
        pytest.param(lambda p: p.update(label="final"), id="phase-to-final"),
        pytest.param(lambda p: p.update(revisions=1), id="revisions-grown"),
        pytest.param(lambda p: p.update(generation=1), id="generation-bumped"),
        pytest.param(lambda p: p.update(id="not-a-session-id"), id="malformed-id"),
        pytest.param(lambda p: p.update(id=""), id="empty-id"),
        pytest.param(lambda p: p.update(id="ses_" + "x" * 100), id="id-too-long"),
        pytest.param(lambda p: p.update(id="ses_../../etc/passwd"), id="id-path-traversal-shaped"),
    ],
)
def test_session_ref_resets_on_a_structural_mismatch(activation: state.State, git_repo: Path, mutate: object) -> None:
    pointer = stored_pointer()
    mutate(pointer)  # type: ignore[operator]
    activation.data["reviewer_session"] = pointer
    activation.save()

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())

    assert ref.session_id == ""
    assert ref.capturable is True
    assert ref.round == 1


# -- session_ref: the listing verify -----------------------------------------


def test_session_ref_rejects_an_unrelated_session_in_the_same_repo(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """The case that motivates the check: an id belonging to the user's own TUI session in
    the same repository must not be joined."""
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    activation.save()
    row = {"id": "ses_unrelated9", "title": "someone's TUI session", "created": 1, "directory": str(git_repo)}
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())

    assert ref.session_id == ""
    assert ref.capturable is True


def test_session_ref_rejects_a_title_mismatch(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    activation.save()
    row = matching_row(pointer, git_repo)
    row["title"] = "a different title"
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())
    assert ref.session_id == ""


def test_session_ref_rejects_a_created_mismatch(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    activation.save()
    row = matching_row(pointer, git_repo)
    row["created"] = 999
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())
    assert ref.session_id == ""


def test_session_ref_accepts_a_symlinked_directory_and_rejects_a_different_repo(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    activation.save()

    symlinked = tmp_path / "symlinked-repo"
    symlinked.symlink_to(git_repo)
    row = matching_row(pointer, symlinked)
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))
    accepted = reviewer.session_ref(activation, target_for(git_repo), config=config_with())
    assert accepted.session_id == pointer["id"]

    other_repo = tmp_path / "genuinely-different"
    other_repo.mkdir()
    row_wrong = matching_row(pointer, other_repo)
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row_wrong], name="session-list-2.sh"))
    rejected = reviewer.session_ref(activation, target_for(git_repo), config=config_with())
    assert rejected.session_id == ""


def test_session_ref_falls_back_to_fresh_when_the_listing_is_unavailable(activation: state.State, git_repo: Path) -> None:
    """No ``OCRL_SESSION_LIST_CMD`` -- the listing call is skipped, and an unverifiable
    pointer falls back to a fresh session, never to an error."""
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ.pop("OCRL_SESSION_LIST_CMD", None)
    os.environ["OCRL_REVIEWER_CMD"] = str(FAKE_REVIEWER)

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())
    assert ref.session_id == ""
    assert ref.capturable is True


# -- the atomic claim ---------------------------------------------------------


def test_session_ref_claims_an_unclaimed_pointer(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())

    assert ref.session_id == pointer["id"]
    assert ref.claim_id != ""
    assert ref.round == 2
    stored = activation.data["reviewer_session"]
    assert stored["claim_id"] == ref.claim_id
    assert stored["claimed_at"] != ""


def test_session_ref_treats_a_live_claim_as_busy(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(claimed_at=ocrl_now(), claim_id="owner-token")
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with())

    assert ref.session_id == ""
    assert ref.capturable is False
    stored = activation.data["reviewer_session"]
    assert stored["claim_id"] == "owner-token"


def test_session_ref_reclaims_an_expired_claim(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(claimed_at=ocrl_now() - 10_000, claim_id="dead-token")
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    ref = reviewer.session_ref(activation, target_for(git_repo), config=config_with(timeout_sec=1))

    assert ref.session_id == pointer["id"]
    assert ref.claim_id not in ("", "dead-token")


def test_a_stale_owners_release_is_a_no_op_after_reclaim(activation: state.State, git_repo: Path) -> None:
    """The ABA case the claim token exists to prevent: A's claim expires, B reclaims with a
    new token, then A's release must not clear B's still-live claim."""
    pointer = stored_pointer(claimed_at=1, claim_id="A-token")
    activation.data["reviewer_session"] = pointer
    activation.save()
    config = config_with()
    expected = hooks.activation(activation, config)

    # B reclaims (A's claim is ancient -> expired).
    activation.data["reviewer_session"]["claim_id"] = "B-token"
    activation.data["reviewer_session"]["claimed_at"] = ocrl_now()
    activation.save()

    reviewer._release_claim(activation, claim_id="A-token", round_number=99, expected=expected, config=config)

    stored = activation.data["reviewer_session"]
    assert stored["claim_id"] == "B-token"
    assert stored["round"] != 99


# -- capture_session ----------------------------------------------------------


def test_capture_session_requires_exactly_one_match(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    target = target_for(git_repo)
    ctx = reviewer._CaptureContext(target=target, title="review-loop phase 1 [x/001]", round_number=1)
    started_ms = _future_ms() - 1_000_000
    act_dir = activation.act_dir

    # None at all.
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [], name="none.sh"))
    assert not reviewer.capture_session(ctx, config=config_with(), act_dir=act_dir, seq="001", started_ms=started_ms)

    # Two rows carrying the title -- not a guess at which is ours.
    row = {"id": "ses_aaaaaaaa", "title": ctx.title, "created": _future_ms(), "directory": str(git_repo)}
    row2 = {**row, "id": "ses_bbbbbbbb"}
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row, row2], name="two.sh"))
    assert not reviewer.capture_session(ctx, config=config_with(), act_dir=act_dir, seq="002", started_ms=started_ms)

    # A row that predates the run.
    stale = {**row, "created": started_ms - 1}
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [stale], name="stale.sh"))
    assert not reviewer.capture_session(ctx, config=config_with(), act_dir=act_dir, seq="003", started_ms=started_ms)

    # Exactly one, valid, in-window match.
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row], name="one.sh"))
    captured = reviewer.capture_session(ctx, config=config_with(), act_dir=act_dir, seq="004", started_ms=started_ms)
    assert captured.session_id == "ses_aaaaaaaa"
    assert bool(captured) is True


def test_capture_session_survives_a_non_json_listing(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    ctx = reviewer._CaptureContext(target=target_for(git_repo), title="t", round_number=1)
    script = tmp_path / "broken.sh"
    script.write_text("#!/usr/bin/env bash\nprintf 'not json'\n")
    script.chmod(0o755)
    os.environ["OCRL_SESSION_LIST_CMD"] = str(script)

    captured = reviewer.capture_session(ctx, config=config_with(), act_dir=activation.act_dir, seq="001", started_ms=0)
    assert not captured


# -- the cold-approval invariant ----------------------------------------------


def test_capture_and_reuse_a_session_across_rounds(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    target = target_for(git_repo)
    label = f"{activation.get_int('report_seq') + 1:03d}"
    title = reviewer._unique_title(activation, target, label)
    session_id = "ses_deadbeef01"
    row = {"id": session_id, "title": title, "created": _future_ms(), "directory": str(git_repo)}

    os.environ["OCRL_REVIEWER_CMD"] = str(continuity_reviewer(tmp_path))
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    first = reviewer.execute(target, state=activation, config=config_with())
    assert first.verdict == "CHANGES_REQUIRED"
    # The captured session is not known until after the round ran, but the round's own
    # report must still be able to say which session it created.
    assert first.session == session_id
    assert first.round == 1
    assert first.confirmed is None

    pointer = activation.data["reviewer_session"]
    assert pointer["id"] == session_id
    assert pointer["label"] == target.label
    assert pointer["round"] == 1
    assert pointer["claim_id"] == ""
    assert pointer["claimed_at"] == ""

    second = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    # The cold-approval invariant: the continued round approved, but the returned, acted-on
    # review is the cold confirmation -- which this stub always denies.
    assert second.verdict == "CHANGES_REQUIRED"
    assert second.session == ""
    assert second.round == 0
    assert second.confirmed is not None
    assert second.confirmed.verdict == "APPROVED"
    assert second.confirmed.session == session_id
    assert second.confirmed.round == 2

    pointer = activation.data["reviewer_session"]
    assert pointer["round"] == 2
    assert pointer["claim_id"] == ""
    assert pointer["claimed_at"] == ""

    raw_dir = activation.act_dir / "raw"
    assert (raw_dir / f"002-{target.label}.out").is_file()
    assert (raw_dir / f"002-{target.label}-cold.out").is_file()


def test_a_continued_changes_required_triggers_no_cold_call(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    target = target_for(git_repo)
    label = f"{activation.get_int('report_seq') + 1:03d}"
    title = reviewer._unique_title(activation, target, label)
    session_id = "ses_cafebabe1"
    row = {"id": session_id, "title": title, "created": _future_ms(), "directory": str(git_repo)}

    os.environ["OCRL_REVIEWER_CMD"] = str(FAKE_REVIEWER)
    os.environ["OCRL_FAKE_MODE"] = "changes"
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    reviewer.execute(target, state=activation, config=config_with())
    second = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    assert second.session == session_id
    assert second.verdict == "CHANGES_REQUIRED"
    assert second.confirmed is None

    raw_names = {p.name for p in (activation.act_dir / "raw").iterdir()}
    assert not any("cold" in name for name in raw_names)


def test_a_cold_approval_triggers_no_second_call(activation: state.State, git_repo: Path) -> None:
    """No ``OCRL_SESSION_LIST_CMD`` -- capture and verify are both skipped, so every review
    here is cold by construction, and a first, already-cold approval must not double-review."""
    review = execute_fake(activation, git_repo, "approve")
    assert review.verdict == "APPROVED"
    assert review.confirmed is None


def test_capture_is_fingerprinted_against_a_concurrent_generation_bump(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    target = target_for(git_repo)
    label = f"{activation.get_int('report_seq') + 1:03d}"
    title = reviewer._unique_title(activation, target, label)
    row = {"id": "ses_race000001", "title": title, "created": _future_ms(), "directory": str(git_repo)}

    os.environ["OCRL_REVIEWER_CMD"] = str(generation_bumping_reviewer(tmp_path, activation.act_dir / "state.json"))
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    review = reviewer.execute(target, state=activation, config=config_with())

    assert review.verdict == "CHANGES_REQUIRED"  # the review's own verdict is unaffected
    activation.load()
    assert activation.data.get("reviewer_session") == {}


def test_bundles_directory_holds_only_gate_generated_evidence(activation: state.State, git_repo: Path) -> None:
    """The invariant the cold-approval design rests on: a continued reviewer's
    ``external_directory`` reach is the bundles root, so nothing in here may be model output."""
    execute_fake(activation, git_repo, "approve")
    bundle_dir = activation.act_dir / "bundles" / "001"
    names = {p.name for p in bundle_dir.iterdir()}
    for name in names:
        assert name in {"range.txt", "chunks"} or name.startswith(("changes.", "plan.rev")), name
    assert "reviewer.out" not in names
    assert not any(name.startswith("session-list") for name in names)


def test_the_range_text_discloses_the_round(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    target = target_for(git_repo)
    label = f"{activation.get_int('report_seq') + 1:03d}"
    title = reviewer._unique_title(activation, target, label)
    row = {"id": "ses_round0002", "title": title, "created": _future_ms(), "directory": str(git_repo)}

    os.environ["OCRL_REVIEWER_CMD"] = str(FAKE_REVIEWER)
    os.environ["OCRL_FAKE_MODE"] = "changes"
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [row]))

    reviewer.execute(target, state=activation, config=config_with())
    bundle_dir = activation.act_dir / "bundles" / "001"
    assert "round: 1\n" in (bundle_dir / "range.txt").read_text()

    reviewer.execute(target_for(git_repo), state=activation, config=config_with())
    second_bundle_dir = activation.act_dir / "bundles" / "002"
    assert "round: 2\n" in (second_bundle_dir / "range.txt").read_text()


def test_the_permission_scope_allows_the_bundles_root_for_a_continued_reviewer(activation: state.State, git_repo: Path) -> None:
    """A continued reviewer remembers paths from an earlier round's bundle -- confirmed by
    actually invoking the fake reviewer in `echo-bundle` mode against the bundles root."""
    execute_fake(activation, git_repo, "approve")
    document = json.loads(reviewer.permission(activation.act_dir / "bundles" / "001"))
    bundles_root = activation.act_dir / "bundles"
    assert document["external_directory"][f"{bundles_root}/**"] == "allow"
    assert f"{activation.act_dir}/**" not in document["external_directory"]


# --------------------------------------------------------------------------
# Fixes from adversarial review: reload safety, reclaim window, claim races
# --------------------------------------------------------------------------


def test_session_ref_reads_state_as_the_caller_loaded_it(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """``session_ref`` must not defensively reload state itself: a transient reload failure
    would silently replace an already-loaded, caller-validated document with an empty one,
    corrupting the rest of this review's evidence for no reason. The structural read is
    exactly ``state.data`` as the caller (``execute``) already has it -- proven here by an
    in-memory mutation that is visible immediately, with no ``.save()`` in between."""
    pointer = stored_pointer()
    activation.data["reviewer_session"] = pointer
    target = target_for(git_repo)
    assert reviewer._pointer_structurally_usable(pointer, activation, target) is True

    # Persisting only now is what lets the (legitimate, atomic) claim step below succeed --
    # it always reloads under its own lock, by design; only the earlier structural read must
    # not.
    activation.save()
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))
    ref = reviewer.session_ref(activation, target, config=config_with())
    assert ref.session_id == pointer["id"]


def test_the_reclaim_window_accounts_for_verify_cmd_time(git_repo: Path) -> None:
    config = config_with(timeout_sec=900)
    assert reviewer._reclaim_after(config) == 900 + reviewer.VERIFY_TIMEOUT_SEC + 60


def test_a_concurrent_live_claim_stops_a_fresh_capture_from_overwriting_it(activation: state.State, git_repo: Path) -> None:
    """The window between deciding "capturable" (no usable pointer) and this write is the
    review itself -- long enough for someone else to have claimed a pointer in the meantime.
    Overwriting that live claim would be the same corruption the claim exists to prevent."""
    target = target_for(git_repo)
    ctx = reviewer._CaptureContext(target=target, title="t", round_number=1)
    captured = reviewer._Captured(session_id="ses_freshcapture1", created=1)
    config = config_with()
    expected = hooks.activation(activation, config)

    live_pointer = stored_pointer(session_id="ses_liveowner001", claimed_at=ocrl_now(), claim_id="live-token")
    activation.data["reviewer_session"] = live_pointer
    activation.save()

    reviewer._store_captured_session(activation, ctx, captured, expected=expected, config=config)

    stored = activation.data["reviewer_session"]
    assert stored["id"] == "ses_liveowner001"
    assert stored["claim_id"] == "live-token"


def test_a_bundle_failure_releases_the_claim_without_advancing_the_round(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with(hard_diff_ceiling=1))

    assert review.verdict == "NEEDS_HUMAN"
    stored = activation.data["reviewer_session"]
    assert stored["claim_id"] == ""
    assert stored["claimed_at"] == ""
    assert stored["round"] == 1


def failing_then_working_reviewer(tmp_path: Path) -> Path:
    """Fails (non-zero exit) on its first call, then answers ``CHANGES_REQUIRED`` -- proves a
    failed invocation releases its claim so an immediate retry can actually continue it,
    rather than finding it "busy" and being forced fresh until the reclaim window elapses.
    Always denies once past the marker, deliberately, so this stays a claim-release test and
    never triggers the (separately tested) cold-approval confirmation."""
    marker = tmp_path / "failing-reviewer-ran-once"
    seen = tmp_path / "seen-session-id"
    script = tmp_path / "failing-then-working.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"if [ ! -f {str(marker)!r} ]; then\n"
        f"    touch {str(marker)!r}\n"
        "    echo boom >&2\n"
        "    exit 3\n"
        "fi\n"
        f'if [ -n "${{OCRL_SESSION_ID:-}}" ]; then echo "$OCRL_SESSION_ID" > {str(seen)!r}; fi\n'
        "printf 'Fine.\\n\\n<<<OCRL-FINDINGS>>>\\n"
        "FINDING severity=high actionable=yes file=a.txt:1 | still there\\n"
        "VERDICT CHANGES_REQUIRED\\n<<<OCRL-END>>>\\n'\n"
    )
    script.chmod(0o755)
    return script


def test_a_failed_invocation_releases_its_claim_so_a_retry_can_continue_it(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_REVIEWER_CMD"] = str(failing_then_working_reviewer(tmp_path))
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    first = reviewer.execute(target_for(git_repo), state=activation, config=config_with())
    assert first.verdict == "OP_FAILURE"
    stored = activation.data["reviewer_session"]
    assert stored["claim_id"] == ""
    assert stored["claimed_at"] == ""
    assert stored["round"] == 1

    second = reviewer.execute(target_for(git_repo), state=activation, config=config_with())
    assert (tmp_path / "seen-session-id").read_text().strip() == pointer["id"]
    assert second.session == pointer["id"]


# --------------------------------------------------------------------------
# Fixes from a second adversarial review: unbounded bundle time, benign advances
# --------------------------------------------------------------------------


def test_a_benign_round_advance_is_not_treated_as_a_moved_pointer(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    """A concurrent round on the *same* session completing and releasing only touches
    round/claimed_at/claim_id -- that must not be read as "the pointer moved", or this call
    would discard continuity and later overwrite the completed round with a brand new
    session, losing its findings and round history."""
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    target = target_for(git_repo)

    # Between a listing verify and a claim attempt, someone else completed a round and
    # released, advancing `round` -- a benign field-level change to the same session.
    activation.data["reviewer_session"] = dict(pointer, round=5)
    activation.save()

    claim_id, round_number = reviewer._try_claim(activation, target=target, session_id=str(pointer["id"]), config=config_with())

    assert claim_id not in (None, "")
    assert round_number == 6  # built on the completed round, not discarded


def test_try_claim_still_resets_on_a_genuine_identity_change(activation: state.State, git_repo: Path) -> None:
    """The re-verification is narrower, not weaker: a *different* session id, or a structural
    field actually changing, is still treated as moved."""
    pointer = stored_pointer(round_number=1)
    target = target_for(git_repo)

    activation.data["reviewer_session"] = dict(pointer, id="ses_somethingnew1")
    activation.save()
    claim_id, _ = reviewer._try_claim(activation, target=target, session_id=str(pointer["id"]), config=config_with())
    assert claim_id is None

    activation.data["reviewer_session"] = dict(pointer, generation=7)
    activation.save()
    claim_id, _ = reviewer._try_claim(activation, target=target, session_id=str(pointer["id"]), config=config_with())
    assert claim_id is None


def test_reconfirm_claim_detects_a_reclaim_before_invoking(activation: state.State, git_repo: Path, tmp_path: Path) -> None:
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    target = target_for(git_repo)
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    ref = reviewer.session_ref(activation, target, config=config_with())
    assert ref.session_id == pointer["id"]
    assert reviewer._reconfirm_claim(activation, ref, config=config_with()) is True

    # Someone else reclaims the pointer -- our claim id no longer matches.
    stored = activation.data["reviewer_session"]
    stored["claim_id"] = "someone-else"
    activation.data["reviewer_session"] = stored
    activation.save()

    assert reviewer._reconfirm_claim(activation, ref, config=config_with()) is False


def test_execute_falls_back_to_fresh_when_the_claim_is_lost_before_invoking(
    activation: state.State, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closes the gap padding the reclaim window cannot: bundle building (ordinary git calls,
    ``verify_cmd``) has no fixed upper bound, so ownership is re-checked right before the one
    call the claim actually protects -- simulated here by stealing the claim from inside
    ``build_bundle`` itself, standing in for a build that outlasted the reclaim window."""
    pointer = stored_pointer(round_number=1)
    activation.data["reviewer_session"] = pointer
    activation.save()
    os.environ["OCRL_REVIEWER_CMD"] = str(continuity_reviewer(tmp_path))
    os.environ["OCRL_SESSION_LIST_CMD"] = str(session_list_script(tmp_path, [matching_row(pointer, git_repo)]))

    real_build_bundle = reviewer.build_bundle

    def stealing_build_bundle(*args: object, **kwargs: object) -> None:
        stored = activation.data["reviewer_session"]
        stored["claim_id"] = "thief"
        activation.data["reviewer_session"] = stored
        activation.save()
        real_build_bundle(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(reviewer, "build_bundle", stealing_build_bundle)

    review = reviewer.execute(target_for(git_repo), state=activation, config=config_with())

    # `continuity_reviewer` approves iff OCRL_SESSION_ID is set -- it must not be, since the
    # claim was lost before invoke ran, so this must be a fresh, uncontinued round.
    assert review.verdict == "CHANGES_REQUIRED"
    assert review.confirmed is None
    assert review.session == ""

    # The bundle was built disclosing the old, continued round (2) -- it must not still tell
    # the reviewer that, now that the invocation actually sent is cold.
    range_text = (activation.act_dir / "bundles" / "001" / "range.txt").read_text()
    assert "round: 1\n" in range_text
    assert "round: 2\n" not in range_text
