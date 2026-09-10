"""Detecting oscillation across ``round_history`` -- pure functions, no I/O.

A phase can flip a design point across rounds without ever saying so: round 1 blocks on
missing warn-before, round 2 on missing warn-after instead, round 3 on "needs both". Nothing
in the ordinary evidence says "this is a reversal", so the gate cannot tell a genuinely new
finding from a rehash. This module answers that from ``round_history`` alone.

A finding's **anchor** is what it is *about*: its ``file`` value with any trailing ``:line``
stripped, paired with its ``severity``. Three questions are asked over anchors:

- :func:`reversals` -- raised, absent for a later round, raised again (``reappeared``); or the
  anchor's file named by a *valid retirement* in two or more distinct rounds
  (``supersedes_rounds``), which catches the warn-before/warn-after case where the anchor never
  actually disappears;
- :func:`persisting` -- the same anchor raised in every one of the last ``stall_rounds``
  consecutive rounds.

**Both ask only about findings a round still stands behind**, and the two signals are
complementary, which a change must keep: retract properly and it is not a reappearance, but
every retraction feeds ``supersedes_rounds``; drop a finding silently and re-raise it and
``reappeared`` catches it. Retiring is never free, so neither can be dodged by leaning on the
other. An anchor no round stands behind is dropped entirely -- nothing blocks on it, so there
is no disagreement to escalate.

**Only a finding that can block raises an anchor**: ``actionable=yes`` and at or above the
caller's ``block_severity``. Three rounds of ``severity=info actionable=no`` is a reviewer
repeating a remark, and escalating it spends a human interrupt on a phase that was never
blocked -- a live run did exactly that. ``late_block_severity`` deliberately does not enter:
the late-round rule only ever applies to a finding that is *new*.

The filter is applied where anchors and the reversal count are collected, never inside
:func:`_parsed_findings`: position in that list is the identity a retirement consumes, so
dropping entries there would change which ``SUPERSEDES`` lines validate. Retirement *identity*
therefore covers every finding while both *signals* count only blocking ones.

**A ``SUPERSEDES`` line retires one specific earlier finding, and counting lines is not
counting reversals.** ``SUPERSEDES round=N file=F`` in round *r* retires the ``FINDING`` of
round *N* whose ``file=`` is **exactly** ``F``. Five things make it retire nothing, each a real
false positive: ``N`` naming no earlier round; ``F`` matching no finding (``file=-`` included);
``F`` matching two or more, so which was reversed is unknowable; the finding already retired,
since retirement is **consumptive** and ``prior-rounds.txt`` keeps showing a reversal already
made; and the round numbering not being provably the one the reviewer was shown -- see
:func:`_ordered_rounds`.

Neither signal changes a verdict on its own. ``round_history`` is not a trust boundary, and
the guarantee here is narrow: a **malformed** field -- a non-string, a value carrying an
embedded newline, a line that does not fully re-validate against the grammar -- is silently
excluded rather than raising, so smuggled text can never reach a reader, and an invented
``SUPERSEDES`` can only ever retire a finding that was never reversed, which hides a stall
rather than inventing one. That is also why the round numbering must be *provable* before any
``SUPERSEDES`` is read: a renumbering could manufacture a retirement. **A well-formed edit is
not covered.** Anything that can write ``round_history`` can write entries that spell out a
genuine A / absent / A history and force a ``NEEDS_HUMAN`` -- an escalation, never an
approval, which is the direction this module is allowed to be wrong in. See
``docs/design/state-fields.md``.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from arl.config import severity_rank, threshold_rank

__all__ = ["Anchor", "OscillationPoint", "PersistingPoint", "persisting", "render", "render_persisting", "reversals"]

#: POSIX ``[[:space:]]`` in the C locale, spelled out rather than left to ``\s`` -- the same
#: reasoning and the same literal class as ``reviewer._SPACE``.
_SPACE: Final = " \t\n\r\f\v"

#: The full ``FINDING`` / ``SUPERSEDES`` grammar, byte-for-byte the same character classes
#: as ``reviewer._FINDING_RE`` / ``reviewer._SUPERSEDES_RE``, just with a named ``file``
#: group added (and a ``round`` group on ``_SUPERSEDES_RE``) -- the reviewer's own regexes
#: validate the grammar but never capture the fields out. This module keeps its own copy rather than importing ``reviewer``'s:
#: ``reviewer.py`` is this module's caller (``_prior_rounds_section``, ``execute``), and
#: importing back would cycle. Matching only a loose ``severity=`` / ``file=...|`` fragment
#: (an earlier version of this module did) accepts a line like
#: ``FINDING severity=medium garbage file=x.py | fake`` -- missing ``actionable=``, extra
#: prose in between -- as a real anchor. Requiring the *whole* grammar, anchored at the
#: start exactly as the reviewer's own parser requires it, is what keeps a stored line from
#: fabricating a reversal it was never validated as being.
_FINDING_RE: Final = re.compile(
    r"^FINDING[ \t]+severity=(?P<severity>info|low|medium|high|critical)"
    r"[ \t]+actionable=(?P<actionable>yes|no)"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)
_SUPERSEDES_RE: Final = re.compile(
    r"^SUPERSEDES[ \t]+round=(?P<round>[0-9]{1,9})"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)

#: Line numbers move; the file does not. Stripped from the end of a ``file=`` value before
#: it becomes part of an anchor.
_LINE_SUFFIX_RE: Final = re.compile(r":[0-9]+$")


def _is_single_line(value: object) -> bool:
    """A stored value that is exactly one ``_records`` record: no embedded ``\\n``.

    Mirrors ``reviewer._records`` -- the only place a ``round_history`` finding/supersedes
    line is ever split going in -- which splits on ``\\n`` alone, deliberately not
    ``str.splitlines()``'s broader break set (``\\r``, ``\\v``, ``\\f``, ...): "``grep``,
    ``sed`` and ``head`` break on ``\\n`` alone, so a ``FINDING`` line carrying a stray
    ``\\r`` would be one line to the shell gate and two" to ``splitlines()``. A legitimately
    stored CRLF-terminated finding is one record by that contract -- checking the broader
    set here would silently drop it from oscillation detection. A value that still has an
    embedded ``\\n`` is real tampering (``_records`` would have split it into two stored
    entries) and stays rejected; ``.match()`` below only anchors at the start, so a
    multi-record value smuggled into one array element must never reach it.
    """
    return isinstance(value, str) and "\n" not in value


def _anchor_file(raw: str) -> str:
    return _LINE_SUFFIX_RE.sub("", raw.strip())


@dataclass(frozen=True)
class Anchor:
    """What a finding is about -- stable across rounds even as its detail text changes."""

    file: str
    severity: str


@dataclass(frozen=True)
class _Parsed:
    """One stored ``FINDING`` line that fully re-validated, kept four ways.

    ``file`` is the **exact** ``file=`` value, ``:line`` suffix and all -- what a
    ``SUPERSEDES`` line must match to retire this finding. ``anchor`` is the line-stripped
    subject two rounds are compared on. ``line`` is the verbatim record, which is what
    :func:`persisting` quotes back. ``blocking`` is whether this finding could stop a commit
    at the caller's ``block_severity`` -- ``actionable=yes`` and at or above that threshold --
    and only a blocking finding raises an anchor (see the module docstring). It is carried
    here rather than filtered out because retirement identity is this list's position.
    """

    line: str
    file: str
    anchor: Anchor
    blocking: bool


def _parsed_findings(entry: Mapping[str, object], *, block_severity: str) -> list[_Parsed]:
    """Every ``FINDING`` line of one round that is a single line fully matching ``_FINDING_RE``, in
    stored order -- a tampered or malformed entry is silently excluded.

    Position in this list is the identity a retirement consumes, so the order and the exclusions
    must be the same for every caller. That is why a non-blocking finding is kept with
    ``blocking=False`` rather than dropped: excluding it would renumber the positions a
    ``SUPERSEDES`` line resolves against.

    ``block_severity`` is ranked exactly as ``reviewer._interpret`` ranks it: an unrecognised
    *finding* severity ranks highest so it still blocks, an unrecognised *threshold* ranks lowest
    so a typo makes the gate stricter.
    """
    threshold = threshold_rank(block_severity)
    stored = entry.get("findings")
    parsed: list[_Parsed] = []
    for line in stored if isinstance(stored, list) else []:
        if not _is_single_line(line):
            continue
        assert isinstance(line, str)  # narrowed by _is_single_line, for mypy --strict
        match = _FINDING_RE.match(line)
        if match is None:
            continue
        raw = match.group("file")
        severity = match.group("severity")
        parsed.append(
            _Parsed(
                line=line,
                file=raw,
                anchor=Anchor(file=_anchor_file(raw), severity=severity),
                blocking=match.group("actionable") == "yes" and severity_rank(severity) >= threshold,
            )
        )
    return parsed


def _supersedes_target(line: object) -> tuple[int, str] | None:
    """``(round ordinal, exact file value)`` of one stored ``SUPERSEDES`` line, or ``None``.

    ``None`` for anything that is not a single line fully matching ``_SUPERSEDES_RE``, and
    for ``file=-``: a reversal that names no path names no finding, so there is nothing for
    it to retire. The value is returned **unstripped** -- retirement matches the exact
    ``file=`` value of a finding, not its anchor (see the module docstring).
    """
    if not _is_single_line(line):
        return None
    assert isinstance(line, str)  # narrowed by _is_single_line, for mypy --strict
    match = _SUPERSEDES_RE.match(line)
    if match is None:
        return None
    file = match.group("file")
    if file == "-":
        return None
    # `[0-9]{1,9}` by construction, so this cannot raise and cannot be unbounded.
    return int(match.group("round")), file


def _entry_seq(entry: Mapping[str, object]) -> int | None:
    """``seq`` if it is a genuine int, or ``None`` -- never a coerced ``0``. A tampered or
    missing ``seq`` is not "round zero"; it is an entry :func:`reversals` cannot order and
    must drop rather than silently pretend came first."""
    seq = entry.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def _ordered_rounds(history: Sequence[Mapping[str, object]], label: str) -> tuple[list[tuple[int, Mapping[str, object]]], bool]:
    """``([(seq, entry), ...], numbering_is_trustworthy)`` for every round of ``label``, in **stored
    order**.

    The shared front half of :func:`reversals` and :func:`persisting`, and it must stay shared: a
    retirement is addressed by round ordinal, so the two must agree on which entries are rounds and
    in what order.

    **Stored order, deliberately not ``seq`` order.** ``round=N`` means the ordinal the reviewer
    was shown, which is ``reviewer._prior_rounds_section``'s stored-order enumeration. Re-deriving
    an order from ``seq`` -- an untrusted integer -- hands a doctored history a lever to renumber
    rounds underneath claims written against the numbering on screen: with stored order ``A, B,
    C`` and seqs ``2, 1, 3``, sorting renumbers ``A`` to round 2 and can manufacture two
    retirements, and a ``NEEDS_HUMAN``, out of a history that truthfully has none.

    The second element is whether the ordinals can be **proved** equal to the ones on screen:
    ``True`` only when nothing was dropped and every ``seq`` strictly increases.
    :func:`_retirements` interprets no ``SUPERSEDES`` line when it is ``False``.
    """
    rounds: list[tuple[int, Mapping[str, object]]] = []
    trustworthy = True
    previous: int | None = None
    for entry in history:
        if entry.get("label") != label:
            continue
        seq = _entry_seq(entry)
        if seq is None:
            # Dropped here but still numbered by `_prior_rounds_section`, so every ordinal
            # after it now disagrees with the screen.
            trustworthy = False
            continue
        if previous is not None and seq <= previous:
            trustworthy = False
        previous = seq
        rounds.append((seq, entry))
    return rounds, trustworthy


def _retirements(
    rounds: Sequence[tuple[int, Mapping[str, object]]], parsed: Sequence[Sequence[_Parsed]], *, trustworthy: bool
) -> tuple[set[tuple[int, int]], dict[str, set[int]]]:
    """Which findings later rounds retired, and which rounds did the retiring.

    ``retired`` holds ``(round index, position in that round's :func:`_parsed_findings` list)`` for
    every finding a valid ``SUPERSEDES`` claimed; ``rounds_by_file`` maps a retired finding's
    **anchor** file to the index of every round that validly retired a **blocking** finding there.

    **The two are filtered differently, and have to be.** ``retired`` covers every finding a valid
    claim named, because retirement is consumptive and its identity is a position in that list.
    ``rounds_by_file`` is a signal keyed on the line-stripped anchor, so retiring the non-blocking
    ``a.py:20`` in one round and ``a.py:30`` in another would otherwise hand
    ``supersedes_rounds >= 2`` to an unrelated blocking ``a.py`` anchor nobody reversed.

    Rounds and their ``supersedes`` lists are walked in stored order, which makes retirement
    consumptive in a defined way: the earliest claim wins.

    ``trustworthy=False`` retires **nothing at all**. Interpreting a ``SUPERSEDES`` against a
    numbering the reviewer never saw does not fail safe in one direction -- it can silence a
    finding never retracted *and* validate a claim never true, and the second invents an
    escalation.

    Rounds are identified by index rather than ``seq``, so correctness does not rest on ``seq``
    being unique.
    """
    retired: set[tuple[int, int]] = set()
    rounds_by_file: dict[str, set[int]] = {}
    if not trustworthy:
        return retired, rounds_by_file
    for index, (_seq, entry) in enumerate(rounds):
        stored = entry.get("supersedes")
        for line in stored if isinstance(stored, list) else []:
            target = _supersedes_target(line)
            if target is None:
                continue
            ordinal, file = target
            earlier = ordinal - 1
            if not 0 <= earlier < index:
                continue
            matches = [position for position, item in enumerate(parsed[earlier]) if item.file == file]
            if len(matches) != 1:
                # Nothing to retire, or two candidates and no way to tell which -- see the
                # module docstring for why ambiguity retires neither.
                continue
            claim = (earlier, matches[0])
            if claim in retired:
                continue
            retired.add(claim)
            target_finding = parsed[earlier][matches[0]]
            if target_finding.blocking:
                rounds_by_file.setdefault(target_finding.anchor.file, set()).add(index)
    return retired, rounds_by_file


def _reappeared(flags: list[bool]) -> bool:
    """``True`` once ``flags`` -- one round's presence per element, in order -- shows a
    round where the anchor was raised, then a later round where it was not, then a still
    later round where it was raised again. A trailing gap with no third appearance (a
    finding that was simply fixed) is not a reversal, and this returns ``False`` for it."""
    seen_true = False
    gap_since_true = False
    for flag in flags:
        if flag:
            if gap_since_true:
                return True
            seen_true = True
        elif seen_true:
            gap_since_true = True
    return False


@dataclass(frozen=True)
class OscillationPoint:
    """One anchor flagged as oscillating, and why."""

    anchor: Anchor
    #: The anchor was raised, absent for at least one round, then raised again.
    reappeared: bool
    #: In how many **distinct rounds** a valid ``SUPERSEDES`` retired a finding whose anchor
    #: file is this one. ``>= 2`` is oscillating on its own, independent of ``reappeared`` --
    #: see the module docstring for the persisting-anchor case this catches, and for why this
    #: counts retirements rather than ``SUPERSEDES`` lines (two reversals inside one round are
    #: two fixes, not a flip-flop).
    supersedes_rounds: int
    #: ``seq`` of every round (in filtered history order) that raised this anchor.
    seqs: tuple[int, ...]


def reversals(history: Sequence[Mapping[str, object]], label: str, *, block_severity: str) -> list[OscillationPoint]:
    """Anchors that reversed across ``history``: reappeared after disappearing, or were named by two
    or more ``SUPERSEDES`` lines.

    ``history`` should already be narrowed to one ``activation_generation`` -- an anchor
    reappearing across a ``resume --replan`` boundary is a new phase, not a reversal. Rounds are
    read in **stored order** (see :func:`_ordered_rounds`); returned points are ordered by the
    ``seq`` of the anchor's first appearance, so the output is deterministic.

    **Presence is computed over live findings; ``seqs`` is not.** An anchor whose round-1 finding
    was explicitly retracted and which turns up in round 4 as a different defect at a different
    line is one finding fixed and another found -- flagging it escalated a converging phase
    (measured: ``services.go:180`` retracted in round 2, ``services.go:226`` raised in round 4).
    ``seqs`` stays unfiltered because it is the evidence line a human reads.

    A consequence worth naming: an anchor every round has retracted never enters ``presence``, so
    it cannot be reported however high ``supersedes_rounds`` climbs. Intended -- nothing is
    blocking on it.

    **``block_severity`` narrows both signals and neither narrowing touches retirement identity.**

    Deliberately not fixed: the anchor is ``(file, severity)``, so a finding whose label wanders
    ``medium`` -> ``high`` reads as two anchors and escapes both signals. Dropping severity would
    widen the ``services.go`` false positive above.
    """
    rounds, trustworthy = _ordered_rounds(history, label)
    parsed = [_parsed_findings(entry, block_severity=block_severity) for _seq, entry in rounds]
    retired, rounds_by_file = _retirements(rounds, parsed, trustworthy=trustworthy)

    seqs_by_anchor: dict[Anchor, list[int]] = {}
    presence: dict[Anchor, list[bool]] = {}

    for index, ((seq, _entry), items) in enumerate(zip(rounds, parsed, strict=True)):
        raised: set[Anchor] = set()
        live: set[Anchor] = set()
        for position, item in enumerate(items):
            if not item.blocking:
                continue
            raised.add(item.anchor)
            if (index, position) not in retired:
                live.add(item.anchor)

        for anchor in raised:
            seqs_by_anchor.setdefault(anchor, []).append(seq)

        for anchor in set(presence) | live:
            presence.setdefault(anchor, []).append(anchor in live)

    points: list[OscillationPoint] = []
    for anchor, flags in presence.items():
        supersedes_rounds = len(rounds_by_file.get(anchor.file, ()))
        reappeared = _reappeared(flags)
        if reappeared or supersedes_rounds >= 2:
            points.append(
                OscillationPoint(
                    anchor=anchor,
                    reappeared=reappeared,
                    supersedes_rounds=supersedes_rounds,
                    seqs=tuple(seqs_by_anchor.get(anchor, [])),
                )
            )

    # `presence` (and therefore this loop) is built by iterating `set` unions above, which
    # is not insertion-ordered -- two anchors first raised in the *same* round would
    # otherwise sort in whatever order the set's hash-randomised iteration happened to
    # produce that run. `(first seq, file, severity)` is a total order with no such tie.
    points.sort(key=lambda point: (point.seqs[0] if point.seqs else 0, point.anchor.file, point.anchor.severity))
    return points


@dataclass(frozen=True)
class PersistingPoint:
    """An anchor raised in every one of the last ``stall_rounds`` consecutive rounds.

    Distinct from :class:`OscillationPoint`: that one is about a position *changing* --
    disappearing and coming back, or being reversed more than once. This one is about a
    position that never changed at all, round after round -- the "genuinely stuck" signal
    :func:`persisting` exists to answer.
    """

    anchor: Anchor
    #: ``(seq, verbatim FINDING line)`` -- one pair per matching line, across exactly the last
    #: ``stall_rounds`` rounds, oldest first. More than one pair can share a ``seq`` when a
    #: single round raised the same anchor with more than one ``FINDING`` line.
    lines: tuple[tuple[int, str], ...]


def persisting(history: Sequence[Mapping[str, object]], label: str, stall_rounds: int, *, block_severity: str) -> list[PersistingPoint]:
    """Anchors raised in every one of the last ``stall_rounds`` consecutive rounds of
    ``history`` for ``label`` -- the same finding, round after round, with no sign of
    convergence.

    Fewer than ``stall_rounds`` rounds recorded yet answers ``[]``: there is not enough
    history for "every one of the last N" to mean anything. ``stall_rounds <= 0`` also
    answers ``[]`` -- the caller's own config gate (``0`` disables the check entirely), kept
    here too so a caller that forgets the gate still gets the safe answer rather than a
    negative-slice surprise.

    Scoping, ordering and the untrusted-input handling are exactly :func:`reversals`': entries
    for another label or with no genuine int ``seq`` are dropped, and a finding line is an
    anchor only once it fully re-validates against the grammar (see the module docstring --
    state is not a trust boundary).

    **A retired finding was never raised, for this question.** A ``SUPERSEDES`` line in any
    later round (not only one inside the window) removes the finding it validly retires from
    the round that raised it, before anchors are computed -- so "the same finding, round after
    round" cannot be satisfied by a finding the reviewer has explicitly retracted, however
    many later findings land in the same file. Retirements are therefore computed over the
    **whole** history and only then sliced to the window; a claim in round 5 against round 3
    has to be seen even when the window is rounds 3-5.

    **A finding that cannot block cannot stall.** Only a blocking finding
    (``actionable=yes``, at or above ``block_severity``) raises an anchor here: a
    ``severity=info actionable=no`` note repeated every round is a reviewer repeating itself,
    not a phase stuck on a disagreement, and it must not escalate. Retirement identity is
    computed over every parsed finding regardless, so a ``SUPERSEDES`` retiring a non-blocking
    finding still validates. See the module docstring.
    """
    if stall_rounds <= 0:
        return []
    rounds, trustworthy = _ordered_rounds(history, label)
    if len(rounds) < stall_rounds:
        return []
    parsed = [_parsed_findings(entry, block_severity=block_severity) for _seq, entry in rounds]
    retired = _retirements(rounds, parsed, trustworthy=trustworthy)[0]

    start = len(rounds) - stall_rounds
    recent = rounds[start:]

    lines_by_round: list[dict[Anchor, list[str]]] = []
    for index in range(start, len(rounds)):
        by_anchor: dict[Anchor, list[str]] = {}
        for position, item in enumerate(parsed[index]):
            if not item.blocking or (index, position) in retired:
                continue
            by_anchor.setdefault(item.anchor, []).append(item.line)
        lines_by_round.append(by_anchor)

    common: set[Anchor] = set(lines_by_round[0])
    for by_anchor in lines_by_round[1:]:
        common &= set(by_anchor)

    points: list[PersistingPoint] = []
    for anchor in common:
        pairs: list[tuple[int, str]] = []
        for (seq, _entry), by_anchor in zip(recent, lines_by_round, strict=True):
            pairs.extend((seq, line) for line in by_anchor[anchor])
        points.append(PersistingPoint(anchor=anchor, lines=tuple(pairs)))

    # Same reasoning as `reversals`' own sort: `common` is a `set`, not insertion-ordered.
    points.sort(key=lambda point: (point.anchor.file, point.anchor.severity))
    return points


def _render_one(point: OscillationPoint) -> str:
    reasons: list[str] = []
    if point.reappeared:
        reasons.append("reappeared after being absent")
    if point.supersedes_rounds >= 2:
        reasons.append(f"reversed via SUPERSEDES in {point.supersedes_rounds} round(s)")
    seqs_text = ", ".join(str(seq) for seq in point.seqs)
    return f"- `{point.anchor.file}` (severity {point.anchor.severity}): {'; '.join(reasons)} -- raised in round(s) with seq {seqs_text}\n"


def render(points: Sequence[OscillationPoint], *, max_points: int | None = None, max_bytes: int | None = None) -> str:
    """One line per point, in the order given. ``""`` for an empty sequence.

    Purely gate-computed text -- a file path and integers this module derived itself, never
    reviewer prose -- so unlike a ``FINDING``/``SUPERSEDES`` line this needs no re-validation
    by a reader; it is not something a tampered ``round_history`` can turn into smuggled text
    (see the module docstring).

    Bounded the same way ``reviewer._prior_rounds_section`` bounds its own findings, and for
    the same reason: a phase with enough rounds (a non-converging one raises a fresh finding
    every round, by design -- see phase 5) can have an unbounded number of anchors, and this
    text is appended, unbounded, straight into a hook's JSON response
    (``report.reason`` -> ``pretool``/``stop``). ``max_points`` and ``max_bytes`` are both
    optional and independent; either left ``None`` leaves that dimension unbounded, and a
    caller composing this into an already-bounded document (:func:`arl.reviewer` composes
    both callers with the same ``max_findings`` / ``max_findings_bytes`` config) should pass
    both.

    ``max_bytes``, when given, is the ceiling on the **whole return value**, the disclosure
    line included -- its size is reserved out of the budget up front, not added on top, so a
    caller than sizes a document around ``max_bytes`` never sees it overrun. In the
    degenerate case where ``max_bytes`` is smaller than the disclosure line itself, the
    disclosure is dropped rather than the ceiling: an empty result that says nothing is
    within budget, one that announces a cap while still busting it is not.
    """
    disclosure = "(further oscillating points are past the max_findings / max_findings_bytes cap and are not shown)\n"
    disclosure_size = len(disclosure.encode("utf-8", "surrogateescape"))
    content_budget = max(max_bytes - disclosure_size, 0) if max_bytes is not None else None

    lines: list[str] = []
    total = 0
    capped = False
    for index, point in enumerate(points):
        if max_points is not None and index >= max_points:
            capped = True
            break
        line = _render_one(point)
        size = len(line.encode("utf-8", "surrogateescape"))
        if content_budget is not None and total + size > content_budget:
            capped = True
            break
        lines.append(line)
        total += size
    if capped and (max_bytes is None or disclosure_size <= max_bytes):
        lines.append(disclosure)
    return "".join(lines)


def _render_persisting_one(point: PersistingPoint) -> str:
    seqs = sorted({seq for seq, _line in point.lines})
    header = f"- `{point.anchor.file}` (severity {point.anchor.severity}), raised in every one of the last {len(seqs)} round(s) (seq {', '.join(str(seq) for seq in seqs)}):\n"
    body = "".join(f"    - round seq {seq}: {line}\n" for seq, line in point.lines)
    return header + body


def render_persisting(points: Sequence[PersistingPoint], *, max_points: int | None = None, max_bytes: int | None = None) -> str:
    """One block per point, in the order given -- the file, the severity, and every matching
    round's verbatim ``FINDING`` line. ``""`` for an empty sequence.

    Bounded exactly like :func:`render`, for the same reason: ``review.error`` (composed by
    ``reviewer._stall_review``, this function's only caller) is appended, unbounded, straight
    into a hook's JSON response. See :func:`render`'s own docstring for what ``max_points`` /
    ``max_bytes`` mean and how the degenerate small-budget case is handled -- this mirrors it
    exactly, capping whole *points* (one point can render several lines) rather than
    individual lines.
    """
    disclosure = "(further persisting findings are past the max_findings / max_findings_bytes cap and are not shown)\n"
    disclosure_size = len(disclosure.encode("utf-8", "surrogateescape"))
    content_budget = max(max_bytes - disclosure_size, 0) if max_bytes is not None else None

    blocks: list[str] = []
    total = 0
    capped = False
    for index, point in enumerate(points):
        if max_points is not None and index >= max_points:
            capped = True
            break
        block = _render_persisting_one(point)
        size = len(block.encode("utf-8", "surrogateescape"))
        if content_budget is not None and total + size > content_budget:
            capped = True
            break
        blocks.append(block)
        total += size
    if capped and (max_bytes is None or disclosure_size <= max_bytes):
        blocks.append(disclosure)
    return "".join(blocks)
