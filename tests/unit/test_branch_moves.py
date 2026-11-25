"""A root whose checkout left its base's line of history: the base follows it when it safely can.

A base that is a clean checkout of a commit moves to the merge-base of that commit and HEAD, so a
fix only the old line has is not shown as removed by the phase. A base with uncommitted changes, or
with no shared history, stays, and the reviewer and Claude are told why the diff may show more.
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
from run_common import PAST, arl, backdate, git_at, make_repo, run_env, ws

from arl import state

__all__ = ["run_env", "ws"]  # fixtures, re-exported so pytest finds them

Engine = Any

APPROVE = "<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n"


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Engine:
    return partial(arl, monkeypatch=monkeypatch, capsys=capsys)


def started(engine: Engine, launch: Path) -> None:
    assert engine(launch, "init", "--plan", str(launch / "plan.md"))["ok"]
    assert engine(launch, "set-phases", stdin=json.dumps(["phase 1", "phase 2"]))["ok"]


def range_text(launch: Path, ticket: dict[str, Any]) -> str:
    seq = int(ticket["review_id"].rsplit("-", 1)[1])
    return next((launch / ".arl" / "reviews").glob(f"*-{seq:03d}")).joinpath("bundle", "range.txt").read_text()


def diff_text(launch: Path, ticket: dict[str, Any]) -> str:
    seq = int(ticket["review_id"].rsplit("-", 1)[1])
    bundle = next((launch / ".arl" / "reviews").glob(f"*-{seq:03d}")) / "bundle"
    return "".join(path.read_text() for path in sorted(bundle.glob("changes.*.diff")))


def warnings_of(launch: Path, ticket: dict[str, Any]) -> str:
    return range_text(launch, ticket).partition("## Snapshot warnings")[2]


def record(engine: Engine, launch: Path, ticket: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    answer = tmp_path / f"answer-{ticket['review_id']}.md"
    answer.write_text(f"Read it.\n\n{APPROVE}")
    result: dict[str, Any] = engine(launch, "review", "record", ticket["review_id"], "--answer", str(answer))
    return result


def on_a_fix_branch(api: Path) -> None:
    """``api`` on a branch holding a fix ``main`` does not have, as rust-common was in the runhold run."""
    git_at(api, "checkout", "-q", "-b", "fix/prereqs")
    (api / "src" / "main.py").write_text("print('fixed')\n")
    git_at(api, "commit", "-qam", "a fix only this branch has")


def test_a_clean_base_follows_the_checkout_to_another_line_of_history(ws: Path, engine: Engine, tmp_path: Path) -> None:
    """**Fails on the old code**, which diffed from the old branch: its fix read as removed."""
    api = ws / "api"
    on_a_fix_branch(api)
    main = git_at(api, "rev-parse", "main")
    started(engine, ws)
    run = state.RunState.load(state.run_paths(ws))
    assert run is not None
    [root] = [root for root in run.roots if root["alias"] == "api"]
    assert (root["approved_branch"], root["start_commit"]) == ("fix/prereqs", git_at(api, "rev-parse", "HEAD"))

    # The work goes on a new branch from main: the fix is not in it.
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    diff = diff_text(ws, ticket)
    assert "+print('new')" in diff
    assert "print('fixed')" not in diff
    moved = (
        f"api: the checkout left its starting base, commit {root['start_commit'][:12]} on branch 'fix/prereqs', for branch "
        f"'feat/work', so the base moved to the commit the two lines share, {main[:12]}."
    )
    assert moved in warnings_of(ws, ticket)
    assert "web:" not in warnings_of(ws, ticket)
    outcome = record(engine, ws, ticket, tmp_path)
    assert "Note: api: the checkout left its starting base" in outcome["message"]

    # Approved on the new line: the next phase says nothing.
    (api / "src" / "new.py").write_text("print('newer')\n")
    second = engine(ws, "review", "prepare")
    assert second["ok"], second
    assert "api:" not in warnings_of(ws, second)


def test_a_branch_made_from_the_base_changes_nothing(ws: Path, engine: Engine) -> None:
    started(engine, ws)
    git_at(ws / "web", "checkout", "-q", "-b", "feat/web")
    (ws / "web" / "src" / "main.py").write_text("print(2)\n")
    git_at(ws / "web", "commit", "-qam", "work")
    git_at(ws / "web", "checkout", "-q", "--detach")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "web:" not in warnings_of(ws, ticket)
    assert "+print(2)" in diff_text(ws, ticket)


def test_an_approved_base_never_follows_so_approved_work_the_line_lacks_reads_as_removed(ws: Path, engine: Engine, tmp_path: Path) -> None:
    """An approved base is the run's own work: when the checkout leaves it for a line without it,
    the reviewer sees that work go, and is told why."""
    api = ws / "api"
    started(engine, ws)
    (api / "src" / "main.py").write_text("print('phase 1')\n")
    record(engine, ws, engine(ws, "review", "prepare"), tmp_path)
    git_at(api, "checkout", "-q", "-b", "phase-1")
    git_at(api, "commit", "-qam", "phase 1, committed after its approval")
    # Phase 2 starts on a line from the seed commit, without phase 1.
    git_at(api, "checkout", "-q", "-b", "phase-2", "main")
    (api / "src" / "other.py").write_text("print('phase 2')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    said = (
        "api: its base, the snapshot approved last, was taken on branch 'main', and it is now on branch 'phase-2'. "
        "An approved base does not follow the checkout"
    )
    assert said in warnings_of(ws, ticket)
    diff = diff_text(ws, ticket)
    assert "+print('phase 2')" in diff
    assert "-print('phase 1')" in diff


def test_a_base_with_uncommitted_changes_stays_and_the_diff_is_explained(ws: Path, engine: Engine) -> None:
    api = ws / "api"
    on_a_fix_branch(api)
    (api / "src" / "draft.py").write_text("print('draft')\n")
    started(engine, ws)
    (api / "src" / "draft.py").unlink()
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    stays = "api: its base was taken on branch 'fix/prereqs' with uncommitted changes, and it is now on branch 'feat/work'."
    assert stays in warnings_of(ws, ticket)
    # Nothing is hidden: the old line's fix and the draft both read as removed.
    diff = diff_text(ws, ticket)
    assert "-print('fixed')" in diff
    assert "-print('draft')" in diff


def test_a_line_with_no_shared_history_is_not_followed(ws: Path, engine: Engine) -> None:
    api = ws / "api"
    started(engine, ws)
    git_at(api, "checkout", "-q", "--orphan", "unrelated")
    git_at(api, "commit", "-qm", "an unrelated root commit")
    (api / "src" / "main.py").write_text("print('unrelated')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "is not in the history of HEAD (now on branch 'unrelated'), and the two share no commit" in warnings_of(ws, ticket)


def test_a_run_stored_before_commits_were_recorded_does_not_guess_its_base(ws: Path, engine: Engine) -> None:
    api = ws / "api"
    on_a_fix_branch(api)
    started(engine, ws)
    run = state.RunState.load(state.run_paths(ws))
    assert run is not None
    with state.locked_state(run.paths) as locked:
        assert locked is not None
        for root in locked.roots:
            for key in ("approved_branch", "start_branch", "start_commit"):
                root.pop(key, None)
        locked.save()
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    # Nothing recorded says which commit the base was, or when: it stays, and nothing is hidden.
    assert "-print('fixed')" in diff_text(ws, ticket)
    assert "api:" not in warnings_of(ws, ticket)


def test_a_commit_the_user_chose_is_the_base_exactly_even_off_heads_history(ws: Path, engine: Engine) -> None:
    """A chosen commit is never moved to a merge-base: the user named it, and the review diffs
    from exactly that, saying it is not in HEAD's history."""
    lib = make_repo(ws.parent / "lib", when=PAST, files={"lib.py": "print('lib')\n"})
    git_at(lib, "checkout", "-q", "-b", "side", when=PAST)
    (lib / "side.py").write_text("print('only on side')\n")
    git_at(lib, "add", "side.py")
    git_at(lib, "commit", "-qm", "side work", when=PAST)
    side = git_at(lib, "rev-parse", "HEAD")
    git_at(lib, "checkout", "-q", "main", when=PAST)
    backdate(lib)
    started(engine, ws)
    (lib / "lib.py").write_text("print('the run')\n")
    added = engine(ws, "root-add", str(lib))
    assert added["baseline"] == "unknown", added

    chosen = engine(ws, "root-baseline", added["alias"], "--commit", side[:10])
    assert chosen["ok"], chosen
    assert "not in the history of HEAD" in chosen["note"]
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    diff = diff_text(ws, ticket)
    assert "-print('only on side')" in diff
    assert "+print('the run')" in diff
    # Not moved, and said: what only the chosen commit holds reads as removed.
    assert f"{added['alias']}: its base, commit {side[:12]} as the user chose it" in range_text(ws, ticket)


def test_a_commit_chosen_before_the_rule_still_follows_as_it_was_saved(ws: Path, engine: Engine) -> None:
    """The documented limit of :func:`arl.workspace.remember_commit`: a state saved by an older
    engine holds the chosen commit as ``start_commit``, like a base taken from a detached HEAD,
    and is left as saved. The move is said, so nothing changes unseen."""
    lib = make_repo(ws.parent / "lib", when=PAST, files={"lib.py": "print('lib')\n"})
    git_at(lib, "checkout", "-q", "-b", "side")
    (lib / "side.py").write_text("print('only on side')\n")
    git_at(lib, "add", "side.py")
    git_at(lib, "commit", "-qm", "side work", when=PAST)
    side = git_at(lib, "rev-parse", "HEAD")
    git_at(lib, "checkout", "-q", "main")
    started(engine, ws)
    (lib / "lib.py").write_text("print('the run')\n")
    added = engine(ws, "root-add", str(lib))
    assert engine(ws, "root-baseline", added["alias"], "--commit", side)["ok"]
    saved = json.loads((ws / ".arl" / "state.json").read_text())
    for root in saved["roots"]:
        if root["alias"] == added["alias"]:
            root["start_commit"] = side
    (ws / ".arl" / "state.json").write_text(json.dumps(saved))

    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "-print('only on side')" not in diff_text(ws, ticket)
    assert f"{added['alias']}:" in range_text(ws, ticket)


def test_head_as_the_run_found_it_follows_the_checkout_when_chosen(ws: Path, engine: Engine) -> None:
    """The "when the run started" candidate is a base taken from HEAD as it was: chosen, it
    follows a checkout that leaves its line, as AGENTS.md rule 2 has it, and the move is said."""
    lib = make_repo(ws.parent / "lib", when=PAST, files={"src/main.py": "print('main')\n"})
    # On a fix branch when the run starts, every reflog entry of it older than the run.
    git_at(lib, "checkout", "-q", "-b", "fix/prereqs", when=PAST)
    (lib / "src" / "main.py").write_text("print('fixed')\n")
    git_at(lib, "commit", "-qam", "a fix only this branch has", when=PAST)
    backdate(lib)
    started(engine, ws)
    git_at(lib, "commit", "-q", "--allow-empty", "-m", "bash commit")
    added = engine(ws, "root-add", str(lib))
    start = next(entry["commit"] for entry in added["candidates"] if entry["why"] == "start")
    chosen = engine(ws, "root-baseline", added["alias"], "--commit", start)
    assert chosen["ok"] and "note" not in chosen, chosen
    git_at(lib, "checkout", "-q", "main")
    git_at(lib, "checkout", "-q", "-b", "feat/work")
    (lib / "run.py").write_text("print('the run')\n")

    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    diff = diff_text(ws, ticket)
    assert "-print('fixed')" not in diff
    assert "+print('the run')" in diff
    assert f"{added['alias']}:" in range_text(ws, ticket)


def test_a_typed_commit_that_head_leaves_later_stays_and_is_said(ws: Path, engine: Engine) -> None:
    lib = make_repo(ws.parent / "lib", when=PAST, files={"src/main.py": "print('main')\n"})
    # On a fix branch when the run starts, every reflog entry of it older than the run.
    git_at(lib, "checkout", "-q", "-b", "fix/prereqs", when=PAST)
    (lib / "src" / "main.py").write_text("print('fixed')\n")
    git_at(lib, "commit", "-qam", "a fix only this branch has", when=PAST)
    backdate(lib)
    started(engine, ws)
    git_at(lib, "commit", "-q", "--allow-empty", "-m", "bash commit")
    bash = git_at(lib, "rev-parse", "HEAD")
    added = engine(ws, "root-add", str(lib))
    # Not the start candidate: the bash commit itself, named by the user.
    chosen = engine(ws, "root-baseline", added["alias"], "--commit", bash)
    assert chosen["ok"], chosen
    git_at(lib, "checkout", "-q", "main")
    git_at(lib, "checkout", "-q", "-b", "feat/work")
    (lib / "run.py").write_text("print('the run')\n")

    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "-print('fixed')" in diff_text(ws, ticket)
    assert f"{added['alias']}: its base, commit {bash[:12]} as the user chose it" in range_text(ws, ticket)


def test_a_chosen_commit_in_heads_history_says_nothing(ws: Path, engine: Engine) -> None:
    lib = make_repo(ws.parent / "lib", when=PAST, files={"lib.py": "print('lib')\n"})
    base = git_at(lib, "rev-parse", "HEAD")
    started(engine, ws)
    (lib / "lib.py").write_text("print('the run')\n")
    added = engine(ws, "root-add", str(lib))
    chosen = engine(ws, "root-baseline", added["alias"], "--commit", base)
    assert chosen["ok"] and "note" not in chosen, chosen


def test_an_empty_base_never_moves_so_no_file_of_the_root_leaves_the_review(ws: Path, engine: Engine) -> None:
    """An empty base makes every file the run's own work. A commit elsewhere with the empty tree
    (one that removed every file) is not its commit: moving to the merge-base would drop the files."""
    started(engine, ws)
    lib = make_repo(ws / "lib", when=None, files={"lib.py": "print('lib')\n"})
    added = engine(ws, "root-add", str(lib))
    assert added["ok"], added
    chosen = engine(ws, "root-baseline", "lib", "--empty")
    assert chosen["ok"], (added, chosen)
    git_at(lib, "checkout", "-q", "-b", "wipe")
    git_at(lib, "rm", "-rq", ".")
    git_at(lib, "commit", "-qm", "remove every file")
    git_at(lib, "checkout", "-q", "main")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "+print('lib')" in diff_text(ws, ticket)
    assert "lib:" not in warnings_of(ws, ticket)


def test_a_base_is_never_matched_to_an_older_commit_with_the_same_tree(ws: Path, engine: Engine) -> None:
    """Only a commit made since the base was taken is the base's own: the same content earlier in
    history says nothing about where the base came from."""
    api = ws / "api"
    git_at(api, "checkout", "-q", "-b", "old")
    (api / "src" / "main.py").write_text("print('v2')\n")
    git_at(api, "commit", "-qam", "an older commit elsewhere", when=PAST)
    git_at(api, "checkout", "-q", "main")
    # The base holds uncommitted changes that happen to equal that commit's tree.
    (api / "src" / "main.py").write_text("print('v2')\n")
    started(engine, ws)
    before = next(root["approved_tree"] for root in state.RunState.load(state.run_paths(ws)).roots if root["alias"] == "api")  # type: ignore[union-attr]
    git_at(api, "checkout", "-q", "-b", "feat/work")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "the checkout left its starting base" not in warnings_of(ws, ticket)
    after = next(root["approved_tree"] for root in state.RunState.load(state.run_paths(ws)).roots if root["alias"] == "api")  # type: ignore[union-attr]
    assert after == before


def test_a_move_is_said_again_when_the_review_that_first_saw_it_could_not_be_built(ws: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    api = ws / "api"
    on_a_fix_branch(api)
    started(engine, ws)
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n" * 200)
    monkeypatch.setenv("ARL_HARD_DIFF_CEILING", "100")
    refused = engine(ws, "review", "prepare")
    assert (refused["ok"], refused["code"]) == (False, "oversized"), refused
    monkeypatch.delenv("ARL_HARD_DIFF_CEILING")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "api: the checkout left its starting base" in warnings_of(ws, ticket)
    run = state.RunState.load(state.run_paths(ws))
    assert run is not None
    assert "api: the checkout left its starting base" in str(run.data["pending_ticket"]["note"])


def test_an_approved_base_committed_on_a_new_branch_says_nothing(ws: Path, engine: Engine, tmp_path: Path) -> None:
    """Claude often makes a branch after an approval and commits the phase there: nothing is lost."""
    api = ws / "api"
    started(engine, ws)
    (api / "src" / "main.py").write_text("print('phase 1')\n")
    record(engine, ws, engine(ws, "review", "prepare"), tmp_path)
    git_at(api, "checkout", "-q", "-b", "phase-1")
    git_at(api, "commit", "-qam", "phase 1, committed after its approval")
    (api / "src" / "other.py").write_text("print('phase 2')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert "api:" not in warnings_of(ws, ticket)
    assert "print('phase 1')" not in diff_text(ws, ticket)


def test_the_final_review_follows_the_starting_base_too(ws: Path, engine: Engine, tmp_path: Path) -> None:
    api = ws / "api"
    on_a_fix_branch(api)
    assert engine(ws, "init", "--plan", str(ws / "plan.md"))["ok"]
    assert engine(ws, "set-phases", stdin=json.dumps(["the only phase"]))["ok"]
    assert engine(ws, "finish")["ok"]
    # The phase is done on the fix branch, then the work moves to a line from main.
    (api / "src" / "new.py").write_text("print('new')\n")
    record(engine, ws, engine(ws, "review", "prepare"), tmp_path)
    git_at(api, "add", "-A")
    git_at(api, "commit", "-qm", "the phase")
    git_at(api, "checkout", "-q", "-b", "feat/work", "main")
    (api / "src" / "new.py").write_text("print('new')\n")
    ticket = engine(ws, "review", "prepare")
    assert ticket["ok"], ticket
    assert ticket["label"] == "final"
    assert "api: the checkout left its starting base" in warnings_of(ws, ticket)
    diff = diff_text(ws, ticket)
    assert "+print('new')" in diff
    assert "print('fixed')" not in diff
