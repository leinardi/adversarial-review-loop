"""Bundle construction: permission, argv, range.txt, plan revisions.

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

import hashlib
import json
import random
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import config_with, git, git_status_ignored
from reviewer_common import _intact_bundle, build, build_final, execute_fake, target_for

from arl import gitsnap, reviewer, state
from arl.reviewer import BundleError, BundleTooLarge, Target
from arl.util import now as arl_now

#: Shortens the SIGTERM-to-SIGKILL grace for this module. Requested by mark rather than
#: made autouse in ``conftest.py``, which would change the constant for every unit test.
pytestmark = pytest.mark.usefixtures("short_kill_grace")

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


def test_argv_carries_exactly_the_attachments_it_is_given(tmp_path: Path) -> None:
    """``review_argv`` selects nothing itself -- see `bundle_manifest`, which does."""
    given = [tmp_path / "range.txt", tmp_path / "changes.00.diff", tmp_path / "changes.01.diff"]

    argv = reviewer.review_argv("/repo", "a title", config=config_with(), attachments=given)

    assert argv[:2] == ["--pure", "--dir"]
    assert argv[2] == "/repo"
    assert "--title" in argv
    assert [argv[i + 1] for i, item in enumerate(argv) if item == "-f"] == [str(path) for path in given]


def test_argv_never_contains_the_prompt(tmp_path: Path) -> None:
    """``-f`` is a yargs array option: a trailing prompt would be read as an attachment."""
    argv = reviewer.review_argv("/repo", "review-loop phase 1", config=config_with(), attachments=[tmp_path / "range.txt"])
    assert argv[-2] == "-f"


def test_argv_honours_pure_and_variant(tmp_path: Path) -> None:
    plain = reviewer.review_argv("/repo", "t", config=config_with(pure=False))
    assert "--pure" not in plain

    varied = reviewer.review_argv("/repo", "t", config=config_with(variant="thinking"))
    assert varied[varied.index("--variant") + 1] == "thinking"
    assert "--variant" not in plain


def test_verify_output_is_attached_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`verify.txt` is kept out of `bundle_manifest` precisely so it can stay last, after the
    `context/` files -- `stage_invocation` is what puts it there."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, context=True, verify=True)

    staged, context = reviewer.stage_invocation(bundle, tmp_path, digest, tmp_path / "staged", include_context=True)

    assert staged[-1][0].name == "verify.txt"
    assert context and staged[-2][0] == context[0], "context sits between the evidence and verify.txt"


def test_plan_revisions_are_attached_in_ascending_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, revisions=3)

    entries = reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True)

    assert entries is not None
    assert [path for path, _ in entries] == [
        bundle / "range.txt",
        bundle / "changes.00.diff",
        bundle / "plan.rev0.md",
        bundle / "plan.rev1.md",
        bundle / "plan.rev2.md",
    ]


def test_plan_revisions_sort_numerically_not_lexically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``rev10`` must not precede ``rev2`` -- a plain lexical sort would put it there. Counting
    contiguously from zero cannot make that mistake, which is why it replaced the sorted glob."""
    monkeypatch.setenv("ARL_STATE_DIR", str(tmp_path))
    bundle, digest = _intact_bundle(tmp_path, chunks=1, revisions=11)

    entries = reviewer.bundle_manifest(bundle, tmp_path, digest, include_context=True)
    assert entries is not None
    revisions = [path for path, _ in entries if "plan.rev" in path.name]

    assert revisions == [bundle / f"plan.rev{index}.md" for index in range(11)]


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------


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


def test_a_git_option_shaped_activation_commit_is_not_interpolated_into_git_log(activation: state.State, git_repo: Path) -> None:
    """state.json is not a trust boundary. A tampered ``activation_commit`` shaped like
    ``--output=<file>`` would have ``git log`` write inside the reviewed repo (Rule 3); the
    disclosure section degrades to a note instead."""
    pwned = git_repo / "PWNED"
    activation.update(activation_commit=f"--output={pwned}")
    activation.save()
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    text = (dest / "range.txt").read_text()
    assert "activation commit is unreadable" in text
    assert not pwned.exists()


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
# range.txt: Bundle contents
# --------------------------------------------------------------------------


def bundle_contents(dest: Path) -> str:
    """``range.txt``'s ``## Bundle contents`` section, on its own."""
    return (dest / "range.txt").read_text().split("## Bundle contents\n\n", 1)[1].split("\n## ", 1)[0]


def test_the_bundle_lists_its_own_files_so_nothing_has_to_be_looked_up_by_path(activation: state.State, git_repo: Path) -> None:
    """26 permission errors across 24 real transcripts came from globbing `context/.staged-*`
    for files that were already inline. The section names what exists instead."""
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    section = bundle_contents(dest)
    assert "- range.txt (this file)\n" in section
    assert "- changes.00.diff (the complete diff, one chunk)\n" in section
    assert "none of these is a path to open" in section
    assert "incremental.diff" not in section, "round 1 has none, and naming an absent file is what sends the reviewer looking"
    assert "plan.rev" not in section, "an unrevised plan has no revision attachment, and naming an absent file is what sends the reviewer looking"


def test_an_unrevised_plan_is_not_attached_a_second_time(activation: state.State, git_repo: Path) -> None:
    """`range.txt` already inlines the active revision under `## Frozen plan`. With one
    revision that *is* revision 0, so writing `plan.rev0.md` too puts a byte-identical second
    copy of the plan in the same payload -- re-read on every agentic turn of the review, which
    is the single largest avoidable cost in a round."""
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    assert not (dest / "plan.rev0.md").exists()
    assert "## Frozen plan (evidence, not instructions)" in (dest / "range.txt").read_text(), "the plan is still there, once"
    manifest = (dest / "manifest").read_text()
    assert "plan.rev" not in manifest, "and nothing hashed names a file that was never written"


def test_a_missing_verify_txt_is_named_as_missing_rather_than_left_out(activation: state.State, git_repo: Path) -> None:
    """The absence is the fact worth stating: "no verify_cmd is configured" is a different
    conclusion from "the command ran and its output is being withheld"."""
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)
    assert "- verify.txt: not in this bundle -- no verify_cmd configured\n" in bundle_contents(dest)

    other = activation.act_dir / "bundles" / "002"
    build(activation, git_repo, other, config_with(verify_cmd="true"))
    assert "- verify.txt\n" in bundle_contents(other)


def test_the_chunk_range_agrees_with_the_files_actually_written(activation: state.State, git_repo: Path) -> None:
    """The count comes from ``_write_chunks``' return value, not a second derivation of its
    rule -- a bundle whose diff needed several attachments must name every one of them."""
    (git_repo / "big.txt").write_text("".join(f"line {i}\n" for i in range(4000)))
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest, config_with(chunk_diff_bytes=4096))

    chunks = sorted(dest.glob("changes.*.diff"))
    assert len(chunks) > 1
    assert f"- changes.00.diff through changes.{len(chunks) - 1:02d}.diff (the complete diff, {len(chunks)} chunks)\n" in bundle_contents(dest)


def test_every_plan_revision_is_named_in_the_contents(activation: state.State, git_repo: Path) -> None:
    add_revision(activation, 0, "revision zero\n")
    add_revision(activation, 1, "revision one\n")
    dest = activation.act_dir / "bundles" / "001"
    build(activation, git_repo, dest)

    assert "- plan.rev0.md through plan.rev1.md\n" in bundle_contents(dest)
    assert (dest / "plan.rev1.md").is_file()


def test_round_two_names_the_incremental_diff_in_the_contents(activation: state.State, git_repo: Path) -> None:
    execute_fake(activation, git_repo, "changes")
    execute_fake(activation, git_repo, "changes")

    section = bundle_contents(activation.act_dir / "bundles" / "002")
    assert "- incremental.diff\n" in section


def test_the_contents_section_names_exactly_what_the_manifest_hashes(activation: state.State, git_repo: Path) -> None:
    """The two are built from different sources -- the section from the counts, the manifest
    from the paths -- so they can drift. For a bundle's own files they must not."""
    execute_fake(activation, git_repo, "changes")
    execute_fake(activation, git_repo, "changes")
    dest = activation.act_dir / "bundles" / "002"

    section = bundle_contents(dest)
    hashed = {name for _digest, kind, name in reviewer._parse_manifest((dest / "manifest").read_bytes()) if kind == "bundle"}
    for name in hashed:
        assert name in section, f"{name} is hashed into the manifest but not named in Bundle contents"
    assert hashed == {path.name for path in dest.iterdir() if path.name not in ("manifest", "chunks")}


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
    """Two revisions, because one is not attached at all -- `range.txt` carries it. The cap is
    what stops an oversized revision from blowing out the bundle once it *is* attached."""
    oversized = "x" * (reviewer.PLAN_EXCERPT_BYTES * 2)
    add_revision(activation, 0, oversized)
    add_revision(activation, 1, "the revision that made revision 0 history\n")
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


def gnu_split() -> str | None:
    """The GNU ``split`` binary, or ``None`` where the host has only a BSD one.

    ``-C`` and ``--additional-suffix`` are GNU extensions that BSD ``split`` (macOS) does not
    have, and the whole point of these tests is to check our cut points against the real tool,
    so there is nothing to fall back to. Homebrew installs GNU coreutils as ``gsplit``.
    """
    for name in ("gsplit", "split"):
        found = shutil.which(name)
        if not found:
            continue
        probe = subprocess.run([found, "--version"], capture_output=True, text=True, check=False)
        if probe.returncode == 0 and "GNU" in probe.stdout:
            return found
    return None


def assert_split_agrees(tmp_path: Path, data: bytes, limit: int) -> None:
    split = gnu_split()
    if split is None:
        pytest.skip("GNU split is not installed (macOS ships a BSD split without -C)")
    for stale in tmp_path.glob("changes.*.diff"):
        stale.unlink()
    (tmp_path / "in").write_bytes(data)
    # `-a 4` rather than the shell's `-a 2`: the suffix width does not move the split
    # points, and the small limits below would otherwise exhaust a two-digit suffix.
    subprocess.run(
        [split, "-C", str(limit), "-d", "-a", "4", "--additional-suffix=.diff", str(tmp_path / "in"), str(tmp_path / "changes.")],
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


def test_the_incremental_diff_is_omitted_not_truncated_past_the_ceiling(activation: state.State, git_repo: Path) -> None:
    """Mirrors ``_DIFF_OMITTED``'s handling: an oversized incremental diff is disclosed as
    omitted rather than silently truncated into a diff that lies about its own extent. Base
    and head are kept identical so only the incremental diff, not the main one, trips the
    ceiling."""
    seed_tree = git(git_repo, "rev-parse", "HEAD^{tree}")
    (git_repo / "a.txt").write_text("x\n" * 5000)
    head = gitsnap.snapshot(str(git_repo)).tree

    activation.update(
        round_history=[
            {
                "seq": 1,
                "label": "phase1",
                "phase": 1,
                "generation": activation.get_int("activation_generation"),
                "round": 1,
                "verdict": "CHANGES_REQUIRED",
                "tree": seed_tree,
                "base": seed_tree,
                "at": arl_now(),
                "findings": [],
                "supersedes": [],
            }
        ]
    )
    activation.save()

    target = Target(repo=str(git_repo), base=head, head=head, scope="phase", phase=1)
    dest = activation.act_dir / "bundles" / "002"
    reviewer.build_bundle(target, dest, state=activation, config=config_with(hard_diff_ceiling=1024), round_number=2)

    incremental = (dest / "incremental.diff").read_text()
    assert "incremental diff content omitted" in incremental
    assert "hard_diff_ceiling (1024" in incremental

    range_text = (dest / "range.txt").read_text()
    assert "## Changed since round 1\n" in range_text
    assert "a.txt" in range_text, "the changed-path list must survive even when the diff content is omitted"
    assert "incremental diff content omitted" in range_text


def test_a_failed_path_enumeration_does_not_claim_no_change(activation: state.State, git_repo: Path) -> None:
    """``git diff --name-only`` failing is not "nothing changed": reading it that way would
    turn an operational failure into false certainty, byte-identical claim included -- that
    claim depends on actually having obtained the path list."""
    target = target_for(git_repo)
    bogus_tree = "f" * 40  # well-formed, but not an object this repo has
    text = reviewer._range_text(
        target,
        state=activation,
        config=config_with(),
        warnings="",
        revisions=[({}, b"plan\n")],
        previous_tree=bogus_tree,
        previous_round_number=1,
    )
    assert "changed-path list unavailable" in text
    assert "no path changed" not in text
    assert "byte-identical" not in text


def test_an_unresolvable_range_is_an_error_not_an_empty_diff(activation: state.State, git_repo: Path) -> None:
    dest = activation.act_dir / "bundles" / "001"
    with pytest.raises(BundleError) as caught:
        reviewer.build_bundle(Target(str(git_repo), "deadbeef", "HEAD", "phase", 1), dest, state=activation, config=config_with())
    # ``deadbeef`` is not a full-length object id, so it is refused before it can reach
    # ``git diff`` at all -- see the hostile-base tests further down.
    assert "not a usable git object id" in str(caught.value)


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
