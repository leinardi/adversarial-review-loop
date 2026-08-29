"""Storing reports on disk and rendering the text Claude actually sees.

Ports ``scripts/lib/report.sh``. Every ``FINDING`` line is returned inline; prose is what
truncates, never the actionable set. A finding trimmed for length is a finding the model
never fixes.

**Nothing here writes to stdout** (Rule 2). This code runs inside the hook entrypoints,
whose stdout is the hook's JSON response, so the renderers return text and the caller
decides where it goes. The shell needed a comment on ``ocrl_report_store`` saying so; here
it is enforced by the signatures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ocrl.atomic import ensure_private_dir, write_private_atomic
from ocrl.config import Config
from ocrl.paths import state_root
from ocrl.util import truncate

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for the type checker only
    from ocrl.reviewer import Review, Target

__all__ = [
    "AcceptRecord",
    "accept_report_path",
    "deferred_text",
    "list_reports",
    "promote_accept",
    "reason",
    "render",
    "render_accept",
    "render_report",
    "report_path",
    "stage_accept",
    "store",
]

_FOOTER = "Verify and address the findings above, then commit again. The commit is gated until the review passes.\n"

_DEFERRED_INTRO = (
    "Deferred findings -- actionable, at or above block_severity, but new and outside the paths changed since the "
    "previous round, so below late_block_severity they did not block this {what}. Fix them now if cheap; they are "
    "recorded, and if this phase is reviewed again they will block:"
)


def deferred_text(review: Review, *, what: str) -> str:
    """The paragraph every approval path shows when ``review.deferred`` is non-empty.

    ``what`` names the thing that was approved despite them -- "commit" for the commit gate,
    "turn end" for the Stop sweep. Empty when nothing was deferred, so a caller can append it
    unconditionally. The lines are the ``FINDING`` lines verbatim, exactly as the blocking set
    is rendered: deferral changes what blocks, never what is shown.
    """
    if not review.deferred:
        return ""
    return f"{_DEFERRED_INTRO.format(what=what)}\n\n{review.deferred}"


def _timestamp() -> str:
    """``date -Is``: seconds precision, with the local offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def report_path(act_dir: Path, target: Target, seq: str, verdict: str) -> Path:
    """Where a report is stored. The verdict is in the filename, so ``ls`` is a summary."""
    return act_dir / "reports" / f"{seq}-{target.label}-{verdict.lower()}.md"


def _session_line(review: Review) -> str:
    """``- opencode session: `ses_…` (round N[, continued])``, or "" when there is none."""
    if not review.session:
        return ""
    suffix = ", continued" if review.round > 1 else ""
    return f"- opencode session: `{review.session}` (round {review.round}{suffix})\n"


def _findings_and_raw(review: Review, *, heading_level: str = "##") -> str:
    out = [f"\n{heading_level} Blocking findings\n\n"]
    out.append(f"```\n{review.findings}```\n" if review.findings else "(none)\n")
    if review.deferred:
        out.append(f"\n{heading_level} Deferred findings\n\n")
        out.append(
            "Actionable and at or above block_severity, but new, outside the paths changed since the previous "
            "round, and below late_block_severity -- so they did not block this round. Recorded: a later review "
            "of this phase treats their paths as known and blocks on them.\n\n"
        )
        out.append(f"```\n{review.deferred}```\n")
    if review.supersedes:
        out.append(f"\n{heading_level} Reversals of earlier rounds (SUPERSEDES)\n\n")
        out.append(f"```\n{review.supersedes}```\n")
    out.append(f"\n{heading_level} Raw reviewer output\n\n")
    out.append("````\n")
    out.append(_raw_text(review.raw))
    out.append("\n````\n")
    return "".join(out)


def _invocation_section(review: Review, *, heading: str) -> str:
    """One invocation's own verdict, session, findings and transcript, under ``## heading``.

    Used only for the two-invocation case (``render_report`` inlines the single-invocation
    shape directly, unchanged, so an ordinary report's headings are exactly what they always
    were).
    """
    out = [f"\n## {heading}\n\n"]
    out.append(f"- verdict: **{review.verdict or 'UNKNOWN'}**\n")
    out.append(_session_line(review))
    if review.error:
        out.append(f"- gate note: {review.error}\n")
    out.append(_findings_and_raw(review, heading_level="###"))
    return "".join(out)


def render_report(review: Review, target: Target, *, seq: str, config: Config) -> str:
    """The stored report's full text, raw reviewer output included verbatim.

    When ``review.confirmed`` is set -- only reachable under ``cold_confirm``, which is off by
    default -- ``review`` is the cold confirmation and ``review.confirmed`` the approving round
    it exists to check: a round that held model-influenced context, meaning a continued session,
    a ``context/`` attachment carrying an earlier round's findings, or both; see
    ``reviewer.execute``'s docstring for the rule this reflects and why it is opt-in.
    Both get their own verdict, findings, session id, round and raw
    transcript, under headings that say which is which, because the cold verdict recorded at
    the top is the one the gate acted on and a reader has to be able to tell that apart from
    the round that triggered it.
    """
    verdict = review.verdict or "UNKNOWN"
    variant = config.as_str("variant")

    out: list[str] = [f"# OpenCode review {seq} ({target.label})\n\n"]
    out.append(f"- verdict (recomputed by the gate): **{verdict}**\n")
    out.append(f"- base tree: `{target.base}`\n")
    out.append(f"- head tree: `{target.head}`\n")
    out.append(f"- model: `{config.as_str('model')}`{f' (variant `{variant}`)' if variant else ''}\n")
    out.append(f"- block_severity: `{config.as_str('block_severity')}`\n")
    if target.is_phase:
        out.append(f"- late_block_severity: `{config.as_str('late_block_severity')}`\n")
    out.append(f"- generated: {_timestamp()}\n")
    if review.confirmed is None:
        out.append(_session_line(review))
    if review.error:
        out.append(f"- gate note: {review.error}\n")

    if review.confirmed is not None:
        continued = review.confirmed
        out.append(
            "\nThis round was shown model-influenced context -- a continued session, an "
            "earlier round's own findings, or both -- and independently returned "
            f"{continued.verdict or 'UNKNOWN'}. `cold_confirm` is on, so such an approval was "
            "not acted on by itself: one more, cold review of the same bundle decided, and "
            "that cold verdict -- the one recorded at the top of this report -- is the one "
            "acted on.\n\nThe cold section's finding lists are this **round's record**: the "
            "cold call's own lines plus any the round with context reported and the cold one "
            "did not, since a confirmation replaces the verdict, not the record. Each "
            "section's raw transcript below it is that one invocation's own output, which is "
            "what to read to tell which call said what.\n"
        )
        out.append(_invocation_section(continued, heading="Round with context (not the verdict acted on)"))
        out.append(_invocation_section(review, heading="Cold confirmation (the verdict acted on)"))
    else:
        out.append(_findings_and_raw(review))
    return "".join(out)


def _raw_text(raw: str) -> str:
    """The reviewer's own bytes, or nothing -- the shell's ``cat … 2>/dev/null``.

    Decoded with ``surrogateescape`` and written back the same way, so a reviewer that
    emits a stray non-UTF-8 byte gets its output stored byte-for-byte instead of taking the
    whole report down with an encoding error.
    """
    if not raw:
        return ""
    try:
        return Path(raw).read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return ""


def store(review: Review, target: Target, *, seq: str, act_dir: Path, config: Config) -> Path:
    """Write the report and record its path on ``review``.

    Deliberately silent, and deliberately durable: the report is what a denial points the
    user at, so a half-written one is worse than none.
    """
    path = report_path(act_dir, target, seq, review.verdict or "UNKNOWN")
    ensure_private_dir(path.parent, root=state_root())
    write_private_atomic(path, render_report(review, target, seq=seq, config=config), root=state_root(), errors="surrogateescape")
    review.report = str(path)
    return path


def accept_report_path(act_dir: Path, seq: str, label: str) -> Path:
    """Where a manual acceptance's report lands once it is promoted. ``ocrl accept`` only."""
    return act_dir / "reports" / f"{seq}-{label}-accepted.md"


def _accept_staging_path(act_dir: Path, seq: str, label: str) -> Path:
    """Where a manual acceptance's report is written before it is durable.

    The leading dot and the ``.pending`` suffix both keep it off ``list_reports`` and
    ``render``'s ``*.md`` globs -- invisible to every reader until ``promote_accept`` renames
    it onto :func:`accept_report_path`, which only happens once the approval it documents is
    itself durable. See ``ocrl.commands.accept`` for why the two must not be able to come
    apart in the unsafe direction.
    """
    return act_dir / "reports" / f".accept-{seq}-{label}.pending"


@dataclass(frozen=True)
class AcceptRecord:
    """Everything one manual acceptance's report needs to render.

    Mirrors the ``manual_accepts`` entry ``ocrl.commands.accept`` writes to ``state.json``,
    plus the report sequence and the filenames of the reviews it overrides.
    """

    seq: str
    phase: int
    tree: str
    base: str
    reason: str
    reviews: list[str]


def render_accept(record: AcceptRecord) -> str:
    """A manual acceptance's report: what was accepted, and the reviews it overrides."""
    out: list[str] = [f"# Manual acceptance {record.seq} (phase{record.phase})\n\n"]
    out.append("- verdict: **ACCEPTED (manual, by the user)**\n")
    out.append(f"- phase: {record.phase}\n")
    out.append(f"- tree: `{record.tree}`\n")
    out.append(f"- base tree (last approved before this acceptance): `{record.base}`\n")
    out.append(f"- accepted: {_timestamp()}\n")
    out.append(f"- reviews overridden: {len(record.reviews)}\n")
    out.append("\n## Reason\n\n")
    out.append(f"{record.reason or '(none given)'}\n")
    out.append("\n## Reviews this acceptance overrides\n\n")
    out.append("".join(f"- {name}\n" for name in record.reviews) if record.reviews else "(none)\n")
    return "".join(out)


def stage_accept(content: str, *, act_dir: Path, seq: str, label: str) -> Path:
    """Write a manual acceptance's report where no reader can see it yet.

    The caller promotes it with :func:`promote_accept`, and only after the approval it
    documents is durably written -- see that function's docstring for why the order matters.
    """
    path = _accept_staging_path(act_dir, seq, label)
    ensure_private_dir(path.parent, root=state_root())
    write_private_atomic(path, content, root=state_root())
    return path


def promote_accept(staged: Path, *, act_dir: Path, seq: str, label: str) -> Path:
    """Publish a staged acceptance report by renaming it onto its real, discoverable name.

    Same directory as the staged file, so the rename is atomic -- there is no window in which
    a reader sees a half-written report at the final name. Called only after the transaction
    that granted the approval has itself saved successfully: an acceptance report at its final
    name for an approval that was never durably recorded is worse than one left staged and
    invisible, because a later review can reuse the same report sequence and the accepted
    report -- ``accepted`` sorts before every other verdict -- would then shadow it.
    """
    final = accept_report_path(act_dir, seq, label)
    os.replace(staged, final)
    return final


def reason(review: Review, headline: str, *, config: Config) -> str:
    """The message handed back through a deny or a Stop block.

    Order is the point: the blocking set first, the full set next, prose last, because prose
    is the only part that may be cut. ``max_reason_bytes`` bounds the prose alone.
    """
    out: list[str] = [f"{headline}\n"]
    if review.error:
        out.append(f"\nGate note: {review.error}\n")
    if review.findings:
        out.append(f"\nBlocking findings (actionable, severity >= {config.as_str('block_severity')}) -- every one must be resolved:\n\n")
        out.append(review.findings)
    if review.deferred:
        # A `CHANGES_REQUIRED` that carries deferred lines: the reviewer's own verdict blocked
        # (stricter wins), or other findings did. Either way these are shown as what they are.
        out.append(f"\n{deferred_text(review, what='round')}")
    if review.all_findings and review.all_findings != review.findings:
        out.append("\nAll findings reported (non-blocking ones included, for context):\n\n")
        out.append(review.all_findings)
    if review.supersedes:
        out.append("\nReversals of earlier rounds (SUPERSEDES lines) -- recorded, not verdict-changing:\n\n")
        out.append(review.supersedes)
    if review.oscillating:
        out.append(
            "\nOscillating points -- these anchors reappeared or were reversed more than once across earlier rounds of this same review; this is a reversal, not a new finding:\n\n"
        )
        out.append(review.oscillating)
    if review.prose:
        out.append("\nReviewer prose:\n\n")
        out.append(truncate(review.prose, config.as_int("max_reason_bytes")))
        out.append("\n")
    if review.report:
        out.append(f"\nFull report: {review.report}\n")
    out.append(f"\n{_FOOTER}")
    return "".join(out)


def list_reports(act_dir: Path) -> list[str]:
    """Report filenames for this activation, oldest first. Empty when there are none."""
    directory = act_dir / "reports"
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*.md"))


def render(act_dir: Path, n: int | None = None) -> str:
    """One stored report in full: the ``n``-th, or the newest when ``n`` is omitted.

    Returns the text rather than printing it, so a hook entrypoint cannot corrupt its own
    response by calling this (Rule 2).
    """
    directory = act_dir / "reports"
    if not directory.is_dir():
        return "No reports have been produced for this activation yet.\n"

    if n is None:
        candidates = sorted(directory.glob("*.md"))
        chosen = candidates[-1] if candidates else None
    else:
        candidates = sorted(directory.glob(f"{n:03d}-*.md"))
        chosen = candidates[0] if candidates else None

    if chosen is None:
        available = "".join(f"{name}\n" for name in list_reports(act_dir))
        return f"No such report. Available:\n{available}"
    try:
        return chosen.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return "No such report. Available:\n"
