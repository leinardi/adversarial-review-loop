"""The harness seam: the contract every reviewer CLI implementation must satisfy.

Separate from ``test_runner.py`` because these are assertions about the *seam*, not about
OpenCode: each one is parametrised over every registered harness, so a second implementation
cannot be added without meeting them.
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

import json
import time
from pathlib import Path

import pytest
from conftest import config_with

from arl import harness, runner
from arl.config import DEFAULTS
from arl.harness import opencode

#: Payloads the stdin tests round-trip. Named because ruff counts a bytes literal in a
#: comparison as a magic value, and because both sides of each assertion must be the one
#: value -- a test that echoes a different constant than it sent proves nothing.
_STDIN_MARKER = b"contract-marker\n"
_NO_STDIN_OUTPUT = b"ok\n"


def every_harness() -> list[harness.Harness]:
    return [harness.get(name) for name in harness.names()]


def spec_for(implementation: harness.Harness, tmp_path: Path, *, system_prompt: str = "") -> harness.ReviewSpec:
    del implementation
    return harness.ReviewSpec(
        repo="/launch",
        prompt_text="review this",
        system_prompt=system_prompt,
        title="review-loop phase 1",
        bundle_dir=tmp_path / "reviews" / "001" / "bundle",
        roots=("/launch/api", "/elsewhere/web"),
        config=config_with(),
    )


# --------------------------------------------------------------------------
# run_bounded's deadline must cover writing stdin, not only waiting
# --------------------------------------------------------------------------


def test_stdin_write_is_inside_the_deadline(tmp_path: Path, short_kill_grace: None) -> None:
    """A child that never drains stdin must still be killed at ``timeout_sec``.

    **Fails on the version of this function that wrote stdin before starting the timed
    wait.** A pipe holds 64KiB; a bundle-sized prompt is far past it, so the write blocks the
    moment the reviewer stops reading and the deadline is never reached. Measured on that
    version: 30s elapsed under ``timeout_sec=1`` -- no deadline at all, with the hook that
    launched it wedged until the outer shim's own timeout.
    """
    out = tmp_path / "out"
    sleeper = ["python3", "-c", "import time; time.sleep(30)"]
    payload = b"x" * (1024 * 1024)

    started = time.monotonic()
    with out.open("wb") as sink:
        status = runner.run_bounded(sleeper, stdout=sink, timeout_sec=1, stdin=payload)
    elapsed = time.monotonic() - started

    assert status == 124, "a child killed at the deadline reports the timeout status"
    # The deadline plus the SIGTERM->SIGKILL grace, with room to spare -- and nowhere near
    # the child's own 30s sleep, which is what the pre-fix version waited out in full. The
    # grace is the shortened one: what is under test is that the deadline binds at all, and
    # the full 2s was paid in real time by a test that never asserts on it.
    assert elapsed < 1 + runner.KILL_GRACE_SEC + 5, f"the deadline did not bind the stdin write: {elapsed:.1f}s"


def test_stdin_reaches_the_child(tmp_path: Path) -> None:
    """The bytes actually arrive -- the deadline fix must not have broken delivery."""
    out = tmp_path / "out"
    echo = ["python3", "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    with out.open("wb") as sink:
        status = runner.run_bounded(echo, stdout=sink, timeout_sec=60, stdin=_STDIN_MARKER)
    assert status == 0
    assert out.read_bytes() == _STDIN_MARKER


def test_stdin_none_leaves_the_child_reading_nothing_from_us(tmp_path: Path) -> None:
    """``stdin=None`` keeps the pre-existing path: no pipe is created at all."""
    out = tmp_path / "out"
    with out.open("wb") as sink:
        status = runner.run_bounded(["python3", "-c", "print('ok')"], stdout=sink, timeout_sec=60)
    assert status == 0
    assert out.read_bytes() == _NO_STDIN_OUTPUT


def test_a_child_that_exits_early_is_not_an_error(tmp_path: Path) -> None:
    """EPIPE while writing is swallowed; the child's own status is what counts."""
    out = tmp_path / "out"
    with out.open("wb") as sink:
        status = runner.run_bounded(["python3", "-c", "raise SystemExit(3)"], stdout=sink, timeout_sec=60, stdin=b"y" * (1024 * 1024))
    assert status == 3


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_every_harness_satisfies_the_protocol(implementation: harness.Harness) -> None:
    assert isinstance(implementation, harness.Harness)
    assert implementation.name and implementation.binary and implementation.default_model


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_command_can_name_its_working_directory(implementation: harness.Harness, tmp_path: Path) -> None:
    """``Command`` carries ``cwd``, and :func:`runner._capture_to_file` honours it.

    **Fails on a ``Command`` that has only argv/env/stdin.** Where a reviewer runs is not a
    detail the gate can decide for every CLI: some persist their sessions under the working
    directory, so a harness must be able to move that off the repository under review without
    further surgery in the runner. Asserted on the seam rather than on one harness --
    OpenCode names the repository with ``--dir`` and correctly leaves ``cwd`` unset.
    """
    built = implementation.review_command(spec_for(implementation, tmp_path))
    assert hasattr(built, "cwd")

    out = tmp_path / "cwd-out"
    status = runner._capture_to_file(["pwd"], {}, out, 60, None, str(tmp_path))
    assert status == 0
    assert out.read_text().strip() == str(Path(str(tmp_path)).resolve()), "cwd was not honoured"


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_review_command_starts_with_the_harness_binary(implementation: harness.Harness, tmp_path: Path) -> None:
    built = implementation.review_command(spec_for(implementation, tmp_path))
    assert built.argv[0] == implementation.binary


def test_an_unknown_harness_is_refused_not_defaulted() -> None:
    with pytest.raises(harness.UnknownHarness):
        harness.get("no-such-harness")


# --------------------------------------------------------------------------
# Session continuity: the strategy every harness has to bring
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# selection and model resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_the_harness_key_selects_the_implementation_of_that_name(implementation: harness.Harness) -> None:
    """``harness`` is a config key like any other, and its value is the harness's own ``name``.

    Asserted per implementation rather than for one hard-coded string, so a harness registered
    under a name its module does not answer to -- which would make ``config harness <name>``
    select something else -- cannot be added.
    """
    assert harness.selected(config_with(harness=implementation.name)) is implementation


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_an_unset_model_resolves_to_the_harnesss_own_default(implementation: harness.Harness) -> None:
    """``DEFAULTS["model"]`` is "" on purpose: a provider-qualified id meaningful to one CLI is
    meaningless to another, so the default belongs to the harness and is read through one helper."""
    assert DEFAULTS["model"] == ""
    assert harness.model(config_with(harness=implementation.name)) == implementation.default_model
    assert implementation.default_model, f"{implementation.name} offers no default model to fall back to"


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_configured_model_beats_the_harnesss_default(implementation: harness.Harness) -> None:
    assert harness.model(config_with(harness=implementation.name, model="vendor/chosen")) == "vendor/chosen"


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_harness_composing_its_own_command_uses_its_own_default(implementation: harness.Harness) -> None:
    """A harness passes itself to ``model``, so a directly-called command builder cannot pick up
    a *different* harness's default when configuration selects one and this module spells the
    other -- which is exactly what the dry run and these tests do."""
    other = next((candidate for candidate in every_harness() if candidate is not implementation), None)
    if other is None:  # pragma: no cover - only with a single registered harness
        pytest.skip("a second harness is needed to tell the two defaults apart")
    assert harness.model(config_with(harness=other.name), implementation) == implementation.default_model


def test_an_unimplemented_harness_is_refused_rather_than_replaced() -> None:
    """No silent fallback to the default harness: a typo that quietly selected a *different*
    reviewer would produce verdicts nobody asked for, from a CLI nobody chose."""
    with pytest.raises(harness.UnknownHarness) as excinfo:
        harness.selected(config_with(harness="not-a-harness"))
    assert "not-a-harness" in str(excinfo.value)
    for name in harness.names():
        assert name in str(excinfo.value), "the refusal must say what this build does implement"

    with pytest.raises(harness.UnknownHarness):
        harness.model(config_with(harness="not-a-harness"))


def test_display_model_renders_an_unimplemented_harness_instead_of_raising() -> None:
    """``status`` and `config` are how a user *finds* a bad ``harness`` value, so they must not
    be the thing that crashes on one -- and must not print a model name that nothing will run."""
    shown = harness.display_model(config_with(harness="not-a-harness"))

    assert shown == harness.UNIMPLEMENTED_MODEL
    assert shown not in {implementation.default_model for implementation in every_harness()}


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_the_review_path_binds_to_the_configured_harness(implementation: harness.Harness) -> None:
    """``runner._harness`` is the one reader on the review path, so the harness a command is
    built from, the harness a message names and the harness a lease is sized for agree."""
    config = config_with(harness=implementation.name)

    assert runner._harness(config) is implementation


def test_the_review_path_refuses_an_unimplemented_harness_rather_than_defaulting() -> None:
    """A configuration naming no implemented reviewer is refused, never served by the default."""
    config = config_with(harness="not-a-harness")

    with pytest.raises(harness.UnknownHarness):
        runner._harness(config)


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_every_harness_can_be_asked_what_a_run_cost(implementation: harness.Harness) -> None:
    """Part of the seam, so a second implementation cannot forget it. ``None`` is a valid
    answer -- a CLI with no accounting to report has none -- but the method has to exist, and
    it has to answer rather than raise on bytes it cannot read: this is display data, and a
    report that raises while describing a finished review destroys the record it exists to keep.
    """
    for raw in (b"", b"not json", b"[]", b"VERDICT APPROVED\n"):
        result = implementation.usage(raw)
        assert result is None or isinstance(result, harness.Usage)


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_every_harness_delivers_the_working_guidance_somewhere(implementation: harness.Harness, tmp_path: Path) -> None:
    """*Where* it goes is the harness's business -- Claude Code has `--append-system-prompt`,
    OpenCode has no system-prompt flag at all and appends it to the prompt. That it arrives is
    the seam's business, so it is asserted here rather than twice in the per-harness files."""
    marker = "batch every independent tool call"
    built = implementation.review_command(spec_for(implementation, tmp_path, system_prompt=marker))

    delivered = marker in " ".join(built.argv) or (built.stdin is not None and marker.encode() in built.stdin)
    assert delivered, f"{implementation.name} dropped the working guidance"


# --------------------------------------------------------------------------
# What the runner keeps from v3
# --------------------------------------------------------------------------


def test_a_huge_configured_timeout_is_clamped_to_the_ceiling() -> None:
    assert runner._timeout_sec(config_with(timeout_sec=100_000)) == runner.MAX_TIMEOUT_SEC
    assert runner._timeout_sec(config_with(timeout_sec=60)) == 60


def test_a_harness_that_needs_no_reduction_leaves_no_envelope(tmp_path: Path) -> None:
    """OpenCode writes the answer and nothing around it, so nothing is rewritten."""
    answer = b"Fine.\n\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n"
    out = tmp_path / "raw" / "001-phase.out"
    out.parent.mkdir()
    out.write_bytes(answer)

    runner._reduce_transcript(harness.get("opencode"), out)

    assert out.read_bytes() == answer
    assert not (tmp_path / "raw" / "001-phase.out.envelope").exists()


def test_the_permission_reads_the_bundle_and_every_root_and_nothing_else(tmp_path: Path) -> None:
    """A root outside the launch directory is still a root under review: a reviewer denied it
    would judge its diff blind. Nothing but the bundle and the roots is opened up."""
    bundle_dir = tmp_path / "reviews" / "007" / "bundle"
    document = json.loads(opencode.permission(bundle_dir, ("/launch/api", "/elsewhere/web/")))
    assert document["external_directory"] == {
        "*": "deny",
        "/launch/api/**": "allow",
        "/elsewhere/web/**": "allow",
        f"{bundle_dir}/**": "allow",
    }
    assert next(iter(document)) == "*", "the broad deny must come first: patterns are last-match-wins"
    assert {key for key, value in document.items() if value == "allow"} == {"read", "grep", "glob", "list"}


@pytest.mark.parametrize("which", ["review", "clarify"])
def test_both_opencode_commands_carry_the_roots_in_their_permission(which: str, tmp_path: Path) -> None:
    spec = spec_for(opencode.HARNESS, tmp_path)
    if which == "review":
        built = opencode.HARNESS.review_command(spec)
    else:
        built = opencode.HARNESS.clarify_command(
            harness.ClarifySpec(repo=spec.repo, prompt_text="why?", title="t", bundle_dir=spec.bundle_dir, roots=spec.roots, config=spec.config)
        )
    allowed = json.loads(built.env["OPENCODE_PERMISSION"])["external_directory"]
    assert "/elsewhere/web/**" in allowed
    assert "-s" not in built.argv, "every round is a fresh reviewer; nothing continues a session"
