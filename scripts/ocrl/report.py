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

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ocrl.atomic import ensure_private_dir, write_private_atomic
from ocrl.config import Config
from ocrl.paths import state_root
from ocrl.util import truncate

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for the type checker only
    from ocrl.reviewer import Review, Target

__all__ = ["list_reports", "reason", "render", "render_report", "report_path", "store"]

_FOOTER = "Fix the findings above, then commit again. The commit is gated until the review passes.\n"


def _timestamp() -> str:
    """``date -Is``: seconds precision, with the local offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def report_path(act_dir: Path, target: Target, seq: str, verdict: str) -> Path:
    """Where a report is stored. The verdict is in the filename, so ``ls`` is a summary."""
    return act_dir / "reports" / f"{seq}-{target.label}-{verdict.lower()}.md"


def render_report(review: Review, target: Target, *, seq: str, config: Config) -> str:
    """The stored report's full text, raw reviewer output included verbatim."""
    verdict = review.verdict or "UNKNOWN"
    variant = config.as_str("variant")

    out: list[str] = [f"# OpenCode review {seq} ({target.label})\n\n"]
    out.append(f"- verdict (recomputed by the gate): **{verdict}**\n")
    out.append(f"- base tree: `{target.base}`\n")
    out.append(f"- head tree: `{target.head}`\n")
    out.append(f"- model: `{config.as_str('model')}`{f' (variant `{variant}`)' if variant else ''}\n")
    out.append(f"- block_severity: `{config.as_str('block_severity')}`\n")
    out.append(f"- generated: {_timestamp()}\n")
    if review.error:
        out.append(f"- gate note: {review.error}\n")

    out.append("\n## Blocking findings\n\n")
    out.append(f"```\n{review.findings}```\n" if review.findings else "(none)\n")

    out.append("\n## Raw reviewer output\n\n")
    out.append("````\n")
    out.append(_raw_text(review.raw))
    out.append("\n````\n")
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
    if review.all_findings and review.all_findings != review.findings:
        out.append("\nAll findings reported (non-blocking ones included, for context):\n\n")
        out.append(review.all_findings)
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
