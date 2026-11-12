"""The reviewer-harness seam: the external reviewer CLIs the engine itself runs.

The default reviewer, ``subagent``, is not here: the mod spawns it, and the engine only
prepares the bundle and records the answer. An external harness is a CLI the engine runs
(``review run-external``), and what varies between such CLIs is narrow and mechanical: how one
invocation is spelled, how its transcript is reduced to the answer, and whether its model list
can be probed. Every round starts a fresh reviewer, so there is no session to name or continue.

**A harness composes a command; it never decides anything.** Nothing here reads a verdict or
touches ``state.json``, and nothing may turn a failure into an approval (AGENTS.md rule 1).

**The test seam sits above this layer, deliberately.** ``ARL_REVIEWER_CMD`` short-circuits in
:mod:`arl.runner` before a harness is consulted.
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

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from arl.errors import OcrlError

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from arl.config import Config

__all__ = [
    "SUBAGENT",
    "UNIMPLEMENTED_MODEL",
    "ClarifySpec",
    "Command",
    "Harness",
    "PayloadError",
    "ReviewSpec",
    "TranscriptError",
    "UnknownHarness",
    "Usage",
    "display_model",
    "get",
    "model",
    "names",
    "selected",
]


class PayloadError(OcrlError):
    """A command could not be composed, so **nothing ran**.

    Raised while building a :class:`Command` -- a payload that cannot be written, a working
    directory that cannot be created. :func:`arl.runner.invoke` turns it into a
    :class:`arl.runner.LaunchError`: no reviewer was launched, so there is no transcript to
    parse, and the review is an operational failure.
    """


class TranscriptError(OcrlError):
    """The reviewer ran, but its output could not be reduced to an answer (Rule 1).

    Raised by :meth:`Harness.transcript` for anything the exit status does not already
    report -- a CLI that framed a failed turn as a successful process, a tool call that was
    denied, output that is not the shape the flags promised. Every one of them reaches the
    gate as an ``OP_FAILURE`` and blocks; none of them may become "no findings".
    """


class UnknownHarness(Exception):
    """The configured ``harness`` names something this build does not implement.

    Always a hard refusal at the point of use, never a silent fallback to the default:
    a typo that quietly selected a *different* reviewer than the one configured would
    produce verdicts nobody asked for, from a CLI nobody chose.
    """


@dataclass(frozen=True)
class Command:
    """One fully-composed reviewer invocation, ready for :func:`arl.runner.run_bounded`.

    ``env`` is *overrides*, not a whole environment, so a harness cannot drop a variable it does
    not know about. ``stdin`` is the bytes to feed the child, or ``None`` for one that reads
    nothing.

    ``cwd`` is the directory the child runs in, or ``None`` to inherit. It is part of *composing
    the command* rather than something :mod:`arl.runner` decides, because where a reviewer runs
    is also where some CLIs persist their sessions -- a harness must be able to keep that out of
    the user's own working directory without the engine knowing why.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    cwd: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReviewSpec:
    """Everything a harness needs to spell one review (or one contract repair).

    ``prompt_text`` is the composed prompt, already naming every file of the bundle; the
    reviewer reads them itself. Outside ``repo`` it may read ``bundle_dir`` and every root in
    ``roots``: a plan's roots need not sit under the launch directory.
    """

    #: Where the reviewer runs: the run's launch directory.
    repo: str
    prompt_text: str
    #: The plugin's working guidance (``prompts/reviewer-efficiency.md``), for a harness that can
    #: put it somewhere other than the prompt.
    system_prompt: str = ""
    title: str
    bundle_dir: Path
    roots: tuple[str, ...] = ()
    config: Config


@dataclass(frozen=True, kw_only=True)
class ClarifySpec:
    """One clarify question about a review already given; the same shape as a review."""

    repo: str
    prompt_text: str
    system_prompt: str = ""
    title: str
    bundle_dir: Path
    roots: tuple[str, ...] = ()
    config: Config


@dataclass(frozen=True)
class Usage:
    """What one reviewer invocation cost, as its CLI reported it.

    **Observability only. Nothing in the engine reads this to decide anything** -- not a verdict, not
    a budget, not a retry. It exists because the cost of a round was otherwise invisible.

    Every field is ``None`` when the CLI did not report it or reported a non-number, and the whole
    object is ``None`` when there is nothing to read. A missing figure is displayed as missing,
    never defaulted to ``0``, which would read as "this round was free".

    ``cache_read_tokens`` is roughly *context size x turns*, normally the largest number here by an
    order of magnitude -- which is what makes turn count, not payload size, the thing that drives
    the bill.
    """

    #: Agentic turns the run took -- each one a full re-read of the context.
    turns: int | None = None
    input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    output_tokens: int | None = None
    #: What the CLI itself says the run cost, in US dollars.
    cost_usd: float | None = None
    duration_ms: int | None = None


@runtime_checkable
class Harness(Protocol):
    """What :mod:`arl.runner` requires of an external reviewer CLI.

    ``binary`` is the executable to look for on ``PATH``. ``default_model`` is what
    ``model`` resolves to when configuration leaves it unset -- per harness, because a
    provider-qualified id that is meaningful to one CLI is meaningless to another.
    """

    @property
    def name(self) -> str:
        """The value the ``harness`` config key takes for this implementation."""

    @property
    def binary(self) -> str:
        """The executable this harness runs."""

    @property
    def default_model(self) -> str:
        """``model``'s default when configuration does not set one."""

    def review_command(self, spec: ReviewSpec) -> Command:
        """The command that runs one review."""

    def clarify_command(self, spec: ClarifySpec) -> Command:
        """The command that answers one clarify question."""

    def transcript(self, raw: bytes) -> bytes:
        """The reviewer's answer, extracted from whatever its CLI actually wrote.

        The engine parses **one** thing, the ``FINDING``/``VERDICT`` contract, out of prose. A CLI that
        answers in prose returns ``raw`` unchanged; one that wraps the answer unwraps it here, so
        :func:`arl.contract.parse` never learns more than one shape exists.

        **This is also where a run that "succeeded" is refused.** Some CLIs report a failed turn, or a
        tool call they denied, in that wrapper while still exiting ``0`` -- measured on Claude Code.
        Anything of that kind raises :class:`TranscriptError`, because a review of less evidence than
        the engine believes it sent must never reach the parser as a verdict (Rule 1).
        """

    def usage(self, raw: bytes) -> Usage | None:
        """What this run cost, read out of the same bytes :meth:`transcript` reduces.

        ``None`` when the CLI reports nothing to read -- which is the correct answer for a
        plain-text transcript, not a failure.

        **Never raises, and never gates anything.** This is the one method on the protocol
        whose result is displayed and nothing more (:class:`Usage`), so unlike
        :meth:`transcript` it has no fail-closed direction to honour: malformed output means
        an unknown cost, and an unknown cost is reported as unknown. Raising here would let a
        display concern fail a review that had already succeeded.
        """

    def probe_models(self, timeout: float) -> list[str] | None:
        """The models this reviewer reports, or ``None`` when it cannot enumerate them.

        ``None`` is not a failure -- it means "this CLI has no model-list command", and a
        caller must then check binary presence only. A harness that *can* enumerate raises
        ``arl.reviewer_probe.ProbeFailed`` when the probe itself does not complete, which
        is a different thing and stays distinguishable.
        """


#: Every harness this build implements, keyed by its ``harness`` config value. New
#: implementations are registered here and nowhere else.
def _registry() -> dict[str, Harness]:
    # Imported inside the function, not at module scope: an implementation module imports
    # this one for `Command`/`ReviewSpec`, so a module-scope import here would be a cycle.
    from arl.harness import opencode  # noqa: PLC0415 - see comment above

    return {implementation.name: implementation for implementation in (opencode.HARNESS,)}


def names() -> list[str]:
    """Every implemented harness name, sorted -- for error messages and `config` output."""
    return sorted(_registry())


def get(name: str) -> Harness:
    """The harness ``name`` selects. Raises :class:`UnknownHarness` for anything else."""
    try:
        return _registry()[name]
    except KeyError:
        raise UnknownHarness(f"unknown harness {name!r}; this build implements: {', '.join(names())}") from None


def selected(config: Config) -> Harness:
    """The harness this configuration's ``harness`` key selects.

    The one place that key is turned into an implementation, so the harness a command is
    built from, the harness a status line names and the harness a lease is sized for cannot
    disagree. Raises :class:`UnknownHarness` rather than falling back to the default: see
    that class for why a typo must never quietly select a different reviewer.
    """
    return get(config.as_str("harness"))


def model(config: Config, implementation: Harness | None = None) -> str:
    """The model id one invocation actually runs with.

    ``model`` is per-harness by default (:data:`arl.config.DEFAULTS`), so "the configured
    model" is not something ``Config`` can answer on its own -- and four commands print it
    while a fifth passes it to the CLI. This is the single reader they all go through.

    ``implementation`` is passed by a harness composing its *own* command, where the answer
    must come from that module rather than from whatever the configuration happens to select
    -- a directly-called ``review_argv`` must not pick up another harness's default. Everyone
    else omits it and gets :func:`selected`'s answer, raising :class:`UnknownHarness` with it.
    """
    chosen = selected(config) if implementation is None else implementation
    return config.as_str("model") or chosen.default_model


#: What :func:`display_model` shows when the configured harness is not one this build has.
#: A placeholder, never a model name: nothing can run under an unknown harness, so there is
#: no id to report and inventing the default harness's would be a lie about what would run.
UNIMPLEMENTED_MODEL: Final = "<no harness>"


#: The ``harness`` value for the default reviewer, a subagent the mod spawns. Not in the registry:
#: the engine never runs it.
SUBAGENT: Final = "subagent"


def display_model(config: Config) -> str:
    """:func:`model`, for the two callers that must render rather than raise.

    ``status`` and ``config`` describe a configuration; they are how a user *finds* a bad
    ``harness`` value, so crashing on one would take away the tool that diagnoses it. Every
    caller that decides something -- arming, resuming, composing a command -- uses
    :func:`model` and takes the refusal.
    """
    if config.as_str("harness") == SUBAGENT:
        return config.as_str("model") or "the session's default"
    try:
        return model(config)
    except UnknownHarness:
        return UNIMPLEMENTED_MODEL
