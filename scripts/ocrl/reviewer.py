"""Building the reviewer bundle, invoking OpenCode, and parsing the contract.

Ports ``scripts/lib/reviewer.sh``. Claude composes none of this: every attachment is
generated from git, and the prompt is a fixed file shipped with the plugin.

The shell carried its result in seven ``OCRL_REVIEW_*`` globals; here it is one
:class:`Review`, returned by :func:`execute` and rendered by :mod:`ocrl.report`.

**Every failure mode ends in a verdict that is not an approval** (Rule 1). A diff that
cannot be produced, a reviewer that times out, exits non-zero, says nothing, omits the
markers or emits a verdict the gate does not recognise -- each maps to ``OP_FAILURE`` or
``NEEDS_HUMAN``. There is no path from an operational failure to ``APPROVED``, and the
reviewer's own verdict is advisory: an actionable finding at or above ``block_severity``
blocks regardless of what the reviewer concluded.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

import ocrl
from ocrl import report
from ocrl.atomic import FILE_MODE, ensure_private_dir
from ocrl.config import Config, severity_rank
from ocrl.errors import OcrlError
from ocrl.gitsnap import git_run
from ocrl.paths import state_root
from ocrl.state import State
from ocrl.util import log

__all__ = [
    "BundleError",
    "BundleTooLarge",
    "ContractError",
    "Finding",
    "Invocation",
    "Review",
    "ReviewerFailed",
    "Target",
    "build_bundle",
    "byte_lines",
    "execute",
    "invoke",
    "parse",
    "permission",
    "review_argv",
    "run_bounded",
    "split_lines_by_size",
]

#: The contract the reviewer prompts demand. Both must be present or the output is refused.
FINDINGS_MARKER: Final = "<<<OCRL-FINDINGS>>>"
END_MARKER: Final = "<<<OCRL-END>>>"

#: Ceilings the shell expressed as ``head``/``tail`` invocations.
LOG_LINES: Final = 200
DIFFSTAT_LINES: Final = 200
PLAN_EXCERPT_BYTES: Final = 65536
DIFF_ERROR_BYTES: Final = 500

#: ``verify_cmd`` runs under its own fixed ceiling, unrelated to the review timeout.
VERIFY_TIMEOUT_SEC: Final = 600
VERIFY_TAIL_BYTES: Final = 200000

#: How long a timed-out process group gets to honour SIGTERM before SIGKILL follows.
KILL_GRACE_SEC: Final = 2.0

#: ``split -d -a 2`` can name 100 files before it gives up.
MAX_CHUNKS: Final = 100

#: Exit statuses ``timeout`` uses for "killed before it finished".
_TIMEOUT_STATUSES: Final = frozenset({124, 137})

#: OpenCode writes a styled transcript; the escape codes carry no information and would
#: otherwise be quoted back at Claude.
_ANSI_RE: Final = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

#: POSIX ``[[:space:]]`` in the C locale, spelled out rather than left to ``\s``, which is
#: Unicode-aware in Python and would split on characters ``grep`` treats as ordinary.
_SPACE: Final = " \t\n\r\f\v"
_VERDICT_LINE: Final = re.compile(rf"^[{_SPACE}]*VERDICT[: ]")
_VERDICT_PREFIX: Final = re.compile(rf"^[{_SPACE}]*VERDICT[: ]+")
_TRAILING_SPACE: Final = re.compile(rf"[{_SPACE}]+$")

#: The ``FINDING`` grammar, exactly as ``prompts/reviewer-*.md`` specifies it:
#: ``FINDING severity=<label> actionable=yes|no file=<path[:line]|-> | <detail>``.
#: ``severity`` is one of five documented labels and ``actionable`` one of two, because a
#: field the gate cannot read is a finding it cannot weigh -- and weighing it as "does not
#: block" is exactly the failure-into-approval Rule 1 forbids.
_FINDING_RE: Final = re.compile(
    r"^FINDING[ \t]+severity=(?P<severity>info|low|medium|high|critical)"
    r"[ \t]+actionable=(?P<actionable>yes|no)"
    rf"[ \t]+file=[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?[ \t]*\|[ \t]*[^{_SPACE}]"
)

#: How much of an offending line is echoed back, so a denial names what to fix.
CONTRACT_ECHO_CHARS: Final = 120

#: The one byte the shell gate cannot hold; see :func:`parse`.
NUL: Final = b"\0"

_APPROVING_VERDICTS: Final = frozenset({"APPROVE", "APPROVED", "OK", "PASS"})
_BLOCKING_VERDICTS: Final = frozenset({"CHANGES_REQUIRED", "CHANGES-REQUIRED", "REJECT", "REJECTED", "BLOCK"})


class BundleError(OcrlError):
    """The evidence bundle could not be built, so there is nothing to review.

    Caught by :func:`execute`, which reports it as ``OP_FAILURE``. Like
    ``gitsnap.SnapshotError`` it is deliberately catchable, and like it, being uncaught
    still denies through the fail-closed guard.
    """


class BundleTooLarge(BundleError):
    """The diff is past ``hard_diff_ceiling``.

    Separate from :class:`BundleError` because it escalates to ``NEEDS_HUMAN`` rather than
    counting as one more operational failure: approving on a partial view is not an option,
    and retrying will not shrink the diff.
    """


class ReviewerFailed(OcrlError):
    """The reviewer did not run to completion. Always ``OP_FAILURE``, never an approval."""


class ContractError(OcrlError):
    """The reviewer's output is not the documented contract.

    Caught inside :func:`parse` and turned into ``OP_FAILURE``. It exists so that every way
    of failing the contract leaves through one place, rather than each check having to
    remember to return rather than fall through to the verdict.
    """


@dataclass(frozen=True)
class Target:
    """What one review is about: the range under review, and where it sits in the plan.

    These five travel together through every function here and in :mod:`ocrl.report` --
    the bundle header, the report filename and the report body all restate them -- so they
    are one value rather than five parallel parameters that a call site can transpose.
    """

    repo: str
    base: str
    head: str
    #: ``phase`` for one phase's delta, ``final`` for the cumulative review.
    scope: str
    phase: int

    @property
    def is_phase(self) -> bool:
        return self.scope == "phase"

    @property
    def label(self) -> str:
        """How this review is named on disk and in the report heading."""
        return f"phase{self.phase}" if self.is_phase else self.scope


@dataclass(frozen=True)
class Invocation:
    """Where one run of the reviewer reads its bundle and writes its answer."""

    bundle_dir: Path
    prompt_file: Path
    title: str
    out_path: Path


@dataclass(frozen=True)
class Finding:
    """One validated ``FINDING`` line, kept verbatim alongside its parsed fields."""

    line: str
    severity: str
    actionable: bool


@dataclass
class Review:
    """One review's outcome, recomputed by the gate rather than taken from the reviewer."""

    #: ``APPROVED`` | ``CHANGES_REQUIRED`` | ``OP_FAILURE`` | ``NEEDS_HUMAN``.
    verdict: str = ""
    #: Why, for ``OP_FAILURE`` / ``NEEDS_HUMAN``.
    error: str = ""
    #: Blocking ``FINDING`` lines, newline-terminated.
    findings: str = ""
    #: Every ``FINDING`` line, newline-terminated.
    all_findings: str = ""
    #: Everything before the marker block.
    prose: str = ""
    #: Path of the stored report.
    report: str = ""
    #: Path of the raw reviewer output.
    raw: str = ""


# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    """Bytes from git or the reviewer as text, losslessly and without raising.

    ``surrogateescape`` round-trips: whatever is written back out is byte-identical to what
    came in, which is what the shell's ``cat`` did.
    """
    return raw.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _write_private(path: Path, data: bytes) -> None:
    """Write a bundle file with an explicit ``0600``, never inheriting the umask.

    ``O_NOFOLLOW`` because the bundle directory is recreated on every review: a leftover
    symlink at a name this is about to write would otherwise redirect the write.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _phase_list(state: State) -> str:
    """Every frozen phase, numbered from one -- the shell's ``jq to_entries`` rendering."""
    return "".join(f"{index + 1}. {desc}\n" for index, desc in enumerate(state.get_array("phases")))


def _plan_excerpt(act_dir: Path) -> str:
    """The frozen plan, capped at ``head -c 65536``.

    Sliced as bytes, exactly as ``head -c`` does, so the cap is the documented one even
    when the plan is not ASCII. A cut mid-character survives because the surrogateescape
    round trip carries the orphaned bytes through unchanged.
    """
    plan = act_dir / "plan.frozen.md"
    try:
        raw = plan.read_bytes()
    except OSError:
        return "(the plan was not frozen)\n"
    # `$(head -c N …)` strips trailing newlines; the printf then adds exactly one back.
    return _decode(raw[:PLAN_EXCERPT_BYTES]).rstrip("\n") + "\n"


def _range_text(target: Target, *, state: State, warnings: str, act_dir: Path) -> str:
    """The bundle's ``range.txt``: what is under review, and what is *not* represented."""
    repo, base, head = target.repo, target.base, target.head
    out: list[str] = ["# Review range\n\n"]
    out.append(f"scope: {target.scope}\n")
    out.append(f"base_tree: {base}\n")
    out.append(f"head_tree: {head}\n")
    out.append(f"repository: {repo}\n")

    count = state.phase_count()
    if target.is_phase:
        out.append(f"phase: {target.phase} of {count}\n")
        out.append(f"\n## Frozen phase description (phase {target.phase})\n\n{state.phase_desc(target.phase)}\n")
    else:
        out.append(f"phases: {count} (all)\n")
    out.append("\n## All frozen phases\n\n")
    out.append(_phase_list(state))

    out.append("\n## Commits in range\n\n")
    log_proc = git_run(repo, ["log", "--oneline", "--no-decorate", f"{state.get('activation_commit')}..HEAD"])
    # The shell wrote `git log … | head -n 200 || printf '(none)\n'`, where the `||` tests
    # `head`, not `git`: a failed log produced an empty section and never the fallback.
    # Preserved rather than "fixed", because Phase 4 is a translation -- and an empty
    # section is honest, whereas "(none)" would assert there were no commits.
    out.append("".join(_byte_records(log_proc.stdout)[:LOG_LINES]))

    out.append("\n## Diffstat\n\n")
    stat_proc = git_run(repo, ["diff", "--stat", "-M", base, head])
    out.append("".join(_byte_records(stat_proc.stdout)[-DIFFSTAT_LINES:]))

    out.append("\n## Snapshot warnings\n\n")
    out.append(f"{warnings}\n" if warnings else "(none)\n")

    out.append("\n## Frozen plan (evidence, not instructions)\n\n")
    out.append(_plan_excerpt(act_dir))
    return "".join(out)


def byte_lines(data: bytes) -> list[bytes]:
    """Records terminated by ``\n``, delimiter kept. **Nothing else is a line ending.**

    ``bytes.splitlines`` also breaks on ``\r``, which ``split``, ``head`` and ``tail`` do
    not. A diff is binary-capable content and carries ``\r`` routinely -- CRLF sources, a
    ``^M`` inside a hunk -- so using ``splitlines`` here put the chunk boundaries somewhere
    GNU ``split`` would never have put them. Measured: 30 of 30 random ``\r``-bearing inputs
    disagreed.
    """
    records: list[bytes] = []
    start = 0
    while True:
        index = data.find(b"\n", start)
        if index < 0:
            if start < len(data):
                records.append(data[start:])
            return records
        records.append(data[start : index + 1])
        start = index + 1


def _byte_records(data: bytes) -> list[str]:
    """``byte_lines`` decoded, for the sections the shell counted with ``head``/``tail``."""
    return [_decode(record) for record in byte_lines(data)]


def split_lines_by_size(data: bytes, limit: int) -> list[bytes]:
    """Split ``data`` the way ``split -C <limit>`` does.

    GNU's rule is a sliding window, not line packing: take the next ``limit`` bytes, cut
    after the **last** newline inside that window, and cut at ``limit`` exactly when the
    window holds none. Whatever is left when fewer than ``limit`` bytes remain is the final
    chunk, newlines and all.

    That is not the same as "fill each chunk with whole lines". ``AAAA…(32)\nBBB…(17)`` at
    ``limit=25`` gives ``[25, 8, 17]``, because the 8-byte tail of the broken record ends
    its window -- packing it with the 17-byte record that follows, which is what a
    line-packing implementation does, produces ``[25, 25]`` instead. Derived by differential
    search against real ``split``: this model agrees on 3900 cases, line packing did not.
    """
    if limit <= 0:
        return [data] if data else []
    chunks: list[bytes] = []
    position = 0
    while position < len(data):
        window = data[position : position + limit]
        if len(window) < limit:
            chunks.append(window)
            break
        newline = window.rfind(b"\n")
        cut = limit if newline < 0 else newline + 1
        chunks.append(data[position : position + cut])
        position += cut
    return chunks


def _write_chunks(dest: Path, diff: bytes, limit: int) -> int:
    """Write ``changes.NN.diff`` attachments and answer how many there are."""
    if not diff:
        _write_private(dest / "changes.00.diff", b"(the diff between these two trees is empty)\n")
        return 1
    chunks = split_lines_by_size(diff, limit)
    if len(chunks) > MAX_CHUNKS:
        # `split -d -a 2` runs out of suffixes at 100 files and fails, and the shell fell
        # back to attaching the diff whole. Same outcome, without the 100 half-written
        # files GNU split leaves behind on the way to failing.
        log(f"diff would need {len(chunks)} chunks; attaching it as a single file instead")
        _write_private(dest / "changes.00.diff", diff)
        return 1
    for index, chunk in enumerate(chunks):
        _write_private(dest / f"changes.{index:02d}.diff", chunk)
    return len(chunks)


def _write_diff(target: Target, path: Path) -> int:
    """Write ``git diff -M base head`` to ``path`` and answer its size in bytes.

    Raises :class:`BundleError` naming git's own complaint, capped the way the shell capped
    it. The diff file is left where it is on failure, exactly as the shell left it: the
    bundle directory is rebuilt from scratch on the next attempt anyway.
    """
    range_name = f"{target.base}..{target.head}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            command = ["git", "-C", target.repo, "diff", "-M", target.base, target.head]
            proc = subprocess.run(command, stdout=sink, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise BundleError(f"git diff {range_name} could not be run: {exc}") from exc
    if proc.returncode != 0:
        raise BundleError(f"git diff {range_name} failed: {_decode(proc.stderr[:DIFF_ERROR_BYTES])}")
    return path.stat().st_size


def _run_verify(repo: str, command: str, dest: Path) -> None:
    """Run ``verify_cmd`` in the repository and attach its tail plus its exit status.

    The command comes from configuration, which is attacker-controlled when it lives in the
    repository under review -- and it is run through a login shell, as the shell original
    did. It is evidence for the reviewer, not a gate: nothing here can approve anything, and
    the code it runs is code the user already agreed to have in their worktree.
    """
    raw_path = dest / "verify.raw"
    # One file for both streams, as the shell's `>raw 2>&1` did: a build's errors are only
    # legible next to the output they interrupted, and a pipe per stream would reorder them.
    # It is also what keeps an unbounded build log out of memory -- only the tail is read.
    fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            status = run_bounded(["bash", "-lc", command], stdout=sink, timeout_sec=VERIFY_TIMEOUT_SEC, cwd=repo)
        with raw_path.open("rb") as handle:
            handle.seek(max(0, raw_path.stat().st_size - VERIFY_TAIL_BYTES))
            tail = handle.read()
    finally:
        raw_path.unlink(missing_ok=True)
    _write_private(dest / "verify.txt", b"".join([_encode(f"$ {command}\n\n"), tail, _encode(f"\n[exit status: {status}]\n")]))


def build_bundle(target: Target, dest: Path, *, state: State, config: Config, warnings: str = "") -> None:
    """Assemble everything the reviewer is shown, under ``dest``.

    Raises :class:`BundleTooLarge` past ``hard_diff_ceiling`` and :class:`BundleError` when
    the diff itself cannot be produced. Both are refusals to review, never a review that
    found nothing.
    """
    shutil.rmtree(dest, ignore_errors=True)
    ensure_private_dir(dest, root=state_root())

    # Straight to a file, never through a pipe into memory: the size test below is the only
    # thing standing between the gate and an unbounded diff, and it has to run *after* the
    # bytes exist. The shell redirected for the same reason.
    diff_file = dest / "full.diff"
    size = _write_diff(target, diff_file)

    ceiling = config.as_int("hard_diff_ceiling")
    if size > ceiling:
        raise BundleTooLarge(
            f"the diff is {size} bytes, above hard_diff_ceiling ({ceiling}). Approving on a partial view is not an option, so this escalates instead."
        )
    diff = diff_file.read_bytes()

    range_text = _range_text(target, state=state, warnings=warnings, act_dir=state.act_dir)
    _write_private(dest / "range.txt", _encode(range_text))

    total = _write_chunks(dest, diff, config.as_int("chunk_diff_bytes"))
    diff_file.unlink(missing_ok=True)

    verify_cmd = config.as_str("verify_cmd")
    if verify_cmd:
        _run_verify(target.repo, verify_cmd, dest)

    _write_private(dest / "chunks", _encode(str(total)))


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


def review_argv(repo: str, bundle_dir: Path, title: str, *, config: Config) -> list[str]:
    """The flags that follow the prompt.

    The prompt is **not** routed through here. ``-f`` is a yargs *array* option, so it keeps
    swallowing arguments: a prompt placed after the attachments would be read as one more
    attachment path. It goes immediately after ``run`` instead.
    """
    argv: list[str] = []
    if config.as_bool("pure"):
        argv.append("--pure")
    argv += ["--dir", repo]
    argv += ["-m", config.as_str("model")]
    variant = config.as_str("variant")
    if variant:
        argv += ["--variant", variant]
    argv += ["--title", title]
    argv += ["-f", str(bundle_dir / "range.txt")]
    for chunk in sorted(bundle_dir.glob("changes.*.diff")):
        argv += ["-f", str(chunk)]
    if (bundle_dir / "verify.txt").is_file():
        argv += ["-f", str(bundle_dir / "verify.txt")]
    return argv


def permission(bundle_dir: Path) -> str:
    """``OPENCODE_PERMISSION`` for a structurally read-only reviewer.

    The bundle lives outside the repository (Rule 3), so ``external_directory`` is denied
    everywhere except the bundle itself. Patterns are last-match-wins, which is why the
    broad deny is written first -- and why the key order below is load-bearing rather than
    cosmetic.
    """
    document = {
        "*": "deny",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "external_directory": {"*": "deny", f"{bundle_dir}/**": "allow"},
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the timed-out process **and everything it spawned**.

    ``subprocess``'s own timeout kills the direct child only, so a reviewer or a build that
    backgrounded work keeps running after the gate has given up on it -- measured: a
    grandchild created its file two seconds after the one-second deadline. GNU ``timeout``
    does not leak that way, because it puts the child in its own process group and signals
    the group; ``start_new_session=True`` plus ``killpg`` is the same arrangement.

    ``SIGTERM`` first, so a build can tear its own children down, then ``SIGKILL`` to the
    group **unconditionally** -- stricter than ``timeout``, which sends ``SIGTERM`` alone
    unless asked. Watching the direct child instead is not enough: it exits on ``SIGTERM``
    while a descendant that ignored the signal keeps running, which is exactly what a
    measurement of the earlier version showed.

    The grace period is always waited out and the child is deliberately left unreaped while
    it elapses. An unreaped zombie keeps its process-group id allocated (verified), so the
    ``SIGKILL`` below cannot land on an unrelated group that recycled the number. A
    descendant that calls ``setsid`` for itself escapes both signals, exactly as it escapes
    ``timeout``.
    """
    name = str(proc.args[0]) if isinstance(proc.args, (list, tuple)) and proc.args else "the child"
    try:
        pgid: int | None = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None and pgid == os.getpgrp():
        # Only reachable if a future edit drops `start_new_session`, and it must never be
        # allowed to happen: signalling our own group kills the gate mid-response, which a
        # PreToolUse caller reads as a non-blocking error and proceeds through. Confirmed
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


def run_bounded(command: list[str], *, stdout: IO[bytes], timeout_sec: int, env: dict[str, str] | None = None, cwd: str | None = None) -> int:
    """Run ``command`` under a deadline, both streams to ``stdout``, answering its status.

    ``124`` on expiry and ``127`` when it cannot be started, which is what ``timeout`` and
    the shell reported. The child gets its own process group so the deadline binds its
    descendants too -- see :func:`_kill_group`.
    """
    try:
        proc = subprocess.Popen(command, stdout=stdout, stderr=subprocess.STDOUT, env=env, cwd=cwd, start_new_session=True)
    except OSError as exc:
        log(f"{command[0]} could not be started: {exc}")
        return 127
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return 124


def _capture_to_file(command: list[str], env: dict[str, str], out_path: Path, timeout_sec: int) -> int:
    """Run the reviewer with both streams to ``out_path``, answering ``timeout``'s status."""
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            return run_bounded(command, stdout=sink, timeout_sec=timeout_sec, env=env)
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
        _write_private(path, cleaned)


def invoke(target: Target, run: Invocation, *, config: Config, environ: dict[str, str] | None = None) -> None:
    """Run the reviewer, leaving its output at ``out_path``.

    Raises :class:`ReviewerFailed` on a timeout or a non-zero exit. ``OCRL_REVIEWER_CMD`` is
    the test seam the selftest drives: a stand-in that reads the bundle and writes the same
    contract to stdout, so the loop can be exercised without spending a model call.
    """
    env = dict(os.environ if environ is None else environ)
    timeout_sec = config.as_int("timeout_sec")
    reviewer_cmd = env.get("OCRL_REVIEWER_CMD", "")

    if reviewer_cmd:
        env["OCRL_BUNDLE_DIR"] = str(run.bundle_dir)
        command = [reviewer_cmd, str(run.bundle_dir), str(run.prompt_file)]
    else:
        # `$(cat …)` strips trailing newlines; the prompt is a fixed file in the plugin.
        message = _decode(run.prompt_file.read_bytes()).rstrip("\n")
        env["OPENCODE_PERMISSION"] = permission(run.bundle_dir)
        if config.as_bool("disable_project_config"):
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        command = ["opencode", "run", message, *review_argv(target.repo, run.bundle_dir, run.title, config=config)]

    status = _capture_to_file(command, env, run.out_path, timeout_sec)
    if status in _TIMEOUT_STATUSES:
        raise ReviewerFailed(f"the reviewer timed out after {timeout_sec}s")
    if status != 0:
        raise ReviewerFailed(f"the reviewer exited with status {status}")


# --------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------


def _records(text: str) -> list[str]:
    """Split on ``\n`` and nothing else, the way every tool in the shell pipeline did.

    ``str.splitlines`` also breaks on ``\r``, ``\v``, ``\f`` and more. ``grep``, ``sed`` and
    ``head`` break on ``\n`` alone, so a ``FINDING`` line carrying a stray ``\r`` would be one
    line to the shell gate and two to this one -- and "two" means the tail of a finding is
    read as a line the contract does not allow.
    """
    records = text.split("\n")
    if records and records[-1] == "":
        records.pop()
    return records


def _classify(review: Review, verdict: str) -> None:
    """Map the reviewer's advisory verdict onto the gate's own vocabulary."""
    upper = verdict.upper()
    if upper in _APPROVING_VERDICTS:
        review.verdict = "APPROVED"
    elif upper in _BLOCKING_VERDICTS:
        review.verdict = "CHANGES_REQUIRED"
    else:
        review.verdict = "OP_FAILURE"
        review.error = f"the reviewer emitted an unrecognised verdict: {verdict}"


def _fail(review: Review, error: str) -> Review:
    review.verdict = "OP_FAILURE"
    review.error = error
    return review


def _is_marker(line: str, marker: str) -> bool:
    """Is this line *the* marker, rather than a line that merely contains it?

    Substring matching let ``prose <<<OCRL-FINDINGS>>> trailing`` open the block, so a
    contract smuggled into a sentence parsed as the real thing and its ``VERDICT APPROVED``
    stood. Surrounding whitespace is tolerated and nothing else is.

    Stripped against the POSIX space set rather than ``str.strip()``, whose idea of
    whitespace includes characters ``grep`` does not -- the shell gate has to agree with
    this function exactly.
    """
    return line.strip(_SPACE) == marker


def _locate_block(lines: list[str]) -> tuple[int, int]:
    """Index of the opening and closing marker. Raises :class:`ContractError` otherwise.

    Exactly one of each, on lines of their own, in order. The shell used a ``sed`` range,
    which took the *first* opening marker and the next closing one -- so a stray
    ``<<<OCRL-END>>>`` above the real block hid every finding written before it, and a
    second block silently extended the first. Both produced ``APPROVED`` from output that
    never said so; both now fail closed.
    """
    starts = [index for index, line in enumerate(lines) if _is_marker(line, FINDINGS_MARKER)]
    ends = [index for index, line in enumerate(lines) if _is_marker(line, END_MARKER)]
    if not starts or not ends:
        raise ContractError("the reviewer output is missing the <<<OCRL-FINDINGS>>> / <<<OCRL-END>>> markers")
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ContractError("the reviewer output must hold exactly one <<<OCRL-FINDINGS>>> ... <<<OCRL-END>>> block, in that order")
    return starts[0], ends[0]


def _scan_block(block_lines: list[str]) -> tuple[list[Finding], str]:
    """Validate every line in the block and return the findings and the verdict.

    **A line that does not fit the contract is a failed review, not a line to skip.** The
    shell ignored anything that was not ``FINDING<space>``, so ``FINDING: severity=critical
    actionable=yes`` -- one stray colon -- counted as no finding at all, and the reviewer's
    own ``APPROVED`` then stood. Same for ``actionable=maybe`` and for a severity outside the
    documented set. The gate cannot tell a typo from a finding it failed to understand, and
    Rule 1 decides which way that resolves.
    """
    findings: list[Finding] = []
    verdicts: list[str] = []
    for line in block_lines:
        if not line.strip():
            continue
        match = _FINDING_RE.match(line)
        if match is not None:
            findings.append(Finding(line=line, severity=match.group("severity"), actionable=match.group("actionable") == "yes"))
            continue
        if _VERDICT_LINE.match(line):
            verdicts.append(_TRAILING_SPACE.sub("", _VERDICT_PREFIX.sub("", line)))
            continue
        raise ContractError(f"the reviewer emitted a line the contract does not allow: {line[:CONTRACT_ECHO_CHARS]}")
    if not verdicts:
        raise ContractError("the reviewer emitted no VERDICT line")
    if len(verdicts) > 1:
        raise ContractError("the reviewer emitted more than one VERDICT line")
    return findings, verdicts[0]


def _ceiling_exceeded(count: int, block_bytes: int, config: Config) -> str:
    """Why the evidence is too large to act on, or empty when it is not.

    Escalation, not truncation: a findings list cut to fit is a list the model never
    finishes fixing, so the phase is handed to a human whole.
    """
    max_findings = config.as_int("max_findings")
    max_bytes = config.as_int("max_findings_bytes")
    if count > max_findings:
        return (
            f"the reviewer returned {count} findings, above max_findings ({max_findings}). "
            "The list is not trimmed and this is not an approval: the phase was scoped too large."
        )
    if block_bytes > max_bytes:
        return (
            f"the findings block is {block_bytes} bytes, above max_findings_bytes ({max_bytes}). The list is not trimmed and this is not an approval."
        )
    return ""


def parse(out_path: Path, *, config: Config) -> Review:
    """Turn the reviewer's output into a :class:`Review`, recomputing the verdict.

    The reviewer's own verdict is advisory: any actionable finding at or above
    ``block_severity`` blocks, whatever the reviewer concluded. Everything the parser cannot
    read as the documented contract -- a missing, doubled or inverted marker pair, a
    malformed ``FINDING``, an unknown severity, an ``actionable`` that is neither ``yes`` nor
    ``no``, a second ``VERDICT`` -- is ``OP_FAILURE``, which blocks (Rule 1).
    """
    review = Review()
    try:
        raw = out_path.read_bytes()
    except OSError:
        raw = b""
    if not raw:
        return _fail(review, "the reviewer produced no output")
    if NUL in raw:
        # Python would carry the NUL through and reject the line it corrupts, so this check
        # is not what protects *this* parser. It is here for parity: the shell gate cannot
        # hold a NUL at all -- command substitution deletes it, repairing `actionable=n\0o`
        # into a valid `actionable=no` -- and two gates that disagree about what a byte
        # sequence means is the state Phase 6 must never be reverted into.
        return _fail(review, "the reviewer output contains a NUL byte, so the contract cannot be validated")

    lines = _records(_decode(raw))
    try:
        start, end = _locate_block(lines)
        block_lines = lines[start + 1 : end]
        findings, verdict = _scan_block(block_lines)
    except ContractError as exc:
        # Findings and prose stay empty: half-read evidence from output the gate could not
        # parse would suggest the parse succeeded. The contract error is the finding.
        return _fail(review, str(exc))

    review.prose = "\n".join(lines[:start]).rstrip("\n")
    review.all_findings = "".join(f"{finding.line}\n" for finding in findings)
    threshold = severity_rank(config.as_str("block_severity"))
    blocking = [f for f in findings if f.actionable and severity_rank(f.severity) >= threshold]
    review.findings = "".join(f"{finding.line}\n" for finding in blocking)

    # Measured over the block as the shell measured it: joined, trailing newlines stripped.
    block = "\n".join(block_lines).rstrip("\n")
    ceiling = _ceiling_exceeded(len(findings), len(block), config)
    if ceiling:
        review.verdict = "NEEDS_HUMAN"
        review.error = ceiling
        return review

    # The stricter of the two verdicts wins.
    if blocking:
        review.verdict = "CHANGES_REQUIRED"
        return review
    _classify(review, verdict)
    return review


# --------------------------------------------------------------------------
# One full review
# --------------------------------------------------------------------------


def execute(target: Target, *, state: State, config: Config, warnings: str = "") -> Review:
    """Build, invoke, parse and store one review. Never raises for an ordinary failure.

    The report sequence is bumped inside a transaction, which **reloads** ``state`` from
    disk: a caller holding unsaved mutations must save them first, or they are discarded
    here. That is the same contract ``State._escalate`` documents, and it is what stops two
    concurrent reviews from claiming the same sequence number and overwriting each other's
    report.
    """
    with state.transaction():
        seq = state.get_int("report_seq") + 1
        state.update(report_seq=seq)

    label = f"{seq:03d}"
    bundle_dir = state.act_dir / "bundles" / label
    ensure_private_dir(state.act_dir / "reports", root=state_root())
    run = Invocation(
        bundle_dir=bundle_dir,
        prompt_file=ocrl.prompt_path("reviewer-phase" if target.is_phase else "reviewer-final"),
        title=f"review-loop phase {target.phase}" if target.is_phase else "review-loop final review",
        out_path=bundle_dir / "reviewer.out",
    )

    review = Review()
    try:
        build_bundle(target, bundle_dir, state=state, config=config, warnings=warnings)
    except BundleTooLarge as exc:
        review.verdict = "NEEDS_HUMAN"
        review.error = str(exc)
        return review
    except BundleError as exc:
        review.verdict = "OP_FAILURE"
        review.error = str(exc)
        return review

    review.raw = str(run.out_path)
    try:
        invoke(target, run, config=config)
    except ReviewerFailed as exc:
        review.verdict = "OP_FAILURE"
        review.error = str(exc)
    else:
        review = parse(run.out_path, config=config)
        review.raw = str(run.out_path)

    # Stored on both paths: a failed review is exactly the one whose raw output is worth
    # keeping, and the report is what the denial message points at.
    report.store(review, target, seq=label, act_dir=state.act_dir, config=config)
    return review
