"""The reviewer-harness seam: which CLI the gate actually asks for a review.

The gate is not tied to one reviewer CLI. Everything that decides an *outcome* --
bundle building, staging and manifest verification, the ``FINDING``/``VERDICT``
contract, the cold-approval invariant, ``round_history`` bookkeeping, the retry
classes -- lives in :mod:`ocrl.reviewer` and is harness-agnostic. What varies per
harness is narrow and mechanical: how one invocation is spelled as a command, how a
session is named and continued, and whether the reviewer's model list can be probed
at all.

**A harness composes a command; it never decides anything.** Nothing here reads a
verdict, touches ``state.json``, or may turn a failure into an approval (Rule 1) --
it answers with a :class:`Command` and :mod:`ocrl.reviewer` runs it. That is what
keeps "add a third harness" a new module rather than another pass over the gate.

**The test seam sits above this layer, deliberately.** ``OCRL_REVIEWER_CMD`` (and
``OCRL_SESSION_LIST_CMD``) short-circuit in :mod:`ocrl.reviewer` *before* a harness is
consulted, so ``tests/selftest.sh`` exercises the loop without any harness being
involved and a new harness cannot quietly change what the selftest measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from ocrl.config import Config

__all__ = [
    "ClarifySpec",
    "Command",
    "Harness",
    "ReviewSpec",
    "UnknownHarness",
    "get",
    "names",
]


class UnknownHarness(Exception):
    """The configured ``harness`` names something this build does not implement.

    Always a hard refusal at the point of use, never a silent fallback to the default:
    a typo that quietly selected a *different* reviewer than the one configured would
    produce verdicts nobody asked for, from a CLI nobody chose.
    """


@dataclass(frozen=True)
class Command:
    """One fully-composed reviewer invocation, ready for :func:`ocrl.reviewer.run_bounded`.

    ``env`` is *overrides*, not a whole environment: the caller layers it onto the
    environment it already decided on, so a harness cannot drop a variable it does not
    know about. ``stdin`` is the bytes to feed the child, or ``None`` for a child that
    reads nothing -- OpenCode takes its prompt as an argument, so it is ``None`` there;
    a harness whose prompt does not fit an argv uses this instead.

    ``cwd`` is the directory the child runs in, or ``None`` to inherit the gate's own.
    A harness that names the repository with a flag (OpenCode's ``--dir``) leaves it
    ``None``; one that has no such flag sets it. It is part of *composing the command*
    rather than something :mod:`ocrl.reviewer` decides, because where a reviewer runs is
    also where some CLIs persist their sessions -- a harness must be able to keep that
    out of the user's own working directory without the gate knowing why.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    cwd: str | None = None


@dataclass(frozen=True, kw_only=True)
class ReviewSpec:
    """Everything one review invocation needs, in harness-neutral terms.

    Deliberately says *what the invocation is*, never how to spell it: the prompt is
    already-decoded text, ``attachments`` is the exact ordered list
    :func:`ocrl.reviewer.stage_invocation` staged (never a directory to glob), and
    ``cold`` states the intent -- "this run must see no model-influenced context" --
    which each harness honours in whatever way its own CLI provides.
    """

    repo: str
    prompt_text: str
    title: str
    bundle_dir: Path
    #: The activation directory this review belongs to -- the root of everything the gate
    #: persists for it. Offered so a harness can put its own scratch or session state
    #: somewhere outside the repository under review (Rule 3) without deriving paths itself.
    act_dir: Path
    config: Config
    attachments: tuple[Path, ...] = ()
    session_id: str = ""
    cold: bool = False


@dataclass(frozen=True, kw_only=True)
class ClarifySpec:
    """One clarify invocation: a question about a review already given.

    Narrower than :class:`ReviewSpec` by construction -- there is no ``session_id``
    field at all, because a clarify never continues a session and a harness must not be
    able to be handed one. It is always cold. See ``ocrl.commands.clarify``.
    """

    repo: str
    prompt_text: str
    title: str
    bundle_dir: Path
    #: See :attr:`ReviewSpec.act_dir`.
    act_dir: Path
    config: Config
    question_file: Path
    attachments: tuple[Path, ...] = ()


@runtime_checkable
class Harness(Protocol):
    """What :mod:`ocrl.reviewer` requires of a reviewer CLI.

    ``binary`` is the executable to look for on ``PATH``; ``arm``, ``resume`` and
    ``config`` all report it by name when it is missing, so it is a property of the
    harness rather than a string repeated at three call sites. ``default_model`` is what
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

    def probe_models(self, timeout: float) -> list[str] | None:
        """The models this reviewer reports, or ``None`` when it cannot enumerate them.

        ``None`` is not a failure -- it means "this CLI has no model-list command", and a
        caller must then check binary presence only. A harness that *can* enumerate raises
        ``ocrl.reviewer_probe.ProbeFailed`` when the probe itself does not complete, which
        is a different thing and stays distinguishable.
        """


#: Every harness this build implements, keyed by its ``harness`` config value. New
#: implementations are registered here and nowhere else.
def _registry() -> dict[str, Harness]:
    # Imported inside the function, not at module scope: an implementation module imports
    # this one for `Command`/`ReviewSpec`, so a module-scope import here would be a cycle.
    from ocrl.harness import opencode  # noqa: PLC0415 - see comment above

    return {opencode.HARNESS.name: opencode.HARNESS}


def names() -> list[str]:
    """Every implemented harness name, sorted -- for error messages and `config` output."""
    return sorted(_registry())


def get(name: str) -> Harness:
    """The harness ``name`` selects. Raises :class:`UnknownHarness` for anything else."""
    try:
        return _registry()[name]
    except KeyError:
        raise UnknownHarness(f"unknown harness {name!r}; this build implements: {', '.join(names())}") from None
