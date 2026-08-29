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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from collections.abc import Mapping

    from ocrl.config import Config

__all__ = [
    "CaptureSpec",
    "Captured",
    "ClarifySpec",
    "Command",
    "Harness",
    "ReviewSpec",
    "SessionStrategy",
    "UnknownHarness",
    "get",
    "names",
    "strategies",
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

    **The two session fields are never both set, and they mean different things.**
    ``session_id`` is a session that already exists and this run continues; it is only ever
    non-empty when the gate decided continuity holds. ``new_session_id`` is an id
    :meth:`SessionStrategy.mint` produced for a *fresh* run, so a CLI that pre-assigns
    sessions can name the one it is about to create -- empty for a harness that cannot
    pre-assign, which is what leaves post-hoc discovery the only way to learn it.
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
    new_session_id: str = ""
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


@dataclass(frozen=True, kw_only=True)
class CaptureSpec:
    """What a strategy needs to answer "which session did this fresh run use?".

    ``started_ms`` is the wall clock immediately before the reviewer was launched, in
    milliseconds: it bounds a discovery search to sessions this run could actually have
    created. ``seq`` names any scratch file the call writes, so two concurrent reviews cannot
    collide over one. ``new_session_id`` is what :meth:`SessionStrategy.mint` pre-assigned,
    which a pre-assigning strategy simply hands back.
    """

    repo: str
    title: str
    act_dir: Path
    seq: str
    started_ms: int
    config: Config
    new_session_id: str = ""


@dataclass(frozen=True)
class Captured:
    """The session one fresh run turned out to have used, or nothing.

    Falsy when the session could not be established, which every caller must treat as
    "this round has no continuity to offer the next one" -- never as an error. Capturing a
    session is an optimisation; failing to capture one costs tokens, not correctness.
    """

    session_id: str = ""
    #: The CLI's own creation timestamp, in milliseconds. Stored beside the id and re-checked
    #: on every later use, so an id that is reused for a *different* session does not read as
    #: the same one. ``0`` for a harness that has no such timestamp to offer.
    created: int = 0

    def __bool__(self) -> bool:
        return bool(self.session_id)


@runtime_checkable
class SessionStrategy(Protocol):
    """How one harness's sessions come into existence, and how one is recognised.

    **The two harness families differ in kind here, not in detail.** OpenCode *discovers* a
    session after the fact -- it is created by the run itself, and the only way to learn its
    id is to list sessions and match the unique ``--title`` the run was given. Claude Code
    *assigns* one up front: the gate mints a uuid, hands it over, and there is nothing to
    look up afterwards. Everything else about continuity -- the claim, the round cap, the
    structural pointer checks, the cold-approval invariant -- is shared, so only this seam
    varies.

    Everything a strategy produces is a continuity **hint**. Nothing here can authorise an
    approval: the cold-approval invariant in ``reviewer.execute`` is what makes a tampered or
    wrong pointer unable to turn a review into a pass, and it does not consult this at all.
    """

    @property
    def capture_timeout_sec(self) -> int:
        """The longest one :meth:`verify` or :meth:`capture` call can take, in seconds.

        The gate's claim leases are sized from this rather than from a constant, because a
        strategy that runs no subprocess at all genuinely needs none of that window -- and a
        lease padded for a listing that never happens is a lease an abandoned claim is
        honoured far past anything real. ``0`` for a strategy that makes no call.
        """

    def is_session_id(self, value: object) -> bool:
        """Is ``value`` a well-formed session id for this harness?

        Given ``object``, not ``str``, deliberately: every caller reads its value out of
        ``state.json`` or a CLI's own output, neither of which is a trust boundary, so the
        type check belongs here with the shape check rather than being repeated -- and
        forgotten -- at each call site.
        """

    def mint(self) -> str:
        """A fresh session id for a run about to start, or ``""`` for a harness that cannot
        pre-assign one. See :attr:`ReviewSpec.new_session_id`."""

    def verify(self, pointer: Mapping[str, Any], *, repo: str, config: Config, act_dir: Path, seq: str) -> bool:
        """Does the remembered ``pointer`` still name a session this harness can continue?

        ``False`` drops continuity for this round -- never an error, and never anything the
        caller has to distinguish: a fresh review is always a correct review. A strategy that
        has nothing to check answers ``True`` and lets its CLI refuse the id itself, which is
        a non-zero exit and therefore an ``OP_FAILURE`` that blocks (Rule 1).

        Any *reason* worth an operator's attention is logged here, by the strategy that knows
        what it looked at; the caller logs only the consequence.
        """

    def capture(self, spec: CaptureSpec) -> Captured:
        """The session this fresh run used, for the next round to continue.

        Must never raise: every failure is a log line and a falsy :class:`Captured`.
        """


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

    def sessions(self) -> SessionStrategy:
        """How this harness's sessions are named, minted and found again."""

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


def strategies() -> list[SessionStrategy]:
    """Every implemented harness's session strategy.

    For the one thing that has to hold across *all* of them at once rather than for the
    configured one: ``reviewer._MAX_LEASE_SEC``, the ceiling a stored ``lease_sec`` is
    validated against. That ceiling has to be the largest lease **any** harness can
    legitimately produce, or a real lease from a slower harness would read as tampered.
    """
    return [implementation.sessions() for implementation in _registry().values()]


def get(name: str) -> Harness:
    """The harness ``name`` selects. Raises :class:`UnknownHarness` for anything else."""
    try:
        return _registry()[name]
    except KeyError:
        raise UnknownHarness(f"unknown harness {name!r}; this build implements: {', '.join(names())}") from None
