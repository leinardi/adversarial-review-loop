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

**Session continuity, and the invariant it is built around.** Within one review label
(``phase3``, or ``final``) consecutive reviews continue the same OpenCode session where one
can be found and safely claimed (``session_ref``); a resume or a new phase starts fresh. The
session id travels through ``state.json``, which ``AGENTS.md`` is explicit is not a trust
boundary -- so it must never be able to *authorize* anything. It cannot: **an approving
verdict must come from a session whose entire content the gate created.** When a continued
review returns ``APPROVED``, ``execute`` does not act on it. It runs one more review of the
same bundle in a cold session -- no ``-s``, evidence built from git -- and that cold review's
verdict is the one every caller acts on. The stricter of the two always wins. A tampered
session pointer can therefore make the reviewer hold extra context and produce a verdict that
cannot be an approval; at worst it denies, which the user answers with
``/opencode-review-loop:accept``. See ``docs/security.md`` for the full argument.
"""

from __future__ import annotations

import contextlib
import datetime
import difflib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final

import ocrl
from ocrl import planrev, report
from ocrl.atomic import FILE_MODE, ensure_private_dir
from ocrl.config import Config, severity_rank, threshold_rank
from ocrl.errors import OcrlError
from ocrl.gitsnap import checked_tree, git_run, looks_like_object_id
from ocrl.paths import sha256_hex, state_root
from ocrl.state import State
from ocrl.util import log, now

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for the type checker only
    from ocrl.commands import hooks

__all__ = [
    "BundleError",
    "BundleTooLarge",
    "ContractError",
    "Finding",
    "Invocation",
    "PlanEvidenceCorrupted",
    "Review",
    "ReviewerFailed",
    "SessionRef",
    "Target",
    "build_bundle",
    "byte_lines",
    "capture_session",
    "execute",
    "invoke",
    "parse",
    "permission",
    "review_argv",
    "run_bounded",
    "session_ref",
    "split_lines_by_size",
]

#: How long a `session list` call is given. Metadata, not a model call -- bounded well below
#: the review timeout.
SESSION_LIST_TIMEOUT_SEC: Final = 60

#: A canonical OpenCode session id. Matched before a stored or listed id is ever compared,
#: joined, or shown to a reviewer -- see `_pointer_structurally_usable` and `capture_session`.
_SESSION_ID_RE: Final = re.compile(r"^ses_[A-Za-z0-9]{8,64}$")

#: The contract the reviewer prompts demand. Both must be present or the output is refused.
FINDINGS_MARKER: Final = "<<<OCRL-FINDINGS>>>"
END_MARKER: Final = "<<<OCRL-END>>>"

#: Ceilings the shell expressed as ``head``/``tail`` invocations.
LOG_LINES: Final = 200
DIFFSTAT_LINES: Final = 200
PLAN_EXCERPT_BYTES: Final = 65536
DIFF_ERROR_BYTES: Final = 500

#: Cap on one plan-revision hop's diff in ``range.txt``. Orientation only -- the attachments
#: are the evidence -- so a diff past this is omitted rather than truncated mid-hunk.
PLAN_REVISION_DIFF_BYTES: Final = 16384

#: Cap on *either side's* size before a hop is even attempted, checked separately from and
#: ahead of ``PLAN_REVISION_DIFF_BYTES``. Deliberately more generous than the output cap: a
#: modest edit inside an otherwise sizeable plan still produces a small, useful diff, and only
#: inputs large enough that computing the diff is itself the expensive part should be skipped
#: outright -- see ``_revision_diff``.
PLAN_REVISION_DIFF_INPUT_CEILING: Final = PLAN_REVISION_DIFF_BYTES * 2

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


class PlanEvidenceCorrupted(BundleError):
    """A recorded plan revision could not be verified as itself.

    Separate from :class:`BundleTooLarge` in name only -- both escalate to ``NEEDS_HUMAN``
    rather than counting as an operational failure, because neither is something a retry can
    fix. A missing revision file, a symlink, a containment failure or a hash mismatch means
    the evidence a review would be shown is no longer the evidence a phase was agreed
    against; approving on a substituted ``plan.frozen.md``, or silently skipping the
    attachment, is exactly the failure freezing the plan exists to prevent (see
    ``ocrl.planrev``).
    """


class ReviewerFailed(OcrlError):
    """The reviewer did not run to completion. Always ``OP_FAILURE``, never an approval."""


class ContractError(OcrlError):
    """The reviewer's output is not the documented contract.

    Caught inside :func:`parse` and turned into ``OP_FAILURE``. It exists so that every way
    of failing the contract leaves through one place, rather than each check having to
    remember to return rather than fall through to the verdict.
    """


class _TransactionAborted(Exception):
    """Raised inside a ``state.transaction()`` block to abort it **without a save**.

    ``State.transaction`` calls ``save`` on every clean exit, so a branch that decides not
    to write cannot just ``return`` -- that still rewrites ``state.json``, and a
    content-identical rewrite of a *retired* activation's document is exactly what AGENTS.md
    forbids ("a retired activation's directory is never mutated"). Every fingerprint-mismatch
    and lost-ownership branch in this module raises this instead; each caller catches it
    right outside its own ``with`` block and carries on.
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
    """Where one run of the reviewer reads its bundle and writes its answer.

    ``session_id`` is "" for a fresh run (``--title`` is passed) or a session to continue
    (``-s``, no ``--title``) -- see ``review_argv``. ``capture`` is only ever true for a fresh
    run whose session, once it exists, is eligible to become the phase's continuity pointer;
    a cold confirmation is always ``capture=False``, and so is a fresh run reached because the
    real pointer was claimed by a live owner elsewhere. See ``session_ref``.
    """

    bundle_dir: Path
    prompt_file: Path
    title: str
    out_path: Path
    session_id: str = ""
    capture: bool = True


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
    #: The OpenCode session this review ran in, "" for a cold one.
    session: str = ""
    #: Which round of that session this was. 0 for a cold confirmation -- it is not a round
    #: of the continued session, and is never stored as one (see ``session_ref``).
    round: int = 0
    #: Set only on the *returned* ``Review`` of an approving continued round: the continued
    #: verdict that triggered the cold confirmation, kept so the report can show both. The
    #: returned review is always the cold one -- see ``execute``'s docstring for why that is
    #: the verdict every caller must act on.
    confirmed: Review | None = None


@dataclass(frozen=True)
class SessionRef:
    """What ``session_ref`` decided this review should do about session continuity.

    ``session_id`` is "" for a fresh run. ``claim_id`` is only meaningful when ``session_id``
    is set -- it is what ``execute`` must present back, unchanged, to release the claim or
    store the round result; a mismatch at that point means this call no longer owns the
    pointer and the write is skipped (see the module docstring, "the claim is atomic, owned,
    and tri-state"). ``capturable`` is only meaningful when ``session_id`` is "": whether a
    fresh run's session, once it exists, may become the phase's continuity pointer. ``round``
    is the round this invocation represents, needed before the review even runs (it goes into
    ``range.txt``) -- 1 for any fresh run, one past the stored round for a continued one.

    A ``session_id`` here is a hint the reviewer may hold extra context, never an
    authorization: see the module docstring, "the cold-approval invariant".
    """

    session_id: str
    claim_id: str
    capturable: bool
    round: int


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


def _format_at(at: object) -> str:
    """A ``plan_revisions`` entry's ``at`` (epoch seconds) as UTC, for a human reading range.txt."""
    if isinstance(at, bool) or not isinstance(at, (int, float, str)):
        return "(unknown time)"
    try:
        seconds = int(at)
    except (TypeError, ValueError):
        return "(unknown time)"
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: What every "omitted" outcome says, whichever check triggered it -- the reader only needs
#: one message, and a shared constant keeps the two checks below from drifting apart.
#:
#: Deliberately does **not** say "see the attachment for the full text": the attachment is
#: itself capped at ``PLAN_EXCERPT_BYTES`` (``build_bundle``), and a revision large enough to
#: have its diff omitted (``PLAN_REVISION_DIFF_INPUT_CEILING``) is frequently the same one
#: whose attachment was truncated too -- promising "full text" one section after the revision
#: list marked that same file as cut would contradict it. Point at the attachment without
#: claiming what it contains; the truncation marker on the revision line is what says that.
_DIFF_OMITTED: Final = (
    f"(diff omitted: past {PLAN_REVISION_DIFF_BYTES} bytes; see the plan.revN.md attachments above -- each capped at "
    f"{PLAN_EXCERPT_BYTES} bytes, see the revision list for which ones were truncated)\n"
)


def _revision_diff(prev_content: bytes, curr_content: bytes) -> str:
    """A unified diff between two plan revisions, capped, purely for orientation.

    The numbered ``plan.revN.md`` attachments are the evidence; this is only what makes a
    string of revision metadata legible without opening every attachment by hand. Capped
    independently of ``PLAN_EXCERPT_BYTES`` -- more than one hop can appear in one bundle, and
    each is one ``range.txt`` section rather than a whole attachment, so it is omitted past the
    cap rather than truncated mid-hunk, which would print a diff that lies about its own extent.

    **Checked before ``difflib`` ever runs, not only on its output** -- two separate bounds.
    ``unified_diff`` is driven by ``SequenceMatcher``, which is worst-case quadratic in the
    number of lines, so decoding, splitting and diffing an oversized or adversarial revision
    only to discard the result past the byte cap would still pay that cost on *every* review
    this hop appears in. The input ceiling (``PLAN_REVISION_DIFF_INPUT_CEILING``) is
    deliberately looser than the output cap (``PLAN_REVISION_DIFF_BYTES``) rather than reusing
    it: a one-line edit inside two several-KiB plans produces a tiny diff regardless of how
    large the plans themselves are, and gating solely on input size at the *output* cap would
    omit exactly that useful, small diff. Only content large enough that diffing it is itself
    the expensive part is skipped before ``difflib`` runs at all; the *result* is still capped
    separately below, for the (large-input, small-output-cap-exceeding) hops that ceiling lets
    through but that still produce more text than belongs in ``range.txt``.
    """
    if len(prev_content) > PLAN_REVISION_DIFF_INPUT_CEILING or len(curr_content) > PLAN_REVISION_DIFF_INPUT_CEILING:
        return _DIFF_OMITTED
    prev_lines = _decode(prev_content).splitlines(keepends=True)
    curr_lines = _decode(curr_content).splitlines(keepends=True)
    diff_text = "".join(difflib.unified_diff(prev_lines, curr_lines, fromfile="before", tofile="after"))
    if not diff_text:
        return "(no textual difference)\n"
    if len(_encode(diff_text)) > PLAN_REVISION_DIFF_BYTES:
        return _DIFF_OMITTED
    return diff_text


def _plan_revisions_section(revisions: list[tuple[dict[str, Any], bytes]]) -> str:
    """``## Plan revisions``, only when the plan changed since arming (more than one entry).

    Revision 0 is always recorded, so "more than one entry" is exactly "the plan was revised".
    Each attachment is disclosed by the same numbering ``build_bundle`` writes it under, so the
    reviewer can be told "see plan.rev<n>.md" and find exactly that file. A capped diff for
    every adjacent hop follows the list, purely as orientation -- not a substitute for the
    earlier phase descriptions being reviewed against a plan that changed underneath them,
    which is why every revision, not just the diffs, is attached.

    **The attachments are capped at ``PLAN_EXCERPT_BYTES`` each** (``build_bundle`` writes
    ``content[:PLAN_EXCERPT_BYTES]``, the same cap the active plan's own excerpt already used),
    and that has to be said here rather than implied: claiming a revision was "attached in
    full" when a plan past the cap was silently cut would let the reviewer approve believing
    it saw every historical requirement when it did not. So the cap is disclosed once, and any
    revision it actually cut is marked individually -- not left to be discovered by comparing
    byte counts.
    """
    if len(revisions) <= 1:
        return ""
    out = ["\n## Plan revisions\n\n"]
    out.append(
        f"The plan changed after this activation was armed. Every revision below is attached as "
        f"the numbered plan.revN.md files, each capped at {PLAN_EXCERPT_BYTES} bytes (marked below "
        "where a revision exceeded that and was therefore cut) -- because an earlier phase may "
        "have been reviewed against an earlier one.\n\n"
    )
    for index, (entry, content) in enumerate(revisions):
        truncated = f" -- TRUNCATED at {PLAN_EXCERPT_BYTES} bytes, this is not the complete revision" if len(content) > PLAN_EXCERPT_BYTES else ""
        out.append(
            f"- revision {index}: recorded at phase {entry.get('phase')}, {_format_at(entry.get('at'))} -- see plan.rev{index}.md{truncated}\n"
        )
    for index in range(1, len(revisions)):
        out.append(f"\n### revision {index - 1} -> revision {index}\n\n")
        out.append(_revision_diff(revisions[index - 1][1], revisions[index][1]))
    return "".join(out)


def _manual_accepts_section(state: State) -> str:
    """``## Manually accepted phases`` -- omitted entirely when nothing was ever accepted.

    A phase the user accepted with ``ocrl accept`` passed the commit gate without an
    approving review, and every later review of this activation -- this phase's own next
    round included, and the final cumulative review most of all -- must be told so plainly.
    Silence here would let a reviewer, and a reader of ``COMPLETE``, believe every phase
    passed a gate that one of them did not.
    """
    accepts = state.get_array_of_dicts("manual_accepts")
    if not accepts:
        return ""
    out = ["\n## Manually accepted phases\n\n"]
    out.append(
        "The user manually accepted the phases below with `ocrl accept`, overriding the review gate for "
        "that exact tree. No approving review ran for them.\n\n"
    )
    for entry in accepts:
        phase = entry.get("phase")
        tree = entry.get("tree")
        at = _format_at(entry.get("at"))
        reviews = entry.get("reviews")
        reason = entry.get("reason") or "(none given)"
        out.append(f"- phase {phase}, tree `{tree}`, accepted at {at}, overriding {reviews} prior review(s): {reason}\n")
    return "".join(out)


def _range_text(target: Target, *, state: State, warnings: str, revisions: list[tuple[dict[str, Any], bytes]], round_number: int = 0) -> str:
    """The bundle's ``range.txt``: what is under review, and what is *not* represented."""
    repo, base, head = target.repo, target.base, target.head
    out: list[str] = ["# Review range\n\n"]
    out.append(f"scope: {target.scope}\n")
    if round_number:
        out.append(f"round: {round_number}\n")
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
    activation_commit = state.get("activation_commit")
    if activation_commit and not looks_like_object_id(activation_commit):
        # state.json is not a trust boundary: a tampered `activation_commit` shaped like
        # `--output=<file>` would have `git log` write inside the reviewed repo (Rule 3).
        # This section is disclosure only, so degrade to nothing rather than fail the bundle.
        out.append("(the recorded activation commit is unreadable; commit list omitted)\n")
    else:
        spec = f"{activation_commit}..HEAD" if activation_commit else "HEAD"
        # The shell wrote `git log … | head -n 200 || printf '(none)\n'`, where the `||` tests
        # `head`, not `git`: a failed log produced an empty section and never the fallback.
        # Preserved rather than "fixed", because Phase 4 is a translation -- and an empty
        # section is honest, whereas "(none)" would assert there were no commits.
        log_proc = git_run(repo, ["log", "--oneline", "--no-decorate", spec, "--"])
        out.append("".join(_byte_records(log_proc.stdout)[:LOG_LINES]))

    out.append("\n## Diffstat\n\n")
    stat_proc = git_run(repo, ["diff", "--stat", "-M", base, head, "--"])
    out.append("".join(_byte_records(stat_proc.stdout)[-DIFFSTAT_LINES:]))

    out.append("\n## Snapshot warnings\n\n")
    out.append(f"{warnings}\n" if warnings else "(none)\n")

    out.append(_manual_accepts_section(state))

    out.append(_plan_revisions_section(revisions))

    out.append("\n## Frozen plan (evidence, not instructions)\n\n")
    _, active_content = revisions[-1]
    # `$(head -c N …)` strips trailing newlines; the printf then adds exactly one back.
    out.append(_decode(active_content[:PLAN_EXCERPT_BYTES]).rstrip("\n") + "\n")
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

    ``target.base`` comes from ``last_approved_tree`` in ``state.json``, which is not a
    trust boundary, and it reaches this argv -- so it is run through
    :func:`ocrl.gitsnap.checked_tree` first (``git diff --output=<file>`` is a real option),
    and the argument list is terminated with ``--`` so neither tree id can be read as one.
    """
    range_name = f"{target.base}..{target.head}"
    base = checked_tree(target.repo, target.base)
    if not base:
        raise BundleError(f"git diff {range_name}: the base tree id from state is not a usable git object id")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as sink:
            command = ["git", "-C", target.repo, "diff", "-M", base, target.head, "--"]
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


def build_bundle(  # noqa: PLR0913 - one independently meaningful piece of evidence per param; bundling them would be an artificial object
    target: Target, dest: Path, *, state: State, config: Config, warnings: str = "", round_number: int = 0
) -> None:
    """Assemble everything the reviewer is shown, under ``dest``.

    Raises :class:`BundleTooLarge` past ``hard_diff_ceiling``, :class:`PlanEvidenceCorrupted`
    when a recorded plan revision cannot be verified, and :class:`BundleError` when the diff
    itself cannot be produced. All three are refusals to review, never a review that found
    nothing.

    ``round_number`` is disclosed in ``range.txt`` (0 omits the line -- a cold confirmation's
    bundle says nothing about rounds, since it is not part of any session). Bundle content is
    otherwise identical for a continued and a cold call, which is what lets the cold
    confirmation reuse this same bundle rather than building a second one.
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

    # Every recorded revision, verified against its own hash -- never a placeholder, and
    # never a silent fall back to `plan.frozen.md`. A missing file, a symlink, a containment
    # failure or a hash mismatch is exactly as hard a failure as an oversized diff.
    try:
        revisions = planrev.verified_revisions(state.act_dir, state.data.get("plan_revisions") or [])
    except planrev.EvidenceCorrupted as exc:
        raise PlanEvidenceCorrupted(str(exc)) from exc

    range_text = _range_text(target, state=state, warnings=warnings, revisions=revisions, round_number=round_number)
    _write_private(dest / "range.txt", _encode(range_text))

    # One attachment per revision, numbered exactly as `range.txt`'s disclosure names them --
    # `N` entries in `plan_revisions` produce exactly `N` attachments, `plan.rev0.md` through
    # `plan.rev<N-1>.md`. Driven from the state document, not a directory glob: a glob keyed
    # to the on-disk source names (`plan.frozen.md` for revision 0, `plan.rev<n>.md` after)
    # would silently omit revision 0 under this numbering. Capped at the same
    # `PLAN_EXCERPT_BYTES` the active plan's own excerpt is capped at -- a revision file is
    # untrusted-length input the moment it comes from `state.json`, and attaching it whole
    # would let an oversized one blow out the bundle and the reviewer's context exactly the
    # way an unbounded `plan.frozen.md` would, which is why that one was always capped.
    for index, (_entry, content) in enumerate(revisions):
        _write_private(dest / f"plan.rev{index}.md", content[:PLAN_EXCERPT_BYTES])

    total = _write_chunks(dest, diff, config.as_int("chunk_diff_bytes"))
    diff_file.unlink(missing_ok=True)

    verify_cmd = config.as_str("verify_cmd")
    if verify_cmd:
        _run_verify(target.repo, verify_cmd, dest)

    _write_private(dest / "chunks", _encode(str(total)))


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


#: ``plan.rev<n>.md``, matched to sort attachments numerically -- lexical order would put
#: ``plan.rev10.md`` before ``plan.rev2.md``.
_REVISION_FILE_RE: Final = re.compile(r"plan\.rev(\d+)\.md$")


def _revision_sort_key(path: Path) -> int:
    match = _REVISION_FILE_RE.match(path.name)
    return int(match.group(1)) if match else -1


def _isolation_argv(config: Config) -> list[str]:
    """The flags that keep any reviewer-adjacent OpenCode call structurally isolated.

    Shared by :func:`review_argv` and :func:`_list_sessions`, so a unit test can assert the
    two cannot drift apart -- see that test's own docstring for why this matters: a
    ``session list`` call missing these flags would load the repository under review's own
    OpenCode plugins and project config while running *from inside* that repository, which is
    exactly the boundary the reviewer's own isolation exists to hold.
    """
    return ["--pure"] if config.as_bool("pure") else []


def _isolation_env(config: Config, base: dict[str, str]) -> dict[str, str]:
    """``base``, plus the isolation env vars, iff configured. Never mutates ``base``."""
    env = dict(base)
    if config.as_bool("disable_project_config"):
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return env


def review_argv(repo: str, bundle_dir: Path, title: str, *, config: Config, session_id: str = "") -> list[str]:
    """The flags that follow the prompt.

    The prompt is **not** routed through here. ``-f`` is a yargs *array* option, so it keeps
    swallowing arguments: a prompt placed after the attachments would be read as one more
    attachment path. It goes immediately after ``run`` instead.

    ``--title`` and ``-s`` are mutually exclusive: ``-s <session_id>`` continues a remembered
    session and is passed alone; a fresh run passes ``--title`` instead, and only a fresh run
    -- re-passing a newer-sequence title on a continuation would rename the row the stored id
    was matched against. See ``session_ref``.
    """
    argv: list[str] = [*_isolation_argv(config)]
    argv += ["--dir", repo]
    argv += ["-m", config.as_str("model")]
    variant = config.as_str("variant")
    if variant:
        argv += ["--variant", variant]
    if session_id:
        argv += ["-s", session_id]
    else:
        argv += ["--title", title]
    argv += ["-f", str(bundle_dir / "range.txt")]
    for chunk in sorted(bundle_dir.glob("changes.*.diff")):
        argv += ["-f", str(chunk)]
    for revision_file in sorted(bundle_dir.glob("plan.rev*.md"), key=_revision_sort_key):
        argv += ["-f", str(revision_file)]
    if (bundle_dir / "verify.txt").is_file():
        argv += ["-f", str(bundle_dir / "verify.txt")]
    return argv


def permission(bundle_dir: Path) -> str:
    """``OPENCODE_PERMISSION`` for a structurally read-only reviewer.

    The bundle lives outside the repository (Rule 3), so ``external_directory`` is denied
    everywhere except the bundles root -- ``bundle_dir.parent``, not the activation directory,
    which also holds ``state.json``, ``plan.frozen.md`` and the reports. Widened from a single
    bundle to the whole bundles root so a continued reviewer can re-open paths it remembers
    from an earlier round's bundle; every one of them is still gate-generated evidence only,
    never model output -- see the module docstring, "bundles/ holds gate-generated evidence
    only". Patterns are last-match-wins, which is why the broad deny is written first -- and
    why the key order below is load-bearing rather than cosmetic.
    """
    document = {
        "*": "deny",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "external_directory": {"*": "deny", f"{bundle_dir.parent}/**": "allow"},
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
        if run.session_id:
            env["OCRL_SESSION_ID"] = run.session_id
        command = [reviewer_cmd, str(run.bundle_dir), str(run.prompt_file)]
    else:
        # `$(cat …)` strips trailing newlines; the prompt is a fixed file in the plugin.
        message = _decode(run.prompt_file.read_bytes()).rstrip("\n")
        env = _isolation_env(config, env)
        env["OPENCODE_PERMISSION"] = permission(run.bundle_dir)
        command = [
            "opencode",
            "run",
            message,
            *review_argv(target.repo, run.bundle_dir, run.title, config=config, session_id=run.session_id),
        ]

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


def _byte_contract_violation(raw: bytes) -> str:
    """A reason the raw reviewer bytes fail the contract before any line parsing, or ``""``.

    Two byte-level refusals, both for the same reason the contract must be read strictly:

    - **A NUL byte.** Python would carry it through and reject the line it corrupts, so this
      is not what protects *this* parser on its own. It is here because the retired Bash gate
      could not hold a NUL at all -- command substitution deleted it, repairing
      ``actionable=n\\0o`` into a valid ``actionable=no`` -- and an explicit refusal keeps that
      historical leniency from being reintroduced by accident.
    - **Not valid UTF-8.** ``_decode`` is ``surrogateescape``, so invalid bytes would survive
      as lone surrogates. That is fine for the bundle files (``report.store`` writes them back
      ``surrogateescape``), but a surrogate that reaches ``round_history`` cannot be encoded
      when ``state.json`` is saved and would crash the whole review. The contract is a UTF-8
      text protocol; output that is not valid UTF-8 fails it, exactly like a NUL.
    """
    if NUL in raw:
        return "the reviewer output contains a NUL byte, so the contract cannot be validated"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "the reviewer output is not valid UTF-8, so the contract cannot be validated"
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
    byte_violation = _byte_contract_violation(raw)
    if byte_violation:
        return _fail(review, byte_violation)

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
    # `threshold_rank`, not `severity_rank`: an unrecognised *finding* severity must rank
    # highest to guarantee it blocks (Rule 1), but the same rule applied to the *threshold*
    # would do the opposite -- an unknown `block_severity` ranking at 5 would clear almost
    # nothing, silently blocking far less than the default. See `config.threshold_rank`.
    threshold = threshold_rank(config.as_str("block_severity"))
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
# Session continuity
#
# Everything here is an optimisation hint, never an authorization -- see the module
# docstring's "cold-approval invariant". Every failure mode in this section is `log(...)`
# plus a fresh, uncaptured session; nothing here may ever raise into a review.
# --------------------------------------------------------------------------


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _reclaim_after(config: Config) -> int:
    """How long a claim is honoured before it is considered abandoned.

    ``timeout_sec`` alone is not enough: the claim is taken *before* ``build_bundle`` runs,
    and ``verify_cmd`` -- run inside the bundle build, before the reviewer is ever invoked --
    can itself take up to :data:`VERIFY_TIMEOUT_SEC`. A grace window of ``timeout_sec + 60``
    can then expire while the legitimate owner is still inside its own, still-running
    ``verify_cmd`` plus review, and a second review would reclaim and invoke the same session
    the first is still talking to -- the exact interleaving the claim exists to prevent,
    arriving through a window the grace period did not account for.
    """
    return config.as_int("timeout_sec") + VERIFY_TIMEOUT_SEC + 60


def _claim_is_live(pointer: dict[str, Any], config: Config) -> bool:
    """Is ``pointer`` held by an owner who has not yet had time to finish?

    A pointer carrying one of ``claimed_at``/``claim_id`` without the other is unusable --
    they are written together and cleared together, so a half-true pair means something else
    is already wrong with it. Not live, not trusted.
    """
    claimed_at = pointer.get("claimed_at")
    claim_id = pointer.get("claim_id")
    if not (claimed_at and claim_id):
        return False
    return (now() - _as_int(claimed_at)) <= _reclaim_after(config)


def _unique_title(state: State, target: Target, label: str) -> str:
    """A ``--title`` unique enough that a listing match is reliable. See ``capture_session``."""
    base = f"review-loop phase {target.phase}" if target.is_phase else "review-loop final review"
    fingerprint = sha256_hex(str(state.act_dir))[:8]
    return f"{base} [{fingerprint}/{label}]"


def _same_repo(directory: object, repo: str) -> bool:
    """Canonicalised on both sides, so a symlinked path reads as neither a false mismatch
    nor a false match."""
    return isinstance(directory, str) and os.path.realpath(directory) == os.path.realpath(repo)


def _list_sessions(target: Target, *, config: Config, act_dir: Path, seq: str) -> list[Any] | None:
    """``opencode session list --format json -n 50``, parsed -- or ``None`` on anything else.

    Written to ``act_dir/tmp/session-list-<seq>.json`` and deleted in a ``finally``, never
    inside the bundle: the bundle is what a continued reviewer can read back
    (``permission``), and this listing names every other OpenCode session in the repository --
    ids, titles and all, including the user's own unrelated work.

    ``OCRL_SESSION_LIST_CMD`` is the test seam, parallel to ``OCRL_REVIEWER_CMD``: a stand-in
    that writes the same JSON shape to stdout. When the reviewer seam is active and this one
    is not, the call is skipped entirely rather than reaching a real ``opencode`` -- exactly
    like every other reviewer-adjacent call under that seam.
    """
    reviewer_cmd = os.environ.get("OCRL_REVIEWER_CMD", "")
    session_list_cmd = os.environ.get("OCRL_SESSION_LIST_CMD", "")
    if reviewer_cmd and not session_list_cmd:
        return None

    if session_list_cmd:
        argv = [session_list_cmd]
        env = dict(os.environ)
    else:
        argv = ["opencode", *_isolation_argv(config), "session", "list", "--format", "json", "-n", "50"]
        env = _isolation_env(config, dict(os.environ))

    listing_path = act_dir / "tmp" / f"session-list-{seq}.json"
    try:
        ensure_private_dir(listing_path.parent, root=state_root())
        fd = os.open(listing_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(fd, "wb") as sink:
            status = run_bounded(argv, stdout=sink, timeout_sec=SESSION_LIST_TIMEOUT_SEC, env=env, cwd=target.repo)
        if status != 0:
            log(f"session list exited with status {status}")
            return None
        data = json.loads(listing_path.read_bytes())
        if not isinstance(data, list):
            log("session list output was not a JSON list")
            return None
    except Exception as exc:
        log(f"session list failed: {exc}")
        return None
    finally:
        listing_path.unlink(missing_ok=True)
    return data


def _pointer_structurally_usable(pointer: dict[str, Any], state: State, target: Target) -> bool:
    """The cheap checks -- no subprocess -- that decide whether a listing verify is worth
    running at all."""
    session_id = pointer.get("id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return False
    if pointer.get("label") != target.label:
        return False
    if pointer.get("revisions") != len(state.data.get("plan_revisions") or []):
        return False
    return pointer.get("generation") == state.get_int("activation_generation")


def _exactly_one_match(rows: list[Any], *, session_id: str, title: object, created: object, repo: str) -> bool:
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("id") == session_id
        and row.get("title") == title
        and row.get("created") == created
        and _same_repo(row.get("directory"), repo)
    ]
    return len(matches) == 1


def _try_claim(state: State, *, target: Target, session_id: str, config: Config) -> tuple[str | None, int]:
    """Atomically claim the pointer for ``session_id``, re-verified fresh under the lock.

    Re-checks *identity* (the id, and every structural field :func:`_pointer_structurally_usable`
    covers) against a fresh reload rather than comparing the whole pointer dict against a
    snapshot taken before the (slow) listing verify -- a snapshot comparison would treat any
    change at all as "moved", including a concurrent round on this *same* session completing
    and releasing in the meantime, which only ever touches ``round``/``claimed_at``/``claim_id``.
    Discarding continuity and overwriting that round's own result on a benign advance like
    that is exactly the corruption the claim exists to prevent, arriving through the one
    field this function used to trust from before the lock.

    Returns ``(None, 1)`` when the pointer no longer names this session at all -- a genuinely
    different capture replaced it, or a structural field (label, revisions, generation)
    changed -- treated as "no usable pointer": fresh, capturable. Returns ``("", 1)`` when a
    live owner already holds it: fresh, but **not** capturable -- storing this call's own
    fresh session over a claim someone else is still using would be the same corruption,
    arriving one step later. Otherwise the new claim id and the round to use, read fresh so a
    concurrent round that already completed is built on rather than discarded.

    The read and the write happen in one ``state.transaction()``, deliberately: two reviews
    can overlap (a commit gate and a `gate-stop` phase review), and both reading the same
    unclaimed pointer before either writes would put two ``opencode run -s <id>`` against one
    conversation, interleaving two different prospective trees. The two "no usable claim"
    branches write nothing and abort the transaction rather than resave -- see
    :class:`_TransactionAborted`.
    """
    claimed: str | None = None
    round_number = 1
    try:
        with state.transaction():
            current = state.data.get("reviewer_session")
            current = current if isinstance(current, dict) else {}
            if current.get("id") != session_id or not _pointer_structurally_usable(current, state, target):
                claimed = None
                raise _TransactionAborted
            if _claim_is_live(current, config):
                claimed = ""
                raise _TransactionAborted
            claimed = secrets.token_hex(8)
            round_number = _as_int(current.get("round")) + 1
            current["claimed_at"] = now()
            current["claim_id"] = claimed
            state.data["reviewer_session"] = current
    except _TransactionAborted:
        pass
    return claimed, round_number


def session_ref(state: State, target: Target, *, config: Config) -> SessionRef:
    """Decide whether this review continues a remembered session. Never raises.

    Two phases, deliberately: the listing verify below can take up to
    :data:`SESSION_LIST_TIMEOUT_SEC` and runs with **no lock held**, exactly like every other
    slow operation in this gate. Only the final claim -- a reload, a comparison and a write --
    takes the activation lock, and it is fast (:func:`_try_claim`).

    Reads ``state.data`` as the caller already loaded it, deliberately without a defensive
    reload here: this runs synchronously inside the same call chain that loaded it (no slow
    work has happened yet), and a reload that transiently failed would silently replace a
    document the caller already validated with an empty one -- turning a caller's guarantee
    into corruption for the rest of this review. ``execute`` requires an already-loaded
    ``state``, same as every other reviewer entry point.

    These checks catch a stale id, an accidental collision and a wrong-project match, which
    are the failures that will actually happen -- but they are **not** what makes continuity
    safe, and that has to stay true reading this function in isolation: the safety comes from
    the cold-approval invariant in :func:`execute`, not from anything here. Anything
    unverifiable falls back to a fresh session, never to an error (Rule 1).
    """
    pointer = state.data.get("reviewer_session")
    pointer = pointer if isinstance(pointer, dict) else {}
    if not _pointer_structurally_usable(pointer, state, target):
        return SessionRef(session_id="", claim_id="", capturable=True, round=1)

    session_id = str(pointer["id"])
    rows = _list_sessions(target, config=config, act_dir=state.act_dir, seq=f"verify-{secrets.token_hex(4)}")
    if rows is None or not _exactly_one_match(
        rows, session_id=session_id, title=pointer.get("title"), created=pointer.get("created"), repo=target.repo
    ):
        return SessionRef(session_id="", claim_id="", capturable=True, round=1)

    claim_id, round_number = _try_claim(state, target=target, session_id=session_id, config=config)
    if claim_id is None:
        return SessionRef(session_id="", claim_id="", capturable=True, round=1)
    if claim_id == "":
        return SessionRef(session_id="", claim_id="", capturable=False, round=1)
    return SessionRef(session_id=session_id, claim_id=claim_id, capturable=False, round=round_number)


def _reconfirm_claim(state: State, ref: SessionRef, *, config: Config) -> bool:
    """Re-validate a held claim immediately before ``invoke``, refreshing its lease.

    The claim is taken (in :func:`session_ref`) before :func:`build_bundle` runs, and
    building the bundle -- ordinary git calls today, ``verify_cmd`` when configured -- has no
    fixed upper bound: :func:`ocrl.gitsnap.git_run` is not itself time-boxed. No finite
    padding on the reclaim window (:func:`_reclaim_after`) can cover an unbounded wait, so
    instead of widening it further, ownership is re-checked here, right before the one
    operation the claim actually protects (``-s <id>``), and the lease is refreshed if it is
    still ours. ``False`` means someone else has already reclaimed the pointer -- the caller
    must fall back to a fresh, non-capturable review rather than risking two
    ``opencode run -s <id>`` calls against the same conversation. A no-op, returning ``True``,
    for a review that never held a claim in the first place. When the claim is no longer
    ours the transaction is aborted rather than resaved (:class:`_TransactionAborted`).
    """
    if not ref.session_id:
        return True
    held = False
    try:
        with state.transaction():
            pointer = state.data.get("reviewer_session")
            if not isinstance(pointer, dict) or pointer.get("claim_id") != ref.claim_id:
                raise _TransactionAborted
            held = True
            pointer["claimed_at"] = now()
            state.data["reviewer_session"] = pointer
    except _TransactionAborted:
        pass
    return held


def _downgrade_bundle_round(bundle_dir: Path) -> None:
    """Correct ``range.txt``'s ``round:`` line after a post-build fallback to a fresh review.

    Reached only when :func:`_reconfirm_claim` finds the claim already lost -- rare, and
    never a reason to fail the review over it: this is orientation text, not evidence the
    verdict is computed from, so a failure here is logged and left as it was rather than
    raised. Left uncorrected, the bundle would tell the reviewer it is round N of a
    continuing session while the invocation that follows is cold and carries no such
    history, which is exactly the confusion the continuation paragraph in the prompt exists
    to prevent.
    """
    path = bundle_dir / "range.txt"
    try:
        text = _decode(path.read_bytes())
    except OSError as exc:
        log(f"could not read range.txt to correct its round line: {exc}")
        return
    corrected = re.sub(r"(?m)^round: \d+\n", "round: 1\n", text, count=1)
    if corrected == text:
        return
    try:
        _write_private(path, _encode(corrected))
    except OSError as exc:
        log(f"could not rewrite range.txt's round line: {exc}")


@dataclass(frozen=True)
class _CaptureContext:
    """Everything a fresh run's capture needs to know about the review it belongs to."""

    target: Target
    title: str
    round_number: int


@dataclass(frozen=True)
class _Captured:
    """A fresh run's session, if exactly one row matched. Falsy when nothing did."""

    session_id: str = ""
    created: int = 0

    def __bool__(self) -> bool:
        return bool(self.session_id)


def capture_session(ctx: _CaptureContext, *, config: Config, act_dir: Path, seq: str, started_ms: int) -> _Captured:
    """The session ``opencode session list`` shows for this fresh run's title, or falsy.

    Every failure -- non-zero exit, timeout, unparseable JSON, a row predating this run, no
    match -- is :func:`log` plus the empty result; this must never be able to fail a review.
    **Exactly one** matching row is required: two rows carrying our title inside the window
    means something else created one, so the answer is a cold session, not a guess.
    """
    rows = _list_sessions(ctx.target, config=config, act_dir=act_dir, seq=seq)
    if rows is None:
        return _Captured()
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("title") == ctx.title
        and isinstance(row.get("id"), str)
        and _SESSION_ID_RE.match(row["id"])
        and isinstance(row.get("created"), (int, float))
        and row["created"] >= started_ms
        and _same_repo(row.get("directory"), ctx.target.repo)
    ]
    if len(matches) != 1:
        if matches:
            log(f"capture_session: {len(matches)} rows matched title {ctx.title!r} in the window; not guessing")
        return _Captured()
    row = matches[0]
    return _Captured(session_id=str(row["id"]), created=int(row["created"]))


def _store_captured_session(state: State, ctx: _CaptureContext, captured: _Captured, *, expected: hooks.Activation, config: Config) -> None:
    """Store a fresh, capturable run's session as the phase's new continuity pointer.

    Fingerprinted like every other post-slow-work write: ``expected`` was captured before
    ``invoke`` ran, and a concurrent same-session ``resume --replan`` bumping
    ``activation_generation`` in between must not have this land in a scope that no longer
    applies. On any mismatch: logged, nothing written -- and the transaction is aborted
    rather than resaved, so a cross-session ``resume`` that retired this activation mid-review
    does not have its ``state.json`` rewritten (:class:`_TransactionAborted`). The review's
    own verdict is unaffected either way.

    **Also refuses to overwrite a pointer someone else is actively using.** This call was
    "capturable" because *this* review found no usable pointer to continue -- but a second
    review can have claimed one in the time since (the review itself, between deciding
    "capturable" and this write, is exactly the slow work that window spans). Overwriting a
    still-live claim here would be the same corruption the claim exists to prevent, arriving
    one step later: the owner mid-conversation with that session would have its round result
    silently discarded the moment it tries to release. So the current pointer is re-read and,
    if a live claim holds it, this capture is dropped instead.
    """
    from ocrl.commands import hooks  # noqa: PLC0415 - avoids a top-level import into a hook-only module

    if not captured:
        return
    try:
        with state.transaction():
            current = hooks.activation(state, config)
            if current != expected:
                log("session capture: the activation moved while the review ran; not storing")
                raise _TransactionAborted
            existing = state.data.get("reviewer_session")
            existing = existing if isinstance(existing, dict) else {}
            if _claim_is_live(existing, config):
                log("session capture: another review is actively using the pointer; not overwriting")
                raise _TransactionAborted
            state.data["reviewer_session"] = {
                "label": ctx.target.label,
                "id": captured.session_id,
                "title": ctx.title,
                "created": captured.created,
                "revisions": len(state.data.get("plan_revisions") or []),
                "generation": state.get_int("activation_generation"),
                "round": ctx.round_number,
                "claimed_at": "",
                "claim_id": "",
            }
    except _TransactionAborted:
        pass


def _release_if_claimed(state: State, ref: SessionRef, *, expected: hooks.Activation, config: Config, round_number: int | None) -> None:
    """``_release_claim`` when this call actually holds one; a no-op for a fresh review.

    Called on *every* path out of a review that claimed a continuation -- a bundle build
    failure before ``opencode`` was ever launched, a reviewer that failed to run to
    completion, or an ordinary finished round -- so nothing about how this review ends leaves
    the claim stranded a moment longer than its own lifetime requires. ``round_number=None``
    on the first two: no round of this session actually happened, so the stored round is left
    exactly as it was; only the claim itself is released.
    """
    if ref.session_id:
        _release_claim(state, claim_id=ref.claim_id, round_number=round_number, expected=expected, config=config)


def _release_claim(state: State, *, claim_id: str, round_number: int | None, expected: hooks.Activation, config: Config) -> None:
    """Release the claim, recording the round result iff ``round_number`` is given.

    ``claim_id`` is what makes this safe against the ABA sequence the module docstring
    describes: A's claim expires, B reclaims with a new id, A finally finishes and releases.
    Comparing the token means A's release, arriving after B's, is a no-op rather than an
    overwrite of B's still-live claim. Fingerprinted the same way ``_store_captured_session``
    is, for the same reason -- and, like it, a branch that writes nothing aborts the
    transaction rather than resaving a possibly-retired ``state.json``
    (:class:`_TransactionAborted`).
    """
    from ocrl.commands import hooks  # noqa: PLC0415 - avoids a top-level import into a hook-only module

    try:
        with state.transaction():
            current = hooks.activation(state, config)
            if current != expected:
                log("session claim release: the activation moved while the review ran; not writing")
                raise _TransactionAborted
            pointer = state.data.get("reviewer_session")
            if not isinstance(pointer, dict) or pointer.get("claim_id") != claim_id:
                log("session claim release: no longer the owner of this claim; not writing")
                raise _TransactionAborted
            if round_number is not None:
                pointer["round"] = round_number
            pointer["claimed_at"] = ""
            pointer["claim_id"] = ""
            state.data["reviewer_session"] = pointer
    except _TransactionAborted:
        pass


# --------------------------------------------------------------------------
# One full review
# --------------------------------------------------------------------------


def _run_invocation(target: Target, run: Invocation, *, config: Config) -> tuple[Review, bool]:
    """One invoke()+parse() cycle. The bool says whether the process ran to completion --
    only then is there anything for ``capture_session``/``_release_claim`` to act on."""
    review = Review()
    review.raw = str(run.out_path)
    try:
        invoke(target, run, config=config)
    except ReviewerFailed as exc:
        review.verdict = "OP_FAILURE"
        review.error = str(exc)
        return review, False
    review = parse(run.out_path, config=config)
    review.raw = str(run.out_path)
    return review, True


@dataclass(frozen=True)
class _ReviewRun:
    """Everything about one ``execute()`` call its helpers need, gathered once so each of
    them takes a handful of arguments instead of independently re-deriving or re-threading
    the same half-dozen values."""

    target: Target
    state: State
    config: Config
    label: str
    title: str
    bundle_dir: Path
    raw_dir: Path
    prompt_file: Path
    #: Captured once, before any slow work -- see ``execute``'s own comment on why.
    expected: hooks.Activation


def _settle_pointer(rr: _ReviewRun, ref: SessionRef, *, started_ms: int, invoked: bool) -> str:
    """Release a claimed continuation, or store a fresh capturable one. Called on *every*
    path out of a review that claimed or could capture a continuation, failed or not -- a
    claim left held past a failed invocation is a claim no live retry can actually continue
    (the next attempt would find it "busy" and be forced fresh instead), which defeats the
    reason the claim was taken in the first place.

    ``invoked`` controls only whether a *round* is recorded: a stuck loop turning up
    ``CHANGES_REQUIRED`` every round is exactly what continuity exists to help with, so a
    finished round is settled regardless of its verdict -- but a reviewer that never ran to
    completion produced no round at all, and must not advance the stored counter over one
    that did not happen. Returns the freshly captured session id, if any -- "" otherwise --
    so the caller can record it on a first round's own ``Review`` (see ``execute``).
    """
    if ref.session_id:
        _release_if_claimed(rr.state, ref, expected=rr.expected, config=rr.config, round_number=(ref.round if invoked else None))
        return ""
    if not invoked or not ref.capturable:
        return ""
    ctx = _CaptureContext(target=rr.target, title=rr.title, round_number=ref.round)
    captured = capture_session(ctx, config=rr.config, act_dir=rr.state.act_dir, seq=rr.label, started_ms=started_ms)
    _store_captured_session(rr.state, ctx, captured, expected=rr.expected, config=rr.config)
    return captured.session_id


def _confirm_cold(rr: _ReviewRun, continued: Review) -> Review:
    """The cold-approval invariant: one more, session-less review of the same bundle, in
    place of ``continued`` -- with ``continued`` attached via ``.confirmed`` so the report can
    show both. See ``execute``'s own docstring."""
    cold_run = Invocation(
        bundle_dir=rr.bundle_dir,
        prompt_file=rr.prompt_file,
        title=rr.title,
        out_path=rr.raw_dir / f"{rr.label}-{rr.target.label}-cold.out",
        session_id="",
        capture=False,
    )
    cold, _invoked = _run_invocation(rr.target, cold_run, config=rr.config)
    cold.confirmed = continued
    return cold


def _activation_still_current(rr: _ReviewRun) -> bool:
    """A fresh, lock-free read: does the activation still match ``rr.expected``?

    Best-effort. Used to skip the post-review writes into ``state.act_dir`` (the stored
    report, and -- via :func:`_append_round_history`, which re-checks authoritatively under
    the lock -- the ``round_history`` entry) when a cross-session ``resume`` has retired this
    activation while the review ran. ``build_bundle`` and ``invoke`` wrote earlier and cannot
    be unwound; this keeps everything still in the gate's hands out of the retired directory.
    """
    from ocrl.commands import hooks  # noqa: PLC0415 - avoids a top-level import into a hook-only module

    probe = State(rr.state.worktree, rr.state.session)
    return probe.load() and hooks.activation(probe, rr.config) == rr.expected


def _append_round_history(rr: _ReviewRun, review: Review, *, round_number: int) -> None:
    """Append one ``round_history`` entry for a review that produced a parsed verdict.

    Called only for ``APPROVED`` / ``CHANGES_REQUIRED``. ``OP_FAILURE`` and ``NEEDS_HUMAN``
    are not rounds -- recording them would double-count against phase 5's stall detection
    and phase 6's retry budget. When the cold-approval invariant has already replaced a
    continued ``APPROVED`` with a cold ``CHANGES_REQUIRED``, ``review`` is the cold one by
    the time this runs, so the acted-on (cold) verdict is what is recorded.

    Fingerprint-guarded like :func:`_store_captured_session`: ``rr.expected`` was captured
    before the slow work, so a concurrent same-session ``resume --replan`` bumping
    ``activation_generation``, or a cross-session ``resume`` retiring this activation into
    ``RESUMED`` mid-review, must not have this entry land in a scope that has moved on. The
    check runs inside the transaction, against the freshly reloaded document and under the
    activation lock, so it serialises with a concurrent retirement. On a mismatch it raises
    :class:`_TransactionAborted` rather than returning: ``State.transaction`` saves on a
    clean exit, and a retired activation's ``state.json`` must not be rewritten at all, not
    even a content-identical resave (AGENTS.md).

    Every stored value is either gate-derived (``rr.target``) or the gate's own recomputed
    verdict and finding lines. Finding lines are split with :func:`_records` -- ``\\n`` only,
    never ``str.splitlines`` -- so a ``FINDING`` detail carrying a stray ``\\r`` or a Unicode
    line separator stays the one validated record it was, not two fragments a later
    re-validation would drop.
    """
    from ocrl.commands import hooks  # noqa: PLC0415 - avoids a top-level import into a hook-only module

    state, target, config = rr.state, rr.target, rr.config
    try:
        with state.transaction():
            if hooks.activation(state, config) != rr.expected:
                raise _TransactionAborted
            stored = state.data.get("round_history")
            history = list(stored) if isinstance(stored, list) else []
            history.append(
                {
                    "seq": int(rr.label),
                    "label": target.label,
                    "phase": target.phase,
                    "generation": state.get_int("activation_generation"),
                    "round": round_number,
                    "verdict": review.verdict,
                    "tree": target.head,
                    "base": target.base,
                    "at": now(),
                    "findings": [line for line in _records(review.all_findings) if line],
                    "supersedes": [line for line in _records(str(getattr(review, "supersedes", ""))) if line],
                }
            )
            state.data["round_history"] = history
    except _TransactionAborted:
        log("round history: the activation moved while the review ran; not recording this round")


def execute(target: Target, *, state: State, config: Config, warnings: str = "") -> Review:
    """Build, invoke, parse and store one review. Never raises for an ordinary failure.

    The report sequence is bumped inside a transaction, which **reloads** ``state`` from
    disk: a caller holding unsaved mutations must save them first, or they are discarded
    here. That is the same contract ``State._escalate`` documents, and it is what stops two
    concurrent reviews from claiming the same sequence number and overwriting each other's
    report.

    **The cold-approval invariant lives here.** When the (possibly continued) round below
    returns ``APPROVED`` and it *was* continuing a session, that verdict is never acted on
    directly: :func:`_confirm_cold` runs one more, cold review of the same bundle, and its
    verdict is what this function returns. See the module docstring for why.
    """
    from ocrl.commands import hooks  # noqa: PLC0415 - avoids a top-level import into a hook-only module

    with state.transaction():
        seq = state.get_int("report_seq") + 1
        state.update(report_seq=seq)

    label = f"{seq:03d}"
    bundle_dir = state.act_dir / "bundles" / label
    raw_dir = state.act_dir / "raw"
    ensure_private_dir(state.act_dir / "reports", root=state_root())
    ensure_private_dir(raw_dir, root=state_root())

    # Captured before `session_ref`'s listing verify and before `invoke` -- both are slow,
    # and a concurrent `resume --replan` bumping `activation_generation` in between must not
    # have this review's own capture or claim-release land in a scope that moved on.
    expected = hooks.activation(state, config)
    ref = session_ref(state, target, config=config)
    title = _unique_title(state, target, label)
    rr = _ReviewRun(
        target=target,
        state=state,
        config=config,
        label=label,
        title=title,
        bundle_dir=bundle_dir,
        raw_dir=raw_dir,
        prompt_file=ocrl.prompt_path("reviewer-phase" if target.is_phase else "reviewer-final"),
        expected=expected,
    )

    review = Review()
    try:
        build_bundle(target, bundle_dir, state=state, config=config, warnings=warnings, round_number=ref.round)
    except (BundleTooLarge, PlanEvidenceCorrupted) as exc:
        review.verdict = "NEEDS_HUMAN"
        review.error = str(exc)
        _release_if_claimed(state, ref, expected=expected, config=config, round_number=None)
        return review
    except BundleError as exc:
        review.verdict = "OP_FAILURE"
        review.error = str(exc)
        _release_if_claimed(state, ref, expected=expected, config=config, round_number=None)
        return review

    if not _reconfirm_claim(state, ref, config=config):
        # Lost between the claim and here -- building the bundle has no fixed upper bound, so
        # this window cannot be closed by widening the reclaim timeout alone. Fall back to a
        # fresh, non-capturable round rather than risk `-s <id>` against a conversation this
        # call no longer owns. The bundle was already built disclosing the old (continued)
        # round -- corrected here so the reviewer is not told it is round N of a session this
        # cold invocation carries no history of.
        log(f"session claim: lost ownership before invoking; falling back to a fresh review for {target.label}")
        _downgrade_bundle_round(bundle_dir)
        ref = SessionRef(session_id="", claim_id="", capturable=False, round=1)

    run = Invocation(
        bundle_dir=bundle_dir,
        prompt_file=rr.prompt_file,
        title=title,
        out_path=raw_dir / f"{label}-{target.label}.out",
        session_id=ref.session_id,
        capture=(not ref.session_id) and ref.capturable,
    )

    started_ms = int(time.time() * 1000)
    review, invoked = _run_invocation(target, run, config=config)
    review.session = ref.session_id
    review.round = ref.round

    captured_id = _settle_pointer(rr, ref, started_ms=started_ms, invoked=invoked)
    if captured_id:
        # A fresh round's own session is not known until after it ran -- record it now so
        # this round's report can name the session it just created.
        review.session = captured_id

    if review.verdict == "APPROVED" and ref.session_id:
        review = _confirm_cold(rr, review)

    # `build_bundle` and `invoke` already wrote `bundles/<seq>/` and `raw/<seq>-*` into
    # `state.act_dir`; a cross-session `resume` that retired this activation while `invoke`
    # ran cannot have those unwound (a review holds no lock across its minutes-long run --
    # see AGENTS.md). What *can* still be withheld from the retired directory is everything
    # decided here: the stored report and the `round_history` entry. Both are skipped when a
    # fresh read shows the activation has moved -- `_append_round_history` re-checks under the
    # lock as the authoritative guard, this probe keeps the retired directory clean in the
    # ordinary case and orders the two writes.
    if _activation_still_current(rr):
        # Report first, then history: if `report.store` fails, no `round_history` entry is
        # left pointing at a round with no durable report.
        report.store(review, target, seq=label, act_dir=state.act_dir, config=config)
        if review.verdict in ("APPROVED", "CHANGES_REQUIRED"):
            # A round is a *parsed verdict*. Everything else -- a build failure, a contract
            # error, an evidence ceiling -- appends nothing, which keeps phase 5's stall
            # check and phase 6's retry budget from counting the same stuck phase twice.
            _append_round_history(rr, review, round_number=ref.round)
    else:
        log(f"review for {target.label}: the activation moved after the review ran; not storing its report or recording the round")
    return review
