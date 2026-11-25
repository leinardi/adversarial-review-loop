"""The engine commands the mod's slash commands and session start use: attach, report, root-remove,
config, the start prompt and compaction on continuation."""

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
from run_common import arl, make_repo, run_env, ws

from arl import config, state

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


def started(engine: Engine, launch: Path) -> dict[str, Any]:
    answer: dict[str, Any] = engine(launch, "init", "--plan", str(launch / "plan.md"))
    assert answer["ok"], answer
    return answer


def with_ticket(launch: Path, session: str) -> None:
    with state.locked_state(state.run_paths(launch)) as run:
        assert run is not None
        run.data["pending_ticket"] = {"review_id": "x-1", "session": session}
        run.save()


def pending(launch: Path) -> object:
    run = state.RunState.load(state.run_paths(launch))
    assert run is not None
    return run.data["pending_ticket"]


# -- attach -----------------------------------------------------------------------------------


def test_attach_without_a_run_writes_nothing(engine: Engine, ws: Path) -> None:
    answer = engine(ws, "attach", "--session", "s1")
    assert answer == {"ok": True, "run": None, "foreign_review": False, "effort": ""}
    assert not (ws / ".arl").exists()


def test_attach_reports_another_sessions_review_and_keeps_it(engine: Engine, ws: Path) -> None:
    # That session may still be running its reviewer: only /arl-resume abandons it.
    started(engine, ws)
    with_ticket(ws, "s1")
    answer = engine(ws, "attach", "--session", "s2")
    assert answer["foreign_review"] is True
    assert pending(ws) is not None


def test_attach_knows_its_own_sessions_review(engine: Engine, ws: Path) -> None:
    # A reload of the mod starts the same session again; its review may still be running.
    started(engine, ws)
    with_ticket(ws, "s1")
    assert engine(ws, "attach", "--session", "s1")["foreign_review"] is False
    assert pending(ws) is not None


def test_attach_drops_an_effort_that_is_no_level(engine: Engine, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARL_EFFORT", "ultra")
    answer = engine(ws, "attach")
    assert answer["effort"] == ""
    assert "'ultra' is no level" in answer["effort_note"]


def test_attach_answers_the_configured_effort(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"effort": "high"}))
    assert engine(ws, "attach")["effort"] == "high"


# -- report -----------------------------------------------------------------------------------


def test_report_shows_the_newest_or_the_numbered_one(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    reports = ws / ".arl" / "reports"
    reports.mkdir()
    (reports / "001-phase1-CHANGES_REQUIRED.md").write_text("first\n")
    (reports / "002-phase1-APPROVED.md").write_text("second\n")
    newest = engine(ws, "report")
    assert newest["text"] == "second\n"
    assert newest["reports"] == ["001-phase1-CHANGES_REQUIRED.md", "002-phase1-APPROVED.md"]
    assert engine(ws, "report", "1")["text"] == "first\n"
    assert engine(ws, "report", "9")["text"].startswith("No such report.")


def test_report_without_reports(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    assert engine(ws, "report")["text"].startswith("No reports have been produced")


# -- root-remove ------------------------------------------------------------------------------


def test_root_remove_drops_a_root_and_retires_the_ticket(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    stray = make_repo(run_env / "stray")
    assert engine(ws, "root-add", str(stray))["status"] == "added"
    with_ticket(ws, "s1")
    answer = engine(ws, "root-remove", "stray")
    assert answer["alias"] == "stray"
    assert "stray" not in [root["alias"] for root in answer["run"]["roots"]]
    assert pending(ws) is None


def test_root_remove_by_path(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    stray = make_repo(run_env / "stray")
    engine(ws, "root-add", str(stray))
    assert engine(ws, "root-remove", str(stray))["alias"] == "stray"


def test_root_remove_retires_the_alias(engine: Engine, ws: Path, run_env: Path) -> None:
    # The old root's round history still names `stray/...`: a new folder must not take it.
    started(engine, ws)
    engine(ws, "root-add", str(make_repo(run_env / "stray")))
    engine(ws, "root-remove", "stray")
    other = make_repo(run_env / "again" / "stray")
    assert engine(ws, "root-add", str(other))["alias"] == "stray-2"


def test_root_remove_undo_keeps_the_loop_where_it_was(engine: Engine, ws: Path, run_env: Path) -> None:
    # A refused write takes its root back; the loop must not prompt again for it.
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    assert engine(ws, "next-action")["action"] == "continue"
    engine(ws, "root-add", str(run_env / "new"))
    assert engine(ws, "root-remove", "new", "--undo")["alias"] == "new"
    assert engine(ws, "next-action")["reason"] == "unchanged"


def test_root_remove_undo_keeps_a_root_that_changed(engine: Engine, ws: Path, run_env: Path) -> None:
    # The write ran after all (a hook downstream refused its result): its file must be reviewed.
    started(engine, ws)
    engine(ws, "root-add", str(run_env / "new"))
    (run_env / "new").mkdir()
    (run_env / "new" / "a.py").write_text("x\n")
    answer = engine(ws, "root-remove", "new", "--undo")
    assert answer["code"] == "kept"
    assert "new" in [root["alias"] for root in engine(ws, "status")["run"]["roots"]]


def test_root_remove_undo_keeps_a_root_while_a_review_is_pending(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    engine(ws, "root-add", str(run_env / "new"))
    with_ticket(ws, "s1")
    assert engine(ws, "root-remove", "new", "--undo")["code"] == "kept"
    assert pending(ws) is not None


def test_root_remove_refuses_a_repository_inside_another_root(engine: Engine, ws: Path) -> None:
    # Dropped, `api` would pour into the launch directory's own snapshot.
    started(engine, ws)
    answer = engine(ws, "root-remove", "api")
    assert answer["code"] == "nested-root"


def test_root_remove_refuses_the_last_root(engine: Engine, run_env: Path) -> None:
    # With no root, the bundle is empty and an approval covers changes nobody saw.
    solo = make_repo(run_env / "solo", files={"plan.md": "# Plan\n"})
    started(engine, solo)
    assert [root["alias"] for root in engine(solo, "status")["run"]["roots"]] == ["solo"]
    assert engine(solo, "root-remove", "solo")["code"] == "last-root"


def _scratch_with_clone(ws: Path, run_env: Path, engine: Engine) -> Path:
    """A folder registered by mistake that holds a clone, as Claude's scratchpad did."""
    scratch = run_env / "scratch"
    make_repo(scratch / "clone")
    added = engine(ws, "root-add", str(scratch))
    assert added["status"] == "added"
    assert {"scratch", "clone"} <= {root["alias"] for root in engine(ws, "status")["run"]["roots"]}
    return scratch


def test_root_remove_with_nested_drops_a_folder_and_the_repositories_in_it(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    _scratch_with_clone(ws, run_env, engine)
    with_ticket(ws, "s1")
    answer = engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "clone,scratch")
    assert sorted(answer["removed"]) == ["clone", "scratch"]
    left = [root["alias"] for root in answer["run"]["roots"]]
    assert "scratch" not in left and "clone" not in left and "api" in left
    assert pending(ws) is None
    assert {"scratch", "clone"} <= set(json.loads((ws / ".arl" / "state.json").read_text())["retired_aliases"])


def test_root_remove_with_nested_changes_nothing_when_the_set_is_not_what_was_shown(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    scratch = _scratch_with_clone(ws, run_env, engine)
    # A repository joined after the person was shown "scratch, clone".
    make_repo(scratch / "late")
    engine(ws, "root-add", str(scratch / "late"))
    before = (ws / ".arl" / "state.json").read_bytes()
    answer = engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone")
    assert answer["code"] == "removal-changed"
    assert "late" in answer["error"]
    assert (ws / ".arl" / "state.json").read_bytes() == before
    # One already gone is a change too.
    assert engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone,late,gone")["code"] == "removal-changed"
    assert engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone,late")["ok"]


def test_root_remove_changes_nothing_when_a_nested_baseline_was_set_since_it_was_shown(engine: Engine, ws: Path, run_env: Path) -> None:
    """The container still has no baseline, but a clone inside it got one while the person was
    asked: removing it now would drop a root someone just chose to review."""
    started(engine, ws)
    scratch = run_env / "scratch"
    clone = make_repo(scratch / "clone")
    (scratch / "note.txt").write_text("written during the run\n")
    (clone / "README.md").write_text("changed during the run\n")
    engine(ws, "root-add", str(scratch))
    roots = {root["alias"]: root["baseline"] for root in engine(ws, "status")["run"]["roots"]}
    assert (roots["scratch"], roots["clone"]) == ("unknown", "unknown")
    assert engine(ws, "root-baseline", "clone", "--current")["ok"]
    before = (ws / ".arl" / "state.json").read_bytes()

    shown = "scratch=unknown,clone=unknown"
    answer = engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone", "--expect-baselines", shown)
    assert answer["code"] == "removal-changed"
    assert "clone" in answer["error"]
    assert (ws / ".arl" / "state.json").read_bytes() == before
    # As it is now, the removal goes ahead.
    now = "scratch=unknown,clone=set"
    assert engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone", "--expect-baselines", now)["ok"]


def test_root_remove_with_nested_removes_a_chain_from_its_top(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    scratch = run_env / "scratch"
    make_repo(scratch / "mid")
    make_repo(scratch / "mid" / "deep")
    engine(ws, "root-add", str(scratch))
    aliases = sorted(root["alias"] for root in engine(ws, "status")["run"]["roots"] if root["path"].startswith(str(scratch)))
    assert aliases == ["deep", "mid", "scratch"]
    assert engine(ws, "root-remove", "mid", "--with-nested", "--expect", "mid,deep")["code"] == "nested-root"
    assert sorted(engine(ws, "root-remove", "scratch", "--with-nested", "--expect", ",".join(aliases))["removed"]) == aliases


def test_root_remove_with_nested_refuses_to_empty_the_run(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    roots = sorted(root["alias"] for root in engine(ws, "status")["run"]["roots"])
    before = (ws / ".arl" / "state.json").read_bytes()
    assert engine(ws, "root-remove", "ws", "--with-nested", "--expect", ",".join(roots))["code"] == "last-root"
    assert (ws / ".arl" / "state.json").read_bytes() == before


def test_root_remove_dry_run_resolves_the_root_as_the_removal_does_and_changes_nothing(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    _scratch_with_clone(ws, run_env, engine)
    before = (ws / ".arl" / "state.json").read_bytes()
    # A path relative to the launch directory names the same root as its alias.
    planned = engine(ws, "root-remove", "../scratch", "--with-nested", "--dry-run")
    assert (planned["alias"], sorted(planned["removing"]), planned["run_id"]) == ("scratch", ["clone", "scratch"], load_run_id(ws))
    assert (ws / ".arl" / "state.json").read_bytes() == before


def test_root_remove_refuses_consent_given_for_another_run(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    _scratch_with_clone(ws, run_env, engine)
    before = (ws / ".arl" / "state.json").read_bytes()
    answer = engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone", "--run", "an-older-run")
    assert answer["code"] == "run-changed"
    assert (ws / ".arl" / "state.json").read_bytes() == before
    assert engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone", "--run", load_run_id(ws))["ok"]


def test_root_baseline_refuses_consent_given_for_another_run(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    late = run_env / "late"
    late.mkdir()
    (late / "f.txt").write_text("written during the run\n")
    alias = engine(ws, "root-add", str(late))["alias"]
    before = (ws / ".arl" / "state.json").read_bytes()
    assert engine(ws, "root-baseline", alias, "--current", "--run", "an-older-run")["code"] == "run-changed"
    assert (ws / ".arl" / "state.json").read_bytes() == before
    assert engine(ws, "root-baseline", alias, "--current", "--run", load_run_id(ws))["ok"]


def test_status_peek_answers_while_another_command_holds_the_lock(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    paths = state.run_paths(ws)
    with state.locked_state(paths):
        answer = engine(ws, "status", "--peek")
    info = paths.state_file.stat()
    assert answer["ok"] and answer["run"]["state_stamp"] == f"{info.st_mtime_ns // 1_000_000}:{info.st_size}"
    assert "context" not in answer
    paths.state_file.unlink()
    assert engine(ws, "status", "--peek") == {"ok": True, "run": None}


def test_status_context_is_the_resume_context_of_the_run_as_it_is_now(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    _scratch_with_clone(ws, run_env, engine)
    engine(ws, "root-remove", "scratch", "--with-nested", "--expect", "scratch,clone")
    context = engine(ws, "status", "--context")["context"]
    assert f"api = {ws / 'api'}" in context
    assert "scratch =" not in context and "clone =" not in context
    assert "context" not in engine(ws, "status")


def load_run_id(ws: Path) -> str:
    return str(json.loads((ws / ".arl" / "state.json").read_text())["run_id"])


def test_root_remove_flags_that_do_not_combine_are_refused(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    _scratch_with_clone(ws, run_env, engine)
    assert engine(ws, "root-remove", "scratch", "--with-nested", "--undo", "--expect", "scratch,clone")["code"] == "usage"
    assert engine(ws, "root-remove", "scratch", "--expect", "scratch,clone")["code"] == "usage"
    assert engine(ws, "root-remove", "scratch", "--dry-run")["code"] == "usage"


def test_review_prepare_refuses_a_run_with_no_roots(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.data["roots"] = []
        run.save()
    assert engine(ws, "review", "prepare")["code"] == "no-roots"


def test_root_remove_refuses_an_unknown_root(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    assert engine(ws, "root-remove", "nope")["code"] == "unknown-root"


# -- config -----------------------------------------------------------------------------------


def test_config_shows_every_key_and_its_layer(engine: Engine, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"stall_rounds": 3}))
    monkeypatch.setenv("ARL_TIMEOUT_SEC", "60")
    text = engine(ws, "config")["text"]
    for key in config.DEFAULTS:
        assert f"  {key} " in text
    assert "3  (repo)" in text
    assert "60  (env)" in text
    assert "false  (default)" in text


def test_config_shows_the_late_model_round_2_runs_on(engine: Engine, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = engine(ws, "config")["text"]
    assert "  model                   strong (opus)  (default)" in text
    assert "  late_model              weak (sonnet)  (default)" in text
    assert "  subagent_weak_model     sonnet  (default)" in text
    monkeypatch.setenv("ARL_LATE_MODEL", "")
    assert "  late_model              (model)  (env)" in engine(ws, "config")["text"]
    monkeypatch.delenv("ARL_LATE_MODEL")
    monkeypatch.setenv("ARL_HARNESS", "opencode")
    assert "  late_model              strong (openai/gpt-6.1-sol)  (default)" in engine(ws, "config")["text"]
    monkeypatch.setenv("ARL_SUBAGENT_WEAK_MODEL", "haiku")
    monkeypatch.setenv("ARL_HARNESS", "subagent")
    assert "  late_model              weak (haiku)  (default)" in engine(ws, "config")["text"]


def test_config_sets_and_unsets_the_user_config(engine: Engine, ws: Path) -> None:
    answer = engine(ws, "config", stdin="auto_compact on")
    assert answer["key"] == "auto_compact"
    user = config.user_config_path()
    assert json.loads(user.read_text()) == {"auto_compact": True}
    engine(ws, "config", stdin="auto_compact --unset")
    assert json.loads(user.read_text()) == {}


def test_config_says_when_another_layer_overrides_the_value(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"block_severity": "high"}))
    text = engine(ws, "config", stdin="block_severity low")["text"]
    assert text.startswith("block_severity: high -> high")
    assert "the repo layer overrides it" in text


def test_config_repo_flag_and_a_value_holding_flags(engine: Engine, ws: Path) -> None:
    engine(ws, "config", stdin="verify_cmd --repo -- pytest --maxfail=1")
    assert json.loads((ws / config.REPO_CONFIG_NAME).read_text()) == {"verify_cmd": "pytest --maxfail=1"}


def test_config_keeps_the_quoting_of_a_value_of_several_words(engine: Engine, ws: Path) -> None:
    engine(ws, "config", stdin='verify_cmd pytest -k "a or b"')
    assert json.loads(config.user_config_path().read_text()) == {"verify_cmd": "pytest -k 'a or b'"}


def test_config_answers_the_new_effort(engine: Engine, ws: Path) -> None:
    assert engine(ws, "config", stdin="effort max")["effort"] == "max"


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ("effort huge", "not an effort level"),
        ("auto_compact maybe", "not a boolean"),
        ("stall_rounds three", "not a non-negative integer"),
        ("timeout_sec \u00b2", "not a non-negative integer"),
        ("block_severity scary", "not a severity"),
        ("harness nope", "not a harness"),
        ("nope 1", "unknown key"),
        ("effort", "needs a value"),
        ("effort --bogus", "unknown flag"),
        ("effort high --unset", "--unset takes no value"),
        ("effort 'open", "cannot split"),
    ],
)
def test_config_refuses_what_does_not_parse(engine: Engine, ws: Path, args: str, words: str) -> None:
    answer = engine(ws, "config", stdin=args)
    assert answer["code"] == "config"
    assert words in answer["error"]
    assert not config.user_config_path().exists()


def test_config_refuses_to_overwrite_a_broken_file(engine: Engine, ws: Path) -> None:
    user = config.user_config_path()
    user.parent.mkdir(parents=True)
    user.write_text("{broken")
    assert engine(ws, "config", stdin="effort low")["code"] == "config"
    assert user.read_text() == "{broken"


# -- init's prompt and compaction ---------------------------------------------------------------


def test_init_answers_the_start_prompt(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"verify_cmd": "make test"}))
    prompt = started(engine, ws)["prompt"]
    assert str(ws / "plan.md") in prompt
    assert "set_phases" in prompt
    assert "`make test`" in prompt
    assert f"api = {ws / 'api'}" in prompt
    assert "through Bash, call add_root for it only if it holds the plan's work; never register a scratch" in prompt
    assert "call add_root for that directory" not in prompt


def test_resume_context_carries_the_scratch_rule(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    context = engine(ws, "resume")["context"]
    assert "through Bash, call add_root for it only if it holds the plan's work; never register a scratch" in context
    assert "call add_root for that directory" not in context


def approve_phase_one(engine: Engine, launch: Path) -> None:
    engine(launch, "set-phases", stdin='["one", "two"]')
    assert engine(launch, "accept")["ok"]


def test_next_action_compacts_after_an_approval_when_asked(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"auto_compact": True}))
    started(engine, ws)
    first = engine(ws, "next-action")
    assert first["action"] == "continue"
    assert first["compact"] is False
    approve_phase_one(engine, ws)
    assert engine(ws, "next-action")["compact"] is True


def test_next_action_compacts_once_per_approval(engine: Engine, ws: Path) -> None:
    # A resume after the next phase began must not compact the work in progress.
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"auto_compact": True}))
    started(engine, ws)
    approve_phase_one(engine, ws)
    assert engine(ws, "next-action")["compact"] is True
    engine(ws, "resume")
    again = engine(ws, "next-action")
    assert again["action"] == "continue"
    assert again["compact"] is False


def test_next_action_does_not_compact_once_the_next_stage_was_reviewed(engine: Engine, ws: Path) -> None:
    # Approved, then the next phase implemented and reviewed in the same turn: its findings
    # must not be compacted away before Claude fixes them.
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"auto_compact": True}))
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one", "two"]')
    engine(ws, "next-action")
    assert engine(ws, "accept")["ok"]
    (ws / "notes" / "todo.md").write_text("changed\n")
    assert engine(ws, "review", "prepare")["ok"]
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.data["pending_ticket"] = None
        run.save()
    assert engine(ws, "next-action")["compact"] is False


def test_root_remove_clears_an_oversized_wait(engine: Engine, ws: Path, run_env: Path) -> None:
    started(engine, ws)
    engine(ws, "root-add", str(make_repo(run_env / "huge")))
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.wait_for_user("oversized", "huge/blob.bin is too large")
        run.save()
    assert engine(ws, "root-remove", "huge")["run"]["waiting"] is None


def test_root_remove_undo_does_not_retire_the_alias(engine: Engine, ws: Path, run_env: Path) -> None:
    # A failed Edit takes its root back; the retry must register the same name, not `new-2`.
    started(engine, ws)
    engine(ws, "root-add", str(run_env / "new"))
    engine(ws, "root-remove", "new", "--undo")
    assert engine(ws, "root-add", str(run_env / "new"))["alias"] == "new"


def test_config_answers_only_an_effort_that_is_a_level(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"effort": "huge"}))
    assert engine(ws, "config", stdin="effort --unset")["effort"] == ""


def test_next_action_does_not_compact_by_default(engine: Engine, ws: Path) -> None:
    started(engine, ws)
    approve_phase_one(engine, ws)
    assert engine(ws, "next-action")["compact"] is False


def test_next_action_names_the_configured_checks(engine: Engine, ws: Path) -> None:
    # Set mid-run, or after the start prompt was compacted away: every continuation names it.
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"verify_cmd": "make test"}))
    assert "Run the project's checks (`make test`), then call review." in engine(ws, "next-action")["prompt"]


def test_a_change_to_the_config_file_is_never_ignored(engine: Engine, ws: Path) -> None:
    # An edit setting ignore_globs to everything must not approve itself without a reviewer.
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"ignore_globs": ["**"]}))
    answer = engine(ws, "review", "prepare")
    assert answer["ok"], answer
    assert answer.get("status") != "skipped"
    assert answer["review_id"]


def test_the_final_review_prompt_names_the_configured_checks(engine: Engine, ws: Path) -> None:
    (ws / config.REPO_CONFIG_NAME).write_text(json.dumps({"verify_cmd": "make test", "final_review": True}))
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    assert engine(ws, "accept")["ok"]
    prompt = engine(ws, "next-action")["prompt"]
    assert "run the final review" in prompt
    assert "`make test`" in prompt
