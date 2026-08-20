"""Rule 2 (hook stdout is protocol) and Rule 1 (no failure becomes an approval)."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SCRIPTS_DIR, SOCKET_STDIN

from ocrl.hookio import (
    EXIT_OUTPUT_ERROR,
    Decided,
    Hook,
    HookInput,
    OutputFailure,
    parse_hook_input,
    read_hook_input,
)
from ocrl.util import log, log_exception


class BrokenStream(io.StringIO):
    """A stdout that fails the way a closed pipe or a full disk does."""

    def __init__(self, *, fail_on: str, short: bool = False) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.short = short

    def write(self, s: str, /) -> int:
        if self.fail_on == "write":
            raise OSError(32, "Broken pipe")
        written = super().write(s)
        if self.short:
            # Report a partial write without truncating what a reader would see, so the
            # test cannot pass just because the buffer happens to look short.
            return written - 1
        return written

    def flush(self) -> None:
        if self.fail_on == "flush":
            raise OSError(28, "No space left on device")
        super().flush()


def emit(call: str, *args: object) -> str:
    """Drive one emitter and return exactly what reached stdout."""
    buf = io.StringIO()
    hook = Hook(stream=buf)
    with pytest.raises(Decided):
        getattr(hook, call)(*args)
    assert hook.decided
    return buf.getvalue()


# -- payload parsing -------------------------------------------------------


def test_parses_a_well_formed_payload() -> None:
    raw = json.dumps(
        {
            "session_id": "s1",
            "cwd": "/w",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m x"},
        }
    )
    assert parse_hook_input(raw) == HookInput("s1", "/w", "Bash", "git commit -m x", raw)


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '"a string"', "5", ""])
def test_an_unreadable_payload_yields_no_fields(raw: str) -> None:
    """Callers then treat the event as "not ours" or deny -- never as a silent allow."""
    got = parse_hook_input(raw)
    assert (got.session_id, got.cwd, got.tool_name, got.command) == ("", "", "", "")


def test_a_non_object_tool_input_loses_only_the_command() -> None:
    """Faithful to the jq original, which had already streamed the first three fields."""
    raw = json.dumps({"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": "nope"})
    got = parse_hook_input(raw)
    assert (got.session_id, got.cwd, got.tool_name) == ("s1", "/w", "Bash")
    assert got.command == ""


def test_non_string_fields_are_stringified_the_way_jq_did() -> None:
    raw = json.dumps({"session_id": 5, "tool_name": True, "tool_input": {"command": {"a": 1}}})
    got = parse_hook_input(raw)
    assert got.session_id == "5"
    assert got.tool_name == "true"
    assert got.command == '{"a":1}'


def test_empty_stdin_is_read_as_an_empty_object() -> None:
    assert read_hook_input(io.StringIO("")).raw == "{}"


def test_stdin_on_a_socket_parses(tmp_path: Path) -> None:
    """Claude Code hands hooks a socket, and `$(</dev/stdin)` fails with ENXIO on one.

    This reproduces the original shell bug end to end rather than through a stream object.
    """
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"tool_input": {"command": "git commit -m sock"}}))
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from ocrl.hookio import read_hook_input\n"
        "sys.stdout.write(read_hook_input().command)\n"
    )
    proc = subprocess.run(
        [str(SOCKET_STDIN), str(payload), sys.executable, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "git commit -m sock"


# -- emitter shapes --------------------------------------------------------


def test_allow_shape() -> None:
    assert emit("allow", "why") == (
        '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"why"}}\n'
    )


def test_deny_shape() -> None:
    assert emit("deny", "why") == (
        '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"why"}}\n'
    )


def test_pass_emits_zero_bytes() -> None:
    """The commonest hot-path outcome, which is why empty stdout is never a failure signal."""
    assert emit("pass_") == ""


def test_posttool_context_shape() -> None:
    assert emit("posttool_context", "ctx") == ('{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"ctx"}}\n')


def test_stop_block_shape() -> None:
    assert emit("stop_block", "r") == '{"decision":"block","reason":"r"}\n'


def test_stop_ok_shapes() -> None:
    assert emit("stop_ok", "msg") == '{"systemMessage":"msg"}\n'
    assert emit("stop_ok") == ""


def test_output_is_utf8_not_escaped() -> None:
    """Matches `jq -c`, which emits raw UTF-8; `json.dumps` would escape it by default."""
    out = emit("deny", "héllo 😀 ")
    assert "héllo 😀" in out
    assert "\\u0001" in out


# -- fail closed -----------------------------------------------------------


def test_an_unhandled_exception_still_denies() -> None:
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")

    def body() -> None:
        raise RuntimeError("boom")

    assert hook.run(body) == 0
    payload = json.loads(buf.getvalue())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "RuntimeError" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_unhandled_exception_in_the_stop_gate_blocks() -> None:
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("stop")

    def body() -> None:
        raise RuntimeError("boom")

    assert hook.run(body) == 0
    assert json.loads(buf.getvalue())["decision"] == "block"


def test_posttool_failure_stays_silent_on_a_crash() -> None:
    """Its only job is clearing a pending approval; it can never deny, so it says nothing."""
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("none")

    def body() -> None:
        raise RuntimeError("boom")

    assert hook.run(body) == 0
    assert buf.getvalue() == ""


def test_falling_out_of_the_body_without_deciding_fails_closed() -> None:
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")

    assert hook.run(lambda: None) == 0
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_crash_after_a_decision_does_not_append_a_second_response() -> None:
    """Two concatenated JSON objects do not parse, and an unparseable response is not a deny."""
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")

    def body() -> None:
        hook.allow("fine")

    assert hook.run(body) == 0
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_second_emitter_call_is_dropped() -> None:
    buf = io.StringIO()
    hook = Hook(stream=buf)
    with pytest.raises(Decided):
        hook.allow("first")
    with pytest.raises(Decided):
        hook.deny("second")
    assert buf.getvalue().count("\n") == 1
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"


# -- a broken stdout must never look like a decision -----------------------


@pytest.mark.parametrize("fail_on", ["write", "flush"])
def test_a_failed_write_is_not_a_decision(fail_on: str) -> None:
    """Setting `decided` before the bytes land would suppress the fallback and exit 0."""
    hook = Hook(stream=BrokenStream(fail_on=fail_on))
    with pytest.raises(OutputFailure):
        hook.deny("nope")
    assert not hook.decided


def test_a_short_write_is_not_a_decision() -> None:
    hook = Hook(stream=BrokenStream(fail_on="none", short=True))
    with pytest.raises(OutputFailure):
        hook.deny("nope")
    assert not hook.decided


@pytest.mark.parametrize("fail_on", ["write", "flush"])
def test_a_broken_stream_exits_non_zero_so_the_shim_discards(fail_on: str) -> None:
    """The shim's only discriminator is exit status; a partial response must not exit 0."""
    stream = BrokenStream(fail_on=fail_on)
    hook = Hook(stream=stream)
    hook.arm_failclosed("pretool")

    def body() -> None:
        hook.deny("nope")

    assert hook.run(body) == EXIT_OUTPUT_ERROR


def test_no_second_response_is_appended_to_a_broken_one() -> None:
    """Two concatenated JSON objects do not parse, and that is not a denial."""
    stream = BrokenStream(fail_on="flush")
    hook = Hook(stream=stream)
    hook.arm_failclosed("pretool")

    def body() -> None:
        hook.deny("first")

    assert hook.run(body) == EXIT_OUTPUT_ERROR
    # The flush failed after the bytes reached the buffer; what matters is that no second
    # response was appended behind them.
    assert stream.getvalue().count('"hookSpecificOutput"') == 1
    assert "internal gate error" not in stream.getvalue()


def test_a_crash_onto_a_broken_stream_exits_non_zero() -> None:
    hook = Hook(stream=BrokenStream(fail_on="write"))
    hook.arm_failclosed("pretool")

    def body() -> None:
        raise RuntimeError("boom")

    assert hook.run(body) == EXIT_OUTPUT_ERROR


# -- the decision signal must not be resumable -----------------------------


def test_a_body_catching_exception_cannot_swallow_a_decision() -> None:
    """Entrypoints legitimately wrap work in `except Exception`.

    If that caught the emitter's signal, execution would continue past a written response
    and a later crash would leave an `allow` standing as the final answer.
    """
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")
    reached_after_emit = False

    def body() -> None:
        nonlocal reached_after_emit
        with contextlib.suppress(Exception):  # the hazard under test
            hook.allow("approved")
        reached_after_emit = True
        raise RuntimeError("boom")

    assert hook.run(body) == 0
    assert not reached_after_emit, "execution continued past an emitted decision"
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_body_catching_exception_cannot_swallow_an_output_failure() -> None:
    hook = Hook(stream=BrokenStream(fail_on="write"))
    hook.arm_failclosed("pretool")

    def body() -> None:
        with contextlib.suppress(Exception):  # the hazard under test
            hook.deny("nope")

    assert hook.run(body) == EXIT_OUTPUT_ERROR


# -- a broken stderr must not be able to corrupt stdout --------------------


def test_a_broken_stderr_cannot_turn_a_broken_stdout_into_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """The diagnostic channel must never be able to break the protocol channel.

    If the log write displaced `OutputFailure` with an `OSError`, the top-level guard would
    read that as an ordinary crash and append a full denial behind the partial response.
    """
    monkeypatch.setattr(sys, "stderr", BrokenStream(fail_on="write"))
    stream = BrokenStream(fail_on="flush")
    hook = Hook(stream=stream)
    hook.arm_failclosed("pretool")

    def body() -> None:
        hook.deny("first")

    assert hook.run(body) == EXIT_OUTPUT_ERROR
    assert stream.getvalue().count('"hookSpecificOutput"') == 1
    assert "internal gate error" not in stream.getvalue()


def test_output_failure_survives_a_log_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`log` is non-throwing by contract; this proves `_write` does not depend on that."""

    def exploding_log(message: str) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr("ocrl.hookio.log", exploding_log)
    hook = Hook(stream=BrokenStream(fail_on="write"))
    hook.arm_failclosed("pretool")

    with pytest.raises(OutputFailure):
        hook.deny("nope")
    assert not hook.decided


def test_a_short_write_survives_a_log_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding_log(message: str) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr("ocrl.hookio.log", exploding_log)
    hook = Hook(stream=BrokenStream(fail_on="none", short=True))

    with pytest.raises(OutputFailure):
        hook.deny("nope")
    assert not hook.decided


def test_a_broken_stderr_still_lets_the_fallback_reach_a_working_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost traceback must not cost the denial; stdout is the channel that matters."""
    monkeypatch.setattr(sys, "stderr", BrokenStream(fail_on="write"))
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")

    def body() -> None:
        raise RuntimeError("boom")

    assert hook.run(body) == 0
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_log_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", BrokenStream(fail_on="write"))
    log("this must not raise")
    monkeypatch.setattr(sys, "stderr", BrokenStream(fail_on="flush"))
    log("nor this")


def test_log_exception_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", BrokenStream(fail_on="write"))
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log_exception()


def test_pass_then_crash_stays_silent() -> None:
    """`pass_` decided with zero bytes; the guard must not mistake that for "no decision"."""
    buf = io.StringIO()
    hook = Hook(stream=buf)
    hook.arm_failclosed("pretool")

    def body() -> None:
        hook.pass_()

    assert hook.run(body) == 0
    assert buf.getvalue() == ""
