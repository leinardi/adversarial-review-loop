"""This run's own reviewer model (``model``, ``/arl-model``, ``init --model``).

The choice lives in the run state, beats the config files and loses to ``ARL_*``. A literal is
checked before it is stored; a prepared review keeps the model its ticket recorded, whatever
changes before it runs.
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

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from run_common import arl, run_env, ws

from arl import config, harness, reviewer_probe, runner, state
from arl.harness import opencode

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any

APPROVE = "<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n"
BLOCK = "<<<ARL-FINDINGS>>>\nFINDING severity=high actionable=yes file=api/src/main.py:1 | broken\nVERDICT CHANGES_REQUIRED\n<<<ARL-END>>>\n"


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the fake ``opencode models`` lists; tests edit it."""
    models = ["openai/gpt-6.1-sol", "openai/gpt-6.1-terra", "vendor/other"]
    monkeypatch.setattr(opencode.HARNESS.__class__, "probe_models", lambda _self, _timeout: list(models))
    return models


def started(engine: Engine, launch: Path, *extra: str) -> dict[str, Any]:
    answer: dict[str, Any] = engine(launch, "init", "--plan", str(launch / "plan.md"), *extra)
    if answer["ok"]:
        assert engine(launch, "set-phases", stdin=json.dumps(["phase 1", "phase 2"]))["ok"]
    return answer


def edit(path: Path, text: str) -> None:
    path.write_text(text)


def user_config(document: dict[str, Any]) -> None:
    """A user config: outside every root, so writing it during a review changes no tree."""
    path = config.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def load(launch: Path) -> state.RunState:
    run = state.RunState.load(state.run_paths(launch))
    assert run is not None
    return run


def prepared(engine: Engine, launch: Path, text: str = "print(2)\n") -> dict[str, Any]:
    edit(launch / "api" / "src" / "main.py", text)
    ticket: dict[str, Any] = engine(launch, "review", "prepare")
    assert ticket["ok"], ticket
    return ticket


def answered(engine: Engine, launch: Path, ticket: dict[str, Any], tmp_path: Path, body: str) -> dict[str, Any]:
    answer = tmp_path / f"answer-{ticket['review_id']}.md"
    answer.write_text(f"Read it.\n\n{body}")
    result: dict[str, Any] = engine(launch, "review", "record", ticket["review_id"], "--answer", str(answer))
    return result


def model(engine: Engine, launch: Path, *words: str) -> dict[str, Any]:
    answer: dict[str, Any] = engine(launch, "model", *words)
    return answer


# --------------------------------------------------------------------------
# The overlay
# --------------------------------------------------------------------------


def test_a_run_choice_moves_every_round_and_says_where_it_came_from(ws: Path, engine: Engine, tmp_path: Path) -> None:
    started(engine, ws)
    shown = model(engine, ws, "set", "all", "weak")
    assert shown["overlay"] == {"model": "weak", "late_model": "weak"}
    assert "final review: weak (sonnet)  (run)" in shown["text"]
    assert "later rounds: weak (sonnet)  (run)" in shown["text"]
    first = prepared(engine, ws)
    assert (first["round"], first["reviewer_model"], first["model"]) == (1, "sonnet", "weak (sonnet)")
    assert load(ws).data["pending_ticket"]["model_id"] == "sonnet"
    # Never written to a config file.
    assert not (ws / config.REPO_CONFIG_NAME).exists()
    assert not config.user_config_path().exists()
    del tmp_path


def test_first_moves_round_1_and_the_final_review_late_moves_the_rest(ws: Path, engine: Engine, tmp_path: Path) -> None:
    started(engine, ws)
    model(engine, ws, "set", "first", "haiku")
    model(engine, ws, "set", "late", "strong")
    first = prepared(engine, ws)
    assert first["reviewer_model"] == "haiku"
    answered(engine, ws, first, tmp_path, BLOCK)
    second = prepared(engine, ws, "print(3)\n")
    assert (second["round"], second["reviewer_model"]) == (2, "opus")


def test_the_overlay_survives_resume_a_reload_and_a_replan(ws: Path, engine: Engine) -> None:
    started(engine, ws, "--model", "weak")
    assert load(ws).model_overlay == {"model": "weak", "late_model": "weak"}
    assert engine(ws, "resume")["ok"]
    assert engine(ws, "set-phases", "--replan", stdin=json.dumps(["one", "two", "three"]))["ok"]
    assert load(ws).model_overlay == {"model": "weak", "late_model": "weak"}
    assert prepared(engine, ws)["reviewer_model"] == "sonnet"


def test_a_new_run_starts_without_the_overlay(ws: Path, engine: Engine) -> None:
    started(engine, ws, "--model", "weak")
    assert started(engine, ws, "--new")["ok"]
    assert load(ws).model_overlay == {}
    assert prepared(engine, ws)["reviewer_model"] == "opus"


def test_a_run_from_before_the_overlay_has_none(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    run = load(ws)
    run.data.pop("model_overlay", None)
    run.data["model_overlay"] = {"model": 3, "late_model": "weak", "variant": "x"}
    run.save()
    assert load(ws).model_overlay == {"late_model": "weak"}
    assert "final review: strong (opus)  (default)" in model(engine, ws)["text"]


def test_the_environment_beats_the_run_and_says_so(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    started(engine, ws)
    monkeypatch.setenv("ARL_MODEL", "haiku")
    shown = model(engine, ws, "set", "all", "weak")
    assert shown["warnings"] == ["ARL_MODEL is set, so it beats this run's model"]
    assert "final review: haiku  (env)" in shown["text"]
    assert prepared(engine, ws)["reviewer_model"] == "haiku"


def test_default_is_the_built_in_choice_and_reset_inherits_the_files(ws: Path, engine: Engine) -> None:
    """``set ... default`` beats a repo and user config; ``reset`` gives the files back their say."""
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"model": "weak"}))
    user = config.user_config_path()
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"late_model": "haiku"}))
    started(engine, ws)
    assert "final review: weak (sonnet)  (repo)" in model(engine, ws)["text"]
    shown = model(engine, ws, "set", "all", "default")
    assert "final review: strong (opus)  (run)" in shown["text"]
    assert "later rounds: weak (sonnet)  (run)" in shown["text"]
    assert prepared(engine, ws)["reviewer_model"] == "opus"
    engine(ws, "review", "abandon", load(ws).data["pending_ticket"]["review_id"])
    shown = model(engine, ws, "reset")
    assert shown["overlay"] == {}
    assert "final review: weak (sonnet)  (repo)" in shown["text"]
    assert "later rounds: haiku  (user)" in shown["text"]
    assert prepared(engine, ws, "print(5)\n")["reviewer_model"] == "sonnet"


def test_reset_of_one_kind_keeps_the_other(ws: Path, engine: Engine) -> None:
    started(engine, ws, "--model", "haiku")
    assert model(engine, ws, "reset", "late")["overlay"] == {"model": "haiku"}


def test_config_shows_the_run_layer(ws: Path, engine: Engine) -> None:
    started(engine, ws, "--model", "weak")
    text = engine(ws, "config", stdin="")["text"]
    assert "  model                   weak (sonnet)  (run)" in text


@pytest.mark.parametrize("words", [["set", "all"], ["set", "most", "weak"], ["reset", "all", "more"], ["pick", "weak"]])
def test_a_malformed_command_is_refused(ws: Path, engine: Engine, words: list[str]) -> None:
    started(engine, ws)
    assert model(engine, ws, *words)["code"] == "bad-model"
    assert load(ws).model_overlay == {}


def test_no_run_no_model(ws: Path, engine: Engine) -> None:
    assert model(engine, ws)["code"] == "no-run"


# --------------------------------------------------------------------------
# Checking a literal
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["claude-opus-5-5", "gpt-6", "Opus"])
def test_the_subagent_takes_only_an_alias(ws: Path, engine: Engine, value: str) -> None:
    started(engine, ws)
    refused = model(engine, ws, "set", "all", value)
    assert refused["code"] == "bad-model"
    assert "opus, sonnet, haiku, fable" in refused["error"]
    assert load(ws).model_overlay == {}


def test_init_refuses_a_bad_model_and_starts_no_run(ws: Path, engine: Engine) -> None:
    assert started(engine, ws, "--model", "claude-bogus-9-9")["code"] == "bad-model"
    assert state.RunState.load(state.run_paths(ws)) is None


def test_init_with_a_bad_model_leaves_the_active_run_alone(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    run_id = load(ws).run_id
    assert started(engine, ws, "--new", "--model", "claude-bogus-9-9")["code"] == "bad-model"
    assert load(ws).run_id == run_id


def test_opencode_takes_a_listed_model(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, listed: list[str]) -> None:
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    started(engine, ws)
    assert model(engine, ws, "set", "all", "vendor/other")["ok"]
    assert model(engine, ws, "set", "late", "vendor/missing")["code"] == "bad-model"
    assert load(ws).model_overlay == {"model": "vendor/other", "late_model": "vendor/other"}
    del listed


def test_opencode_checks_the_base_of_a_variant(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, listed: list[str]) -> None:
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    started(engine, ws)
    assert model(engine, ws, "set", "first", "vendor/other#high")["ok"]
    assert model(engine, ws, "set", "first", "vendor/missing#high")["code"] == "bad-model"
    monkeypatch.setenv("ARL_VARIANT", "low")
    refused = model(engine, ws, "set", "first", "vendor/other#high")
    assert (refused["code"], "already names a variant" in refused["error"]) == ("bad-model", True)
    del listed


@pytest.mark.parametrize("probe", ["fails", "none"])
def test_an_opencode_probe_that_does_not_answer_refuses_the_setting(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, probe: str) -> None:
    def failing(_self: object, _timeout: float) -> list[str] | None:
        if probe == "fails":
            raise reviewer_probe.ProbeFailed("`opencode models` exited 1")
        return None

    monkeypatch.setattr(opencode.HARNESS.__class__, "probe_models", failing)
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    started(engine, ws)
    assert model(engine, ws, "set", "all", "vendor/other")["code"] == "bad-model"
    # A tier word needs no probe.
    assert model(engine, ws, "set", "all", "weak")["ok"]


# --------------------------------------------------------------------------
# A prepared review keeps its model
# --------------------------------------------------------------------------


def test_a_subagent_round_records_the_model_it_was_prepared_with(ws: Path, engine: Engine, tmp_path: Path) -> None:
    started(engine, ws)
    ticket = prepared(engine, ws)
    model(engine, ws, "set", "all", "weak")
    user_config({"subagent_strong_model": "haiku"})
    outcome = answered(engine, ws, ticket, tmp_path, APPROVE)
    assert "- model: `strong (opus)`" in Path(outcome["report"]).read_text()
    assert load(ws).data["round_history"][-1]["model"] == "strong (opus)"


def test_run_external_runs_and_repairs_on_the_tickets_model(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, listed: list[str]) -> None:
    """**Fails on the old code**, which resolved the model again when the reviewer ran."""
    argvs: list[list[str]] = []

    def invoke(_target: object, run: Any, *, config: config.Config, environ: object = None) -> harness.Usage | None:
        argvs.append(opencode.review_argv("t", config=config))
        # The first answer breaks the contract, so the repair call runs too.
        run.out_path.write_text("It is broken.\n" if len(argvs) == 1 else f"Broken.\n\n{BLOCK}")
        return None

    monkeypatch.setattr(runner, "invoke", invoke)
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    started(engine, ws)
    ticket = prepared(engine, ws)
    assert ticket["model"] == "strong (openai/gpt-6.1-sol)"
    # Everything that could move the model, moved before the reviewer runs.
    model(engine, ws, "set", "all", "weak")
    user_config({"opencode_strong_model": "vendor/other", "variant": "high"})

    outcome = engine(ws, "review", "run-external", ticket["review_id"])

    assert outcome["verdict"] == "CHANGES_REQUIRED", outcome
    assert [argv[argv.index("-m") + 1] for argv in argvs] == ["openai/gpt-6.1-sol", "openai/gpt-6.1-sol"]
    assert "- model: `strong (openai/gpt-6.1-sol)`" in Path(outcome["report"]).read_text()
    entry = load(ws).data["round_history"][-1]
    assert (entry["harness"], entry["model"]) == ("opencode", "strong (openai/gpt-6.1-sol)")
    # The next review is the new choice's.
    assert prepared(engine, ws, "print(4)\n")["model"] == "weak (openai/gpt-6.1-terra)"
    del listed


def test_a_ticket_from_before_the_pinned_model_resolves_it_again(ws: Path, engine: Engine, tmp_path: Path) -> None:
    started(engine, ws)
    ticket = prepared(engine, ws)
    run = load(ws)
    for key in ("model_id", "model_tier", "variant"):
        run.data["pending_ticket"].pop(key)
    run.save()
    user_config({"model": "haiku"})
    outcome = answered(engine, ws, ticket, tmp_path, APPROVE)
    assert "- model: `haiku`" in Path(outcome["report"]).read_text()


@pytest.mark.parametrize("value", ["vendor/other#", "#high", "#"])
def test_opencode_refuses_an_empty_side_of_a_hash(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch, listed: list[str], value: str) -> None:
    """**Fails on the old code**, which kept ``vendor/other#`` under a configured variant and left
    ``review_argv`` to refuse it at the next review."""
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    started(engine, ws)
    model(engine, ws, "set", "all", "weak")
    monkeypatch.setenv("ARL_VARIANT", "high")
    assert model(engine, ws, "set", "first", value)["code"] == "bad-model"
    monkeypatch.delenv("ARL_VARIANT")
    assert model(engine, ws, "set", "first", value)["code"] == "bad-model"
    assert load(ws).model_overlay == {"model": "weak", "late_model": "weak"}
    del listed
