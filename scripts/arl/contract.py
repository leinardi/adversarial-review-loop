"""The reviewer's output contract: the finding grammar and the verdict the engine recomputes.

**Every failure mode ends in a verdict that is not an approval.** A reviewer that says nothing,
omits the markers, emits a line the contract does not allow or an unrecognised verdict maps to
``OP_FAILURE`` or ``NEEDS_HUMAN``. The reviewer's own verdict is advisory: an actionable finding
at or above ``block_severity`` blocks regardless of what it concluded.

From the second round of a phase, an approval must also leave no earlier blocking finding of the
phase open (:func:`unaccounted`): each one closed by a ``RESOLVED`` naming its id or a
``SUPERSEDES`` naming exactly it, in that round or an earlier one. An approval that skips one is a
contract failure, not an approval.
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from arl.config import Config, late_threshold_rank, severity_rank, threshold_rank
from arl.errors import OcrlError
from arl.util import decode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from arl import harness

__all__ = [
    "CITED_LINE_CAP",
    "END_MARKER",
    "FINDINGS_MARKER",
    "FINDING_RE",
    "LINE_SUFFIX_RE",
    "ROUND_VERDICTS",
    "Closure",
    "ContractError",
    "Finding",
    "LateScope",
    "Ledger",
    "Review",
    "RootRange",
    "Target",
    "citation_notes",
    "cited_line",
    "done_when",
    "finding_id",
    "finding_ids",
    "is_single_stored_line",
    "normalized_file",
    "parse",
    "records",
    "replay",
    "resolved_id",
    "unaccounted",
    "unknown_alias",
    "unknown_resolved",
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


#: A finding's optional suggested check, at the end of its text: `` || done when: <condition>``.
#: The words are matched in any case: ``Done when:`` is the same check, and dropping it would only
#: lose the advice.
_DONE_WHEN_RE: Final = re.compile(rf"\|\|[ \t]*(?i:done when):[ \t]*(?P<condition>[^{_SPACE}].*?)[{_SPACE}]*$")


def done_when(line: str) -> str:
    """The suggested check a ``FINDING`` line ends with, or ``""`` when it names none.

    Read from the finding's text only (after the first ``|``, which ``file=`` cannot hold), so a
    path can never be mistaken for one. A missing or empty condition is not a contract error.
    """
    if FINDING_RE.match(line) is None:
        return ""
    match = _DONE_WHEN_RE.search(line.split("|", 1)[1])
    return match.group("condition") if match else ""


def finding_id(order: int, number: int) -> str:
    """The id of the ``number``-th finding of round ``order`` of a stage: ``r<order>.<number>``.

    Given by the engine, never written by the reviewer: two findings may share a ``file=``, so a
    location cannot name one. ``order`` is the round's position among its stage's recorded rounds,
    ``number`` the finding's position in that round's block, both from 1.
    """
    return f"r{order}.{number}"


def finding_ids(order: int, findings: str, lines: str) -> list[tuple[str, str]]:
    """Each of ``lines`` with its id, ``findings`` being the whole block those lines were taken
    from in order (``Review.all_findings``). A line repeated in the block takes its ids in turn."""
    taken: dict[str, list[str]] = {}
    for number, line in enumerate(records(findings), start=1):
        taken.setdefault(line, []).append(finding_id(order, number))
    return [(taken[line].pop(0) if taken.get(line) else "", line) for line in records(lines)]


#: The ``SUPERSEDES`` grammar, exactly as ``prompts/reviewer-phase.md`` specifies it:
#: ``SUPERSEDES round=<n> file=<path[:line]|-> | <why>``. Its own strict regex alongside
#: ``FINDING_RE`` -- an unrecognised line is still a :class:`ContractError` (Rule 1). The
#: ``file=`` clause is the same shape ``FINDING_RE`` accepts, ``-`` included, and captured the
#: same way.
_SUPERSEDES_RE: Final = re.compile(
    r"^SUPERSEDES[ \t]+round=(?P<round>[0-9]{1,9})"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)


#: The ``RESOLVED`` grammar, exactly as ``prompts/reviewer-phase-late.md`` specifies it:
#: ``RESOLVED id=r<round>.<n> | <evidence>``. The reviewer's statement that an earlier finding,
#: named by its engine-given id (:func:`finding_id`), is gone, with what in the code shows it.
#: Allowed only from the second round of a phase; recorded, and read for nothing but
#: :func:`unaccounted`. It never changes what blocks.
_RESOLVED_RE: Final = re.compile(rf"^RESOLVED[ \t]+id=(?P<id>r[0-9]{{1,9}}\.[0-9]{{1,9}})[ \t]*\|[ \t]*[^{_SPACE}]")


def resolved_id(line: object) -> str:
    """The finding id a stored or parsed ``RESOLVED`` line names, or ``""`` when it is not one."""
    if not is_single_stored_line(line):
        return ""
    match = _RESOLVED_RE.match(str(line))
    return match.group("id") if match else ""


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
    #: The reviewer's suggested check that the defect is gone (`` || done when: <condition>``),
    #: or ``""``. Advice to the implementer and to the next round, never a ceiling: a later round
    #: judges the defect itself, and nothing here is read for a verdict.
    done_when: str = ""


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
    #: This round's position among its stage's recorded rounds, from 1, once it is recorded: what
    #: its findings' ids (:func:`finding_id`) are numbered from. ``0`` before that.
    ordinal: int = 0
    #: What the engine adds for Claude, one line each, newline-terminated: a ``VERDICT`` it
    #: supplied, a cited line past the end of its file. Shown, recorded in the report, and never
    #: read for the verdict.
    notes: str = ""
    #: Every ``SUPERSEDES`` line, newline-terminated. Recorded only -- it never changes
    #: ``verdict`` (a reversal still blocks exactly as its ``FINDING`` lines say).
    supersedes: str = ""
    #: Every ``RESOLVED`` line, newline-terminated: the reviewer's statement that an earlier
    #: finding is gone. Never changes what blocks; read only by :func:`unaccounted`.
    resolved: str = ""
    #: The ids of the earlier blocking findings an approval left open, set only
    #: on the contract failure :func:`unaccounted` causes: what the repair call is asked about.
    unaccounted: tuple[str, ...] = ()
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


def unknown_resolved(review: Review, known: frozenset[str]) -> str:
    """The first id a ``RESOLVED`` line of ``review`` names that is not in ``known``, or ``""``.

    ``known`` is every finding id of the earlier recorded rounds of this review. A statement about a
    finding that does not exist cannot be weighed, so it is a contract error, as a ``file=`` naming
    no root is.
    """
    for line in records(review.resolved):
        ident = resolved_id(line)
        if ident not in known:
            return ident or line[:CONTRACT_ECHO_CHARS]
    return ""


@dataclass(frozen=True)
class Closure:
    """How an earlier finding was closed: by which round, and the line that closed it."""

    #: The ordinal of the recorded round that closed it, ``0`` for the review being read.
    by: int
    #: ``RESOLVED`` or ``SUPERSEDES``.
    kind: str
    #: The closing line, verbatim: the reviewer's evidence or reason, never an instruction.
    line: str


@dataclass
class Ledger:
    """A stage's findings after :func:`replay`: which blocking ones are open, and how the closed
    ones were closed. The one account both the engine's check and ``prior-rounds.txt`` read."""

    #: Every open blocking finding, by id in the order raised: the stored line (``""`` when it is
    #: not a valid ``FINDING``).
    open: dict[str, str] = field(default_factory=dict)
    #: How each closed finding was closed, by id (blocking or not).
    closed: dict[str, Closure] = field(default_factory=dict)
    #: For each round ordinal, each normalised ``file=`` value of its findings with their ids.
    files: dict[int, dict[str, list[str]]] = field(default_factory=dict)

    def close(self, resolved: object, supersedes: object, *, by: int) -> None:
        """Apply one round's ``RESOLVED`` and ``SUPERSEDES`` lines (stored lists, re-validated).

        A ``RESOLVED`` closes the id it names. A ``SUPERSEDES round=<n> file=<v>`` closes a finding
        of round ``n`` only when exactly one finding of that round, blocking or not, has that exact
        ``file=`` value (normalised, the line included): ``-`` names nothing, and a value two
        findings share names neither. Anything else closes nothing.
        """
        for line in resolved if isinstance(resolved, list) else []:
            ident = resolved_id(line)
            if ident:
                self._closed(ident, Closure(by=by, kind="RESOLVED", line=str(line)))
        for line in supersedes if isinstance(supersedes, list) else []:
            match = _SUPERSEDES_RE.match(line) if is_single_stored_line(line) else None
            if match is None:
                continue
            file = normalized_file(match.group("file"))
            named = self.files.get(int(match.group("round")), {}).get(file, []) if file != "-" else []
            if len(named) == 1:
                self._closed(named[0], Closure(by=by, kind="SUPERSEDES", line=str(line)))

    def _closed(self, ident: str, closure: Closure) -> None:
        if ident in self.closed:
            return
        known = any(ident in ids for files in self.files.values() for ids in files.values()) or ident in self.open
        if known:
            self.closed[ident] = closure
            self.open.pop(ident, None)


def replay(rounds: object, *, threshold: int) -> Ledger:
    """Replay a stage's recorded ``round_history`` entries, in order, into a :class:`Ledger`.

    Each round first closes what its stored ``resolved`` and ``supersedes`` lines name exactly
    (:meth:`Ledger.close`), then opens its own blocking findings (``actionable=yes`` at or above
    ``threshold``, a ``threshold_rank``). A round's own findings never close anything: a defect
    raised in round 1 and again in round 2 has two ids, and both must be closed. A stored line that
    is not a valid ``FINDING`` cannot be shown not to block, so it opens and can be closed only by
    its id (Rule 1). An entry recorded before rounds kept ``resolved`` closes nothing by id.
    """
    ledger = Ledger()
    for order, raw in enumerate(rounds if isinstance(rounds, list) else [], start=1):
        entry = raw if isinstance(raw, dict) else {}
        ledger.close(entry.get("resolved"), entry.get("supersedes"), by=order)
        stored = entry.get("findings")
        files: dict[str, list[str]] = {}
        for number, line in enumerate(stored if isinstance(stored, list) else [], start=1):
            ident = finding_id(order, number)
            match = FINDING_RE.match(str(line)) if is_single_stored_line(line) else None
            if match is not None:
                files.setdefault(normalized_file(match.group("file")), []).append(ident)
            if match is None or (match.group("actionable") == "yes" and severity_rank(match.group("severity")) >= threshold):
                ledger.open[ident] = str(line) if match is not None else ""
        ledger.files[order] = files
    return ledger


def unaccounted(review: Review, rounds: object, *, threshold: int) -> list[str]:
    """The ids of the stage's earlier blocking findings still open once ``review`` is read.

    ``rounds`` are the stage's recorded ``round_history`` entries, replayed by :func:`replay`;
    ``review``, an approval, then closes an open finding only with a ``RESOLVED`` naming its id or a
    ``SUPERSEDES`` naming exactly it (:meth:`Ledger.close`). Its ``FINDING`` lines close nothing.
    """
    ledger = replay(rounds, threshold=threshold)
    ledger.close(records(review.resolved), records(review.supersedes), by=0)
    return list(ledger.open)


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


def _scan_block(block_lines: list[str], *, allow_supersedes: bool, allow_resolved: bool) -> tuple[list[Finding], list[str], list[str], str]:
    """Validate every line in the block and return the findings, the ``SUPERSEDES`` lines, the
    ``RESOLVED`` lines and the verdict.

    **A line that does not fit the contract is a failed review, not a line to skip.** Ignoring
    unrecognised lines meant ``FINDING: severity=critical actionable=yes`` -- one stray colon --
    counted as no finding at all and the reviewer's own ``APPROVED`` stood. Same for
    ``actionable=maybe`` and a severity outside the documented set. The engine cannot tell a typo
    from a finding it failed to understand, and Rule 1 decides which way that resolves.

    ``allow_supersedes`` is true only for a phase review, the one prompt that documents the line
    and the one invocation shown ``prior-rounds.txt``. For a final review a ``SUPERSEDES`` is an
    unrecognised line and fails the contract. When accepted, the lines are recorded only and never
    touch the verdict. ``allow_resolved`` is the same for ``RESOLVED``, true only from the second
    round of a phase, the one prompt that documents it.
    """
    findings: list[Finding] = []
    supersedes: list[str] = []
    resolved: list[str] = []
    verdicts: list[str] = []
    for line in block_lines:
        if not line.strip():
            continue
        match = FINDING_RE.match(line)
        if match is not None:
            findings.append(
                Finding(
                    line=line,
                    severity=match.group("severity"),
                    actionable=match.group("actionable") == "yes",
                    file=match.group("file"),
                    done_when=done_when(line),
                )
            )
            continue
        if allow_supersedes and _SUPERSEDES_RE.match(line):
            supersedes.append(line)
            continue
        if allow_resolved and _RESOLVED_RE.match(line):
            resolved.append(line)
            continue
        if _VERDICT_LINE.match(line):
            verdicts.append(_TRAILING_SPACE.sub("", _VERDICT_PREFIX.sub("", line)))
            continue
        raise ContractError(f"the reviewer emitted a line the contract does not allow: {line[:CONTRACT_ECHO_CHARS]}")
    if len(verdicts) > 1:
        raise ContractError("the reviewer emitted more than one VERDICT line")
    # No VERDICT is decided by the caller: a finding that blocks makes it moot, nothing else does.
    return findings, supersedes, resolved, verdicts[0] if verdicts else ""


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


def parse(out_path: Path, *, config: Config, allow_supersedes: bool = False, allow_resolved: bool = False, scope: LateScope | None = None) -> Review:
    """Turn the reviewer's output into a :class:`Review`, recomputing the verdict.

    The reviewer's own verdict is advisory: any actionable finding at or above ``block_severity``
    blocks, whatever it concluded. Everything the parser cannot read as the documented contract --
    a missing, doubled or inverted marker pair, a malformed ``FINDING``, an unknown severity, an
    ``actionable`` that is neither ``yes`` nor ``no``, a second ``VERDICT`` -- is ``OP_FAILURE``,
    which blocks (Rule 1).

    A block with no ``VERDICT`` line is ``CHANGES_REQUIRED`` when a finding blocks, since the
    engine's verdict would be that whatever the reviewer had written, with a note saying so. With
    nothing blocking it stays a contract failure: no verdict is never an approval.

    ``allow_supersedes`` defaults to false and is true only for a phase review; ``allow_resolved``
    likewise, from its second round on. Whether a ``RESOLVED`` id names a finding, and whether an
    approval leaves an earlier blocking finding open (:func:`unaccounted`), is the caller's to check: both
    need the run's history.

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
        findings, supersedes, resolved, verdict = _scan_block(block_lines, allow_supersedes=allow_supersedes, allow_resolved=allow_resolved)
    except ContractError as exc:
        # Findings and prose stay empty: half-read evidence from output the engine could not
        # parse would suggest the parse succeeded. The contract error is the finding.
        return _fail(review, str(exc))

    review.prose = "\n".join(lines[:start]).rstrip("\n")
    review.all_findings = "".join(f"{finding.line}\n" for finding in findings)
    # Recorded only -- never consulted below when the verdict is computed.
    review.supersedes = "".join(f"{line}\n" for line in supersedes)
    review.resolved = "".join(f"{line}\n" for line in resolved)
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

    return _concluded(review, verdict, blocking=len(blocking))


def _concluded(review: Review, verdict: str, *, blocking: int) -> Review:
    """The stricter of the reviewer's verdict and the engine's wins; no verdict approves nothing."""
    if blocking:
        review.verdict = "CHANGES_REQUIRED"
        if not verdict:
            review.notes += f"the reviewer gave no VERDICT line; its {blocking} blocking finding(s) make this CHANGES_REQUIRED\n"
        return review
    if not verdict:
        # Findings and prose dropped as for every other contract failure.
        return _fail(Review(), "the reviewer emitted no VERDICT line, and no finding blocks")
    _classify(review, verdict)
    return review


#: A cited line number at or past this is past the end of any file the engine would count;
#: :func:`cited_line` answers it in place of the number written. Converting thousands of digits
#: raises, and an advisory check must never stop a review from being recorded.
CITED_LINE_CAP: Final = 10**18


def cited_line(file: str) -> tuple[str, int] | None:
    """The path and line a ``file=`` value cites, or ``None`` when it names no line.

    A line number of more digits than :data:`CITED_LINE_CAP` has is answered as the cap.
    """
    file = normalized_file(file)
    match = LINE_SUFFIX_RE.search(file)
    if file == "-" or match is None:
        return None
    digits = match.group()[1:].lstrip("0") or "0"
    number = int(digits) if len(digits) < len(str(CITED_LINE_CAP)) else CITED_LINE_CAP
    return file[: match.start()], number


def citation_notes(findings: str, line_count: Callable[[str], int | None]) -> str:
    """A note for each finding whose cited line is past the end of its file, newline-terminated.

    ``line_count`` answers how many lines the file at an ``<alias>/<path>`` has, or ``None`` when
    the engine cannot tell (no such file, outside its root, too large): then nothing is said. The
    finding itself stands either way, blocking as it did: a wrong line number is a reviewer's slip
    about where, not evidence that nothing is there (Rule 1).
    """
    notes: list[str] = []
    for line in records(findings):
        match = FINDING_RE.match(line)
        cited = cited_line(match.group("file")) if match else None
        if cited is None:
            continue
        path, number = cited
        length = line_count(path)
        if length is not None and number > length:
            which = f"line {number}" if number < CITED_LINE_CAP else "a line number"
            notes.append(f"{path}: cited {which} is past the end of the file ({length} lines); locate it by the description\n")
    return "".join(notes)
