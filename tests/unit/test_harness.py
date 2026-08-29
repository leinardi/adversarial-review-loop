"""The harness seam: the contract every reviewer CLI implementation must satisfy.

Separate from ``test_reviewer.py`` because these are assertions about the *seam*, not about
OpenCode: each one is parametrised over every registered harness, so a second implementation
cannot be added without meeting them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from pathlib import Path

import pytest

from ocrl import harness, reviewer
from ocrl.config import DEFAULTS, Config

#: Payloads the stdin tests round-trip. Named because ruff counts a bytes literal in a
#: comparison as a magic value, and because both sides of each assertion must be the one
#: value -- a test that echoes a different constant than it sent proves nothing.
_STDIN_MARKER = b"contract-marker\n"
_NO_STDIN_OUTPUT = b"ok\n"


def config_with(**overrides: object) -> Config:
    return Config({**DEFAULTS, **overrides})


def every_harness() -> list[harness.Harness]:
    return [harness.get(name) for name in harness.names()]


def written(path: Path, text: str = "evidence\n") -> harness.Attachment:
    """An attachment that exists on disk, with the digest the gate would have staged it under.

    A harness may *name* its attachments in the argv (OpenCode's ``-f``) or **inline** them
    into the payload it composes (Claude Code's stdin), and the second kind reads every one of
    them -- and re-hashes them -- while building the command. A fixture that only invented
    pathnames would therefore pass for one harness and fail for the other, which is precisely
    the drift these parametrised tests exist to catch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return harness.Attachment(path, hashlib.sha256(path.read_bytes()).hexdigest())


def spec_for(implementation: harness.Harness, tmp_path: Path, *, cold: bool = False) -> harness.ReviewSpec:
    del implementation
    return harness.ReviewSpec(
        repo="/repo",
        prompt_text="review this",
        title="review-loop phase 1",
        bundle_dir=tmp_path / "bundles" / "001",
        act_dir=tmp_path,
        config=config_with(),
        attachments=(written(tmp_path / "bundles" / "001" / "range.txt"),),
        cold=cold,
    )


# --------------------------------------------------------------------------
# run_bounded's deadline must cover writing stdin, not only waiting
# --------------------------------------------------------------------------


def test_stdin_write_is_inside_the_deadline(tmp_path: Path) -> None:
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
        status = reviewer.run_bounded(sleeper, stdout=sink, timeout_sec=1, stdin=payload)
    elapsed = time.monotonic() - started

    assert status == 124, "a child killed at the deadline reports the timeout status"
    # The deadline plus the SIGTERM->SIGKILL grace, with room to spare -- and nowhere near
    # the child's own 30s sleep, which is what the pre-fix version waited out in full.
    assert elapsed < 1 + reviewer.KILL_GRACE_SEC + 5, f"the deadline did not bind the stdin write: {elapsed:.1f}s"


def test_stdin_reaches_the_child(tmp_path: Path) -> None:
    """The bytes actually arrive -- the deadline fix must not have broken delivery."""
    out = tmp_path / "out"
    echo = ["python3", "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
    with out.open("wb") as sink:
        status = reviewer.run_bounded(echo, stdout=sink, timeout_sec=60, stdin=_STDIN_MARKER)
    assert status == 0
    assert out.read_bytes() == _STDIN_MARKER


def test_stdin_none_leaves_the_child_reading_nothing_from_us(tmp_path: Path) -> None:
    """``stdin=None`` keeps the pre-existing path: no pipe is created at all."""
    out = tmp_path / "out"
    with out.open("wb") as sink:
        status = reviewer.run_bounded(["python3", "-c", "print('ok')"], stdout=sink, timeout_sec=60)
    assert status == 0
    assert out.read_bytes() == _NO_STDIN_OUTPUT


def test_a_child_that_exits_early_is_not_an_error(tmp_path: Path) -> None:
    """EPIPE while writing is swallowed; the child's own status is what counts."""
    out = tmp_path / "out"
    with out.open("wb") as sink:
        status = reviewer.run_bounded(["python3", "-c", "raise SystemExit(3)"], stdout=sink, timeout_sec=60, stdin=b"y" * (1024 * 1024))
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
    """``Command`` carries ``cwd``, and :func:`reviewer._capture_to_file` honours it.

    **Fails on a ``Command`` that has only argv/env/stdin.** Where a reviewer runs is not a
    detail the gate can decide for every CLI: some persist their sessions under the working
    directory, so a harness must be able to move that off the repository under review without
    further surgery in ``reviewer.py``. Asserted on the seam rather than on one harness --
    OpenCode names the repository with ``--dir`` and correctly leaves ``cwd`` unset.
    """
    built = implementation.review_command(spec_for(implementation, tmp_path))
    assert hasattr(built, "cwd")

    out = tmp_path / "cwd-out"
    status = reviewer._capture_to_file(["pwd"], {}, out, 60, None, str(tmp_path))
    assert status == 0
    assert out.read_text().strip() == str(Path(str(tmp_path)).resolve()), "cwd was not honoured"


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_review_command_starts_with_the_harness_binary(implementation: harness.Harness, tmp_path: Path) -> None:
    built = implementation.review_command(spec_for(implementation, tmp_path))
    assert built.argv[0] == implementation.binary


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_cold_review_never_carries_a_session(implementation: harness.Harness, tmp_path: Path) -> None:
    """A cold confirmation must not continue a session under any harness.

    The cold-approval invariant is the gate's, but every harness has to be incapable of
    quietly reintroducing continuity into the one invocation that must not have it.

    Driven with the ``new_session_id`` a cold confirmation really carries, not with an empty
    spec: a cold confirmation is a *fresh* invocation, so ``reviewer._mint_session`` gives it
    an id of its own (`_confirm_cold`), and a harness that spelled that as a resume rather
    than as a new, empty session would hand the one call that must remember nothing the entire
    warm conversation. ``session_id`` stays "" because that is what the gate passes here --
    the seam's structural refusal of a *continuation* is asserted by ``ClarifySpec`` having no
    such field at all, in the test below.
    """
    spec = dataclasses.replace(
        spec_for(implementation, tmp_path, cold=True),
        new_session_id=implementation.sessions().mint() or "ses_mintedfresh01",
    )
    built = implementation.review_command(spec)
    assert "-s" not in built.argv
    assert "--resume" not in built.argv


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_clarify_command_never_carries_a_session(implementation: harness.Harness, tmp_path: Path) -> None:
    """``ClarifySpec`` has no ``session_id`` field, so no harness can be handed one."""
    assert not hasattr(harness.ClarifySpec, "session_id")
    built = implementation.clarify_command(
        harness.ClarifySpec(
            repo="/repo",
            prompt_text="answer this",
            title="clarify",
            bundle_dir=tmp_path / "bundles" / "001",
            act_dir=tmp_path,
            config=config_with(),
            question_file=written(tmp_path / "q.txt", "why?\n"),
            attachments=(written(tmp_path / "bundles" / "001" / "range.txt"),),
        )
    )
    assert "-s" not in built.argv
    assert "--resume" not in built.argv


def test_an_unknown_harness_is_refused_not_defaulted() -> None:
    with pytest.raises(harness.UnknownHarness):
        harness.get("no-such-harness")


# --------------------------------------------------------------------------
# Session continuity: the strategy every harness has to bring
# --------------------------------------------------------------------------


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_every_harness_brings_a_session_strategy(implementation: harness.Harness) -> None:
    strategy = implementation.sessions()
    assert isinstance(strategy, harness.SessionStrategy)
    assert strategy.capture_timeout_sec >= 0, "a negative window would shrink a lease below the work it covers"


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_a_minted_id_is_one_its_own_strategy_recognises(implementation: harness.Harness) -> None:
    """``mint`` and ``is_session_id`` are two halves of one shape and must agree.

    A strategy that minted an id its own validator rejects would pre-assign a session and
    then refuse to continue it -- continuity silently dead, and every round paying full token
    price with nothing to show for it. ``""`` is the other legal answer: the harness cannot
    pre-assign, so discovery is the only route.
    """
    strategy = implementation.sessions()
    minted = strategy.mint()
    assert minted == "" or strategy.is_session_id(minted)


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_is_session_id_refuses_a_trailing_newline_and_a_non_string(implementation: harness.Harness) -> None:
    """Both checks belong to the strategy, because both call sites read untrusted values.

    Every caller takes its value out of ``state.json`` or a CLI's own output, neither of which
    is a trust boundary. A ``$``-anchored pattern accepts a single trailing newline, and such
    an id travelled as a session id everywhere and rendered a line break into ``ocrl status``;
    a non-string reaches the same validators from the same document.
    """
    strategy = implementation.sessions()
    minted = strategy.mint() or "ses_abcdefgh"
    if not strategy.is_session_id(minted):  # pragma: no cover - a harness whose ids this stand-in does not fit
        pytest.skip(f"{implementation.name} mints nothing and does not recognise the stand-in id")

    assert not strategy.is_session_id(minted + "\n")
    assert not strategy.is_session_id(None)
    assert not strategy.is_session_id(12345)
    assert not strategy.is_session_id("")


@pytest.mark.parametrize("implementation", every_harness(), ids=lambda h: h.name)
def test_no_harness_can_compute_a_lease_above_the_ceiling(implementation: harness.Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_MAX_LEASE_SEC`` is what a stored ``lease_sec`` is validated against, so it has to
    bound the largest lease **every** registered harness can legitimately compute.

    Asserted by running the real budget function under each harness at the largest
    ``timeout_sec`` the clamp allows, rather than by restating the ceiling's own formula --
    a restatement passes however wrong that formula is. A ceiling derived from the
    *configured* harness instead would read a slower harness's perfectly legitimate lease as
    tampered, fall back to the observer's own window, and make the claim observer-relative
    again, which is the whole thing recording it was meant to stop.
    """
    monkeypatch.setattr(reviewer, "_sessions", lambda _config: implementation.sessions())
    largest = reviewer._active_review_reclaim_after(config_with(timeout_sec=reviewer.MAX_TIMEOUT_SEC))

    assert largest <= reviewer._MAX_LEASE_SEC, f"{implementation.name} computes a lease its own ceiling rejects"
