"""``stats``: what each review round cost and found, as ``/arl-stats`` shows it."""

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

import io
import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from run_common import arl, run_env, ws

from arl import cli, state, stats

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any

USAGE = {"turns": 6, "input_tokens": 10, "cache_creation_tokens": 1990, "cache_read_tokens": 30000, "output_tokens": 400, "duration_ms": 9000}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


def answer(directory: Path, *lines: str) -> Path:
    path = directory / f"answer-{len(list(directory.glob('answer-*'))):03d}.md"
    path.write_text("Prose.\n\n<<<ARL-FINDINGS>>>\n" + "".join(f"{line}\n" for line in lines) + "<<<ARL-END>>>\n")
    return path


def edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def round_(engine: Engine, launch: Path, *lines: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    ticket = engine(launch, "review", "prepare")
    assert ticket["ok"], ticket
    extra = ["--usage", json.dumps(usage)] if usage is not None else []
    result: dict[str, Any] = engine(launch, "review", "record", ticket["review_id"], "--answer", str(answer(launch.parent, *lines)), *extra)
    return result


def started(engine: Engine, launch: Path) -> None:
    assert engine(launch, "init", "--plan", str(launch / "plan.md"))["ok"]
    assert engine(launch, "set-phases", stdin=json.dumps(["parse the input", "write the output"]))["ok"]


BLOCKING = "FINDING severity=high actionable=yes file=api/src/main.py:1 | broken"
LOW = "FINDING severity=low actionable=yes file=api/src/main.py:2 | nit"


def test_stats_lists_every_round_and_failure_with_its_cost(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    round_(engine, ws, BLOCKING, LOW, "VERDICT CHANGES_REQUIRED", usage={**USAGE, "model": "claude-opus-5-5"})
    ticket = engine(ws, "review", "prepare")
    engine(ws, "review", "record", ticket["review_id"], "--failed", "transient", "--error", "API error", "--usage", json.dumps({"turns": 2}))
    edit(ws / "api" / "src" / "main.py", "print(3)\n")
    round_(engine, ws, "VERDICT APPROVED", usage=USAGE)

    answer_ = engine(ws, "stats", "--json")

    figures = answer_["stats"]
    [stage] = figures["stages"]
    assert (stage["label"], stage["title"]) == ("phase1", "parse the input")
    first, failed, second = stage["rounds"]
    assert (first["round"], first["verdict"], first["model"], first["requests"]) == (1, "CHANGES_REQUIRED", "claude-opus-5-5", 6)
    assert first["tokens"] == {"new": 2000, "cached": 30000, "output": 400}
    assert (first["findings"], first["blocking"]) == ({"high": 1, "low": 1}, 1)
    # A failure recorded nothing, so the attempt after it is still round 2.
    assert (failed["round"], failed["verdict"], failed["kind"], failed["requests"]) == (2, "OP_FAILURE", "transient", 2)
    assert (second["round"], second["verdict"]) == (2, "APPROVED")
    totals = figures["totals"]
    assert (totals["rounds"], totals["failed"]) == (2, 1)
    assert totals["requests"] == {"total": 14, "missing": 0}
    # The failure reported no tokens: summed over the rest, and said to be missing.
    assert totals["tokens_cached"] == {"total": 60000, "missing": 1}

    text = answer_["text"]
    assert "Phase 1: parse the input" in text
    assert "#1 round 1 · CHANGES_REQUIRED · claude-opus-5-5 · 6 requests · tokens new 2k cached 30k out 400" in text
    assert "findings 1 high, 1 low · blocking 1" in text
    assert "OP_FAILURE (transient)" in text
    assert "cached 60k (1 not reported)" in text
    assert "not proof that any was fixed" in text


def test_a_round_recorded_before_usage_existed_shows_its_figures_as_missing(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    round_(engine, ws, BLOCKING, "VERDICT CHANGES_REQUIRED")
    paths = state.run_paths(ws)
    document = json.loads(paths.state_file.read_text())
    for key in ("usage", "model", "harness", "round", "blocking", "deferred"):
        del document["round_history"][0][key]
    del document["failure_history"]
    paths.state_file.write_text(json.dumps(document))

    figures = engine(ws, "stats")["stats"]

    [row] = figures["stages"][0]["rounds"]
    assert (row["round"], row["requests"], row["blocking"], row["model"]) == (1, None, None, "")
    assert row["tokens"] == {"new": None, "cached": None, "output": None}
    assert figures["totals"]["requests"] == {"total": None, "missing": 1}
    text = engine(ws, "stats")["text"]
    # Missing is "?", never 0: a round with no figures was not free.
    assert "? · ? requests · tokens new ? cached ? out ?" in text
    assert "blocking ?" in text
    assert "Total: ? requests" in text


def test_rounds_of_a_replaced_phase_list_are_marked(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    round_(engine, ws, BLOCKING, "VERDICT CHANGES_REQUIRED", usage=USAGE)
    assert engine(ws, "set-phases", "--replan", stdin=json.dumps(["one phase only"]))["ok"]
    round_(engine, ws, "VERDICT APPROVED", usage=USAGE)

    figures = engine(ws, "stats")["stats"]
    old, new = figures["stages"]
    assert (old["revision"], old["title"]) == (1, "")
    assert (new["revision"], new["title"], new["rounds"][0]["round"]) == (2, "one phase only", 1)
    assert "(phase list revision 1, since replanned)" in stats.render(figures)


def test_stats_with_no_round_says_so(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    answer_ = engine(ws, "stats")
    assert answer_["text"] == f"Run {answer_['stats']['run_id']}: no review has been recorded yet.\n"


def test_stats_without_a_run_is_refused(run_env: Path, engine: Engine) -> None:
    answer_ = engine(run_env, "stats")
    assert (answer_["ok"], answer_["code"]) == (False, "no-run")


@pytest.mark.parametrize(("value", "shown"), [(None, "?"), (0, "0"), (999, "999"), (1499, "1k"), (12_400, "12k"), (2_345_678, "2.3M")])
def test_token_counts_are_short(value: int | None, shown: str) -> None:
    row = {
        "seq": 1,
        "round": 1,
        "verdict": "APPROVED",
        "model": "m",
        "requests": 1,
        "tokens": {"new": value, "cached": 0, "output": 0},
        "cost_usd": None,
    }
    assert f"tokens new {shown} cached" in stats._line(row)


def _strict(raw: str) -> Any:
    def refuse(constant: str) -> None:
        raise ValueError(f"not JSON: {constant}")

    return json.loads(raw, parse_constant=refuse)


@pytest.mark.parametrize("stored", [float("inf"), float("nan"), -1.0, "1.5"])
def test_a_stored_cost_that_is_not_a_finite_number_is_shown_as_missing(
    ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stored: object
) -> None:
    """A state written before costs were checked must not make the answer JSON a strict reader refuses."""
    started(engine, ws)
    edit(ws / "api" / "src" / "main.py", "print(2)\n")
    round_(engine, ws, "VERDICT APPROVED", usage=USAGE)
    paths = state.run_paths(ws)
    document = json.loads(paths.state_file.read_text())
    document["round_history"][0]["usage"]["cost_usd"] = stored
    paths.state_file.write_text(json.dumps(document))

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    cli.main(["--dir", str(ws), "stats"])
    answer_ = _strict(capsys.readouterr().out)

    assert answer_["stats"]["stages"][0]["rounds"][0]["cost_usd"] is None
    assert answer_["stats"]["totals"]["cost_usd"] == {"total": None, "missing": 1}
