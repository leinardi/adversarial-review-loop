"""The reviewer's output contract: the finding grammar, the verdict the engine recomputes, and
the clarify reply.

**Every failure mode ends in a verdict that is not an approval.** A reviewer that says nothing,
omits the markers, emits a line the contract does not allow or an unrecognised verdict maps to
``OP_FAILURE`` or ``NEEDS_HUMAN``. The reviewer's own verdict is advisory: an actionable finding
at or above ``block_severity`` blocks regardless of what it concluded.
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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from arl.config import Config, late_threshold_rank, severity_rank, threshold_rank
from arl.errors import OcrlError
from arl.util import decode, encode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from arl import harness

__all__ = [
    "END_MARKER",
    "FINDINGS_MARKER",
    "FINDING_RE",
    "LINE_SUFFIX_RE",
    "ROUND_VERDICTS",
    "ClarifyReply",
    "ContractError",
    "Finding",
    "LateScope",
    "Review",
    "RootRange",
    "Target",
    "is_single_stored_line",
    "normalized_file",
    "parse",
    "parse_clarify",
    "records",
    "unknown_alias",
    "valid_retractions",
]


#: The contract the reviewer prompts demand. Both must be present or the output is refused.
FINDINGS_MARKER: Final = "<<<ARL-FINDINGS>>>"


END_MARKER: Final = "<<<ARL-END>>>"


#: POSIX ``[[:space:]]`` in the C locale, spelled out rather than left to ``\s``, which is
#: Unicode-aware in Python and would split on characters ``grep`` treats as ordinary.
_SPACE: Final = " \t\n\r\f\v"


_VERDICT_LINE: Final = re.compile(rf"^[{_SPACE}]*VERDICT[: ]")


_VERDICT_PREFIX: Final = re.compile(rf"^[{_SPACE}]*VERDICT[: ]+")


_TRAILING_SPACE: Final = re.compile(rf"[{_SPACE}]+$")


#: The ``FINDING`` grammar, exactly as ``prompts/reviewer-*.md`` specifies it:
#: ``FINDING severity=<label> actionable=yes|no file=<path[:line]|-> | <detail>``.
#: ``severity`` is one of five documented labels and ``actionable`` one of two, because a
#: field the engine cannot read is a finding it cannot weigh -- and weighing it as "does not
#: block" is exactly the failure-into-approval Rule 1 forbids.
FINDING_RE: Final = re.compile(
    r"^FINDING[ \t]+severity=(?P<severity>info|low|medium|high|critical)"
    r"[ \t]+actionable=(?P<actionable>yes|no)"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)


#: The ``SUPERSEDES`` grammar, exactly as ``prompts/reviewer-phase.md`` specifies it:
#: ``SUPERSEDES round=<n> file=<path[:line]|-> | <why>``. Its own strict regex alongside
#: ``FINDING_RE`` -- an unrecognised line is still a :class:`ContractError` (Rule 1). The
#: ``file=`` clause is the same shape ``FINDING_RE`` accepts, ``-`` included, and captured the
#: same way: :func:`valid_retractions` matches it against a finding's.
_SUPERSEDES_RE: Final = re.compile(
    r"^SUPERSEDES[ \t]+round=(?P<round>[0-9]{1,9})"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)


#: How much of an offending line is echoed back, so a denial names what to fix.
CONTRACT_ECHO_CHARS: Final = 120


#: The one byte the shell gate cannot hold; see :func:`parse`.
NUL: Final = b"\0"


_APPROVING_VERDICTS: Final = frozenset({"APPROVE", "APPROVED", "OK", "PASS"})


_BLOCKING_VERDICTS: Final = frozenset({"CHANGES_REQUIRED", "CHANGES-REQUIRED", "REJECT", "REJECTED", "BLOCK"})


#: The only verdicts ``_publish`` ever records as a round. A value outside this set in a
#: stored entry is tampering -- rendered as ``UNKNOWN`` rather than passed through.
ROUND_VERDICTS: Final = frozenset({"APPROVED", "CHANGES_REQUIRED"})


class ContractError(OcrlError):
    """The reviewer's output is not the documented contract.

    Caught inside :func:`parse` and turned into ``OP_FAILURE``. It exists so that every way
    of failing the contract leaves through one place, rather than each check having to
    remember to return rather than fall through to the verdict.
    """


@dataclass(frozen=True)
class RootRange:
    """One root's part of a review: where it is, and the two trees the review diffs."""

    alias: str
    path: str
    kind: str
    base: str
    head: str


@dataclass(frozen=True)
class Target:
    """What one review is about: the roots and their ranges, and where it sits in the plan.

    Every function here and in :mod:`arl.report` restates these, so they travel as one value
    rather than parallel parameters a call site can transpose.
    """

    #: ``phase`` for one phase's delta, ``final`` for the cumulative review.
    scope: str
    phase: int
    roots: tuple[RootRange, ...] = ()
    #: Where the run lives; the external reviewer runs from here.
    launch_dir: str = ""

    @property
    def is_phase(self) -> bool:
        return self.scope == "phase"

    @property
    def label(self) -> str:
        """How this review is named on disk, in ``round_history`` and in the report heading."""
        return f"phase{self.phase}" if self.is_phase else self.scope

    @property
    def aliases(self) -> frozenset[str]:
        return frozenset(root.alias for root in self.roots)


@dataclass(frozen=True)
class Finding:
    """One validated ``FINDING`` line, kept verbatim alongside its parsed fields."""

    line: str
    severity: str
    actionable: bool
    #: The raw ``file=`` value -- ``path``, ``path:line`` or ``-`` -- exactly as written.
    file: str = "-"


@dataclass
class Review:
    """One review's outcome, recomputed by the engine rather than taken from the reviewer."""

    #: ``APPROVED`` | ``CHANGES_REQUIRED`` | ``OP_FAILURE`` | ``NEEDS_HUMAN``.
    verdict: str = ""
    #: Why, for ``OP_FAILURE`` / ``NEEDS_HUMAN``.
    error: str = ""
    #: Set only when ``verdict == "OP_FAILURE"``: ``"transient"`` (a timeout or a matched
    #: rate/usage-limit signal -- see :func:`arl.runner.classify_op_failure`),
    #: ``"operational"`` (every other non-zero exit, a missing/non-executable binary and a bad
    #: ``--model`` included), ``"contract"`` (the reviewer ran to completion but its output was
    #: not the documented contract -- every :class:`ContractError` path, the NUL refusal, and an
    #: unrecognised ``VERDICT``) or ``"bundle"`` (``arl.bundle.BundleError``, or a reviewer
    #: command that could not be composed). The failure budgets read this: a failed run is *not*
    #: one failure class, and treating it as one would spend the same budget on a missing
    #: ``opencode`` binary as on a genuine rate limit. ``""`` for every other verdict.
    kind: str = ""
    #: Blocking ``FINDING`` lines, newline-terminated.
    findings: str = ""
    #: ``FINDING`` lines that are actionable and at or above ``block_severity`` but did
    #: **not** block this review, newline-terminated -- same shape as ``findings``. Only ever
    #: non-empty under the late-round rule (:class:`LateScope`): from the second round of a
    #: phase on, a finding that is new, outside the paths changed since the previous round,
    #: and below ``late_block_severity`` is deferred rather than blocking. Shown on every
    #: approval path so it is fixed or knowingly carried; it stays in ``round_history`` as
    #: evidence, so a later review of the same phase treats its path as a known finding and
    #: blocks on it. Empty whenever the verdict is not one this review recomputed.
    deferred: str = ""
    #: Every ``FINDING`` line, newline-terminated.
    all_findings: str = ""
    #: Every ``SUPERSEDES`` line, newline-terminated. Recorded only -- it never changes
    #: ``verdict`` (a reversal still blocks exactly as its ``FINDING`` lines say).
    supersedes: str = ""
    #: Rendered by :func:`oscillation.render`, one line per anchor that reappeared or was
    #: named by 2+ ``SUPERSEDES`` lines across this label's ``round_history`` -- engine-computed
    #: text, not reviewer prose. Empty when nothing oscillates. Set after this round's own
    #: entry has been recorded, so it reflects this round too -- unlike
    #: ``_prior_rounds``'s own "## Oscillating points", which by construction only
    #: ever sees rounds before this one. Never changes ``verdict``, same as ``supersedes``.
    oscillating: str = ""
    #: Everything before the marker block.
    prose: str = ""
    #: Path of the stored report.
    report: str = ""
    #: Path of the raw reviewer output.
    raw: str = ""
    #: The repo-supplied review guide this round was composed with, already rendered for
    #: display (``<path> (sha256 <digest>)``), or "" when none was in force. Carried on the
    #: review rather than re-read from state when the report is written, so a later guide
    #: change cannot rewrite what a stored report says an earlier round ran under.
    guide: str = ""
    #: The report sequence allocated for this review, which is also the ``seq`` of the
    #: ``round_history`` entry it records. ``0`` for a ``Review`` that never reserved one.
    seq: int = 0
    #: Path of the **malformed primary transcript** whose findings block this review's lines
    #: were re-emitted from, set only by a successful contract repair. ``raw`` is
    #: then the repair call's own transcript, so a report can show both: the block that was
    #: acted on, and the review it came from. Empty for every ordinary review.
    repaired: str = ""
    #: What this invocation cost, when its harness reports it -- display only, never read by
    #: any branch (:class:`arl.harness.Usage`). ``None`` for a harness with no accounting to
    #: offer, for the ``ARL_REVIEWER_CMD`` test seam, and for every failure path, where the
    #: transcript is deliberately left exactly as the CLI wrote it.
    usage: harness.Usage | None = None


@dataclass(frozen=True)
class LateScope:
    """What may block from the second review round of a phase on.

    Built by :func:`late_scope` for phase reviews only, and only when it can be built *honestly*.
    With a scope in hand, :func:`parse` blocks an actionable finding at or above
    ``block_severity`` when its path is in ``changed_paths``, or in ``prior_files``, or when its
    severity reaches ``late_block_severity`` regardless of path. Anything else is *deferred*:
    reported and recorded, but not blocking this approval.

    ``None`` means the ordinary rule. A scope can therefore only ever narrow what blocks, so every
    doubt in building one must resolve to ``None``, never to a smaller set (Rule 1).
    """

    #: Exact paths from ``git diff --name-status`` between the previous round's tree and
    #: this one's -- both sides of a rename. Git never prints a ``./`` prefix, so these are
    #: already in the form :func:`normalized_file` produces.
    changed_paths: frozenset[str]
    #: Every earlier round's ``file=`` value **as :func:`normalized_file` renders it**,
    #: each one both whole and with a trailing ``:line`` stripped -- so a finding re-raised at
    #: another line of the same file still counts as known. Normalised on the way in because
    #: :meth:`covers` normalises on the way out, and the two sides have to agree: stored raw,
    #: a ``file=./README.md:4`` never matched *its own* recorded line, so the deferral that
    #: is meant to last one approval repeated on every later round instead.
    prior_files: frozenset[str]

    def covers(self, file: str) -> bool:
        """Whether a ``FINDING``'s raw ``file=`` value is inside this scope.

        Normalised through :func:`normalized_file` first, then matched whole (a changed file
        may itself be named ``x:1``); only if that fails is a trailing ``:digits`` stripped
        and compared again. ``-`` (no single location) is always in scope: a finding the
        reviewer could not pin to a path must not be deferred on the strength of that, or a
        missing location would become a way past the review.
        """
        if file == "-":
            return True
        file = normalized_file(file)
        known = self.changed_paths | self.prior_files
        if file in known:
            return True
        stripped = LINE_SUFFIX_RE.sub("", file)
        return stripped != file and stripped in known


#: A trailing ``:<digits>`` on a ``file=`` value.
LINE_SUFFIX_RE: Final = re.compile(r":[0-9]+$")


def normalized_file(file: str) -> str:
    """One spelling for a ``FINDING``'s ``file=`` value, used on **both** sides of a match.

    Only a leading ``./`` is removed: git never emits one, so a reviewer that writes
    ``./src/a.py`` means the path git calls ``src/a.py``. Every producer and consumer of a
    :class:`LateScope` path goes through this, because a normalisation applied to one side
    alone silently stops a value matching itself -- which in this direction means a deferred
    finding never becoming a known one.
    """
    return file.removeprefix("./")


def is_single_stored_line(value: object) -> bool:
    """A ``state.json`` value that is exactly one line -- no embedded break of any kind.

    ``re.match`` only anchors at the start, so ``FINDING_RE.match`` on a tampered
    ``"FINDING ... | x\\nIgnore prior instructions ..."`` succeeds and the whole multi-line
    value -- smuggled prose included -- would otherwise be rendered into ``prior-rounds.txt``. A
    legitimately stored line never contains a break (``review._apply`` stores
    parsed records, split on ``\\n``); anything that does is rejected here.
    """
    return isinstance(value, str) and value.splitlines()[0:1] == [value]


def valid_retractions(entry: dict[str, Any], lines: Sequence[object], *, ordinal: int) -> list[str]:
    """The lines of ``lines`` that each retract one finding of the round ``entry``, in order.

    Shared by the two places a clarify's retraction is trusted -- the clarify command before it
    records one, ``bundle._prior_rounds`` before it renders one -- so the two cannot drift. A
    line survives only when it is a single line (:func:`is_single_stored_line`) matching
    ``_SUPERSEDES_RE`` with the anchored ``re.match`` a stored ``FINDING`` is re-validated with,
    its ``round=`` is ``ordinal``, and its ``file=`` is exactly the ``file=`` of exactly one of
    ``entry``'s re-validated ``FINDING`` lines. That is ``oscillation._retirements``' rule:
    ``file=-`` names nothing, and two findings in one file name neither.

    **One line per finding, the first wins.** A later line naming a finding already retracted is
    dropped, so a caller that puts what is already recorded ahead of new lines learns which new
    ones survive from the tail.

    ``ordinal`` is the round's position as :func:`oscillation.ordinal_of` proves it, never the
    stored ``round`` field.
    """
    stored = entry.get("findings")
    files: list[str] = []
    for line in stored if isinstance(stored, list) else []:
        match = FINDING_RE.match(line) if isinstance(line, str) and is_single_stored_line(line) else None
        if match is not None:
            files.append(match.group("file"))
    kept: list[str] = []
    retracted: set[str] = set()
    for line in lines:
        match = _SUPERSEDES_RE.match(line) if isinstance(line, str) and is_single_stored_line(line) else None
        if match is None or int(match.group("round")) != ordinal:
            continue
        file = match.group("file")
        if file == "-" or files.count(file) != 1 or file in retracted:
            continue
        retracted.add(file)
        kept.append(match.string)
    return kept


def unknown_alias(review: Review, aliases: frozenset[str], *, retired: frozenset[str] = frozenset()) -> str:
    """The first ``file=`` value of ``review`` that names no root, or ``""``.

    Every location in a multi-root review is ``<alias>/<path>`` (``range.txt`` maps each alias to
    its folder), so a finding can never be confused between two roots' ``src/main.py``. A path
    with no known alias cannot be attributed, which is a contract error, not a finding to drop.
    A ``SUPERSEDES`` line may also name a ``retired`` root (one the person removed): it retracts
    an earlier round's finding there, which the contract asks for.
    """
    for block, known in ((review.all_findings, aliases), (review.supersedes, aliases | retired)):
        for line in records(block):
            match = FINDING_RE.match(line) or _SUPERSEDES_RE.match(line)
            file = normalized_file(match.group("file")) if match else "-"
            if file != "-" and file.split("/", 1)[0] not in known:
                return file
    return ""


def records(text: str) -> list[str]:
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
    """Map the reviewer's advisory verdict onto the engine's own vocabulary."""
    upper = verdict.upper()
    if upper in _APPROVING_VERDICTS:
        review.verdict = "APPROVED"
    elif upper in _BLOCKING_VERDICTS:
        review.verdict = "CHANGES_REQUIRED"
    else:
        review.verdict = "OP_FAILURE"
        review.kind = "contract"
        review.error = f"the reviewer emitted an unrecognised verdict: {verdict}"


def _fail(review: Review, error: str) -> Review:
    """Every caller is inside :func:`parse`, i.e. the reviewer ran to completion and its
    output was not the documented contract -- always ``"contract"`` (phase 6)."""
    review.verdict = "OP_FAILURE"
    review.kind = "contract"
    review.error = error
    return review


def _is_marker(line: str, marker: str) -> bool:
    """Is this line *the* marker, rather than a line that merely contains it?

    Substring matching let ``prose <<<ARL-FINDINGS>>> trailing`` open the block, so a
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
    ``<<<ARL-END>>>`` above the real block hid every finding written before it, and a
    second block silently extended the first. Both produced ``APPROVED`` from output that
    never said so; both now fail closed.
    """
    starts = [index for index, line in enumerate(lines) if _is_marker(line, FINDINGS_MARKER)]
    ends = [index for index, line in enumerate(lines) if _is_marker(line, END_MARKER)]
    if not starts or not ends:
        raise ContractError("the reviewer output is missing the <<<ARL-FINDINGS>>> / <<<ARL-END>>> markers")
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ContractError("the reviewer output must hold exactly one <<<ARL-FINDINGS>>> ... <<<ARL-END>>> block, in that order")
    return starts[0], ends[0]


def _scan_block(block_lines: list[str], *, allow_supersedes: bool) -> tuple[list[Finding], list[str], str]:
    """Validate every line in the block and return the findings, the ``SUPERSEDES`` lines and the
    verdict.

    **A line that does not fit the contract is a failed review, not a line to skip.** Ignoring
    unrecognised lines meant ``FINDING: severity=critical actionable=yes`` -- one stray colon --
    counted as no finding at all and the reviewer's own ``APPROVED`` stood. Same for
    ``actionable=maybe`` and a severity outside the documented set. The engine cannot tell a typo
    from a finding it failed to understand, and Rule 1 decides which way that resolves.

    ``allow_supersedes`` is true only for a phase review, the one prompt that documents the line
    and the one invocation shown ``prior-rounds.txt``. For a final review a ``SUPERSEDES`` is an
    unrecognised line and fails the contract. When accepted, the lines are recorded only and never
    touch the verdict.
    """
    findings: list[Finding] = []
    supersedes: list[str] = []
    verdicts: list[str] = []
    for line in block_lines:
        if not line.strip():
            continue
        match = FINDING_RE.match(line)
        if match is not None:
            findings.append(
                Finding(line=line, severity=match.group("severity"), actionable=match.group("actionable") == "yes", file=match.group("file"))
            )
            continue
        if allow_supersedes and _SUPERSEDES_RE.match(line):
            supersedes.append(line)
            continue
        if _VERDICT_LINE.match(line):
            verdicts.append(_TRAILING_SPACE.sub("", _VERDICT_PREFIX.sub("", line)))
            continue
        raise ContractError(f"the reviewer emitted a line the contract does not allow: {line[:CONTRACT_ECHO_CHARS]}")
    if not verdicts:
        raise ContractError("the reviewer emitted no VERDICT line")
    if len(verdicts) > 1:
        raise ContractError("the reviewer emitted more than one VERDICT line")
    return findings, supersedes, verdicts[0]


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

    - **A NUL byte.** Not what protects this parser -- Python carries it through and rejects the
      line it corrupts. It is here because the retired Bash gate could not hold a NUL at all:
      command substitution deleted it, repairing ``actionable=n\\0o`` into a valid
      ``actionable=no``. An explicit refusal keeps that leniency from returning by accident.
    - **Not valid UTF-8.** :func:`arl.util.decode` is ``surrogateescape``, so invalid bytes survive as lone
      surrogates -- fine for bundle files, but a surrogate reaching ``round_history`` cannot be
      encoded when ``state.json`` is saved and would crash the review.
    """
    if NUL in raw:
        return "the reviewer output contains a NUL byte, so the contract cannot be validated"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "the reviewer output is not valid UTF-8, so the contract cannot be validated"
    return ""


def parse(out_path: Path, *, config: Config, allow_supersedes: bool = False, scope: LateScope | None = None) -> Review:
    """Turn the reviewer's output into a :class:`Review`, recomputing the verdict.

    The reviewer's own verdict is advisory: any actionable finding at or above ``block_severity``
    blocks, whatever it concluded. Everything the parser cannot read as the documented contract --
    a missing, doubled or inverted marker pair, a malformed ``FINDING``, an unknown severity, an
    ``actionable`` that is neither ``yes`` nor ``no``, a second ``VERDICT`` -- is ``OP_FAILURE``,
    which blocks (Rule 1).

    ``allow_supersedes`` defaults to false and is true only for a phase review.

    ``scope`` (:class:`LateScope`, phase reviews from their second round on) narrows what blocks:
    a finding that would block under ``block_severity`` alone still needs to be in scope -- a
    changed path, a path an earlier round raised, or a severity at or above
    ``late_block_severity`` -- and is otherwise recorded in ``Review.deferred``. The reviewer's
    ``CHANGES_REQUIRED`` still wins over a deferred-only block set.
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

    lines = records(decode(raw))
    try:
        start, end = _locate_block(lines)
        block_lines = lines[start + 1 : end]
        findings, supersedes, verdict = _scan_block(block_lines, allow_supersedes=allow_supersedes)
    except ContractError as exc:
        # Findings and prose stay empty: half-read evidence from output the engine could not
        # parse would suggest the parse succeeded. The contract error is the finding.
        return _fail(review, str(exc))

    review.prose = "\n".join(lines[:start]).rstrip("\n")
    review.all_findings = "".join(f"{finding.line}\n" for finding in findings)
    # Recorded only -- never consulted below when the verdict is computed.
    review.supersedes = "".join(f"{line}\n" for line in supersedes)
    # `threshold_rank`, not `severity_rank`: an unrecognised *finding* severity must rank
    # highest to guarantee it blocks (Rule 1), but the same rule applied to the *threshold*
    # would do the opposite -- an unknown `block_severity` ranking at 5 would clear almost
    # nothing, silently blocking far less than the default. See `config.threshold_rank`.
    threshold = threshold_rank(config.as_str("block_severity"))
    late_threshold = late_threshold_rank(config)
    blocking: list[Finding] = []
    deferred: list[Finding] = []
    for finding in findings:
        rank = severity_rank(finding.severity)
        if not (finding.actionable and rank >= threshold):
            continue
        # Round 1, a final review, or a scope that could not be built honestly: the ordinary
        # rule. Otherwise the late-round rule -- and the severity floor is checked before the
        # path, so a high/critical finding blocks wherever it is.
        if scope is None or rank >= late_threshold or scope.covers(finding.file):
            blocking.append(finding)
        else:
            deferred.append(finding)
    review.findings = "".join(f"{finding.line}\n" for finding in blocking)
    review.deferred = "".join(f"{finding.line}\n" for finding in deferred)

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


@dataclass(frozen=True)
class ClarifyReply:
    """A clarify reply, split by :func:`parse_clarify`.

    ``prose`` is what the implementing agent is shown. ``supersedes`` holds the retraction block's
    lines, checked against the grammar only -- the clarify command still validates each against
    the round it answers (:func:`valid_retractions`) before recording any. ``problem`` says why a
    block that was there did not validate, and ``supersedes`` is then empty.
    """

    prose: str
    supersedes: tuple[str, ...] = ()
    problem: str = ""


def parse_clarify(text: str) -> ClarifyReply:
    """Split a clarify reply into its prose and its retraction block, if it has one. Never raises.

    A clarify answers in prose and may end with one block holding ``SUPERSEDES`` lines and nothing
    else (``prompts/reviewer-clarify.md``). No marker anywhere is the ordinary reply, all of it
    prose. Once either marker appears, the block has to be exactly what that prompt allows or
    nothing is taken from it: the bytes pass :func:`_byte_contract_violation` (a lone surrogate
    must never reach ``state.json``), there is one marker pair in order (:func:`_locate_block`),
    and every non-blank line matches ``_SUPERSEDES_RE`` with the anchored ``re.match``
    :func:`_scan_block` uses. A ``FINDING`` or a ``VERDICT`` there is a re-review, which a clarify
    is not, so it is a problem rather than a line to skip. So is a block with no line in it.

    **The block must end the reply**: anything but blank lines after ``<<<ARL-END>>>`` is a problem
    too. Text there can correct or contradict the retraction above it -- "on reflection, the
    finding stands" -- and recording the block while dropping that text would report a retraction
    the reviewer took back.

    A problem is not a failed clarify: nothing approves either way, so prose is still returned and
    the caller names the problem. The prose is the text above the block, or the whole reply when no
    block could be located or text follows it, so nothing the reviewer wrote after the block is
    hidden.
    """
    lines = records(text)
    if not any(_is_marker(line, FINDINGS_MARKER) or _is_marker(line, END_MARKER) for line in lines):
        return ClarifyReply(prose=text)
    start: int | None = None
    supersedes: list[str] = []
    try:
        violation = _byte_contract_violation(encode(text))
        if violation:
            raise ContractError(violation)
        start, end = _locate_block(lines)
        if any(line.strip() for line in lines[end + 1 :]):
            # Nothing is set aside as a block when the block does not end the reply: the whole
            # reply, trailing text included, is what the implementing agent is shown.
            start = None
            raise ContractError("the retraction block must end the reply, but text follows <<<ARL-END>>>")
        supersedes = [line for line in lines[start + 1 : end] if line.strip()]
        for line in supersedes:
            if not _SUPERSEDES_RE.match(line):
                raise ContractError(f"a clarify block may hold only SUPERSEDES lines, not: {line[:CONTRACT_ECHO_CHARS]}")
        if not supersedes:
            raise ContractError("the block holds no SUPERSEDES line")
    except ContractError as exc:
        prose = text if start is None else "\n".join(lines[:start]).rstrip("\n")
        return ClarifyReply(prose=prose, problem=str(exc))
    return ClarifyReply(prose="\n".join(lines[:start]).rstrip("\n"), supersedes=tuple(supersedes))
