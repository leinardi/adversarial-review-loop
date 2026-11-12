"""Storing reports under ``.arl/reports/`` and rendering the text Claude sees.

Every ``FINDING`` line is returned inline; prose is what truncates, never the actionable set. A
finding trimmed for length is a finding the model never fixes.

**Nothing here writes to stdout** (AGENTS.md rule 3): the renderers return text and the command
decides where it goes.
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

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from arl import harness
from arl.atomic import write_private_atomic
from arl.config import Config
from arl.util import truncate

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for the type checker only
    from arl.contract import Review, Target

__all__ = [
    "deferred_text",
    "list_reports",
    "reason",
    "render",
    "render_report",
    "report_path",
    "store",
]

_FOOTER = "Verify and address the findings above, then ask for the review again.\n"

_DEFERRED_INTRO = (
    "Deferred findings -- actionable, at or above block_severity, but new and outside the paths changed since the "
    "previous round, so below late_block_severity they did not block this {what}. Fix them now if cheap; they are "
    "recorded, and if this phase is reviewed again they will block:"
)


def deferred_text(review: Review, *, what: str) -> str:
    """The paragraph every approval path shows when ``review.deferred`` is non-empty.

    ``what`` names the thing that was approved despite them ("approval"). Empty when nothing
    was deferred, so a caller can append it unconditionally. The lines are the ``FINDING`` lines verbatim, exactly as the blocking set
    is rendered: deferral changes what blocks, never what is shown.
    """
    if not review.deferred:
        return ""
    return f"{_DEFERRED_INTRO.format(what=what)}\n\n{review.deferred}"


def _timestamp() -> str:
    """``date -Is``: seconds precision, with the local offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def report_path(reports_dir: Path, target: Target, seq: str, verdict: str) -> Path:
    """Where a report is stored. The verdict is in the filename, so ``ls`` is a summary."""
    return reports_dir / f"{seq}-{target.label}-{verdict.lower()}.md"


def _findings_and_raw(review: Review) -> str:
    out = ["\n## Blocking findings\n\n"]
    out.append(f"```\n{review.findings}```\n" if review.findings else "(none)\n")
    if review.deferred:
        out.append("\n## Deferred findings\n\n")
        out.append(
            "Actionable and at or above block_severity, but new, outside the paths changed since the previous "
            "round, and below late_block_severity -- so they did not block this round. Recorded: a later review "
            "of this phase treats their paths as known and blocks on them.\n\n"
        )
        out.append(f"```\n{review.deferred}```\n")
    if review.supersedes:
        out.append("\n## Reversals of earlier rounds (SUPERSEDES)\n\n")
        out.append(f"```\n{review.supersedes}```\n")
    out.append("\n## Raw reviewer output\n\n")
    out.append("````\n")
    out.append(_raw_text(review.raw))
    out.append("\n````\n")
    if review.repaired:
        # Both transcripts, because neither is the whole story on its own: the block above was
        # re-emitted by the repair call, and the review it describes -- the prose, the
        # reasoning, everything written before the malformed block -- is only in this one.
        out.append("\n## Malformed primary transcript\n\n")
        out.append(
            "The reviewer ran to completion and then wrote a findings block the engine could not parse. The block "
            "above was re-emitted by a separate repair call, which is shown as the raw output; this is the review "
            "it was re-emitted from, and it is the one to read for what the reviewer actually said.\n\n"
        )
        out.append("````\n")
        out.append(_raw_text(review.repaired))
        out.append("\n````\n")
    return "".join(out)


def _tokens(count: int | None) -> str:
    """A token count as ``1,234`` below a thousand and ``5,650k`` above it.

    Thousands throughout rather than switching to millions: the interesting comparison in a
    review is cache reads against cache creation, and those routinely sit two orders of
    magnitude apart -- rendering one as ``5.6M`` beside the other as ``189k`` makes the ratio
    that explains the bill something the reader has to convert before they can see it.
    """
    if count is None:
        return "?"
    # Rounded, not floored: 5,649,958 truncates to "5,649k", which reads as a precision the
    # figure does not have and is the wrong side of the real number. Nothing computes on this.
    return f"{count:,}" if count < 1000 else f"{round(count / 1000):,}k"


def _usage_line(review: Review) -> str:
    """``- cost: …`` for an invocation whose harness reported one, else "".

    Every field is printed only if the harness gave it, so a CLI that reports turns but no
    dollar figure still gets a useful line and no invented zero. Omitted entirely when there is
    nothing at all to say -- an OpenCode review, a run under the test seam, a failed run -- so
    a report never carries an empty accounting line.

    The cache-read figure is deliberately shown beside the turn count: it is *context x turns*,
    and seeing them together is what makes an expensive round diagnosable rather than merely
    expensive.
    """
    usage = review.usage
    if usage is None:
        return ""
    parts: list[str] = []
    if usage.cost_usd is not None:
        parts.append(f"${usage.cost_usd:.2f}")
    if usage.turns is not None:
        parts.append(f"{usage.turns} turns")
    if usage.cache_creation_tokens is not None or usage.cache_read_tokens is not None:
        parts.append(f"{_tokens(usage.cache_creation_tokens)} new + {_tokens(usage.cache_read_tokens)} cached input")
    if usage.output_tokens is not None:
        parts.append(f"{_tokens(usage.output_tokens)} output")
    if usage.duration_ms is not None:
        parts.append(f"{usage.duration_ms // 1000}s")
    return f"- cost: {' — '.join(parts)}\n" if parts else ""


def render_report(review: Review, target: Target, *, seq: str, config: Config) -> str:
    """The stored report's full text, raw reviewer output included verbatim."""
    verdict = review.verdict or "UNKNOWN"
    variant = config.as_str("variant")

    out: list[str] = [f"# Review {seq} ({target.label})\n\n"]
    out.append(f"- verdict (recomputed by the engine): **{verdict}**\n")
    for root in target.roots:
        out.append(f"- root `{root.alias}` ({root.path}): `{root.base}` -> `{root.head}`\n")
    # `display_model`, not `model`: a report is written *after* a review ran, including the
    # ones that failed, and a stored record of what happened must not be the thing that
    # raises. The harness name is printed beside it, unvalidated, so a configuration that
    # changed underneath the run is visible in the report rather than hidden by it.
    out.append(f"- harness: `{config.as_str('harness')}`\n")
    out.append(f"- model: `{harness.display_model(config)}`{f' (variant `{variant}`)' if variant else ''}\n")
    out.append(f"- block_severity: `{config.as_str('block_severity')}`\n")
    # What this round was told: the project guidance it ran under, as the ticket recorded it.
    if review.guide:
        out.append("- review guidance:\n" + "".join(f"  {line}\n" for line in review.guide.splitlines()))
    if target.is_phase:
        out.append(f"- late_block_severity: `{config.as_str('late_block_severity')}`\n")
    out.append(f"- generated: {_timestamp()}\n")
    if review.repaired:
        out.append(f"- findings block: re-emitted by a repair call; the primary transcript is `{review.repaired}`\n")
    out.append(_usage_line(review))
    if review.error:
        out.append(f"- engine note: {review.error}\n")

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


def store(review: Review, target: Target, *, seq: str, reports_dir: Path, config: Config) -> Path:
    """Write the report and record its path on ``review``. Durable: a half-written report is
    worse than none, since it is what a refusal points the user at."""
    path = report_path(reports_dir, target, seq, review.verdict or "UNKNOWN")
    reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private_atomic(path, render_report(review, target, seq=seq, config=config), root=reports_dir, errors="surrogateescape")
    review.report = str(path)
    return path


def reason(review: Review, headline: str, *, config: Config) -> str:
    """The message ``review record`` hands back to Claude when a review did not approve.

    Order is the point: the blocking set first, the full set next, prose last, because prose
    is the only part that may be cut. ``max_reason_bytes`` bounds the prose alone.
    """
    out: list[str] = [f"{headline}\n"]
    if review.error:
        out.append(f"\nGate note: {review.error}\n")
    if review.repaired:
        out.append(
            "\nGate note: the reviewer's findings block was malformed, so the findings below were re-emitted by a "
            f"repair call against the same transcript. The review itself is at {review.repaired}.\n"
        )
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


def list_reports(reports_dir: Path) -> list[str]:
    """Report filenames for this run, oldest first. Empty when there are none."""
    directory = reports_dir
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob("*.md"))


def render(reports_dir: Path, n: int | None = None) -> str:
    """One stored report in full: the ``n``-th, or the newest when ``n`` is omitted.

    Returns the text rather than printing it, so a hook entrypoint cannot corrupt its own
    response by calling this (AGENTS.md rule 3).
    """
    directory = reports_dir
    if not directory.is_dir():
        return "No reports have been produced for this run yet.\n"

    if n is None:
        candidates = sorted(directory.glob("*.md"))
        chosen = candidates[-1] if candidates else None
    else:
        candidates = sorted(directory.glob(f"{n:03d}-*.md"))
        chosen = candidates[0] if candidates else None

    if chosen is None:
        available = "".join(f"{name}\n" for name in list_reports(reports_dir))
        return f"No such report. Available:\n{available}"
    try:
        return chosen.read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return "No such report. Available:\n"
