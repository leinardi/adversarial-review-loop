#!/usr/bin/env python3
"""Reviewer benchmark: seeded cases, headless reviews, their cost and their recall.

Not part of ``make test`` or CI: ``run`` and ``matrix`` call real models and cost money.
``analyze`` and ``score`` only read what earlier runs left on disk, and are unit-tested.

A *case* is a scratch clone of this repository at ``<commit>^`` with a run initialised by
``scripts/arl.sh``, whose tree is then moved to ``<commit>`` (and optionally seeded with known
bugs), so the next ``review`` sees exactly that commit as its phase. Everything lives under
``ARL_BENCH_DIR`` (default ``~/.local/share/arl-research``), never inside the repository:

- ``cases/<name>/``: the clone and its ``.arl/`` run state;
- ``cases/<name>.plan.md``, ``cases/<name>.bench.json``: the plan and the bench's own record of
  the case, kept beside the clone because a file inside it would be reviewed as a change;
- ``runs/<tag>/``: the headless session's output and a copy of the state and reports.

See ``tests/bench/README.md`` for what a cell is and how to add a seeded phase.
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

import argparse
import collections
import concurrent.futures
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "BENCH",
    "COST_WEIGHTS",
    "LINE_WINDOW",
    "REPO",
    "REVIEWER_AGENT",
    "ROUND2_CLASSES",
    "RUN_PROMPT",
    "SEEDS",
    "Assignment",
    "Cell",
    "Finding",
    "analyze_case",
    "analyze_transcript",
    "assign",
    "bench_dir",
    "diff_coverage",
    "findings_of",
    "log",
    "main",
    "make_case",
    "make_round2",
    "matrix",
    "parse_finding",
    "record_session",
    "resolve",
    "reviewer_transcripts",
    "run_review",
    "score_case",
    "score_round1",
    "score_round2",
]

REPO: Final = Path(__file__).resolve().parents[2]
BENCH: Final = Path(__file__).resolve().parent
SEEDS: Final = BENCH / "seeds"

#: Relative price per token kind: input 1, cache write 1.25, cache read 0.1, output 5. One price
#: for every model, so a cost unit compares requests and context, not a model's list price.
COST_WEIGHTS: Final[Mapping[str, float]] = {"in": 1.0, "cw": 1.25, "cr": 0.1, "out": 5.0}

_SEVERITY_RANK: Final[Mapping[str, int]] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: How far a cited line may sit from a defect's line and still name it.
LINE_WINDOW: Final = 5

#: Round-2 truth classes.
ROUND2_CLASSES: Final = ("open", "regression", "untouched-miss", "fixed")

REVIEWER_AGENT: Final = "adversarial-review-loop:reviewer"

RUN_PROMPT: Final = (
    "Call the adversarial-review-loop `review` tool exactly once, with no arguments. Do not edit any file and do not "
    "call any other tool. Then answer with one line: the verdict it returned."
)

_FINDING: Final = re.compile(r"^FINDING\s+severity=(\w+)\s+actionable=(\w+)\s+file=([^|]*?)\s*\|\s*(.*)$", re.DOTALL)
_DESCRIPTION: Final = re.compile(r"^Review (\S+), round (\d+)")


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout carries the answer."""
    print(f"bench: {message}", file=sys.stderr)


def bench_dir() -> Path:
    """The data directory: ``ARL_BENCH_DIR``, or ``~/.local/share/arl-research``."""
    return Path(os.environ.get("ARL_BENCH_DIR") or Path.home() / ".local/share/arl-research").expanduser()


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _meta(case: str) -> dict[str, Any]:
    path = bench_dir() / "cases" / f"{case}.bench.json"
    return dict(_load_json(path)) if path.exists() else {}


#: A case or run name: one path segment that cannot be ``.`` or ``..``.
_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _named(kind: str, name: str) -> Path:
    """``<bench dir>/<kind>/<name>``, refused unless it is a plain name directly inside that directory."""
    parent = (bench_dir() / kind).resolve()
    path = parent / name
    if not _NAME.match(name) or path.is_symlink() or path.resolve().parent != parent:
        raise SystemExit(f"bench: {name!r} is not a plain {kind[:-1]} name (letters, digits, '.', '_', '-')")
    return path


def _fresh(path: Path, *, replace: bool) -> None:
    """Make room for a new case: an existing one is removed only when replacing was asked for."""
    if path.exists():
        if not replace:
            raise SystemExit(f"bench: {path} exists; pass --replace to replace it")
        shutil.rmtree(path)


def _phases() -> dict[str, dict[str, str]]:
    return dict(_load_json(BENCH / "phases.json"))


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


# --- scoring -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One stored ``FINDING`` line, its path relative to the root."""

    index: int
    severity: str
    actionable: bool
    path: str | None
    line: int | None
    text: str
    raw: str

    def blocks(self, block_severity: str) -> bool:
        """Actionable and at or above ``block_severity``."""
        return self.actionable and _SEVERITY_RANK.get(self.severity, -1) >= _SEVERITY_RANK[block_severity]


def parse_finding(index: int, raw: str, aliases: Iterable[str]) -> Finding | None:
    """Parse one stored line; ``None`` for one that is not a ``FINDING``."""
    match = _FINDING.match(raw.strip())
    if match is None:
        return None
    severity, actionable, file, text = match.groups()
    path: str | None = None
    line: int | None = None
    if file != "-":
        path, sep, number = file.rpartition(":")
        if not sep or not number.isdigit():
            path, number = file, ""
        line = int(number) if number else None
        for alias in aliases:
            if path.startswith(f"{alias}/"):
                path = path[len(alias) + 1 :]
                break
    return Finding(index, severity, actionable == "yes", path, line, text, raw)


def _aliases(state: Mapping[str, Any]) -> list[str]:
    return [str(root["alias"]) for root in state.get("roots", []) if isinstance(root, dict) and "alias" in root]


def findings_of(state: Mapping[str, Any], entry: Mapping[str, Any]) -> list[Finding]:
    """Every ``FINDING`` of one ``round_history`` entry."""
    aliases = _aliases(state)
    parsed = (parse_finding(index, raw, aliases) for index, raw in enumerate(entry.get("findings", [])) if isinstance(raw, str))
    return [finding for finding in parsed if finding is not None]


def _evidence(finding: Finding, defect: Mapping[str, Any]) -> tuple[int, bool] | None:
    """How a finding names a defect: its count of the defect's symbols, and whether its line is near.

    ``None`` when it does not name the defect at all: another file, no symbol and no near line.
    """
    if finding.path != defect["file"]:
        return None
    text = finding.text.casefold()
    symbols = sum(1 for symbol in defect.get("symbols", []) if str(symbol).casefold() in text)
    near = finding.line is not None and abs(finding.line - int(defect["line"])) <= LINE_WINDOW
    return (symbols, near) if symbols or near else None


def resolve(finding: Finding, defects: Sequence[Mapping[str, Any]]) -> tuple[str | None, list[str]]:
    """The one defect a finding names, or ``None`` and the candidates it cannot tell apart.

    A line is shared by every defect near it, so only symbols tell neighbours apart: with two or
    more candidates, the one with strictly the most symbols wins. A tie, or candidates named by
    their line alone, is ambiguous and is left to be judged by hand rather than guessed, because
    a guess between a fixed defect and a regression planted beside it decides a round-2 figure.
    """
    named = [(str(defect["id"]), evidence) for defect in defects if (evidence := _evidence(finding, defect)) is not None]
    if not named:
        return None, []
    if len(named) == 1:
        return named[0][0], []
    most = max(symbols for _, (symbols, _) in named)
    top = [defect for defect, (symbols, _) in named if symbols == most]
    if most and len(top) == 1:
        return top[0], []
    return None, [defect for defect, _ in named]


@dataclass(frozen=True)
class Assignment:
    """Which finding each defect was credited with, and the findings that named several."""

    hits: dict[str, Finding]
    #: Every finding resolved to some defect, the credited one or a duplicate of it.
    named: frozenset[int]
    ambiguous: list[dict[str, Any]]


def assign(findings: Sequence[Finding], defects: Sequence[Mapping[str, Any]], block_severity: str) -> Assignment:
    """Credit each defect with at most one finding; each finding names at most one defect.

    Each finding is first resolved to its defect (:func:`resolve`). Of the findings that name one
    defect, a blocking one is credited first, then one with a near line, then the earlier one.
    """
    by_defect: dict[str, list[Finding]] = collections.defaultdict(list)
    ambiguous: list[dict[str, Any]] = []
    for finding in findings:
        defect, candidates = resolve(finding, defects)
        if defect is not None:
            by_defect[defect].append(finding)
        elif candidates:
            ambiguous.append({"finding": finding.raw, "candidates": candidates})
    lines = {str(defect["id"]): int(defect["line"]) for defect in defects}

    def rank(finding: Finding, line: int) -> tuple[int, int, int]:
        near = finding.line is not None and abs(finding.line - line) <= LINE_WINDOW
        return (0 if finding.blocks(block_severity) else 1, 0 if near else 1, finding.index)

    hits = {defect: min(named, key=functools.partial(rank, line=lines[defect])) for defect, named in by_defect.items()}
    return Assignment(hits, frozenset(finding.index for named in by_defect.values() for finding in named), ambiguous)


def _hit_record(finding: Finding | None, block_severity: str) -> dict[str, Any]:
    if finding is None:
        return {"reported": None, "blocking": False}
    return {"reported": finding.severity, "blocking": finding.blocks(block_severity), "finding": finding.raw}


def _unmatched_blocking(findings: Sequence[Finding], assignment: Assignment, block_severity: str) -> list[str]:
    """Blocking findings that named no defect; an ambiguous one is listed under ``ambiguous`` instead."""
    ambiguous = {entry["finding"] for entry in assignment.ambiguous}
    return [
        finding.raw
        for finding in findings
        if finding.blocks(block_severity) and finding.index not in assignment.named and finding.raw not in ambiguous
    ]


def score_round1(
    state: Mapping[str, Any], entry: Mapping[str, Any], truth: Sequence[Mapping[str, Any]], *, block_severity: str = "medium"
) -> dict[str, Any]:
    """Reported and blocking recall of one round against a seeded phase's truth."""
    findings = findings_of(state, entry)
    assignment = assign(findings, truth, block_severity)
    hits = assignment.hits
    per_defect = {str(defect["id"]): _hit_record(hits.get(str(defect["id"])), block_severity) for defect in truth}
    return {
        "verdict": entry.get("verdict"),
        "reported": sum(1 for record in per_defect.values() if record["reported"]),
        "blocking": sum(1 for record in per_defect.values() if record["blocking"]),
        "of": len(truth),
        "defects": per_defect,
        "findings": len(findings),
        "unmatched_blocking": _unmatched_blocking(findings, assignment, block_severity),
        "ambiguous": assignment.ambiguous,
    }


def score_round2(
    state: Mapping[str, Any], entry: Mapping[str, Any], truth: Sequence[Mapping[str, Any]], *, block_severity: str = "medium"
) -> dict[str, Any]:
    """Recall per round-2 truth class; a finding that matches a ``fixed`` defect is a false re-raise."""
    unknown = sorted({str(defect.get("class")) for defect in truth} - set(ROUND2_CLASSES))
    if unknown:
        raise ValueError(f"unknown round-2 class {unknown}")
    findings = findings_of(state, entry)
    assignment = assign(findings, truth, block_severity)
    hits = assignment.hits
    classes: dict[str, dict[str, Any]] = {}
    for name in ROUND2_CLASSES:
        members = [defect for defect in truth if defect["class"] == name]
        if not members:
            continue
        records = {str(defect["id"]): _hit_record(hits.get(str(defect["id"])), block_severity) for defect in members}
        classes[name] = {
            "reported": sum(1 for record in records.values() if record["reported"]),
            "blocking": sum(1 for record in records.values() if record["blocking"]),
            "of": len(members),
            "defects": records,
        }
    reraised = [hits[str(defect["id"])] for defect in truth if defect["class"] == "fixed" and str(defect["id"]) in hits]
    return {
        "verdict": entry.get("verdict"),
        "classes": classes,
        "false_reraises": len(reraised),
        "false_reraises_blocking": sum(1 for finding in reraised if finding.blocks(block_severity)),
        "reraised": [finding.raw for finding in reraised],
        "findings": len(findings),
        "unmatched_blocking": _unmatched_blocking(findings, assignment, block_severity),
        "ambiguous": assignment.ambiguous,
    }


def _entry(state: Mapping[str, Any], index: int) -> Mapping[str, Any] | None:
    history = state.get("round_history", [])
    return history[index] if 0 <= index < len(history) else None


def score_case(case: str, *, round2: bool, truth_path: Path | None, block_severity: str) -> dict[str, Any]:
    """Score the round a case was built to review: round 1, or the round after ``round2``'s copy."""
    meta = _meta(case)
    phase = str(meta.get("phase", ""))
    if truth_path is None:
        if not phase:
            raise SystemExit(f"bench: {case} has no recorded phase; pass --truth")
        truth_path = SEEDS / (f"{phase}.round2.truth.json" if round2 else f"{phase}.truth.json")
    truth = _load_json(truth_path)
    state_path = bench_dir() / "cases" / case / ".arl" / "state.json"
    if not state_path.exists():
        return {"case": case, "error": "no state"}
    state = _load_json(state_path)
    index = int(meta.get("history_offset", 1 if round2 else 0))
    entry = _entry(state, index)
    if entry is None:
        return {"case": case, "error": f"no recorded round at index {index} (the review failed or never ran)"}
    scorer = score_round2 if round2 else score_round1
    return {"case": case, **scorer(state, entry, truth, block_severity=block_severity)}


# --- analysis ----------------------------------------------------------------------------------


def _rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


#: A bundle diff chunk, as the review prompt names it.
_DIFF_CHUNK: Final = re.compile(r"/bundle/changes\.\d+\.diff$")
#: One line of a Read result: its line number, a tab, the text.
_NUMBERED: Final = re.compile(r"^\s*(\d+)\t", re.MULTILINE)


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(part.get("text", "")) for part in content or [] if isinstance(part, dict))


#: Where the review prompt names its bundle, and each bundle file it lists.
_BUNDLE_DIR: Final = re.compile(r"The bundle is the directory `([^`]+)`")
_LISTED_CHUNK: Final = re.compile(r"`(/[^`]*/bundle/changes\.\d+\.diff)`")


def _prompt_text(rows: Sequence[Mapping[str, Any]]) -> str:
    """The task the reviewer was given: its transcript's first user message."""
    for row in rows:
        message = row.get("message")
        if row.get("type") == "user" and isinstance(message, dict):
            content = message.get("content")
            return content if isinstance(content, str) else _result_text(message)
    return ""


def bundle_chunks(rows: Sequence[Mapping[str, Any]]) -> list[str] | None:
    """Every ``changes.NN.diff`` of the bundle the reviewer was given, whatever it went on to read.

    The bundle directory the prompt names is listed on disk, and every chunk the prompt lists is
    added. ``None`` when the prompt names no bundle: then what the review should have read is not
    known, and coverage must not be reported as complete.
    """
    prompt = _prompt_text(rows)
    named = _BUNDLE_DIR.search(prompt)
    listed = set(_LISTED_CHUNK.findall(prompt))
    if named is None and not listed:
        return None
    if named is not None:
        listed.update(str(path) for path in Path(named.group(1)).glob("changes.*.diff") if _DIFF_CHUNK.search(str(path)))
    return sorted(listed)


def diff_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Which lines of each bundle ``changes.NN.diff`` the reviewer's reads returned.

    The chunks expected come from the bundle itself (:func:`bundle_chunks`), so a chunk never
    opened, or opened by a read that got no answer, counts with ``0`` lines read. Lines read are
    taken from the numbered lines of each ``Read`` result, so a read the tool cut short counts
    only for what it returned. A file's length is read from disk, ``None`` when it is gone, and
    the total is ``None`` when any length or the bundle itself is unknown. A line returned twice
    counts once, and ``reread`` counts the extra returns.
    """
    targets: dict[str, str] = {}
    for row in rows:
        message = row.get("message")
        if row.get("type") != "assistant" or not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Read":
                target = str((block.get("input") or {}).get("file_path") or "")
                if _DIFF_CHUNK.search(target):
                    targets[str(block.get("id"))] = target
    expected = bundle_chunks(rows)
    seen: dict[str, collections.Counter[int]] = {target: collections.Counter() for target in expected or []}
    answered: set[str] = set()
    for row in rows:
        message = row.get("message")
        if row.get("type") != "user" or not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            use = str(block.get("tool_use_id")) if isinstance(block, dict) and block.get("type") == "tool_result" else ""
            # A transcript can repeat a row; its result is counted once.
            if use in targets and use not in answered:
                answered.add(use)
                seen.setdefault(targets[use], collections.Counter()).update(int(number) for number in _NUMBERED.findall(_result_text(block)))
    files: dict[str, dict[str, int | None]] = {}
    for target, lines in sorted(seen.items()):
        try:
            length: int | None = len(Path(target).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            length = None
        # Read numbers the empty line after a final newline too: only the file's own lines count.
        kept = {number: count for number, count in lines.items() if length is None or 1 <= number <= length}
        files[Path(target).name] = {"read": len(kept), "lines": length, "reread": sum(count - 1 for count in kept.values())}
    lengths = [entry["lines"] for entry in files.values()]
    return {
        "files": files,
        "read": sum(int(entry["read"] or 0) for entry in files.values()),
        "lines": None if expected is None or None in lengths else sum(int(length or 0) for length in lengths),
    }


def analyze_transcript(path: Path) -> dict[str, Any]:
    """Requests, tool calls, tokens and cost units of one reviewer subagent transcript.

    A response split over several transcript rows shares one ``message.id``; its usage is counted
    once and its content blocks are joined.
    """
    messages: dict[str, dict[str, Any]] = {}
    rows = _rows(path)
    for row in rows:
        if row.get("type") != "assistant" or not isinstance(row.get("message"), dict):
            continue
        message = row["message"]
        entry = messages.setdefault(str(message.get("id")), {"usage": message.get("usage") or {}, "model": message.get("model"), "blocks": []})
        entry["blocks"].extend(block for block in message.get("content") or [] if isinstance(block, dict))
    calls = [block for entry in messages.values() for block in entry["blocks"] if block.get("type") == "tool_use"]
    per_turn = [sum(1 for block in entry["blocks"] if block.get("type") == "tool_use") for entry in messages.values()]
    reads: collections.Counter[str] = collections.Counter()
    bundle = outside = 0
    for block in calls:
        if block.get("name") != "Read":
            continue
        target = str((block.get("input") or {}).get("file_path") or "")
        reads[target] += 1
        if "/.arl/" in target:
            bundle += 1
        else:
            outside += 1

    def total(key: str) -> int:
        return sum(int(entry["usage"].get(key) or 0) for entry in messages.values())

    tokens = {
        "in": total("input_tokens"),
        "cw": total("cache_creation_input_tokens"),
        "cr": total("cache_read_input_tokens"),
        "out": total("output_tokens"),
    }
    last = next(reversed(messages.values()))["usage"] if messages else {}
    first_model = next(iter(messages.values()))["model"] if messages else None
    return {
        "transcript": str(path),
        "model": first_model,
        "requests": len(messages),
        "tool_calls": len(calls),
        "max_calls_per_turn": max(per_turn, default=0),
        "single_call_turns": sum(1 for count in per_turn if count == 1),
        "tools": dict(collections.Counter(str(block.get("name")) for block in calls)),
        "bundle_reads": bundle,
        "repo_reads": outside,
        "repeat_reads": sum(count - 1 for count in reads.values() if count > 1),
        **{f"tok_{key}": value for key, value in tokens.items()},
        "final_context": sum(int(last.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "cost_units_k": round(sum(tokens[key] * weight for key, weight in COST_WEIGHTS.items()) / 1000, 1),
        "diff_coverage": diff_coverage(rows),
    }


def _project_bucket(case_dir: Path) -> Path:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    return config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(case_dir))


def reviewer_transcripts(bucket: Path, sessions: Sequence[str] | None = None) -> list[tuple[Path, str]]:
    """The reviewer subagents' transcripts under a project bucket, oldest first, with their task description.

    ``sessions`` names the main sessions to read. A case name is reused when a matrix runs again,
    and Claude Code keeps the old sessions' transcripts in the same bucket, so a case reads only the
    sessions its own runs started. ``None`` reads every session (a case made before sessions were
    recorded).
    """
    found: list[tuple[Path, str]] = []
    patterns = ["*/subagents/agent-*.jsonl"] if sessions is None else [f"{session}/subagents/agent-*.jsonl" for session in sessions]
    paths = [path for pattern in patterns for path in bucket.glob(pattern)]
    for path in sorted(paths, key=lambda candidate: candidate.stat().st_mtime):
        meta_path = path.with_suffix(".meta.json")
        meta = _load_json(meta_path) if meta_path.exists() else {}
        if meta and meta.get("agentType") != REVIEWER_AGENT:
            continue
        found.append((path, str(meta.get("description", ""))))
    return found


def _severities(findings: Sequence[Finding]) -> dict[str, int]:
    return dict(collections.Counter(finding.severity for finding in findings))


def analyze_case(
    bucket: Path, state: Mapping[str, Any] | None, *, history_offset: int = 0, sessions: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """One row per reviewer transcript, joined to its ``round_history`` entry when one can be named.

    A transcript's task description (``Review <label>, round <n>``) names the round. Entries before
    ``history_offset`` were recorded in the case a round-2 case was copied from, by transcripts
    stored under that case's own bucket.
    """
    history = list((state or {}).get("round_history", []))
    out: list[dict[str, Any]] = []
    for path, description in reviewer_transcripts(bucket, sessions):
        row = {"description": description, **analyze_transcript(path)}
        match = _DESCRIPTION.match(description)
        if match and state is not None:
            label, number = match.group(1), int(match.group(2))
            same = [entry for entry in history if entry.get("label") == label]
            if len(same) >= number and history.index(same[number - 1]) >= history_offset:
                entry = same[number - 1]
                row["verdict"] = entry.get("verdict")
                row["findings"] = _severities(findings_of(state, entry))
        out.append(row)
    return out


# --- cases and runs ----------------------------------------------------------------------------


def _plugin_dir(spec: str) -> Path:
    """A plugin directory: a path, or ``@<rev>`` for this repository's tree at that revision."""
    if not spec.startswith("@"):
        return Path(spec).expanduser().resolve()
    sha = _git("rev-parse", "--verify", f"{spec[1:]}^{{commit}}", cwd=REPO).strip()
    target = bench_dir() / "plugins" / f"rev-{sha[:12]}"
    if not target.exists():
        # A staging directory of its own: several runs may extract the same revision at once.
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{target.name}.tmp-", dir=target.parent))
        archive = subprocess.run(["git", "archive", sha], cwd=REPO, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(staging)], input=archive, check=True)
        try:
            staging.rename(target)
        except OSError:
            # Another run got there first; its copy is the same tree.
            shutil.rmtree(staging, ignore_errors=True)
            if not target.is_dir():
                raise
    return target


def make_case(  # noqa: PLR0913 - each is an independent property of the case
    commit: str, name: str, *, seed: Path | None, plugin: Path, phase: str = "", replace: bool = False
) -> Path:
    """Clone at ``commit^``, initialise a one-phase run, move the tree to ``commit``, apply ``seed``."""
    cases = bench_dir() / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    case_dir = _named("cases", name)
    _fresh(case_dir, replace=replace)
    subprocess.run(["git", "clone", "-q", str(REPO), str(case_dir)], check=True)
    _git("checkout", "-q", f"{commit}^", cwd=case_dir)
    subject = _git("log", "-1", "--format=%s", commit, cwd=REPO).strip()
    body = _git("log", "-1", "--format=%b", commit, cwd=REPO)
    plan = cases / f"{name}.plan.md"
    plan.write_text(f"# Plan\n\n## Phase 1: {subject}\n\n{body}", encoding="utf-8")
    arl = [str(plugin / "scripts" / "arl.sh"), "--dir", str(case_dir)]
    subprocess.run([*arl, "init", "--plan", str(plan)], check=True, capture_output=True)
    subprocess.run([*arl, "set-phases"], input=json.dumps([subject]), text=True, check=True, capture_output=True)
    _git("checkout", "-q", commit, "--", ".", cwd=case_dir)
    for deleted in _git("diff", "--name-only", "--diff-filter=D", f"{commit}^", commit, cwd=case_dir).splitlines():
        (case_dir / deleted).unlink(missing_ok=True)
    if seed is not None:
        _git("apply", str(seed), cwd=case_dir)
    _git("reset", "-q", cwd=case_dir)
    _write_json(
        cases / f"{name}.bench.json",
        {
            "phase": phase,
            "commit": commit,
            "seed": str(seed) if seed else None,
            "round": 1,
            "history_offset": 0,
            "sessions": [],
            "created": time.time(),
        },
    )
    return case_dir


def _renamed(value: Any, old_path: str, new_path: str, old_alias: str | None, new_alias: str) -> Any:
    if isinstance(value, str):
        value = value.replace(old_path, new_path)
        if old_alias is not None:
            value = new_alias if value == old_alias else value.replace(f"{old_alias}/", f"{new_alias}/")
        return value
    if isinstance(value, list):
        return [_renamed(item, old_path, new_path, old_alias, new_alias) for item in value]
    if isinstance(value, dict):
        # Keys too: a round's ``trees`` are keyed by alias, and the late-round scope reads them.
        return {
            _renamed(key, old_path, new_path, old_alias, new_alias): _renamed(item, old_path, new_path, old_alias, new_alias)
            for key, item in value.items()
        }
    return value


def make_round2(phase: str, source: str, name: str, *, fix: Path | None = None, replace: bool = False) -> Path:
    """Copy a recorded round-1 case and apply the phase's fix patch, so the next review is round 2."""
    cases = bench_dir() / "cases"
    old_dir, new_dir = _named("cases", source), _named("cases", name)
    if old_dir == new_dir:
        raise SystemExit("bench: the round-2 case needs a name of its own; it would replace its source")
    state_path = old_dir / ".arl" / "state.json"
    if not state_path.exists():
        raise SystemExit(f"bench: {source} has no recorded run")
    state = _load_json(state_path)
    if int(_meta(source).get("round", 1)) != 1:
        raise SystemExit(f"bench: {source} is not a round-1 case")
    if state.get("pending_ticket") is not None:
        raise SystemExit(f"bench: {source} has a review pending; it never finished round 1")
    history = state.get("round_history", [])
    if not history or history[-1].get("verdict") != "CHANGES_REQUIRED":
        raise SystemExit(f"bench: {source} did not end round 1 with CHANGES_REQUIRED; there is nothing to re-review")
    _fresh(new_dir, replace=replace)
    shutil.copytree(old_dir, new_dir, symlinks=True)
    old_alias = source if source in _aliases(state) else None
    _write_json(new_dir / ".arl" / "state.json", _renamed(state, str(old_dir), str(new_dir), old_alias, name))
    _git("apply", str(fix or SEEDS / f"{phase}.round2.patch"), cwd=new_dir)
    _git("reset", "-q", cwd=new_dir)
    source_meta = _meta(source)
    _write_json(
        cases / f"{name}.bench.json",
        {
            **source_meta,
            "phase": phase,
            "round": 2,
            "from": source,
            "history_offset": len(history),
            "sessions": [],
            "created": time.time(),
        },
    )
    return new_dir


def record_session(case: str) -> str:
    """A new session id for the next run of ``case``, recorded before the run so a timeout keeps it."""
    session = str(uuid.uuid4())
    meta = _meta(case)
    meta["sessions"] = [*meta.get("sessions", []), session]
    _write_json(bench_dir() / "cases" / f"{case}.bench.json", meta)
    return session


def run_review(case: str, model: str, plugin: Path, tag: str, *, timeout: int = 1500) -> dict[str, Any]:
    """One headless review of a prepared case; the reviewer's model comes through ``ARL_MODEL``."""
    case_dir = _named("cases", case)
    if not case_dir.is_dir():
        raise SystemExit(f"bench: no case {case!r}")
    (bench_dir() / "runs").mkdir(parents=True, exist_ok=True)
    out = _named("runs", tag)
    out.mkdir(exist_ok=True)
    session = record_session(case)
    command = [
        "claude", "-p", RUN_PROMPT, "--session-id", session, "--plugin-dir", str(plugin), "--model", "haiku",
        "--setting-sources", "project,local", "--strict-mcp-config", "--permission-mode", "default", "--output-format", "json",
    ]  # fmt: skip
    start = time.monotonic()
    with (out / "main.json").open("wb") as stdout, (out / "stderr.txt").open("wb") as stderr:
        try:
            code = subprocess.run(
                command,
                cwd=case_dir,
                env={**os.environ, "ARL_MODEL": model},
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            code = -1
    result = {"case": case, "session": session, "model": model, "plugin": str(plugin), "exit": code, "secs": round(time.monotonic() - start)}
    _write_json(out / "exit.json", result)
    arl_dir = case_dir / ".arl"
    if (arl_dir / "reports").is_dir():
        shutil.copytree(arl_dir / "reports", out / "reports", dirs_exist_ok=True)
    if (arl_dir / "state.json").exists():
        shutil.copy2(arl_dir / "state.json", out / "state.json")
    return result


def _analyze(case: str) -> list[dict[str, Any]]:
    case_dir = bench_dir() / "cases" / case
    state_path = case_dir / ".arl" / "state.json"
    state = _load_json(state_path) if state_path.exists() else None
    meta = _meta(case)
    sessions = meta.get("sessions")
    if sessions is None:
        log(f"{case}: no sessions recorded; reading every session in its transcript bucket")
    rows = analyze_case(_project_bucket(case_dir), state, history_offset=int(meta.get("history_offset", 0)), sessions=sessions)
    return [{"case": case, **row} for row in rows]


@dataclass(frozen=True)
class Cell:
    """One cell of a matrix: a fresh case, one headless review, its analysis and score."""

    phase: str
    model: str
    variant: str
    plugin: Path
    seeded: bool
    tag: str


def _cell(cell: Cell, timeout: int, replace: bool) -> dict[str, Any]:
    head: dict[str, Any] = {"case": cell.tag, "phase": cell.phase, "model": cell.model, "variant": cell.variant, "seeded": cell.seeded}
    try:
        seed = SEEDS / f"{cell.phase}.patch" if cell.seeded else None
        make_case(_phases()[cell.phase]["commit"], cell.tag, seed=seed, plugin=cell.plugin, phase=cell.phase, replace=replace)
        result = run_review(cell.tag, cell.model, cell.plugin, cell.tag, timeout=timeout)
        rows = _analyze(cell.tag)
        state_path = bench_dir() / "cases" / cell.tag / ".arl" / "state.json"
        history = _load_json(state_path).get("round_history", []) if state_path.exists() else []
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
        # One broken cell must not lose the others' results.
        log(f"{cell.tag}: {exc}")
        return {**head, "error": str(exc), "requests": 0, "cost_units_k": 0}
    summary: dict[str, Any] = {
        **result, **head, "verdict": history[-1].get("verdict") if history else "no recorded round",
        "requests": sum(row["requests"] for row in rows), "cost_units_k": round(sum(row["cost_units_k"] for row in rows), 1),
        # A round-1 review must read the whole diff: a cheaper one that did not is no saving.
        # The review's own transcript, not its repair's, which reads only the transcript tail.
        "diff_read": next((row["diff_coverage"]["read"] for row in rows if "(repair)" not in row["description"]), 0),
        "diff_lines": next((row["diff_coverage"]["lines"] for row in rows if "(repair)" not in row["description"]), None),
    }  # fmt: skip
    if cell.seeded:
        summary["score"] = score_case(cell.tag, round2=False, truth_path=None, block_severity="medium")
    return summary


def matrix(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Every cell of phases x models x plugin variants x {seeded, clean} x runs, ``--jobs`` at a time."""
    variants = [(name, _plugin_dir(path)) for name, _, path in (spec.partition("=") for spec in args.plugin)]
    kinds = [kind == "seeded" for kind in args.kind]
    cells = [
        Cell(phase, model, variant, plugin, seeded, f"{args.prefix}-{phase}-{model}-{variant}-{'seed' if seeded else 'clean'}-{index}")
        for phase in args.phase
        for model in args.model
        for variant, plugin in variants
        for seeded in kinds
        for index in range(1, args.runs + 1)
        if not seeded or (SEEDS / f"{phase}.patch").exists()
    ]
    taken = [cell.tag for cell in cells if _named("cases", cell.tag).exists()]
    if taken and not args.replace:
        # Checked before any cell runs, so a refused matrix spends nothing.
        raise SystemExit(f"bench: {len(taken)} cases of prefix {args.prefix!r} exist (first {taken[0]}); pass --replace or another --prefix")
    log(f"{len(cells)} cells, {args.jobs} at a time; each one calls a model")
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda cell: _cell(cell, args.timeout, args.replace), cells))
    _write_json(bench_dir() / "runs" / f"{args.prefix}.summary.json", results)
    return results


def _table(results: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| Phase | Model | Variant | Kind | Requests | Units (k) | Diff read | Verdict | Reported | Blocking |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        score = result.get("score") or {}
        verdict = result.get("error") or result.get("verdict", "")
        reported = f"{score['reported']}/{score['of']}" if "of" in score else ""
        blocking = f"{score['blocking']}/{score['of']}" if "of" in score else ""
        kind = "seeded" if result["seeded"] else "clean"
        read = f"{result.get('diff_read', 0)}/{result.get('diff_lines') or '?'}"
        lines.append(
            f"| {result['phase']} | {result['model']} | {result['variant']} | {kind} | {result['requests']} | {result['cost_units_k']} | {read} | {verdict} | {reported} | {blocking} |"
        )
    return "\n".join(lines)


# --- command line ------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench.py", description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    case = sub.add_parser("case", help="prepare a case: clone at <commit>^, initialise a run, move to <commit>")
    case.add_argument("commit")
    case.add_argument("name")
    case.add_argument("--seed", type=Path, help="a seed patch to apply on top of the commit")
    case.add_argument("--phase", default="", help="the seeded phase this case reviews (names its truth)")
    case.add_argument("--plugin", default=str(REPO), help="plugin directory, or @<rev> (default: this checkout)")
    case.add_argument("--replace", action="store_true", help="replace a case of that name")

    run = sub.add_parser("run", help="one headless review of a prepared case (calls a model)")
    run.add_argument("case")
    run.add_argument("--model", required=True, help="the reviewer model, passed as ARL_MODEL")
    run.add_argument("--plugin", default=str(REPO))
    run.add_argument("--tag", help="the runs/ directory name (default: the case name)")
    run.add_argument("--timeout", type=int, default=1500)

    round2 = sub.add_parser("round2", help="copy a recorded round-1 case and apply the phase's fix patch")
    round2.add_argument("phase")
    round2.add_argument("source")
    round2.add_argument("name")
    round2.add_argument("--fix", type=Path, help="a fix patch other than seeds/<phase>.round2.patch")
    round2.add_argument("--replace", action="store_true", help="replace a case of that name")

    analyze = sub.add_parser("analyze", help="one JSON row per reviewer transcript of each case")
    analyze.add_argument("cases", nargs="+")

    score = sub.add_parser("score", help="seeded-defect recall of each case's reviewed round")
    score.add_argument("cases", nargs="+")
    score.add_argument("--round2", action="store_true", help="score against the round-2 truth manifest")
    score.add_argument("--truth", type=Path, help="a truth file other than the case's phase's")
    score.add_argument("--block-severity", default="medium", choices=sorted(_SEVERITY_RANK, key=_SEVERITY_RANK.__getitem__))

    grid = sub.add_parser("matrix", help="prepare, run, analyze and score a grid of round-1 cells (calls models)")
    grid.add_argument("--phase", action="append", help="phase name from phases.json (repeatable)")
    grid.add_argument("--model", action="append", help="reviewer model (repeatable)")
    grid.add_argument("--plugin", action="append", help="<variant>=<dir|@rev> (repeatable; default head=<this checkout>)")
    grid.add_argument("--kind", action="append", choices=["seeded", "clean"], help="seeded or clean cells (repeatable)")
    grid.add_argument("--runs", type=int, default=1)
    grid.add_argument("--jobs", type=int, default=4)
    grid.add_argument("--prefix", default="bench")
    grid.add_argument("--timeout", type=int, default=1500)
    grid.add_argument("--replace", action="store_true", help="replace cases left by an earlier matrix of the same prefix")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one subcommand; JSON on stdout, diagnostics on stderr."""
    args = _parser().parse_args(argv)
    if args.command == "case":
        print(
            make_case(
                args.commit,
                args.name,
                seed=args.seed and args.seed.resolve(),
                plugin=_plugin_dir(args.plugin),
                phase=args.phase,
                replace=args.replace,
            )
        )
    elif args.command == "run":
        print(json.dumps(run_review(args.case, args.model, _plugin_dir(args.plugin), args.tag or args.case, timeout=args.timeout)))
    elif args.command == "round2":
        print(make_round2(args.phase, args.source, args.name, fix=args.fix and args.fix.resolve(), replace=args.replace))
    elif args.command == "analyze":
        for case in args.cases:
            for row in _analyze(case):
                print(json.dumps(row))
    elif args.command == "score":
        for case in args.cases:
            print(json.dumps(score_case(case, round2=args.round2, truth_path=args.truth, block_severity=args.block_severity)))
    else:
        args.phase = args.phase or ["medium", "large1"]
        args.model = args.model or ["opus", "sonnet"]
        args.plugin = args.plugin or [f"head={REPO}"]
        args.kind = args.kind or ["seeded", "clean"]
        results = matrix(args)
        print(_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
