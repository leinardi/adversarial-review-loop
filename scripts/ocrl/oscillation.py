"""Detecting oscillation across ``round_history`` -- pure functions, no I/O.

Phase 4 of the convergence plan. A phase can flip a design point across rounds without ever
saying so: round 1 blocks on missing warn-before, round 2 blocks on missing warn-after
instead (round 1's point silently dropped), round 3 blocks on "needs both" (round 1's point
back). Nothing in the ordinary evidence -- the diff, the prompt, the reviewer's own memory
of the session -- says "this is a reversal", so the gate has no way to tell a genuinely new
finding from a rehash of one already seen. This module answers that question from
``round_history`` alone.

A finding's **anchor** is what it is *about*, stable across rounds even as its wording and
line numbers change: the ``file`` field of its ``FINDING`` line with any trailing ``:line``
suffix stripped, paired with its ``severity``. Two things count as oscillation:

- the anchor is raised, absent for at least one later round, then raised again
  (:func:`reversals`' ``reappeared``);
- the anchor's file is named by two or more ``SUPERSEDES`` lines anywhere in the filtered
  history (``supersedes_count``) -- this is what catches the warn-before/warn-after/both
  case above, where the anchor never actually disappears (a warning is raised every round),
  so ``reappeared`` alone would miss it, but the reviewer's own ``SUPERSEDES`` lines say it
  changed its mind twice.

Neither signal changes a verdict on its own -- see ``reviewer.py``'s docstring for why a
reversal still blocks exactly as its ``FINDING`` lines say. This module only says "here is
where the history disagrees with itself"; :mod:`ocrl.reviewer` and :mod:`ocrl.report` decide
what to show a reader, and a future phase 5 decides whether it is grounds to stop asking the
reviewer at all.

``round_history`` is read out of ``state.json``, which ``AGENTS.md`` is explicit is not a
trust boundary. Every value taken out of an entry here is treated that way: a non-string, a
value carrying an embedded ``\n`` (more than one ``_records`` record smuggled into one
stored line), or a line that does not fully match the expected grammar is silently excluded
from the computation rather than raising -- the worst a tampered history can do is hide a
real oscillation, never fabricate one out of smuggled text (this module does no rendering of
that text either; see :func:`render`, which only ever echoes a file path and an integer
count it computed itself).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["Anchor", "OscillationPoint", "render", "reversals"]

#: POSIX ``[[:space:]]`` in the C locale, spelled out rather than left to ``\s`` -- the same
#: reasoning and the same literal class as ``reviewer._SPACE``.
_SPACE: Final = " \t\n\r\f\v"

#: The full ``FINDING`` / ``SUPERSEDES`` grammar, byte-for-byte the same character classes
#: as ``reviewer._FINDING_RE`` / ``reviewer._SUPERSEDES_RE``, just with a named ``file``
#: group added -- the reviewer's own regexes validate the grammar but never capture the
#: field out. This module keeps its own copy rather than importing ``reviewer``'s:
#: ``reviewer.py`` is this module's caller (``_prior_rounds_section``, ``execute``), and
#: importing back would cycle. Matching only a loose ``severity=`` / ``file=...|`` fragment
#: (an earlier version of this module did) accepts a line like
#: ``FINDING severity=medium garbage file=x.py | fake`` -- missing ``actionable=``, extra
#: prose in between -- as a real anchor. Requiring the *whole* grammar, anchored at the
#: start exactly as the reviewer's own parser requires it, is what keeps a stored line from
#: fabricating a reversal it was never validated as being.
_FINDING_RE: Final = re.compile(
    r"^FINDING[ \t]+severity=(?P<severity>info|low|medium|high|critical)"
    r"[ \t]+actionable=(?:yes|no)"
    rf"[ \t]+file=(?P<file>[^|{_SPACE}](?:[^|]*[^|{_SPACE}])?)[ \t]*\|[ \t]*[^{_SPACE}]"
)
_SUPERSEDES_RE: Final = re.compile(
    r"^SUPERSEDES[ \t]+round=[0-9]{1,9}"
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


def _finding_anchor(line: object) -> Anchor | None:
    """The anchor of one stored ``FINDING`` line, or ``None`` if ``line`` is not a single
    line that fully matches ``_FINDING_RE`` -- a tampered or malformed entry degrades to
    "not an anchor", never to a crash or to smuggled text (see the module docstring)."""
    if not _is_single_line(line):
        return None
    assert isinstance(line, str)  # narrowed by _is_single_line, for mypy --strict
    match = _FINDING_RE.match(line)
    if match is None:
        return None
    return Anchor(file=_anchor_file(match.group("file")), severity=match.group("severity"))


def _supersedes_file(line: object) -> str | None:
    """The line-stripped ``file`` field of one stored ``SUPERSEDES`` line, or ``None`` if
    ``line`` is not a single line that fully matches ``_SUPERSEDES_RE``."""
    if not _is_single_line(line):
        return None
    assert isinstance(line, str)  # narrowed by _is_single_line, for mypy --strict
    match = _SUPERSEDES_RE.match(line)
    if match is None:
        return None
    return _anchor_file(match.group("file"))


def _entry_seq(entry: Mapping[str, object]) -> int | None:
    """``seq`` if it is a genuine int, or ``None`` -- never a coerced ``0``. A tampered or
    missing ``seq`` is not "round zero"; it is an entry :func:`reversals` cannot order and
    must drop rather than silently pretend came first."""
    seq = entry.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


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
    #: How many ``SUPERSEDES`` lines across the filtered history name this anchor's file.
    #: ``>= 2`` is oscillating on its own, independent of ``reappeared`` -- see the module
    #: docstring for the persisting-anchor case this catches.
    supersedes_count: int
    #: ``seq`` of every round (in filtered history order) that raised this anchor.
    seqs: tuple[int, ...]


def reversals(history: Sequence[Mapping[str, object]], label: str) -> list[OscillationPoint]:
    """Anchors that reversed across ``history``: reappeared after disappearing, or were
    named by two or more ``SUPERSEDES`` lines.

    ``history`` should already be narrowed to one ``activation_generation`` by the caller --
    an anchor "reappearing" across a `resume --replan` boundary is a new phase, not a
    reversal of the old one. This function narrows to ``label`` itself as a second, cheap
    filter: an entry for another label is silently excluded rather than raising, the same
    "state is not a trust boundary" treatment the rest of this module gives every stored
    field.

    Ordering follows each entry's ``seq`` (the monotonic report counter) rather than list
    position, for the same reason. Returned points are ordered by the ``seq`` of the anchor's
    first appearance, so output is deterministic regardless of how ``history`` was ordered
    going in.
    """
    rounds: list[tuple[int, Mapping[str, object]]] = []
    for entry in history:
        if entry.get("label") != label:
            continue
        seq = _entry_seq(entry)
        if seq is None:
            continue
        rounds.append((seq, entry))
    rounds.sort(key=lambda pair: pair[0])

    seqs_by_anchor: dict[Anchor, list[int]] = {}
    presence: dict[Anchor, list[bool]] = {}
    supersedes_by_file: dict[str, int] = {}

    for seq, entry in rounds:
        findings = entry.get("findings")
        anchors_this_round: set[Anchor] = set()
        for line in findings if isinstance(findings, list) else []:
            anchor = _finding_anchor(line)
            if anchor is not None:
                anchors_this_round.add(anchor)

        for anchor in anchors_this_round:
            seqs_by_anchor.setdefault(anchor, []).append(seq)

        for anchor in set(presence) | anchors_this_round:
            presence.setdefault(anchor, []).append(anchor in anchors_this_round)

        supersedes = entry.get("supersedes")
        for line in supersedes if isinstance(supersedes, list) else []:
            file = _supersedes_file(line)
            if file is not None:
                supersedes_by_file[file] = supersedes_by_file.get(file, 0) + 1

    points: list[OscillationPoint] = []
    for anchor, flags in presence.items():
        supersedes_count = supersedes_by_file.get(anchor.file, 0)
        reappeared = _reappeared(flags)
        if reappeared or supersedes_count >= 2:
            points.append(
                OscillationPoint(
                    anchor=anchor,
                    reappeared=reappeared,
                    supersedes_count=supersedes_count,
                    seqs=tuple(seqs_by_anchor.get(anchor, [])),
                )
            )

    # `presence` (and therefore this loop) is built by iterating `set` unions above, which
    # is not insertion-ordered -- two anchors first raised in the *same* round would
    # otherwise sort in whatever order the set's hash-randomised iteration happened to
    # produce that run. `(first seq, file, severity)` is a total order with no such tie.
    points.sort(key=lambda point: (point.seqs[0] if point.seqs else 0, point.anchor.file, point.anchor.severity))
    return points


def _render_one(point: OscillationPoint) -> str:
    reasons: list[str] = []
    if point.reappeared:
        reasons.append("reappeared after being absent")
    if point.supersedes_count >= 2:
        reasons.append(f"reversed {point.supersedes_count} time(s) via SUPERSEDES")
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
    caller composing this into an already-bounded document (:func:`ocrl.reviewer` composes
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
