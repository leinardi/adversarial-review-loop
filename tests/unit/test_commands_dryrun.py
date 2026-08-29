"""``dry-run`` is how a change to the reviewer invocation is inspected without a model call.

Its output is the :class:`ocrl.harness.Command` a real review would run, rendered generically,
so the assertions below are about *sections* -- harness, cwd, env overrides, argv, stdin --
rather than about one CLI's spelling. Each harness then gets one test that reads its own
delivery channel out of those sections, which is the difference between "the renderer works"
and "the invocation is right".

It has one property worth pinning beyond its output: the scratch activation it invents when
nothing is armed must not be reachable by a hook. A session pointer for it would make every
tool call in the worktree resolve to an activation the user never asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import run_bootstrap
from test_commands_arm import armed_env, plan_file, state_dir

import ocrl

ARGV_HEADER = "# argv (one element per line)\n"
STDIN_HEADER = "# stdin ("


def section(stdout: str, header: str) -> str:
    """Everything between ``header`` and the next ``#``-prefixed section header."""
    body = stdout.split(header, 1)[1]
    return body.split("\n#", 1)[0]


def argv_of(stdout: str) -> list[str]:
    """The argv block, one element per line -- what every harness's test starts from."""
    return section(stdout, ARGV_HEADER).splitlines()


def armed(git_repo: Path, tmp_path: Path, env: dict[str, str]) -> None:
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan_file(tmp_path))], cwd=git_repo, env=env)
    (git_repo / "changed.txt").write_text("new work\n")


def test_dry_run_prints_the_default_harnesss_whole_invocation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The default harness delivers prompt *and* attachments on stdin, and names nothing
    bundle-derived in its argv -- so the fences are the only place the attachments appear."""
    env = armed_env(clean_env)
    armed(git_repo, tmp_path, env)

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "# harness: claude-code\n" in proc.stdout
    argv = argv_of(proc.stdout)
    assert argv[:2] == ["claude", "-p"]
    assert str(git_repo) in argv, "the repository is reached through --add-dir, not through cwd"
    assert argv[argv.index(str(git_repo)) - 1] == "--add-dir"

    bundle = state_dir(env, git_repo, "s1") / "bundles" / "dry-run"
    payload = proc.stdout.split(STDIN_HEADER, 1)[1].split("\n", 1)[1].split("\n# bundle: ", 1)[0]
    prompt = ocrl.prompt_path("reviewer-phase").read_text()
    assert prompt.rstrip("\n") in payload
    assert "BEGIN ATTACHMENT" in payload and "range.txt =====" in payload
    assert str(bundle) not in payload, "attachments arrive as bytes, never as a path the reviewer could re-open"
    assert f"\n# bundle: {bundle}\n" in proc.stdout
    # The listing comes from `ls -la`, printed after everything the gate composed itself.
    assert "range.txt" in proc.stdout.split(f"# bundle: {bundle}\n", 1)[1]


def test_dry_run_prints_the_opencode_invocation_including_its_permission_document(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """The other delivery channel: the prompt is an argv element and the attachments are ``-f``
    pathnames, so what has to be visible here is the permission document that bounds them."""
    env = armed_env(clean_env, OCRL_HARNESS="opencode")
    armed(git_repo, tmp_path, env)

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "# harness: opencode\n" in proc.stdout
    overrides = section(proc.stdout, "# env overrides (one KEY=value per line, layered onto the gate's environment)\n").strip()
    permission = json.loads(overrides.split("OPENCODE_PERMISSION=", 1)[1])
    assert permission["*"] == "deny"
    assert permission["read"] == "allow"

    argv = argv_of(proc.stdout)
    assert argv[:2] == ["opencode", "run"]
    assert "--dir" in argv
    assert str(git_repo) in argv
    bundle = state_dir(env, git_repo, "s1") / "bundles" / "dry-run"
    # The real path attaches *staged copies*, never the bundle's own stable paths, and the
    # whole point of this command is to print the argv a review actually builds -- so what
    # appears here is a staged `range.txt`, not `<bundle>/range.txt`.
    attachments = [argv[i + 1] for i, item in enumerate(argv) if item == "-f"]
    assert any(path.endswith("/range.txt") for path in attachments)
    assert not any(path.startswith(f"{bundle}/") for path in attachments), "the stable bundle path is not what -f names"
    assert all("/.staged-" in path for path in attachments)

    # The prompt is a multi-line argv element, so it is moved below the argv rather than
    # printed inside it -- under the index it came from, so the argv can still be reassembled.
    assert argv[2].startswith("<element 2:")
    assert ocrl.prompt_path("reviewer-phase").read_text().rstrip("\n") in proc.stdout.split("\n# argv element 2, in full\n", 1)[1]
    assert "# stdin: nothing -- this harness reads no standard input\n" in proc.stdout


def test_dry_run_without_an_activation_invents_no_reachable_session(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    (git_repo / "changed.txt").write_text("new work\n")

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "# harness: " in proc.stdout
    sessions = Path(env["XDG_STATE_HOME"]) / "opencode-review-loop" / "sessions"
    assert not sessions.exists() or list(sessions.iterdir()) == []


def test_dry_run_refuses_a_harness_this_build_does_not_implement(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A dry run cannot show what an unimplemented harness would send, and must not fall back
    to showing what the default one would -- that is an invocation nobody configured."""
    env = armed_env(clean_env, OCRL_HARNESS="not-a-harness")
    (git_repo / "changed.txt").write_text("new work\n")

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "unknown harness 'not-a-harness'" in proc.stderr
    assert ARGV_HEADER not in proc.stdout


def test_dry_run_outside_a_repository_fails(tmp_path: Path, clean_env: dict[str, str]) -> None:
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()

    proc = run_bootstrap(["dry-run"], cwd=elsewhere, env=armed_env(clean_env))

    assert proc.returncode == 1
    assert proc.stderr == "not a git repository\n"
