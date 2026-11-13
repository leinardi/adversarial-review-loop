"""The engine's run commands end to end: init, phases, roots and baselines, user decisions."""

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
import os
import shutil
import stat
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from conftest import BOOTSTRAP, run_bootstrap
from run_common import PAST, arl, backdate, git_at, make_repo, run_env, shadow_files, ws

from arl import state, workspace

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


def started(engine: Engine, launch: Path, *extra: str) -> dict[str, Any]:
    answer: dict[str, Any] = engine(launch, "init", "--plan", str(launch / "plan.md"), *extra)
    assert answer["ok"], answer
    return answer


def roots_by_alias(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {root["alias"]: root for root in answer["run"]["roots"]}


def load(launch: Path) -> state.RunState:
    run = state.RunState.load(state.run_paths(launch))
    assert run is not None
    return run


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def test_init_registers_the_launch_dir_and_every_nested_repository(ws: Path, engine: Engine) -> None:
    roots = roots_by_alias(started(engine, ws))
    assert {alias: root["kind"] for alias, root in roots.items()} == {"ws": "dir", "api": "git", "web": "git"}
    assert all(root["baseline"] == "set" for root in roots.values())
    assert (ws / ".arl" / "plan.md").read_text() == (ws / "plan.md").read_text()


def test_the_folder_snapshot_leaves_out_nested_roots_and_the_run_dir(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    run = load(ws)
    tree = run.root("ws")["start_tree"]  # type: ignore[index]
    assert sorted(shadow_files(ws, "ws", tree)) == ["notes/todo.md", "plan.md"]


def test_init_leaves_the_launch_directory_mode_alone(ws: Path, engine: Engine) -> None:
    """The private writers tighten their root to 0700; the root has to be ``.arl/``, not the project."""
    ws.chmod(0o755)
    started(engine, ws)
    assert stat.S_IMODE(ws.stat().st_mode) == 0o755
    assert stat.S_IMODE((ws / ".arl").stat().st_mode) == 0o700


def test_a_git_launch_dir_excludes_the_run_dir_without_touching_tracked_content(run_env: Path, engine: Engine) -> None:
    repo = make_repo(run_env / "repo")
    (repo / "plan.md").write_text("# Plan\n")
    git_at(repo, "add", "plan.md")
    git_at(repo, "commit", "-qm", "plan", when=PAST)
    index_before = (repo / ".git" / "index").read_bytes()

    started(engine, repo)

    assert "/.arl/" in (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert git_at(repo, "status", "--porcelain") == ""
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_init_refuses_while_a_run_is_active_and_new_archives_it(ws: Path, engine: Engine) -> None:
    first = started(engine, ws)["run"]["run_id"]

    refused = engine(ws, "init", "--plan", str(ws / "plan.md"))
    assert refused["code"] == "run-exists"

    second = engine(ws, "init", "--plan", str(ws / "plan.md"), "--new")
    assert second["archived"] == first
    assert second["run"]["run_id"] != first
    assert json.loads((ws / ".arl" / "runs" / first / "state.json").read_text())["run_id"] == first


def test_a_stopped_run_is_archived_without_new(ws: Path, engine: Engine) -> None:
    first = started(engine, ws)["run"]["run_id"]
    engine(ws, "stop")
    assert started(engine, ws)["archived"] == first


def test_init_registers_a_repository_the_plan_names_outside_the_launch_dir(run_env: Path, ws: Path, engine: Engine) -> None:
    other = make_repo(run_env / "other")
    hidden = run_env / ".config" / "tool"
    hidden.mkdir(parents=True)
    (ws / "plan.md").write_text(f"# Plan\n\nEdit `{other}/README.md` and ~/.config/tool, then /usr/bin.\n")

    roots = roots_by_alias(started(engine, ws))

    assert roots["other"]["path"] == str(other)
    assert not any(root["path"] in (str(hidden), "/usr/bin", "/usr") for root in roots.values())


def test_a_root_argument_is_registered(run_env: Path, ws: Path, engine: Engine) -> None:
    extra = run_env / "docs"
    extra.mkdir()
    backdate(extra)
    roots = roots_by_alias(started(engine, ws, "--root", str(extra)))
    assert roots["docs"]["kind"] == "dir"


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------


def test_phases_are_replaced_freely_until_one_is_approved(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["a", "b"]')
    answer = engine(ws, "set-phases", stdin='["x", "y", "z"]')
    assert answer["run"]["phases"] == ["x", "y", "z"]
    assert answer["run"]["phases_revision"] == 2

    engine(ws, "accept")
    refused = engine(ws, "set-phases", stdin='["q"]')
    assert refused["code"] == "phases-approved"

    replanned = engine(ws, "set-phases", "--replan", stdin='["new two", "new three"]')
    assert replanned["run"]["phases"] == ["x", "new two", "new three"]
    assert replanned["run"]["phase"] == 2


@pytest.mark.parametrize("stdin", ["", "{}", '["ok", ""]', "[]", '["a", 1]'])
def test_a_malformed_phase_list_is_refused(ws: Path, engine: Engine, stdin: str) -> None:
    started(engine, ws)
    assert engine(ws, "set-phases", stdin=stdin)["code"] == "bad-phases"


def test_a_replan_invalidates_the_pending_ticket(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["a"]')
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.issue_ticket({"review_id": "r1"})
        run.save()
    engine(ws, "set-phases", stdin='["b"]')
    run = load(ws)
    assert run.data["pending_ticket"] is None
    assert run.data["last_ticket"] == {"review_id": "r1", "status": "replanned"}


# --------------------------------------------------------------------------
# Approval moves every root
# --------------------------------------------------------------------------


def test_accept_moves_every_root_and_the_next_phase_diffs_from_there(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one", "two"]')
    (ws / "notes" / "todo.md").write_text("done\n")
    (ws / "api" / "src" / "main.py").write_text("print('api v2')\n")
    before = {root["alias"]: root["approved_tree"] for root in load(ws).roots}

    engine(ws, "accept")

    after = load(ws)
    moved = {root["alias"] for root in after.roots if root["approved_tree"] != before[root["alias"]]}
    assert moved == {"ws", "api"}
    assert all(root["start_tree"] == before[root["alias"]] for root in after.roots)
    assert after.data["approvals"][0]["by"] == "user"


def test_accept_refuses_while_a_baseline_is_unknown(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    late = run_env / "late"
    late.mkdir()
    (late / "f.txt").write_text("written during the run\n")
    assert engine(ws, "root-add", str(late))["baseline"] == "unknown"

    assert engine(ws, "accept")["code"] == "baseline-unknown"


# --------------------------------------------------------------------------
# Late roots
# --------------------------------------------------------------------------


def test_root_add_answers_known_and_covered(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    assert engine(ws, "root-add", str(ws / "api" / "src"))["status"] == "known"
    assert engine(ws, "root-add", str(ws / "notes"))["status"] == "covered"
    assert engine(ws, "root-add", str(ws))["status"] == "known"


def test_an_untouched_late_folder_takes_its_current_content(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    old = run_env / "old"
    old.mkdir()
    (old / "f.txt").write_text("from before the run\n")
    backdate(old)
    added = engine(ws, "root-add", str(old))
    assert (added["status"], added["baseline"]) == ("added", "set")


def test_a_folder_about_to_be_created_is_baselined_empty(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    added = engine(ws, "root-add", str(run_env / "fresh"))
    assert added["baseline"] == "set"
    root = load(ws).root(added["alias"])
    assert root is not None
    assert shadow_files(ws, added["alias"], root["start_tree"]) == []


def test_a_late_folder_changed_during_the_run_needs_a_baseline(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    late = run_env / "late"
    late.mkdir()
    (late / "f.txt").write_text("written during the run\n")

    added = engine(ws, "root-add", str(late))
    assert added["baseline"] == "unknown"
    assert added["proposed"] == ""
    assert engine(ws, "root-baseline", added["alias"], "--commit", "HEAD")["code"] == "not-git"

    chosen = engine(ws, "root-baseline", added["alias"], "--empty")
    assert shadow_files(ws, added["alias"], chosen["tree"]) == []
    assert engine(ws, "root-baseline", added["alias"], "--current")["code"] == "baseline-set"


def test_an_unborn_repository_created_during_the_run_needs_a_baseline(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    unborn = run_env / "unborn"
    unborn.mkdir()
    git_at(unborn, "init", "-q")
    (unborn / "new.py").write_text("x = 1\n")

    added = engine(ws, "root-add", str(unborn))
    assert (added["baseline"], added["proposed"]) == ("unknown", "")

    chosen = engine(ws, "root-baseline", added["alias"], "--current")
    assert git_at(unborn, "ls-tree", "-r", "--name-only", chosen["tree"]) == "new.py"


def test_a_repository_committed_to_through_bash_proposes_head_at_the_run_start(run_env: Path, ws: Path, engine: Engine) -> None:
    other = make_repo(run_env / "other")
    before = git_at(other, "rev-parse", "HEAD")
    backdate(other)
    started(engine, ws)
    (other / "README.md").write_text("changed during the run\n")
    git_at(other, "commit", "-qam", "bash commit")

    added = engine(ws, "root-add", str(other))
    assert (added["baseline"], added["proposed"]) == ("unknown", before)
    assert engine(ws, "next-action")["reason"] == "baseline-unknown"

    chosen = engine(ws, "root-baseline", added["alias"], "--commit", added["proposed"])
    assert chosen["tree"] == git_at(other, "rev-parse", f"{before}^{{tree}}")


def test_a_baseline_commit_shaped_like_an_option_is_refused(run_env: Path, ws: Path, engine: Engine) -> None:
    other = make_repo(run_env / "other")
    started(engine, ws)
    (other / "README.md").write_text("changed during the run\n")
    added = engine(ws, "root-add", str(other))
    assert engine(ws, "root-baseline", added["alias"], "--commit=--output=x")["code"] == "bad-commit"


def test_a_repository_cloned_during_the_run_proposes_the_clone(run_env: Path, ws: Path, engine: Engine) -> None:
    upstream = make_repo(run_env / "upstream")
    started(engine, ws)
    git_at(run_env, "clone", "-q", str(upstream), str(run_env / "clone"))
    clone = run_env / "clone"
    git_at(clone, "config", "user.email", "c@example.invalid")
    git_at(clone, "config", "user.name", "c")
    (clone / "README.md").write_text("changed in the clone\n")
    git_at(clone, "commit", "-qam", "change")

    added = engine(ws, "root-add", str(clone))
    assert (added["baseline"], added["proposed"]) == ("unknown", git_at(upstream, "rev-parse", "HEAD"))


def test_a_repository_created_after_init_is_found_by_the_scan(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    make_repo(ws / "libs" / "fresh", when=None)

    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        assert workspace.discover(run, late=True) == ["fresh"]
        run.save()

    fresh = load(ws).root("fresh")
    assert fresh is not None
    assert fresh["baseline"] == "unknown"
    assert engine(ws, "next-action") == {"ok": True, "action": "yield", "reason": "baseline-unknown", "roots": ["fresh"]}

    # The folder root no longer carries the new repository's files.
    run = load(ws)
    ws_root = run.root("ws")
    assert ws_root is not None
    tree = workspace.snapshot(ws_root, run.roots, run.paths)
    assert not any(path.startswith("libs/fresh/") for path in shadow_files(ws, "ws", tree))


def test_an_unborn_nested_repository_does_not_break_its_container(ws: Path, engine: Engine) -> None:
    """``add -A`` refuses an embedded repository with no commit; the container must leave it out."""
    unborn = ws / "scratch"
    unborn.mkdir()
    git_at(unborn, "init", "-q")
    (unborn / "x.txt").write_text("x\n")
    backdate(ws)

    roots = roots_by_alias(started(engine, ws))
    assert roots["scratch"]["kind"] == "git"
    assert "scratch/x.txt" not in shadow_files(ws, "ws", load(ws).root("ws")["start_tree"])  # type: ignore[index]


def test_two_roots_with_the_same_name_get_distinct_aliases(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    twin = make_repo(run_env / "elsewhere" / "api")
    backdate(twin)
    assert engine(ws, "root-add", str(twin))["alias"] == "api-2"


# --------------------------------------------------------------------------
# User decisions and resume
# --------------------------------------------------------------------------


def test_pause_resume_and_stop(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    assert engine(ws, "pause")["run"]["status"] == "paused"
    assert engine(ws, "pause")["code"] == "run-paused"
    assert engine(ws, "resume")["run"]["status"] == "active"
    assert engine(ws, "stop")["run"]["status"] == "stopped"
    assert engine(ws, "resume")["code"] == "run-stopped"
    assert engine(ws, "root-add", str(ws))["code"] == "run-stopped"


def test_resume_abandons_a_pending_review_and_prompts_again(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    assert engine(ws, "next-action")["action"] == "continue"
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.issue_ticket({"review_id": "r7"})
        run.wait_for_user("stall", "five rounds on one finding")
        run.save()

    resumed = engine(ws, "resume")
    assert resumed["abandoned_review"] is True
    assert load(ws).data["last_ticket"] == {"review_id": "r7", "status": "abandoned"}
    assert engine(ws, "next-action")["action"] == "continue"


def test_commands_without_a_run_say_so(ws: Path, engine: Engine) -> None:
    assert engine(ws, "status") == {"ok": True, "run": None}
    assert engine(ws, "next-action") == {"ok": True, "action": "yield", "reason": "no-run"}
    for command in ("resume", "pause", "stop", "accept", "finish"):
        assert engine(ws, command)["code"] == "no-run"


def test_a_corrupt_state_file_is_an_error_not_a_fresh_run(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    (ws / ".arl" / "state.json").write_text('{"version": 4}')
    answer = engine(ws, "status")
    assert (answer["ok"], answer["code"]) == (False, "state-corrupt")
    assert engine(ws, "init", "--plan", str(ws / "plan.md"))["code"] == "state-corrupt"

    fresh = engine(ws, "init", "--plan", str(ws / "plan.md"), "--new")
    assert fresh["archived"].startswith("corrupt-")
    assert (ws / ".arl" / "runs" / fresh["archived"] / "state.json").read_text() == '{"version": 4}'
    assert engine(ws, "status")["run"]["run_id"] == fresh["run"]["run_id"]


# --------------------------------------------------------------------------
# The real entrypoint
# --------------------------------------------------------------------------


def test_the_bootstrap_prints_one_json_answer_and_exits_by_outcome(ws: Path, run_env: Path) -> None:
    env = {"HOME": str(run_env), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(run_env / "config")}
    proc = run_bootstrap(["--dir", str(ws), "init", "--plan", str(ws / "plan.md")], cwd=ws, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"] is True

    proc = run_bootstrap(["--dir", str(ws), "init", "--plan", str(ws / "plan.md")], cwd=ws, env=env)
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["code"] == "run-exists"

    proc = run_bootstrap(["--dir", str(ws), "root-baseline", "x"], cwd=ws, env=env)
    assert (proc.returncode, proc.stdout) == (2, "")


def test_a_nested_root_the_folder_gitignores_still_snapshots(ws: Path, engine: Engine) -> None:
    """``add -A`` refuses a pathspec naming an ignored path, an exclude included."""
    (ws / ".gitignore").write_text("api/\n")
    backdate(ws)
    roots = roots_by_alias(started(engine, ws))
    assert set(roots) == {"ws", "api", "web"}
    assert ".gitignore" in shadow_files(ws, "ws", load(ws).root("ws")["start_tree"])  # type: ignore[index]


def test_init_registers_the_roots_the_config_lists(run_env: Path, ws: Path, engine: Engine) -> None:
    extra = run_env / "shared"
    extra.mkdir()
    backdate(extra)
    (ws / ".adversarial-review-loop.json").write_text(json.dumps({"roots": [str(extra)]}))
    backdate(ws)
    assert "shared" in roots_by_alias(started(engine, ws))


def test_init_registers_a_sibling_the_plan_names_relative_to_the_launch_dir(run_env: Path, ws: Path, engine: Engine) -> None:
    sibling = make_repo(run_env / "sibling")
    backdate(sibling)
    (ws / "plan.md").write_text("# Plan\n\nAlso touch ../sibling/README.md.\n")
    assert roots_by_alias(started(engine, ws))["sibling"]["path"] == str(sibling)


def test_a_relative_root_is_resolved_against_the_launch_dir(run_env: Path, ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    started(engine, ws)
    monkeypatch.chdir(run_env)
    added = engine(ws, "root-add", "../fresh")
    assert added["status"] == "added"
    root = load(ws).root(added["alias"])
    assert root is not None
    assert root["path"] == str(run_env / "fresh")


# --------------------------------------------------------------------------
# Round 1 review: what a late root's touch check must see
# --------------------------------------------------------------------------


def _old_repo(run_env: Path, name: str) -> Path:
    repo = make_repo(run_env / name, files={"a.txt": "a\n", "b.txt": "b\n"})
    backdate(repo)
    return repo


def test_a_staged_addition_matching_the_worktree_makes_a_late_root_unknown(run_env: Path, ws: Path, engine: Engine) -> None:
    repo = _old_repo(run_env, "staged")
    started(engine, ws)
    (repo / "new.py").write_text("print('new')\n")
    git_at(repo, "add", "new.py")
    assert engine(ws, "root-add", str(repo))["baseline"] == "unknown"


def test_a_staged_deletion_makes_a_late_root_unknown(run_env: Path, ws: Path, engine: Engine) -> None:
    repo = _old_repo(run_env, "deleted")
    started(engine, ws)
    git_at(repo, "rm", "-q", "a.txt")
    assert engine(ws, "root-add", str(repo))["baseline"] == "unknown"


def test_an_unborn_repository_with_a_staged_file_makes_a_late_root_unknown(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    unborn = run_env / "unborn-staged"
    unborn.mkdir()
    git_at(unborn, "init", "-q")
    (unborn / "x.py").write_text("x = 1\n")
    git_at(unborn, "add", "x.py")
    assert engine(ws, "root-add", str(unborn))["baseline"] == "unknown"


def test_a_change_inside_node_modules_makes_a_late_folder_unknown(run_env: Path, ws: Path, engine: Engine) -> None:
    """The shadow snapshot includes ``node_modules``, so the touch check has to look there too."""
    folder = run_env / "app"
    (folder / "node_modules" / "dep").mkdir(parents=True)
    (folder / "node_modules" / "dep" / "index.js").write_text("old\n")
    backdate(folder)
    started(engine, ws)
    target = folder / "node_modules" / "dep" / "index.js"
    target.write_text("patched\n")
    stamp = (folder / "node_modules" / "dep").stat()
    os.utime(folder / "node_modules" / "dep", ns=(stamp.st_atime_ns, PAST * 1_000_000_000))
    assert engine(ws, "root-add", str(folder))["baseline"] == "unknown"


def test_an_untouched_late_repository_still_takes_its_current_content(run_env: Path, ws: Path, engine: Engine) -> None:
    repo = _old_repo(run_env, "quiet")
    started(engine, ws)
    assert engine(ws, "root-add", str(repo))["baseline"] == "set"


# --------------------------------------------------------------------------
# Round 1 review: finish, read-only commands, the plan's spelling
# --------------------------------------------------------------------------


def test_finish_after_the_last_approval_reopens_the_run_for_the_final_review(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["only"]')
    assert engine(ws, "accept")["run"]["status"] == "complete"

    finished = engine(ws, "finish")["run"]
    assert (finished["status"], finished["stage"]) == ("active", "final")
    assert engine(ws, "next-action")["stage"] == "final"

    engine(ws, "accept")
    assert engine(ws, "finish")["code"] == "final-done"


def test_read_only_commands_never_create_the_run_dir(run_env: Path, engine: Engine) -> None:
    """The mod asks ``next-action`` after every turn in every project; none may gain ``.arl/``."""
    repo = make_repo(run_env / "plain")
    for command in ("status", "next-action", "resume", "pause", "stop", "accept", "finish"):
        engine(repo, command)
    assert not (repo / ".arl").exists()
    assert git_at(repo, "status", "--porcelain") == ""


def test_status_accepts_the_json_flag(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    assert engine(ws, "status", "--json")["run"]["stage"] == "plan"


def test_a_plain_folder_the_plan_names_is_not_a_root(run_env: Path, ws: Path, engine: Engine) -> None:
    big = run_env / "Workspace"
    make_repo(big / "inner")
    backdate(big)
    (ws / "plan.md").write_text(f"# Plan\n\nLook around {big} for examples.\n")
    roots = roots_by_alias(started(engine, ws))
    assert str(big) not in {root["path"] for root in roots.values()}
    assert "inner" not in roots


# --------------------------------------------------------------------------
# Round 2 review: roots are directories, roots can vanish, init --new is atomic
# --------------------------------------------------------------------------


def test_an_existing_file_registers_the_folder_holding_it(run_env: Path, ws: Path, engine: Engine) -> None:
    folder = run_env / "other"
    folder.mkdir()
    (folder / "note.md").write_text("note\n")
    backdate(folder)
    started(engine, ws)
    added = engine(ws, "root-add", str(folder / "note.md"))
    root = load(ws).root(added["alias"])
    assert root is not None
    assert root["path"] == str(folder)
    assert shadow_files(ws, added["alias"], root["start_tree"]) == ["note.md"]


def test_a_root_that_turns_out_to_be_a_file_fails_rather_than_approving_nothing(run_env: Path, ws: Path, engine: Engine) -> None:
    """Registered before it exists, written as a file: its content must not vanish into an empty tree."""
    (run_env / "other").mkdir()
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    engine(ws, "root-add", str(run_env / "other" / "new.py"))
    (run_env / "other" / "new.py").write_text("print('hello')\n")

    answer = engine(ws, "accept")
    assert (answer["ok"], answer["code"]) == (False, "error")
    assert "no longer a directory" in answer["error"]
    assert load(ws).approved_phases == 0


def test_a_deleted_git_root_is_approved_as_empty_instead_of_wedging_the_run(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["delete web"]')
    shutil.rmtree(ws / "web")

    assert engine(ws, "accept")["ok"] is True
    web = load(ws).root("web")
    assert web is not None
    assert web["approved_tree"] == workspace.EMPTY_TREE_SHA1


def test_a_failed_init_new_puts_the_previous_run_back(run_env: Path, ws: Path, engine: Engine) -> None:
    first = started(engine, ws)["run"]["run_id"]
    engine(ws, "set-phases", stdin='["kept"]')
    broken = make_repo(run_env / "broken")
    (broken / ".git" / "index").write_bytes(b"not an index")

    failed = engine(ws, "init", "--plan", str(ws / "plan.md"), "--new", "--root", str(broken))
    assert failed["ok"] is False

    restored = engine(ws, "status")["run"]
    assert (restored["run_id"], restored["phases"]) == (first, ["kept"])
    assert not (ws / ".arl" / "runs" / first).exists()
    assert engine(ws, "resume")["ok"] is True


def test_resume_keeps_a_review_its_own_session_is_still_running(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        run.issue_ticket({"review_id": "r1", "session": "s-1"})
        run.save()
    engine(ws, "pause")

    kept = engine(ws, "resume", "--session", "s-1")
    assert (kept["abandoned_review"], kept["pending_review"]) == (False, True)

    other = engine(ws, "resume", "--session", "s-2")
    assert (other["abandoned_review"], other["pending_review"]) == (True, False)


def test_choosing_a_baseline_lets_the_loop_continue(run_env: Path, ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    assert engine(ws, "next-action")["action"] == "continue"
    late = run_env / "late"
    late.mkdir()
    (late / "f.txt").write_text("written during the run\n")
    alias = engine(ws, "root-add", str(late))["alias"]
    assert engine(ws, "next-action")["reason"] == "baseline-unknown"

    engine(ws, "root-baseline", alias, "--empty")
    assert engine(ws, "next-action")["action"] == "continue"


# --------------------------------------------------------------------------
# Round 3 review
# --------------------------------------------------------------------------


def test_a_repository_the_run_initialised_proposes_no_commit(run_env: Path, ws: Path, engine: Engine) -> None:
    """Its first commit is the run's own work; proposing it would leave that commit unreviewed."""
    started(engine, ws)
    made = make_repo(run_env / "made", when=None)
    (made / "more.py").write_text("x = 2\n")
    git_at(made, "add", "-A")
    git_at(made, "commit", "-qm", "second")
    added = engine(ws, "root-add", str(made))
    assert (added["baseline"], added["proposed"]) == ("unknown", "")


def test_accept_registers_a_repository_created_inside_a_root_and_refuses(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "set-phases", stdin='["one"]')
    nested = ws / "api" / "sub"
    nested.mkdir()
    git_at(nested, "init", "-q")
    (nested / "x.py").write_text("x = 1\n")

    refused = engine(ws, "accept")
    assert refused["code"] == "baseline-unknown"
    assert "sub" in refused["error"]
    assert load(ws).root("sub") is not None
    assert load(ws).approved_phases == 0


def test_a_folder_not_created_yet_inside_a_repository_registers_the_repository(run_env: Path, ws: Path, engine: Engine) -> None:
    other = make_repo(run_env / "other")
    backdate(other)
    started(engine, ws)
    added = engine(ws, "root-add", str(other / "newdir" / "pkg"))
    assert added["alias"] == "other"
    root = load(ws).root("other")
    assert root is not None
    assert (root["path"], root["kind"]) == (str(other), "git")
    assert engine(ws, "root-add", str(other / "src"))["status"] == "known"


def test_a_repository_its_container_ignores_is_not_a_root(ws: Path, engine: Engine) -> None:
    (ws / "api" / ".gitignore").write_text(".terraform/\n")
    git_at(ws / "api", "add", ".gitignore")
    git_at(ws / "api", "commit", "-qm", "ignore", when=PAST)
    make_repo(ws / "api" / ".terraform" / "modules" / "m")
    backdate(ws)
    roots = roots_by_alias(started(engine, ws))
    assert "m" not in roots

    make_repo(ws / "api" / ".terraform" / "modules" / "late", when=None)
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        assert workspace.discover(run, late=True) == []


def test_a_plan_that_is_not_utf8_is_refused_with_an_answer(ws: Path, engine: Engine) -> None:
    (ws / "plan.md").write_bytes(b"\xff\xfe# plan\n")
    assert engine(ws, "init", "--plan", str(ws / "plan.md"))["code"] == "no-plan"


def test_phases_that_are_not_utf8_are_refused_with_an_answer(ws: Path, run_env: Path) -> None:
    env = {"HOME": str(run_env), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(run_env / "config")}
    assert run_bootstrap(["--dir", str(ws), "init", "--plan", str(ws / "plan.md")], cwd=ws, env=env).returncode == 0
    proc = subprocess.run(
        [sys.executable, "-I", str(BOOTSTRAP), "--dir", str(ws), "set-phases"],
        input=b"\xff\xfe[]",
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["code"] == "bad-phases"


def test_the_scan_finds_a_repository_inside_a_repository_it_just_found(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    outer = make_repo(ws / "outer", when=None)
    make_repo(outer / "inner", when=None)
    with state.locked_state(state.run_paths(ws)) as run:
        assert run is not None
        assert workspace.discover(run, late=True) == ["outer", "inner"]


# --------------------------------------------------------------------------
# Round 4 review
# --------------------------------------------------------------------------


def _add_submodule(parent: Path, source: Path, name: str) -> None:
    git_at(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), name)
    git_at(parent, "commit", "-qm", f"add {name}", when=PAST)


def test_a_chain_of_submodules_snapshots(run_env: Path, engine: Engine) -> None:
    """``ws`` -> submodule ``b`` -> submodule ``c``: excluding ``b/c`` as well as ``b`` fails git."""
    c_src = make_repo(run_env / "c-src")
    b_src = make_repo(run_env / "b-src")
    _add_submodule(b_src, c_src, "c")
    top = make_repo(run_env / "top")
    _add_submodule(top, b_src, "b")
    git_at(top, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init", "--recursive")
    (top / "plan.md").write_text("# Plan\n")
    backdate(top)

    roots = roots_by_alias(started(engine, top))
    assert {"top", "b", "c"} <= set(roots)
    engine(top, "set-phases", stdin='["one"]')
    (top / "b" / "c" / "README.md").write_text("changed in c\n")
    assert engine(top, "accept")["ok"] is True


# --------------------------------------------------------------------------
# Round 5 review
# --------------------------------------------------------------------------


def test_a_repository_created_around_the_run_later_never_stages_it(run_env: Path, engine: Engine) -> None:
    """A run started in an empty folder whose first phase is ``git init``."""
    project = run_env / "project"
    project.mkdir()
    (project / "plan.md").write_text("# Plan\n\n1. git init and scaffold.\n")
    started(engine, project)

    git_at(project, "init", "-q")
    (project / "main.py").write_text("print('hi')\n")
    assert git_at(project, "status", "--porcelain", "--untracked-files=all").splitlines() == ["?? main.py", "?? plan.md"]


def test_the_self_ignore_survives_init_new(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    engine(ws, "init", "--plan", str(ws / "plan.md"), "--new")
    assert (ws / ".arl" / ".gitignore").read_text() == "*\n"


def test_a_git_root_that_lost_its_git_dir_fails_rather_than_snapshotting_its_container(run_env: Path, engine: Engine) -> None:
    top = make_repo(run_env / "top")
    inner = make_repo(top / "inner")
    (top / "plan.md").write_text("# Plan\n")
    backdate(top)
    started(engine, top)
    engine(top, "set-phases", stdin='["one"]')
    shutil.rmtree(inner / ".git")

    answer = engine(top, "accept")
    assert (answer["ok"], answer["code"]) == (False, "error")
    assert "no longer the top of a git repository" in answer["error"]


def test_a_relative_plan_is_read_from_the_launch_dir(run_env: Path, ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    (run_env / "plan.md").write_text("# The wrong plan\n")
    monkeypatch.chdir(run_env)
    engine(ws, "init", "--plan", "plan.md")
    assert (ws / ".arl" / "plan.md").read_text() == (ws / "plan.md").read_text()
