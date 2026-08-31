"""Storing reports on disk and rendering the text Claude actually sees.

Ports ``scripts/lib/report.sh``. Every ``FINDING`` line is returned inline; prose is what
truncates, never the actionable set. A finding trimmed for length is a finding the model
never fixes.

**Nothing here writes to stdout** (Rule 2). This code runs inside the hook entrypoints,
whose stdout is the hook's JSON response, so the renderers return text and the caller
decides where it goes. The shell needed a comment on ``arl_report_store`` saying so; here
it is enforced by the signatures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from arl import commands, harness
from arl.atomic import ensure_private_dir, write_private_atomic
from arl.config import Config
from arl.paths import state_root
from arl.util import truncate

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for the type checker only
    from arl.reviewer import Review, Target
    from arl.state import State

__all__ = [
    "AcceptRecord",
    "accept_report_path",
    "clarify_hint",
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
    "with_clarify_hint",
]

_FOOTER = "Verify and address the findings above, then commit again. The commit is gated until the review passes.\n"

_DEFERRED_INTRO = (
    "Deferred findings -- actionable, at or above block_severity, but new and outside the paths changed since the "
    "previous round, so below late_block_severity they did not block this {what}. Fix them now if cheap; they are "
    "recorded, and if this phase is reviewed again they will block:"
)


_CLARIFY_HINT = (
    "If a finding is ambiguous, or contradicts an earlier round, ask one question with "
    '`{entrypoint} clarify --question "..."` before guessing at a fix -- a wrong guess burns another whole round. '
    "Clarifications left: {left} of {limit}."
)


def clarify_hint(*, state: State, config: Config) -> str:
    """The line every blocking phase verdict carries, or "" when the allowance is spent.

    Claude used ``clarify`` zero times across two full activations, so the hint gets its own
    line rather than trailing a headline sentence, and names what is left: an allowance whose
    size is invisible reads like something to save for later, and never gets spent at all.

    Empty when nothing is left, so a caller can append it unconditionally -- pointing at a
    command that can only refuse would cost a turn to discover. Only phase-scoped verdicts
    should show it: ``clarify`` targets the current phase's latest ``round_history`` entry, and
    a final cumulative review has none.
    """
    limit = config.as_int("max_clarifications")
    left = max(limit - state.get_int("clarifications"), 0)
    if left == 0:
        return ""
    return _CLARIFY_HINT.format(entrypoint=commands.entrypoint(), left=left, limit=limit)


def with_clarify_hint(headline: str, *, state: State, config: Config) -> str:
    """``headline`` with :func:`clarify_hint` under it, as a paragraph of its own.

    Unchanged when the allowance is spent, so both blocking phase paths -- the commit gate and
    the Stop sweep -- can call it unconditionally.
    """
    hint = clarify_hint(state=state, config=config)
    return f"{headline}\n\n{hint}" if hint else headline


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
    """``- reviewer session: `…` (round N[, continued])``, or "" when there is none.

    Names the role rather than the CLI: which reviewer produced it is reported once, on the
    ``harness`` line :func:`render_report` writes, and an id spelled by one harness must not
    be labelled with another's name.
    """
    if not review.session:
        return ""
    suffix = ", continued" if review.round > 1 else ""
    return f"- reviewer session: `{review.session}` (round {review.round}{suffix})\n"


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
    if review.repaired:
        # Both transcripts, because neither is the whole story on its own: the block above was
        # re-emitted by the repair call, and the review it describes -- the prose, the
        # reasoning, everything written before the malformed block -- is only in this one.
        out.append(f"\n{heading_level} Malformed primary transcript\n\n")
        out.append(
            "The reviewer ran to completion and then wrote a findings block the gate could not parse. The block "
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


def _invocation_section(review: Review, *, heading: str) -> str:
    """One invocation's own verdict, session, findings and transcript, under ``## heading``.

    Used only for the two-invocation case (``render_report`` inlines the single-invocation
    shape directly, unchanged, so an ordinary report's headings are exactly what they always
    were).
    """
    out = [f"\n## {heading}\n\n"]
    out.append(f"- verdict: **{review.verdict or 'UNKNOWN'}**\n")
    out.append(_session_line(review))
    # Per invocation, not once at the top: under `cold_confirm` a round is two model calls,
    # and what the confirmation costs is the whole argument about whether to run it.
    out.append(_usage_line(review))
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

    out: list[str] = [f"# Review {seq} ({target.label})\n\n"]
    out.append(f"- verdict (recomputed by the gate): **{verdict}**\n")
    out.append(f"- base tree: `{target.base}`\n")
    out.append(f"- head tree: `{target.head}`\n")
    # `display_model`, not `model`: a report is written *after* a review ran, including the
    # ones that failed, and a stored record of what happened must not be the thing that
    # raises. The harness name is printed beside it, unvalidated, so a configuration that
    # changed underneath the activation is visible in the report rather than hidden by it.
    out.append(f"- harness: `{config.as_str('harness')}`\n")
    out.append(f"- model: `{harness.display_model(config)}`{f' (variant `{variant}`)' if variant else ''}\n")
    out.append(f"- block_severity: `{config.as_str('block_severity')}`\n")
    if target.is_phase:
        out.append(f"- late_block_severity: `{config.as_str('late_block_severity')}`\n")
    out.append(f"- generated: {_timestamp()}\n")
    if review.repaired:
        out.append(f"- findings block: re-emitted by a repair call; the primary transcript is `{review.repaired}`\n")
    if review.confirmed is None:
        out.append(_session_line(review))
        out.append(_usage_line(review))
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
    """Where a manual acceptance's report lands once it is promoted. ``arl accept`` only."""
    return act_dir / "reports" / f"{seq}-{label}-accepted.md"


def _accept_staging_path(act_dir: Path, seq: str, label: str) -> Path:
    """Where a manual acceptance's report is written before it is durable.

    The leading dot and the ``.pending`` suffix both keep it off ``list_reports`` and
    ``render``'s ``*.md`` globs -- invisible to every reader until ``promote_accept`` renames
    it onto :func:`accept_report_path`, which only happens once the approval it documents is
    itself durable. See ``arl.commands.accept`` for why the two must not be able to come
    apart in the unsafe direction.
    """
    return act_dir / "reports" / f".accept-{seq}-{label}.pending"


@dataclass(frozen=True)
class AcceptRecord:
    """Everything one manual acceptance's report needs to render.

    Mirrors the ``manual_accepts`` entry ``arl.commands.accept`` writes to ``state.json``,
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
