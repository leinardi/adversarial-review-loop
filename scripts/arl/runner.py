"""Running a reviewer process: the bounded runner, the transcript reduction, and what kind of
failure a run that did not complete was.

A timeout or a non-zero exit is :class:`ReviewerFailed`, never an approval.
:func:`classify_op_failure` tells a transient failure (a timeout, a matched rate or usage limit)
from an operational one, because the two are retried differently.
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

import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

import arl
from arl import harness
from arl.atomic import FILE_MODE, read_verified_file, write_private_file
from arl.config import Config
from arl.contract import Target
from arl.errors import OcrlError
from arl.util import decode, log

__all__ = [
    "KILL_GRACE_SEC",
    "MAX_TIMEOUT_SEC",
    "Invocation",
    "LaunchError",
    "ReviewerFailed",
    "classify_op_failure",
    "efficiency_text",
    "invoke",
    "kill_group",
    "run_bounded",
    "run_clarify",
]


#: The largest ``timeout_sec`` the reviewer will honour. Configuration is unbounded, but a
#: reviewer deadline past this is not a deadline: the session that asked for the review is
#: long gone.
MAX_TIMEOUT_SEC: Final = 6 * 60 * 60


#: How long a timed-out process group gets to honour SIGTERM before SIGKILL follows. Defined
#: here, ahead of the budgets, because two of them are derived from it: it is paid *after* a
#: deadline expires, so every bounded step's real worst case is its own timeout plus this.
KILL_GRACE_SEC: Final = 2.0


#: Exit statuses ``timeout`` uses for "killed before it finished".
_TIMEOUT_STATUSES: Final = frozenset({124, 137})


#: Phase 6's "transient" class is an allow-list, not a catch-all -- five attempts against a
#: missing ``opencode`` binary must not spend the same budget as five genuine rate limits. A
#: timeout is unambiguous (``_TIMEOUT_STATUSES``); a plain non-zero exit is only "transient"
#: when the process's own output says so, and only the head of it is read, bounded, so a
#: reviewer transcript large enough to be truncated is never scanned in full for this.
_TRANSIENT_OUTPUT_HEAD_BYTES: Final = 4096


#: Known provider/CLI phrasing for a rate or usage limit, case-insensitive. Deliberately
#: specific multi-word phrases rather than a bare "limit" or "quota" -- this is read from a
#: non-zero exit's raw output, which past this point is CLI/provider error text, not
#: reviewer-composed findings prose, but the anchoring stays defensive regardless: an
#: unmatched byte string must classify as "operational" (Rule 1's fail-closed direction --
#: the wider budget, not the one with retry pacing that gives a stuck phase more attempts).
#: ``\b`` on the left of every alternative, and only a space/hyphen/underscore (never an
#: arbitrary character) between "rate" and "limit" -- a bare ``.?`` would also glue onto a run
#: of other letters (``rateXlimit``) or match "rate"/"limit" as a substring buried inside an
#: unrelated word (``\b`` only exists at the edges of a run of word characters, so it cannot
#: match mid-word either way). The right edge is a negative lookahead for a following
#: letter/digit, not a second ``\b``: a trailing ``\b`` would reject a real, snake_cased
#: provider error code like ``rate_limit_exceeded`` -- underscore is itself a word character,
#: so ``\b`` finds no boundary between "limit" and the "_exceeded" that follows it -- while
#: still correctly rejecting "limit" as the front half of a longer word like "limitation".
#: What no purely lexical pattern can rule out is a negation that still contains the literal
#: phrase ("not a rate limit") -- accepted, because at this point in the flow the byte string
#: is CLI/provider error text on a non-zero exit, not reviewer prose arguing about rate limits
#: as a topic.
_RATE_LIMIT_RE: Final = re.compile(rb"(?i)\b(rate[ _-]?limit(?:ed|ing)?|too many requests|quota exceeded|usage limit reached)(?![A-Za-z0-9])")


#: OpenCode writes a styled transcript; the escape codes carry no information and would
#: otherwise be quoted back at Claude.
_ANSI_RE: Final = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


class LaunchError(OcrlError):
    """The reviewer command could not be composed, so nothing was launched.

    Reported as ``OP_FAILURE`` with ``kind`` ``"bundle"``: like a bundle that could not be
    built, it is not a reviewer that ran and failed.
    """


class ReviewerFailed(OcrlError):
    """The reviewer did not run to completion. Always ``OP_FAILURE``, never an approval."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """``status`` is the exit code ``invoke`` observed (124/137 for a timeout, the
        process's own for anything else). :func:`classify_op_failure` reads this rather than
        pattern-matching ``message`` -- the status is the fact; the message is prose. ``None``
        only for a caller that has no status to give, which no call site in this module does
        today.
        """
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Invocation:
    """One external reviewer call: its prompt, the bundle it reads, and its deadline.

    ``timeout_sec`` is ``0`` for every ordinary invocation, meaning "the configured
    ``timeout_sec``, clamped".
    """

    bundle_dir: Path
    prompt_file: Path
    title: str
    out_path: Path
    timeout_sec: int = 0


def _harness(config: Config) -> harness.Harness:
    """Which reviewer CLI this invocation runs: whatever the ``harness`` key selects.

    One reader, so the harness a command is built from, the harness every message names and
    the harness the budgets are sized for can never disagree.

    A value this build does not implement raises :class:`arl.harness.UnknownHarness`, which
    is deliberately *not* caught anywhere on the review path: a review that cannot tell which
    reviewer it is talking to fails, and is never quietly run by the default harness.
    """
    return harness.selected(config)


def efficiency_text() -> str:
    """``prompts/reviewer-efficiency.md``: how to work, as opposed to what to review.

    Read on every invocation rather than cached at import: a hook is a fresh process each
    time, so there is nothing to cache across, and reading it at the call site keeps this the
    same shape as every other prompt read (:func:`arl.prompt_path`).

    **Missing or unreadable degrades to "", never to an error.** This text makes a review
    cheaper, not more correct -- a review that runs without it is an ordinary review, while a
    review that refuses to run because a guidance file went missing is a blocked commit for no
    safety reason at all. That is the opposite direction from every other failure in this
    module, and deliberately so: nothing here bears on a verdict.
    """
    try:
        return decode(arl.prompt_path("reviewer-efficiency").read_bytes()).rstrip("\n")
    except OSError as exc:
        log(f"the reviewer efficiency guidance could not be read ({exc}); continuing without it")
        return ""


def _timeout_sec(config: Config) -> int:
    """``timeout_sec``, clamped to :data:`MAX_TIMEOUT_SEC`.

    Every reader of ``timeout_sec`` goes through here, so no caller can bound a reviewer by
    an unclamped value.
    """
    configured = config.as_int("timeout_sec")
    if configured > MAX_TIMEOUT_SEC:
        log(f"timeout_sec {configured} is above the {MAX_TIMEOUT_SEC}s ceiling; using {MAX_TIMEOUT_SEC}")
        return MAX_TIMEOUT_SEC
    return configured


def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the timed-out process **and everything it spawned**.

    ``subprocess``'s own timeout kills the direct child only, so backgrounded work keeps running
    after the engine gave up on it -- measured: a grandchild created its file two seconds after the
    one-second deadline. ``start_new_session=True`` plus ``killpg`` is GNU ``timeout``'s own
    arrangement.

    ``SIGTERM`` first, so a build can tear its own children down, then ``SIGKILL`` to the group
    **unconditionally** -- stricter than ``timeout``. Watching the direct child instead is not
    enough: it exits on ``SIGTERM`` while a descendant that ignored the signal keeps running,
    which is what a measurement of the earlier version showed.

    The grace period is always waited out and the child is deliberately left unreaped while it
    elapses: an unreaped zombie keeps its process-group id allocated (verified), so the
    ``SIGKILL`` cannot land on an unrelated group that recycled the number. A descendant that
    calls ``setsid`` escapes both signals, exactly as it escapes ``timeout``.
    """
    name = str(proc.args[0]) if isinstance(proc.args, (list, tuple)) and proc.args else "the child"
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None and pgid == os.getpgrp():
        # Only reachable if a future edit drops `start_new_session`, and it must never be
        # allowed to happen: signalling our own group kills the engine mid-response, which leaves
        # the mod with no answer at all. Confirmed
        # by experiment -- the guard was added after this killed the test runner itself.
        log(f"{name} shares this process group; killing only the child")
        pgid = None

    if pgid is None:
        proc.kill()
        proc.wait()
        return

    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)
    time.sleep(KILL_GRACE_SEC)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    proc.wait()


def run_bounded(  # noqa: PLR0913 - each arg is an independent knob of the run; folding them into an object would only move the count
    command: list[str],
    *,
    stdout: IO[bytes],
    timeout_sec: int,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    stdin: bytes | None = None,
) -> int:
    """Run ``command`` under a deadline, both streams to ``stdout``, answering its status.

    ``124`` on expiry and ``127`` when it cannot be started, matching what ``timeout`` and the
    shell reported. The child gets its own process group so the deadline binds its descendants
    too -- see :func:`kill_group`.

    ``stdin`` feeds the child bytes on standard input, for a harness whose prompt does not fit in
    an argv. ``None`` leaves stdin inherited and takes the plain
    :meth:`~subprocess.Popen.wait` path.

    **The deadline covers the write, not just the wait**, which is why
    :meth:`~subprocess.Popen.communicate` is used. A pipe holds 64KiB; writing a bundle-sized
    prompt in full before starting the timed wait blocks the moment the child stops reading --
    and a reviewer that hangs without draining its input is exactly the case a deadline exists
    for. Measured on the first version of this function: a 1MiB payload against a child that
    never read stdin ran **30s under ``timeout_sec=1``**, leaving the hook wedged until the shim's
    own timeout. ``communicate`` writes and waits under one deadline and swallows ``EPIPE`` on a
    child that exits early; the exit status is checked either way.
    """
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        log(f"{command[0]} could not be started: {exc}")
        return 127
    try:
        if stdin is None:
            status = proc.wait(timeout=timeout_sec)
        else:
            # stdout/stderr are a regular file here, never PIPE, so `communicate` only has the
            # input side to service -- it writes it under the same deadline it then waits on.
            proc.communicate(input=stdin, timeout=timeout_sec)
            status = proc.returncode
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return 124
    except BaseException:
        # The engine itself is ending (SIGTERM from the mod, an interrupt): the reviewer and
        # everything it started go with it rather than run on unowned.
        kill_group(proc)
        raise
    return status


def _capture_to_file(  # noqa: PLR0913, PLR0917 - one more knob of the same run; see `run_bounded`'s own note
    command: list[str], env: dict[str, str], out_path: Path, timeout_sec: int, stdin: bytes | None = None, cwd: str | None = None
) -> int:
    """Run the reviewer with both streams to ``out_path``, answering ``timeout``'s status."""
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            return run_bounded(command, stdout=sink, timeout_sec=timeout_sec, env=env, stdin=stdin, cwd=cwd)
    finally:
        _strip_ansi(out_path)


def _strip_ansi(path: Path) -> None:
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if not raw:
        return
    cleaned = _ANSI_RE.sub(b"", raw)
    if cleaned != raw:
        write_private_file(path, cleaned)


#: What the CLI's own output is kept as when a harness reduced it to a transcript. Beside the
#: transcript rather than instead of it: the reduction is a derivation, and the bytes it was
#: derived from are what an operator needs to check it against -- the denied tool calls, the
#: cost, the session the CLI says it actually used. Never read by the engine.
_ENVELOPE_SUFFIX: Final = ".envelope"


def _reduce_transcript(implementation: harness.Harness, out_path: Path) -> harness.Usage | None:
    """Replace the reviewer's own output at ``out_path`` with the answer text :func:`parse` reads,
    and return what that run cost.

    A no-op for a harness whose CLI writes the answer and nothing around it. For one that wraps
    it, the wrapper moves to ``<out_path>.envelope`` and the answer takes its place, which is what
    lets :func:`parse` stay unaware that more than one output shape exists.

    **The usage is read here because here is where the wrapper still exists** -- the accounting
    lives in the same event the answer is extracted from, and the next statement moves that event
    out of the way. Read before the reduction for the same reason, and returned rather than
    stored, so this function keeps its single side effect.

    **Only on the success path.** A failed run keeps its output exactly as the CLI wrote it,
    because that is what :func:`classify_op_failure` reads to tell a rate limit from a bad model
    name, and a wrapper this function could not parse is itself the diagnosis.
    """
    raw = read_verified_file(out_path, root=out_path.parent)
    if raw is None:
        raise ReviewerFailed(f"the reviewer's output at {out_path} could not be read back")
    usage = implementation.usage(raw)
    reduced = implementation.transcript(raw)
    if reduced == raw:
        return usage
    write_private_file(out_path.with_name(out_path.name + _ENVELOPE_SUFFIX), raw)
    write_private_file(out_path, reduced)
    return usage


def invoke(target: Target, run: Invocation, *, config: Config, environ: dict[str, str] | None = None) -> harness.Usage | None:
    """Run the reviewer, leaving its output at ``out_path``, and report what it cost.

    Raises :class:`ReviewerFailed` on a timeout or a non-zero exit. ``ARL_REVIEWER_CMD`` is
    the test seam the suites drive: a stand-in that reads the bundle and writes the same
    contract to stdout, so the loop can be exercised without spending a model call.

    The return value is :class:`arl.harness.Usage` for a harness that reports its accounting,
    and ``None`` for one that does not or for the test seam, where no model call was made and
    there is nothing to account for. It is display-only in every caller (see that class); no
    branch anywhere reads it.
    """
    env = dict(os.environ if environ is None else environ)
    timeout_sec = run.timeout_sec or _timeout_sec(config)
    reviewer_cmd = env.get("ARL_REVIEWER_CMD", "")

    stdin: bytes | None = None
    cwd: str | None = None
    if not any(run.bundle_dir.glob("changes.*.diff")):
        # A review prompt names the bundle's files; one with no diff in it would be judged on
        # nothing, and an empty judgement reads as an approval.
        raise LaunchError(f"the bundle at {run.bundle_dir} holds no changes.*.diff")
    if reviewer_cmd:
        env["ARL_BUNDLE_DIR"] = str(run.bundle_dir)
        command = [reviewer_cmd, str(run.bundle_dir), str(run.prompt_file)]
    else:
        try:
            prompt_text = decode(run.prompt_file.read_bytes()).rstrip("\n")
        except OSError as exc:
            raise LaunchError(f"the reviewer prompt at {run.prompt_file} could not be read: {exc.strerror or exc}") from exc
        spec = harness.ReviewSpec(
            repo=target.launch_dir,
            prompt_text=prompt_text,
            system_prompt=efficiency_text(),
            title=run.title,
            bundle_dir=run.bundle_dir,
            roots=tuple(root.path for root in target.roots),
            config=config,
        )
        try:
            built = _harness(config).review_command(spec)
        except harness.PayloadError as exc:
            # Composing the command failed, so nothing was launched.
            raise LaunchError(str(exc)) from exc
        command, stdin, cwd = built.argv, built.stdin, built.cwd
        env.update(built.env)

    status = _capture_to_file(command, env, run.out_path, timeout_sec, stdin, cwd)
    if status in _TIMEOUT_STATUSES:
        raise ReviewerFailed(f"the reviewer timed out after {timeout_sec}s", status=status)
    if status != 0:
        raise ReviewerFailed(f"the reviewer exited with status {status}", status=status)
    if reviewer_cmd:
        # The stub wrote the contract itself; there was no model call to account for.
        return None
    try:
        return _reduce_transcript(_harness(config), run.out_path)
    except harness.TranscriptError as exc:
        # A zero exit the harness refuses to read as an answer. `status=0` keeps
        # `classify_op_failure` on its "operational" path, which is what this is: the
        # reviewer reported something about its own run that no verdict may be read past.
        raise ReviewerFailed(str(exc)) from exc


def run_clarify(  # noqa: PLR0913 - one knob per argument, as `run_bounded` notes
    launch_dir: str,
    bundle_dir: Path,
    *,
    prompt_file: Path,
    title: str,
    out_path: Path,
    config: Config,
    roots: tuple[str, ...] = (),
) -> str:
    """Answer one question about a review already given, from its stored bundle, and return the
    reply. No ``VERDICT`` is parsed. Raises :class:`ReviewerFailed` on a timeout or a non-zero exit,
    so a failed clarify is reported, never silently empty.

    ``ARL_REVIEWER_CMD`` is honoured for the tests.
    """
    env = dict(os.environ)
    timeout_sec = _timeout_sec(config)
    reviewer_cmd = env.get("ARL_REVIEWER_CMD", "")

    stdin: bytes | None = None
    cwd: str | None = None
    if reviewer_cmd:
        env["ARL_BUNDLE_DIR"] = str(bundle_dir)
        command = [reviewer_cmd, str(bundle_dir), str(prompt_file)]
    else:
        try:
            prompt_text = decode(prompt_file.read_bytes()).rstrip("\n")
        except OSError as exc:
            raise ReviewerFailed(f"the clarify prompt at {prompt_file} could not be read: {exc.strerror or exc}") from exc
        spec = harness.ClarifySpec(
            repo=launch_dir,
            prompt_text=prompt_text,
            system_prompt=efficiency_text(),
            title=title,
            bundle_dir=bundle_dir,
            roots=roots,
            config=config,
        )
        try:
            built = _harness(config).clarify_command(spec)
        except harness.PayloadError as exc:
            raise ReviewerFailed(str(exc)) from exc
        command, stdin, cwd = built.argv, built.stdin, built.cwd
        env.update(built.env)

    status = _capture_to_file(command, env, out_path, timeout_sec, stdin, cwd)
    if status in _TIMEOUT_STATUSES:
        raise ReviewerFailed(f"the reviewer timed out after {timeout_sec}s", status=status)
    if status != 0:
        raise ReviewerFailed(f"the reviewer exited with status {status}", status=status)
    if not reviewer_cmd:
        try:
            _reduce_transcript(_harness(config), out_path)
        except harness.TranscriptError as exc:
            raise ReviewerFailed(str(exc)) from exc
    return decode(out_path.read_bytes())


def classify_op_failure(exc: ReviewerFailed, out_path: Path) -> str:
    """ "transient" for a timeout or a matched rate/usage-limit signal, "operational" for
    every other non-zero exit -- ``126``/``127``, a bad ``--model``, a rejected ``--variant``
    and an expired credential all included. See ``Review.kind`` for why the split matters.
    """
    if exc.status in _TIMEOUT_STATUSES:
        return "transient"
    try:
        # `.read(n)`, not `.read_bytes()[:n]` -- a reviewer that failed non-zero can still
        # have written an unbounded amount to `out_path` before it did, and slicing after the
        # fact would read the whole file into memory just to keep the first few bytes of it.
        with out_path.open("rb") as handle:
            head = handle.read(_TRANSIENT_OUTPUT_HEAD_BYTES)
    except OSError:
        head = b""
    if _RATE_LIMIT_RE.search(head):
        return "transient"
    return "operational"
