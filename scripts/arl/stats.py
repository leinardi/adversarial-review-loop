"""What each review round of a run cost and found: the ``stats`` command behind ``/arl-stats``.

Read-only and display only. Every figure comes from ``round_history`` and ``failure_history``
as the review pipeline recorded them, and nothing here decides anything.

It does not say when a single finding was resolved. The stall anchors are file plus severity,
so they cannot tell bug A leaving a file from bug B arriving in it in the same round. A round's
blocking findings are shown as a count, never as "fixed".

A run started before rounds recorded their usage still loads: a missing figure is shown as
missing (``?``), never as ``0``, which would read as "this round was free".
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

import collections
import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

from arl import contract
from arl.state import DISPOSITION_STATUSES, RunState

__all__ = ["SEVERITY_ORDER", "collect", "render"]

#: Severities from the most to the least severe, the order findings are listed in.
SEVERITY_ORDER: Final = ("critical", "high", "medium", "low", "info")

#: The token figures a row shows: new input (uncached plus written to the cache), cached input
#: read back, and output.
_TOKENS: Final = {"new": ("input_tokens", "cache_creation_tokens"), "cached": ("cache_read_tokens",), "output": ("output_tokens",)}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cost(value: object) -> float | None:
    """A stored cost, or ``None``: an infinity or a NaN would make the answer JSON no strict reader takes."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _tokens(usage: Mapping[str, Any]) -> dict[str, int | None]:
    """Each figure of :data:`_TOKENS`, ``None`` when no part of it was reported."""
    out: dict[str, int | None] = {}
    for name, keys in _TOKENS.items():
        parts = [part for part in (_int(usage.get(key)) for key in keys) if part is not None]
        out[name] = sum(parts) if parts else None
    return out


def _severities(findings: object) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for line in findings if isinstance(findings, list) else []:
        match = contract.FINDING_RE.match(line) if isinstance(line, str) else None
        if match is not None:
            counts[match.group("severity")] += 1
    return {severity: counts[severity] for severity in SEVERITY_ORDER if counts[severity]}


def _stated(run: RunState, label: str, revision: int, number: int) -> dict[str, int]:
    """How many of round ``number``'s findings the implementer stated fixed or rejected."""
    if revision != run.data["phases_revision"]:
        return {}
    counts = collections.Counter(entry["status"] for ident, entry in run.dispositions_of(label).items() if ident.startswith(f"r{number}."))
    return {status: counts[status] for status in DISPOSITION_STATUSES if counts[status]}


def _row(entry: Mapping[str, Any], *, failed: bool, number: int) -> dict[str, Any]:
    usage: Mapping[str, Any] = entry["usage"] if isinstance(entry.get("usage"), dict) else {}
    cost = usage.get("cost_usd")
    row: dict[str, Any] = {
        "seq": _int(entry.get("seq")) or 0,
        "label": str(entry.get("label", "")),
        "revision": _int(entry.get("revision")) or 0,
        "round": _int(entry.get("round")) or number,
        "verdict": str(entry.get("verdict", "")),
        "model": str(entry.get("model") or ""),
        "harness": str(entry.get("harness") or ""),
        "requests": _int(usage.get("turns")),
        "tokens": _tokens(usage),
        "cost_usd": _cost(cost),
        "duration_ms": _int(usage.get("duration_ms")),
    }
    if failed:
        row["kind"] = str(entry.get("kind") or "")
    else:
        row["findings"] = _severities(entry.get("findings"))
        # Recorded since rounds count their own; an older round's is left unknown.
        row["blocking"] = _int(entry.get("blocking"))
        row["deferred"] = _int(entry.get("deferred"))
    return row


def _rows(run: RunState) -> list[dict[str, Any]]:
    """Every recorded and failed round, in the order they were reviewed.

    An entry written before rounds stored their number gets the one the reviewer was shown: one
    past the recorded rounds of its stage before it. A failed round recorded nothing, so the next
    attempt has the same number.
    """
    entries = [(entry, False) for entry in run.data["round_history"] if isinstance(entry, dict)]
    entries += [(entry, True) for entry in run.data["failure_history"] if isinstance(entry, dict)]
    entries.sort(key=lambda item: _int(item[0].get("seq")) or 0)
    recorded: collections.Counter[tuple[str, int]] = collections.Counter()
    rows = []
    for entry, failed in entries:
        stage = (str(entry.get("label", "")), _int(entry.get("revision")) or 0)
        row = _row(entry, failed=failed, number=recorded[stage] + 1)
        if not failed:
            recorded[stage] += 1
            # Ids number a stage's recorded rounds from 1, which is what `recorded` counts.
            row["stated"] = _stated(run, stage[0], stage[1], recorded[stage])
        rows.append(row)
    return rows


def _sum(rows: Sequence[Mapping[str, Any]], pick: Any) -> dict[str, Any]:
    """The total of a figure over ``rows``, and how many rows did not report it."""
    values = [pick(row) for row in rows]
    known = [value for value in values if value is not None]
    return {"total": sum(known) if known else None, "missing": len(values) - len(known)}


def collect(run: RunState) -> dict[str, Any]:
    """Each stage's rounds with their figures, and the run's totals."""
    rows = _rows(run)
    titles = run.data["phases"]
    stages: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["label"], row["revision"])
        if key not in stages:
            number = row["label"].removeprefix("phase")
            title = (
                titles[int(number) - 1]
                if number.isdigit() and 0 < int(number) <= len(titles) and row["revision"] == run.data["phases_revision"]
                else ""
            )
            stages[key] = {"label": row["label"], "revision": row["revision"], "title": title, "rounds": []}
        stages[key]["rounds"].append(row)
    recorded = [row for row in rows if "kind" not in row]
    totals = {
        "rounds": len(recorded),
        "failed": len(rows) - len(recorded),
        "requests": _sum(rows, lambda row: row["requests"]),
        **{f"tokens_{name}": _sum(rows, lambda row, name=name: row["tokens"][name]) for name in _TOKENS},
        "cost_usd": _sum(rows, lambda row: row["cost_usd"]),
        "blocking": _sum(recorded, lambda row: row["blocking"]),
    }
    return {"run_id": run.run_id, "revision": run.data["phases_revision"], "stages": list(stages.values()), "totals": totals}


def _count(value: int | None) -> str:
    """A token count, short: ``850``, ``12k``, ``1.2M``; ``?`` when it was not reported."""
    if value is None:
        return "?"
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{round(value / 1000)}k"
    return f"{value / 1_000_000:.1f}M"


def _figure(value: object) -> str:
    return "?" if value is None else str(value)


def _total(figure: Mapping[str, Any], shown: str) -> str:
    return f"{shown} ({figure['missing']} not reported)" if figure["missing"] and figure["total"] is not None else shown


def _line(row: Mapping[str, Any]) -> str:
    tokens = row["tokens"]
    parts = [
        f"#{row['seq']} round {row['round']}",
        row["verdict"] + (f" ({row['kind']})" if row.get("kind") else ""),
        row["model"] or "?",
        f"{_figure(row['requests'])} requests",
        f"tokens new {_count(tokens['new'])} cached {_count(tokens['cached'])} out {_count(tokens['output'])}",
    ]
    if row["cost_usd"] is not None:
        parts.append(f"${row['cost_usd']:.2f}")
    if "findings" in row:
        found = ", ".join(f"{count} {severity}" for severity, count in row["findings"].items()) or "none"
        parts.append(f"findings {found}")
        parts.append(f"blocking {_figure(row['blocking'])}")
        if row["deferred"]:
            parts.append(f"deferred {row['deferred']}")
        if row.get("stated"):
            parts.append("stated " + ", ".join(f"{status} {count}" for status, count in row["stated"].items()))
    return "  " + " · ".join(parts)


def render(stats: Mapping[str, Any]) -> str:
    """The text ``/arl-stats`` shows: one block per stage, one line per round, then the totals."""
    if not stats["stages"]:
        return f"Run {stats['run_id']}: no review has been recorded yet.\n"
    totals = stats["totals"]
    failed = f", {totals['failed']} failed" if totals["failed"] else ""
    lines = [f"Run {stats['run_id']}: {totals['rounds']} recorded round(s){failed}.", ""]
    for stage in stats["stages"]:
        name = "Final review" if stage["label"] == "final" else f"Phase {stage['label'].removeprefix('phase')}"
        if stage["title"]:
            name += f": {stage['title']}"
        if stage["revision"] != stats["revision"]:
            name += f" (phase list revision {stage['revision']}, since replanned)"
        lines.append(name)
        lines.extend(_line(row) for row in stage["rounds"])
        lines.append("")
    requests = totals["requests"]
    lines.append(
        "Total: "
        + _total(requests, f"{_figure(requests['total'])} requests")
        + " · "
        + " ".join(_total(totals[f"tokens_{name}"], f"{name} {_count(totals[f'tokens_{name}']['total'])}") for name in _TOKENS)
        + (f" · ${totals['cost_usd']['total']:.2f}" if totals["cost_usd"]["total"] is not None else "")
    )
    lines.append("Blocking counts are findings per round as the reviewer gave them, not proof that any was fixed.")
    return "\n".join(lines) + "\n"
