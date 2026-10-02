"""``status --json --session <id>``, and the text form's causes instead of "not armed".

The JSON form is what the plugin's display reads on every turn, so two properties carry it:
it is **bound to the session it is given** (a predecessor is never shown its successor's
state, which the worktree's ``latest`` fallback did), and every way of failing to establish
the answer is ``binding: unknown`` with a cause -- never ``unarmed``. It also writes nothing,
in particular never answering an arming marker the way ``hooks.pending_intent`` does.
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
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import git, run_bootstrap, run_hook
from test_commands_arm import armed_env, state_dir
from test_commands_posttool import end_the_mode
from test_commands_pretool import SESSION, active, patch_state
from test_commands_resume import resume
from test_commands_stop import ended, stop

#: A session that never bound to anything.
STRANGER = "s9"


def status_json(repo: Path, env: dict[str, str], session: str, *, cwd: Path | None = None) -> dict[str, Any]:
    proc = run_bootstrap(["status", "--json", "--session", session], cwd=cwd or repo, env=env)
    assert proc.returncode == 0, proc.stderr
    document: dict[str, Any] = json.loads(proc.stdout)
    return document


def status_text(repo: Path, env: dict[str, str], *, cwd: Path | None = None) -> str:
    proc = run_bootstrap(["status"], cwd=cwd or repo, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def state_file(env: dict[str, str], repo: Path, session: str = SESSION) -> Path:
    return state_dir(env, repo, session) / "state.json"


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["--json"], ["--session", SESSION], ["--json", "--session", "../escape"], ["--verbose"]],
    ids=["json-without-a-session", "session-without-json", "unsafe-session", "unknown-flag"],
)
def test_status_refuses_arguments_it_cannot_honour(git_repo: Path, clean_env: dict[str, str], argv: list[str]) -> None:
    """``--json`` with no session would have to guess from ``latest`` -- the fallback this form exists to drop."""
    proc = run_bootstrap(["status", *argv], cwd=git_repo, env=armed_env(clean_env))
    assert proc.returncode == 2
    assert proc.stdout == ""


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------


def test_status_json_shows_each_session_its_own_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Two sessions in one worktree: the retired predecessor sees its own record, not the successor's."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "first", "second")
    code, out = resume(git_repo, env, session="s2")
    assert code == 0, out

    predecessor = status_json(git_repo, env, SESSION)
    successor = status_json(git_repo, env, "s2")

    assert predecessor["binding"] == "bound"
    assert predecessor["session"] == SESSION
    assert predecessor["status"] == "RESUMED"
    assert successor["binding"] == "bound"
    assert successor["session"] == "s2"
    assert successor["status"] == "ACTIVE"
    assert successor["phase"] == 1
    assert successor["phase_count"] == 2


def test_status_json_reports_the_bound_activation_in_enums_and_integers(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env, "first", "second")

    document = status_json(git_repo, env, SESSION)

    assert document == {
        "binding": "bound",
        "session": SESSION,
        "status": "ACTIVE",
        "stored_status": "ACTIVE",
        "phase": 1,
        "phase_count": 2,
        "rounds_this_phase": 0,
        "failures": 0,
        "max_failures": 2,
        "transient_failures": 0,
        "max_transient_failures": 5,
        "stop_blocks": 0,
        "max_stop_blocks": 3,
        "pause_target": 0,
        "retry_backoff_sec": 0,
        "alerts": [],
    }


def test_status_json_names_another_sessions_live_activation_as_unbound(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)

    assert status_json(git_repo, env, STRANGER) == {"binding": "unbound", "session": SESSION, "status": "ACTIVE", "alerts": []}


def test_status_json_is_unarmed_where_nothing_is(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()

    assert status_json(git_repo, env, SESSION) == {"binding": "unarmed", "alerts": []}
    assert status_json(git_repo, env, SESSION, cwd=elsewhere) == {"binding": "unarmed", "alerts": []}


def test_status_json_is_unarmed_once_the_latest_activation_ended(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    end_the_mode(git_repo, env, "DISARMED")

    assert status_json(git_repo, env, STRANGER) == {"binding": "unarmed", "alerts": []}


# --------------------------------------------------------------------------
# Unknown is never unarmed
# --------------------------------------------------------------------------


def _truncate(path: Path) -> None:
    path.write_text(path.read_text()[:20])


def _future_version(path: Path) -> None:
    document = json.loads(path.read_text())
    document["version"] = 99
    path.write_text(json.dumps(document))


def _unknown_status(path: Path) -> None:
    document = json.loads(path.read_text())
    document["status"] = "\x1b[2JARMED"
    path.write_text(json.dumps(document))


def _remove(path: Path) -> None:
    path.unlink()


def _unreadable(path: Path) -> None:
    path.chmod(0)


_BREAKAGES: list[tuple[str, Callable[[Path], None], str]] = [
    ("truncated", _truncate, "document_malformed"),
    ("future-version", _future_version, "version_conflict"),
    ("unknown-status", _unknown_status, "document_malformed"),
    ("missing", _remove, "document_missing"),
    ("unreadable", _unreadable, "document_unreadable"),
]


@pytest.mark.parametrize("case", [(b, c) for _, b, c in _BREAKAGES], ids=[name for name, _, _ in _BREAKAGES])
@pytest.mark.parametrize("session", [SESSION, STRANGER], ids=["bound", "unbound"])
def test_a_document_that_cannot_be_read_is_unknown_never_unarmed(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    case: tuple[Callable[[Path], None], str],
    session: str,
) -> None:
    breakage, cause = case
    if cause == "document_unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    breakage(state_file(env, git_repo))

    assert status_json(git_repo, env, session) == {"binding": "unknown", "cause": cause, "alerts": ["state_unknown"]}


@pytest.mark.parametrize(("breakage", "cause"), [(b, c) for _, b, c in _BREAKAGES], ids=[name for name, _, _ in _BREAKAGES])
def test_the_text_form_names_the_cause_instead_of_not_armed(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    breakage: Callable[[Path], None],
    cause: str,
) -> None:
    if cause == "document_unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    breakage(state_file(env, git_repo))

    out = status_text(git_repo, env)

    assert "not armed" not in out
    assert "does NOT mean it is unarmed" in out
    assert f"cause:  {cause}\n" in out


def test_a_repository_git_cannot_resolve_is_unknown(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """From a subdirectory with git off PATH, "could not ask" must not read as "not a repository"."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    subdir = git_repo / "sub"
    subdir.mkdir()
    no_git = {**env, "PATH": str(tmp_path / "empty-path")}

    for session in (SESSION, STRANGER):
        assert status_json(git_repo, no_git, session, cwd=subdir) == {"binding": "unknown", "cause": "repo_unresolvable", "alerts": ["state_unknown"]}
    assert "cause:  repo_unresolvable\n" in status_text(git_repo, no_git, cwd=subdir)


# --------------------------------------------------------------------------
# Reads only
# --------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(root.rglob("*")) if path.is_file()}


def test_status_changes_nothing_not_even_an_arming_marker(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``hooks.pending_intent`` unlinks an answered marker and publishes a pointer for a late one.

    A display that refreshes on every turn must never be what does either, so the whole state
    root -- the marker and every pointer included -- is byte- and mtime-identical afterwards.
    """
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    marker_session = "s7"
    prompt = {"session_id": marker_session, "cwd": str(git_repo), "prompt": "/adversarial-review-loop:implement plan.md"}
    proc = run_hook("intent", prompt, cwd=git_repo, env=env)
    assert proc.returncode == 0, proc.stderr
    root = Path(env["XDG_STATE_HOME"])
    assert any("s7" in name for name in _snapshot(root)), "the intent hook left no marker to protect"
    before = _snapshot(root)

    documents = [status_json(git_repo, env, session) for session in (SESSION, marker_session, STRANGER)]
    status_text(git_repo, env)

    assert _snapshot(root) == before
    assert [document["binding"] for document in documents] == ["bound", "unbound", "unbound"]


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def _commit_ungated(repo: Path) -> None:
    (repo / "sneaked.txt").write_text("never gated\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ungated")


@pytest.mark.parametrize(
    "case",
    [
        ("DISARMED", True, "unreviewed_at_exit"),
        ("DISARMED", False, ""),
        ("RESUMED", True, "unreviewed_at_exit"),
    ],
    ids=["disarmed-ungated", "disarmed-clean", "resumed-ungated"],
)
def test_status_json_alerts_exactly_when_the_stop_gate_reports(
    git_repo: Path,
    tmp_path: Path,
    clean_env: dict[str, str],
    case: tuple[str, bool, str],
) -> None:
    """``_ended_alert`` mirrors ``stop._ended``; this holds the two together."""
    status, ungated, alert = case
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    if ungated:
        _commit_ungated(git_repo)
    end_the_mode(git_repo, env, status)

    alerts = status_json(git_repo, env, SESSION)["alerts"]
    message = ended(stop(git_repo, env))

    assert alerts == ([alert] if alert else [])
    assert bool(alerts) == bool(message)


def test_an_edited_ended_record_alerts_as_the_stop_gate_does(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """``status`` written straight into the document leaves the record present and empty."""
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="DISARMED")

    alerts = status_json(git_repo, env, SESSION)["alerts"]

    assert alerts == ["ended_record_malformed"]
    assert "not one this gate could have written" in ended(stop(git_repo, env))


def test_needs_human_is_an_alert(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    active(git_repo, tmp_path, env)
    patch_state(env, git_repo, status="NEEDS_HUMAN")

    document = status_json(git_repo, env, SESSION)

    assert document["status"] == "NEEDS_HUMAN"
    assert document["alerts"] == ["needs_human"]
