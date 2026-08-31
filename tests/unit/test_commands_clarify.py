"""``clarify`` -- one prose question about a review that already ran.

The command grants nothing and parses no verdict, so the tests here are about *scope* and
*targeting*: that it leaves every fingerprinted field and ``round_history`` byte-identical,
that its argv never continues a session, and that it points at the most recent round's own
bundle rather than at whatever the continuity pointer happens to name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import run_bootstrap
from test_commands_arm import armed_env, read_state, state_dir
from test_commands_posttool import COMMIT
from test_commands_pretool import SESSION, active, patch_state, pretool

from arl import reviewer
from arl.config import Config

_FINGERPRINT = (
    "armed_at",
    "baseline_tree",
    "session_id",
    "status",
    "phase",
    "last_approved_tree",
    "pending_approved_tree",
    "pending_head",
    "pending_command",
    "activation_generation",
    "round_history",
)


def clarify(repo: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    proc = run_bootstrap(["clarify", *args], cwd=repo, env=env)
    return proc.returncode, proc.stdout


def _round(repo: Path, env: dict[str, str], content: str) -> None:
    """Drive one denied review of phase 1, leaving a ``round_history`` entry and its bundle."""
    (repo / "a.txt").write_text(content)
    verdict, _ = pretool(repo, env, command=COMMIT)
    assert verdict == "deny"


def test_clarify_leaves_the_fingerprint_and_round_history_untouched(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    before = read_state(env, git_repo, SESSION)
    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "what did finding 1 mean?")
    assert code == 0, out
    after = read_state(env, git_repo, SESSION)

    for key in _FINGERPRINT:
        assert after[key] == before[key], key
    assert after["clarifications"] == 1
    assert after["clarify_seq"] == 1
    assert "Clarification." in out
    assert "what did finding 1 mean?" in out


def test_clarify_argv_never_continues_a_session(git_repo: Path) -> None:
    attachments = [git_repo / "bundles" / "001" / "range.txt", git_repo / "bundles" / "001" / "changes.00.diff"]
    argv = reviewer.clarify_argv(
        str(git_repo),
        attachments,
        git_repo / "context" / "001-question.txt",
        "review-loop clarify [deadbeef/001]",
        config=Config({"model": "m", "variant": "", "pure": True}),
    )
    assert "-s" not in argv
    assert "--title" in argv
    # Exactly the supplied attachments plus the one question -- no globbing here.
    assert argv.count("-f") == 3
    assert [argv[i + 1] for i, tok in enumerate(argv) if tok == "-f"] == [*map(str, attachments), str(git_repo / "context" / "001-question.txt")]


def test_clarify_targets_the_last_rounds_bundle_not_the_session_pointer(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")
    _round(git_repo, env, "v2\n")

    history = read_state(env, git_repo, SESSION)["round_history"]
    assert isinstance(history, list)
    assert [entry["seq"] for entry in history] == [1, 2]

    # A continuity pointer that names an earlier round -- the mismatch that motivated
    # running clarify cold against round_history rather than against reviewer_session.
    patch_state(env, git_repo, reviewer_session={"round": 1, "id": "ses_stale00"})

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "which round stands?")
    assert code == 0, out
    assert "bundles/002" in out
    assert "bundles/001" not in out


def test_clarify_is_refused_before_any_round(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")

    code, out = clarify(git_repo, env, "--question", "anything?")
    assert code == 1
    assert "no review has run" in out


def test_clarify_refuses_when_the_round_bundle_lost_a_diff_chunk(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`range.txt` alone is not an intact bundle -- a missing `changes.NN.diff` would have the
    reviewer answer from less evidence than the verdict was formed on."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    act = state_dir(env, git_repo, SESSION)
    (act / "bundles" / "001" / "changes.00.diff").unlink()

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 1
    assert "no longer on disk" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 0


def test_clarify_never_attaches_a_diff_file_the_manifest_does_not_name(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A `changes.NN.diff` beyond the manifest -- here a symlink to a repo file -- must not
    ride `-f` into the provider prompt.

    The manifest is authoritative, so the plant is simply never in the attachment set. It is
    *ignored* rather than fatal, deliberately: a stray file must not be able to break clarify
    for a round permanently, and it can no longer reach the provider either way."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    (git_repo / "secret.txt").write_text("secret\n")
    (state_dir(env, git_repo, SESSION) / "bundles" / "001" / "changes.99.diff").symlink_to(git_repo / "secret.txt")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 0, out
    assert "secret" not in out
    assert "changes.99" not in out


def test_clarify_rejects_a_bundle_directory_that_is_a_symlink(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The plant a per-file symlink check cannot see.

    Every file *below* a symlinked ``bundles/<seq>/`` is an ordinary regular file, so
    ``is_file() and not is_symlink()`` passes on each of them and the whole planted directory
    rides ``-f`` into the provider prompt. Containment has to be proved by walking the
    components, not by inspecting the leaves. Fails on the old code, which ran the clarify."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    act = state_dir(env, git_repo, SESSION)
    real = act / "bundles" / "001"
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "chunks").write_text("1\n")
    (planted / "range.txt").write_text("someone else's range\n")
    (planted / "changes.00.diff").write_text("someone else's secrets\n")

    shutil.rmtree(real)
    real.symlink_to(planted, target_is_directory=True)
    assert (real / "range.txt").is_file() and not (real / "range.txt").is_symlink(), "both naive checks pass"

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify"), "--question", "q")
    assert code == 1
    assert "no longer on disk" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 0


def test_clarify_discards_a_reply_when_the_activation_moves_during_the_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify-mutate"), "--question", "q")
    assert code == 1
    assert "discarded" in out
    # The allowance is still spent -- the counter bump landed before the invocation.
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1


def test_clarify_discards_a_reply_when_a_newer_round_completes_during_the_run(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`round_history` is not in `hooks.Activation`, so a concurrent `reviewer.execute`
    finishing a newer round leaves the fingerprint intact -- the round check is what catches it."""
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    code, out = clarify(git_repo, armed_env(clean_env, ARL_FAKE_MODE="clarify-supersede"), "--question", "q")
    assert code == 1
    assert "no longer the latest" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1


def test_clarify_is_refused_past_max_clarifications(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env, ARL_FAKE_MODE="changes")
    active(git_repo, tmp_path, env, "phase one", "phase two")
    _round(git_repo, env, "v1\n")

    ask_env = armed_env(clean_env, ARL_FAKE_MODE="clarify", ARL_MAX_CLARIFICATIONS="1")
    assert clarify(git_repo, ask_env, "--question", "one")[0] == 0
    code, out = clarify(git_repo, ask_env, "--question", "two")
    assert code == 1
    assert "already used" in out
    assert "accept" in out
    assert read_state(env, git_repo, SESSION)["clarifications"] == 1
