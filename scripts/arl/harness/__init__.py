"""The reviewer-harness seam: which CLI the gate actually asks for a review.

The gate is not tied to one reviewer CLI. Everything that decides an *outcome* --
bundle building, staging and manifest verification, the ``FINDING``/``VERDICT``
contract, the cold-approval invariant, ``round_history`` bookkeeping, the retry
classes -- lives in :mod:`arl.reviewer` and is harness-agnostic. What varies per
harness is narrow and mechanical: how one invocation is spelled as a command, how a
session is named and continued, and whether the reviewer's model list can be probed
at all.

**A harness composes a command; it never decides anything.** Nothing here reads a
verdict, touches ``state.json``, or may turn a failure into an approval (Rule 1) --
it answers with a :class:`Command` and :mod:`arl.reviewer` runs it. That is what
keeps "add a third harness" a new module rather than another pass over the gate.

**The test seam sits above this layer, deliberately.** ``ARL_REVIEWER_CMD`` (and
``ARL_SESSION_LIST_CMD``) short-circuit in :mod:`arl.reviewer` *before* a harness is
consulted, so ``tests/selftest.sh`` exercises the loop without any harness being
involved and a new harness cannot quietly change what the selftest measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from arl.errors import OcrlError

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from collections.abc import Mapping

    from arl.config import Config

__all__ = [
    "UNIMPLEMENTED_MODEL",
    "Attachment",
    "CaptureSpec",
    "Captured",
    "ClarifySpec",
    "Command",
    "Harness",
    "PayloadError",
    "ReviewSpec",
    "SessionStrategy",
    "TranscriptError",
    "UnknownHarness",
    "Usage",
    "display_model",
    "get",
    "model",
    "names",
    "selected",
    "strategies",
]


class PayloadError(OcrlError):
    """A command could not be composed, so **nothing ran**.

    Raised while building a :class:`Command` -- an attachment that cannot be read back for
    inlining, a working directory that cannot be created. :mod:`arl.reviewer` turns it into
    the same blocking bundle failure as a staged attachment that changed underneath: no
    reviewer was launched, so there is no transcript to parse and no session to release.
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
    """One fully-composed reviewer invocation, ready for :func:`arl.reviewer.run_bounded`.

    ``env`` is *overrides*, not a whole environment: the caller layers it onto the
    environment it already decided on, so a harness cannot drop a variable it does not
    know about. ``stdin`` is the bytes to feed the child, or ``None`` for a child that
    reads nothing -- OpenCode takes its prompt as an argument, so it is ``None`` there;
    a harness whose prompt does not fit an argv uses this instead.

    ``cwd`` is the directory the child runs in, or ``None`` to inherit the gate's own.
    A harness that names the repository with a flag (OpenCode's ``--dir``) leaves it
    ``None``; one that has no such flag sets it. It is part of *composing the command*
    rather than something :mod:`arl.reviewer` decides, because where a reviewer runs is
    also where some CLIs persist their sessions -- a harness must be able to keep that
    out of the user's own working directory without the gate knowing why.
    """

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class Attachment:
    """One thing the reviewer is given, and the bytes it is required to be.

    **The digest travels with the path because the two delivery styles differ in who opens
    the file.** A harness that names the attachment in an argv hands the pathname to another
    process, which opens it later -- the gate's own check can only be moved close to that
    open, never made to cover it (see ``arl.reviewer.stage_attachments``). A harness that
    *inlines* the attachment reads it in this process, which means the gap between the check
    and the read is one this code can close outright -- but only if it knows what the bytes
    were supposed to be. Carrying the path alone would silently leave the second kind as
    exposed as the first, while looking safer.

    ``digest`` is the sha256 hex of the bytes the gate staged and verified. It is required
    rather than defaulted: an attachment nobody vouched for is exactly the case that must be
    impossible to construct by accident.
    """

    path: Path
    digest: str


@dataclass(frozen=True, kw_only=True)
class ReviewSpec:
    """Everything one review invocation needs, in harness-neutral terms.

    Deliberately says *what the invocation is*, never how to spell it: the prompt is
    already-decoded text, ``attachments`` is the exact ordered list
    :func:`arl.reviewer.stage_invocation` staged (never a directory to glob), and
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
    #: Plugin-shipped guidance on *how* to work, as opposed to what to review
    #: (``prompts/reviewer-efficiency.md``). Carried separately from ``prompt_text`` because
    #: the two CLIs deliver it differently and only one of them has somewhere better than the
    #: prompt to put it: Claude Code takes it as ``--append-system-prompt``, where an
    #: instruction about tool use is followed far more reliably than the same words buried in
    #: a 100 KB user message, while OpenCode has no system-prompt flag and appends it to the
    #: prompt instead. **Fixed plugin text, never repo- or model-derived**, so it carries no
    #: evidence-boundary weight -- it has exactly the standing of the prompt file itself.
    #:
    #: Defaults to "" and every harness skips it when empty. A review that runs without the
    #: guidance is an ordinary review -- it costs more turns, not correctness -- so this is one
    #: of the few fields whose absence is allowed to degrade quietly.
    system_prompt: str = ""
    title: str
    bundle_dir: Path
    #: The activation directory this review belongs to -- the root of everything the gate
    #: persists for it. Offered so a harness can put its own scratch or session state
    #: somewhere outside the repository under review (Rule 3) without deriving paths itself.
    act_dir: Path
    config: Config
    attachments: tuple[Attachment, ...] = ()
    session_id: str = ""
    new_session_id: str = ""
    cold: bool = False


@dataclass(frozen=True, kw_only=True)
class ClarifySpec:
    """One clarify invocation: a question about a review already given.

    Narrower than :class:`ReviewSpec` by construction -- there is no ``session_id``
    field at all, because a clarify never continues a session and a harness must not be
    able to be handed one. It is always cold. See ``arl.commands.clarify``.
    """

    repo: str
    prompt_text: str
    #: See :attr:`ReviewSpec.system_prompt`. A clarify is an agentic run over the same
    #: repository, so the same guidance applies to it.
    system_prompt: str = ""
    title: str
    bundle_dir: Path
    #: See :attr:`ReviewSpec.act_dir`.
    act_dir: Path
    config: Config
    question_file: Attachment
    attachments: tuple[Attachment, ...] = ()


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


@dataclass(frozen=True)
class Usage:
    """What one reviewer invocation cost, as its CLI reported it.

    **Observability only. Nothing in the gate reads this to decide anything** -- not a verdict,
    not a budget, not a retry. It exists because the cost of a round was previously invisible:
    the figures were sitting in the CLI's own output, which
    :func:`arl.reviewer._reduce_transcript` moved aside into an ``.envelope`` file nobody
    reads. A round that costs several dollars should say so in its report rather than only in
    the provider's billing page.

    Every field is ``None`` when the CLI did not report it or reported it as something other
    than a number, and the whole object is ``None`` when there is nothing to read at all
    (:meth:`Harness.usage`). A missing figure is displayed as missing; it is never defaulted to
    ``0``, which would read as "this round was free".

    ``cache_read_tokens`` is the one worth understanding: an agentic review re-reads its whole
    context on every turn, so this is roughly *context size x turns* and is normally the
    largest number here by an order of magnitude. It is what makes turn count, not payload
    size alone, the thing that drives the bill.
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
    """What :mod:`arl.reviewer` requires of a reviewer CLI.

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

    def transcript(self, raw: bytes) -> bytes:
        """The reviewer's answer, extracted from whatever its CLI actually wrote.

        The gate parses **one** thing -- the ``FINDING``/``VERDICT`` contract -- and it parses
        it out of prose. A CLI that answers in prose returns ``raw`` unchanged; one that wraps
        the answer in a report of its own unwraps it here, so
        :func:`arl.reviewer.parse` never learns that more than one shape exists.

        **This is also where a run that "succeeded" is refused.** Some CLIs report a failed
        turn, or a tool call they denied, in that wrapper while still exiting ``0`` -- measured
        on Claude Code. Anything of that kind raises :class:`TranscriptError` rather than
        returning text, because a review of less evidence than the gate believes it sent must
        never reach the parser as a verdict (Rule 1).
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
    from arl.harness import claudecode, opencode  # noqa: PLC0415 - see comment above

    return {implementation.name: implementation for implementation in (opencode.HARNESS, claudecode.HARNESS)}


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


def display_model(config: Config) -> str:
    """:func:`model`, for the two callers that must render rather than raise.

    ``status`` and ``config`` describe a configuration; they are how a user *finds* a bad
    ``harness`` value, so crashing on one would take away the tool that diagnoses it. Every
    caller that decides something -- arming, resuming, composing a command -- uses
    :func:`model` and takes the refusal.
    """
    try:
        return model(config)
    except UnknownHarness:
        return UNIMPLEMENTED_MODEL
