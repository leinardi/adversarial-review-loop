"""Hook input parsing and fail-closed decision emitters.

**Rule 2 lives here: hook stdout is protocol.** A hook entrypoint emits valid Claude hook
JSON or nothing at all. Diagnostics go to stderr. A stray write from a library running
under a hook corrupts the response, and a corrupt ``PreToolUse`` response is not a denial
-- the tool proceeds.

**Rule 1 lives here too: nothing converts a failure into an approval.** Every path that
cannot reach a decision emits the fallback its event calls for, and that fallback is never
``allow``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, Final, Literal, NamedTuple, NoReturn, TextIO

from ocrl.util import log, log_exception

__all__ = [
    "DENY_REASON",
    "EXIT_OUTPUT_ERROR",
    "STOP_BLOCK_REASON",
    "Decided",
    "FailClosed",
    "Hook",
    "HookInput",
    "OutputFailure",
    "parse_hook_input",
    "read_hook_input",
]

#: Which fallback a crashing entrypoint owes its event. Mirrors ``OCRL_FAILCLOSED``.
FailClosed = Literal["none", "pretool", "stop", "posttool"]

DENY_REASON: Final = (
    "opencode-review-loop: internal gate error ({detail}). Denying to fail closed. "
    "Run /opencode-review-loop:status, then /opencode-review-loop:stop if the mode is wedged."
)

STOP_BLOCK_REASON: Final = (
    "opencode-review-loop: internal error in the Stop gate ({detail}). The final review did not run. Run /opencode-review-loop:status."
)

POSTTOOL_CONTEXT_REASON: Final = (
    "opencode-review-loop: internal error in the post-commit check ({detail}). "
    "The commit was NOT confirmed against the approved tree, so this is not a verification. "
    "Run /opencode-review-loop:status."
)


#: Exit status for "nothing reliable reached stdout". Any non-zero status makes the shim
#: discard what it captured and emit its own fallback; this one is merely distinguishable
#: from 1 (generic), 2 (usage) and 124 (timeout) in a log.
EXIT_OUTPUT_ERROR: Final = 3


class _HookControl(BaseException):
    """Base for the emitters' control-flow signals.

    Deliberately **not** an ``Exception`` subclass. Entrypoint bodies legitimately wrap work
    in ``except Exception``; if that swallowed the signal raised by an emitter, execution
    would carry on past a decision that had already been written, and a later crash would
    then find ``decided`` set and leave, say, an ``allow`` standing as the final answer.
    Deriving from ``BaseException`` makes an emitter call genuinely non-resumable.
    """


class Decided(_HookControl):
    """Raised by an emitter once the response has been written in full.

    The shell emitters end in ``exit 0``; unwinding by exception is the Python equivalent,
    and unlike ``SystemExit`` it cannot be confused with an interpreter failure by the
    top-level guard.
    """


class OutputFailure(_HookControl):
    """Raised when the response could not be written, or could not be written whole.

    This is the one case that must **not** exit 0. Nothing reliable reached stdout, so the
    shim has to discard whatever bytes escaped and emit the fallback itself -- writing a
    second response here would append to a partial one, and two concatenated JSON objects
    do not parse. An unparseable ``PreToolUse`` response is not a denial.
    """


class HookInput(NamedTuple):
    """The fields every hook entrypoint needs, pulled out of the payload in one pass."""

    session_id: str = ""
    cwd: str = ""
    tool_name: str = ""
    command: str = ""
    #: The file an editing tool is about to write. ``Write`` and ``Edit`` call it
    #: ``file_path``, ``NotebookEdit`` calls it ``notebook_path``; both land here.
    path: str = ""
    #: ``UserPromptSubmit`` only: the prompt as the user typed it.
    prompt: str = ""
    raw: str = "{}"


def _as_str(value: Any) -> str:
    """Coerce a JSON value the way the shell's jq filter did.

    ``null`` becomes empty, a string passes through, anything else is stringified --
    ``true``, ``5``, ``{"a":1}``. Verified against ``jq`` for each of those shapes.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def parse_hook_input(raw: str) -> HookInput:
    """Parse a hook payload, yielding empty fields for anything unreadable.

    A payload that will not parse yields no fields, and every caller then treats the event
    as "not ours" or denies -- never as a silent allow.

    The partial-failure shape is faithful to the jq original, which streamed its output:
    when ``.tool_input`` is present but not an object, jq had already emitted
    ``session_id``, ``cwd`` and ``tool_name`` before erroring, so only ``command`` is lost.
    A payload whose top level is not an object loses everything.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return HookInput(raw=raw)

    if payload is None:
        # jq indexes null happily and yields null for every field.
        return HookInput(raw=raw)
    if not isinstance(payload, dict):
        return HookInput(raw=raw)

    session_id = _as_str(payload.get("session_id"))
    cwd = _as_str(payload.get("cwd"))
    tool_name = _as_str(payload.get("tool_name"))
    prompt = _as_str(payload.get("prompt"))

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = _as_str(tool_input.get("command"))
        path = _as_str(tool_input.get("file_path")) or _as_str(tool_input.get("notebook_path"))
    elif tool_input is None:
        command = path = ""
    else:
        # jq: "Cannot index string with string \"command\"" -- the field is lost, the
        # three already-emitted ones are not.
        command = path = ""

    return HookInput(session_id=session_id, cwd=cwd, tool_name=tool_name, command=command, path=path, prompt=prompt, raw=raw)


def read_hook_input(stream: TextIO | None = None) -> HookInput:
    """Read the payload from stdin.

    Always ``read()`` on the stream, never ``/dev/stdin``: Claude Code hands hooks a
    socket, and reopening ``/dev/stdin`` on a socket fails with ENXIO. That was a real bug
    in the shell implementation and ``tests/fixtures/socket-stdin.py`` reproduces it.
    """
    if stream is not None:
        raw = stream.read()
    else:
        try:
            raw = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError:
            # jq would have failed on this too; every field ends up empty.
            log("hook payload is not valid UTF-8")
            return HookInput(raw="")
        except OSError as exc:
            log(f"could not read the hook payload: {exc}")
            return HookInput(raw="")
    if not raw:
        raw = "{}"
    return parse_hook_input(raw)


class Hook:
    """Owns a hook entrypoint's stdout, its decided flag and its fail-closed fallback.

    One instance per process. The shell used globals (``OCRL_DECIDED``, ``OCRL_FAILCLOSED``)
    and an ``EXIT`` trap; this is the same contract with the state made explicit so it can
    be driven by a test.
    """

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout
        self.decided: bool = False
        self.failclosed: FailClosed = "none"

    # -- plumbing --------------------------------------------------------

    def arm_failclosed(self, mode: FailClosed) -> None:
        """Declare which fallback this entrypoint owes if it dies before deciding."""
        self.failclosed = mode

    def _write(self, payload: dict[str, Any] | None) -> NoReturn:
        """Emit at most one response, in a single write, then unwind.

        A second emitter call is dropped rather than appended: two JSON objects
        concatenated on stdout do not parse, and a ``PreToolUse`` response that does not
        parse is not a denial.

        ``decided`` is set only once the bytes are written *and* flushed. Setting it first
        would mean an ``EPIPE`` or a short write left the gate believing it had answered:
        the fallback would then be suppressed, the process would exit 0, and the shim would
        forward a truncated response as though it were the decision.
        """
        if self.decided:
            log("a decision was already emitted; dropping a second one")
            raise Decided
        if payload is None:
            self.decided = True
            raise Decided

        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        failure: str | None = None
        written: int | None = None
        try:
            written = self._stream.write(blob)
            self._stream.flush()
        except (OSError, ValueError) as exc:
            # ValueError covers a stream closed underneath us.
            failure = str(exc)
        else:
            if written is not None and written != len(blob):
                failure = f"short write: {written} of {len(blob)}"

        if failure is not None:
            # The diagnostic goes inside try/finally so that OutputFailure reaches the
            # caller even if writing the diagnostic itself fails. Without this, a
            # simultaneously broken stderr would replace OutputFailure with an ordinary
            # OSError, the top-level guard would read that as a plain crash, and it would
            # append a fail-closed response behind the partial one already on stdout.
            # `log` is separately documented and tested as non-throwing; this is the
            # structural guarantee that does not depend on remembering that.
            try:
                log(f"the hook response could not be written in full: {failure}")
            finally:
                raise OutputFailure(failure)

        self.decided = True
        raise Decided

    # -- PreToolUse ------------------------------------------------------
    #
    # Every emitter below is ``NoReturn``: ``_write`` always raises, either ``Decided`` once
    # the response is out or ``OutputFailure`` when it could not be. Saying so in the
    # signature is not decoration -- it is what lets an entrypoint read as a flat sequence of
    # terminal decisions, the way the shell's ``ocrl_deny`` / ``exit 0`` did, instead of
    # every branch needing a ``return`` that only exists to convince a reader (and a type
    # checker) that execution stopped.

    def emit_pretool(self, decision: str, reason: str) -> NoReturn:
        self._write(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )

    def allow(self, reason: str = "opencode-review-loop: allowed") -> NoReturn:
        self.emit_pretool("allow", reason)

    def deny(self, reason: str) -> NoReturn:
        self.emit_pretool("deny", reason)

    def pass_(self) -> NoReturn:
        """Silent pass-through: no opinion, leave the normal permission flow alone.

        Zero bytes and exit 0 -- and this is the commonest hot-path outcome, which is why
        no caller may ever use empty stdout as a failure signal.
        """
        self._write(None)

    # -- PostToolUse -----------------------------------------------------

    def posttool_context(self, context: str) -> NoReturn:
        self._write(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )

    # -- UserPromptSubmit ------------------------------------------------

    def prompt_block(self, reason: str) -> NoReturn:
        self._write({"decision": "block", "reason": reason})

    # -- Stop ------------------------------------------------------------

    def stop_block(self, reason: str) -> NoReturn:
        self._write({"decision": "block", "reason": reason})

    def stop_ok(self, message: str = "") -> NoReturn:
        self._write({"systemMessage": message} if message else None)

    # -- fail closed -----------------------------------------------------

    def failclosed_exit(self, detail: str) -> None:
        """Emit this event's fallback, unless a decision was already made.

        A crashing gate must not masquerade as approval, and it must not answer in another
        event's protocol either: ``permissionDecision`` is a ``PreToolUse`` field, so
        emitting it from a post-hook is corruption rather than a denial.

        ``OutputFailure`` is deliberately allowed to propagate: if the stream is broken
        there is nothing useful to say on it, and the caller must exit non-zero instead of
        appending a second response to a partial one.
        """
        if self.decided:
            return
        try:
            if self.failclosed == "pretool":
                self.deny(DENY_REASON.format(detail=detail))
            elif self.failclosed == "stop":
                self.stop_block(STOP_BLOCK_REASON.format(detail=detail))
            elif self.failclosed == "posttool":
                self.posttool_context(POSTTOOL_CONTEXT_REASON.format(detail=detail))
            else:
                # posttool-failure emits nothing today, and inventing a protocol shape
                # here would trade an inert inconsistency for a possibly-ignored message.
                self.decided = True
        except Decided:
            pass

    def _fallback(self, detail: str) -> int:
        """Emit the fail-closed response, or report that stdout is unusable."""
        try:
            self.failclosed_exit(detail)
        except OutputFailure:
            return EXIT_OUTPUT_ERROR
        return 0

    def run(self, body: Callable[[], None]) -> int:
        """Run an entrypoint under the fail-closed guard, returning the process exit status.

        Exit status is the shim's only discriminator -- never empty stdout, since ``pass_``
        legitimately emits zero bytes and is the commonest hot-path outcome. So:

        - a response written and flushed in full exits ``0``;
        - anything else exits non-zero, which tells the shim to discard whatever it
          captured and emit that event's fallback itself.

        No second write is ever attempted on a stream that has already failed.
        """
        try:
            body()
        except Decided:
            return 0
        except OutputFailure:
            return EXIT_OUTPUT_ERROR
        except BaseException as exc:
            log_exception()
            return self._fallback(type(exc).__name__)
        # Falling out of the body without deciding is itself a defect; the shell's EXIT
        # trap covered this case and so does this.
        return self._fallback("no decision was reached")
