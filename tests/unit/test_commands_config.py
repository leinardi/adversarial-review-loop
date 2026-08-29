"""``config`` is the one command that writes outside any activation.

It has its own trust properties worth pinning: an unknown key or an unparseable value must
write nothing at all, a file that already exists but fails to parse must be refused rather
than clobbered, and the ``--repo`` target must never touch the user config file (or vice
versa). ``model`` additionally carries the reachability probe ``arm`` runs at arm time --
here it is a warning, not a refusal, when the reviewer cannot be reached at all.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

from conftest import SCRIPTS_DIR, run_bootstrap
from test_commands_arm import _path_without_opencode, armed_env

from ocrl import config as config_module


def user_config_file(env: dict[str, str]) -> Path:
    return Path(env["XDG_CONFIG_HOME"]) / "opencode-review-loop" / "config.json"


def repo_config_file(repo: Path) -> Path:
    return repo / config_module.REPO_CONFIG_NAME


# --------------------------------------------------------------------------
# `config` with no arguments
# --------------------------------------------------------------------------


def test_no_arguments_shows_every_key_at_its_default(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    ttl_line = next(line for line in proc.stdout.splitlines() if line.startswith("ttl_hours"))
    assert "24" in ttl_line
    assert "(default)" in ttl_line


def test_show_reports_the_layer_that_set_a_key(git_repo: Path, clean_env: dict[str, str]) -> None:
    run_bootstrap(["config", "ttl_hours", "5"], cwd=git_repo, env=clean_env)
    run_bootstrap(["config", "block_severity", "high", "--repo"], cwd=git_repo, env=clean_env)
    env = {**clean_env, "OCRL_MAX_DEFERS": "9"}

    proc = run_bootstrap(["config"], cwd=git_repo, env=env)

    lines = proc.stdout.splitlines()
    ttl_line = next(line for line in lines if line.startswith("ttl_hours"))
    severity_line = next(line for line in lines if line.startswith("block_severity"))
    defers_line = next(line for line in lines if line.startswith("max_defers"))
    assert "5" in ttl_line and "(user)" in ttl_line
    assert "high" in severity_line and "(repo)" in severity_line
    assert "9" in defers_line and "(env)" in defers_line


# --------------------------------------------------------------------------
# set / unset
# --------------------------------------------------------------------------


def test_set_writes_the_user_config_and_reports_old_to_new(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "block_severity", "high"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert "medium -> high" in proc.stdout
    assert json.loads(user_config_file(clean_env).read_text()) == {"block_severity": "high"}
    assert not repo_config_file(git_repo).exists()


def test_repo_flag_writes_the_repository_config_only(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "block_severity", "high", "--repo"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(repo_config_file(git_repo).read_text()) == {"block_severity": "high"}
    assert not user_config_file(clean_env).exists()


def test_repo_flag_leaves_no_lock_file_or_anything_else_behind_in_the_repository(git_repo: Path, clean_env: dict[str, str]) -> None:
    """Rule 3 permits exactly one file to be written inside the repository under review --
    the config file itself. The read-modify-write lock this command takes must not be a
    second, permanent, untracked file left beside it."""
    before = set(git_repo.iterdir())
    repo_mode_before = stat.S_IMODE(git_repo.stat().st_mode)

    proc = run_bootstrap(["config", "block_severity", "high", "--repo"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    after = set(git_repo.iterdir())
    assert after - before == {repo_config_file(git_repo)}
    assert stat.S_IMODE(git_repo.stat().st_mode) == repo_mode_before


def test_unset_removes_the_key(git_repo: Path, clean_env: dict[str, str]) -> None:
    run_bootstrap(["config", "ttl_hours", "5"], cwd=git_repo, env=clean_env)

    proc = run_bootstrap(["config", "ttl_hours", "--unset"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {}


def test_unset_of_a_key_that_was_never_set_is_a_no_op(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "ttl_hours", "--unset"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert "nothing to do" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_final_review_is_set_like_any_other_boolean(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "final_review", "true"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert "false -> true" in proc.stdout
    assert json.loads(user_config_file(clean_env).read_text()) == {"final_review": True}


def test_a_multi_word_value_is_rejoined_with_spaces(git_repo: Path, clean_env: dict[str, str]) -> None:
    """`$ARGUMENTS` reaches the shim unquoted, so a `verify_cmd` arrives as several tokens."""
    proc = run_bootstrap(["config", "verify_cmd", "npm", "test"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"verify_cmd": "npm test"}


# --------------------------------------------------------------------------
# Refusals that must write nothing
# --------------------------------------------------------------------------


def test_unknown_key_is_refused_and_writes_nothing(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "not_a_real_key", "x"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "unknown key" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_an_unparseable_int_is_refused_rather_than_coerced_to_zero(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "ttl_hours", "soon"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "not a non-negative integer" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_an_unparseable_bool_is_refused(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "pure", "sortof"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "not a boolean" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_an_unparseable_final_review_bool_is_refused(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "final_review", "sortof"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "not a boolean" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_an_unrecognised_block_severity_is_refused(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A typo here does not error downstream, it silently ranks the threshold at 1 -- the
    laxest possible setting -- so catching it at set time matters more than for an ordinary
    string key."""
    proc = run_bootstrap(["config", "block_severity", "hihg"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "not a recognised severity" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_a_recognised_block_severity_is_accepted_case_insensitively(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "block_severity", "HIGH"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"block_severity": "high"}


def test_critical_is_a_recognised_block_severity(git_repo: Path, clean_env: dict[str, str]) -> None:
    """`critical` is the reviewer contract's own fifth severity tier, not a typo -- it must
    not be refused, and it must not be silently treated as the laxest possible threshold."""
    proc = run_bootstrap(["config", "block_severity", "critical"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"block_severity": "critical"}


def test_a_list_value_is_split_on_commas(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "ignore_globs", "a,b,c"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"ignore_globs": ["a", "b", "c"]}


def test_malformed_existing_file_is_refused_not_overwritten(git_repo: Path, clean_env: dict[str, str]) -> None:
    path = user_config_file(clean_env)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")

    proc = run_bootstrap(["config", "ttl_hours", "5"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "could not be parsed" in proc.stdout
    assert path.read_text() == "{ not json"


def test_malformed_existing_file_does_not_block_unrelated_layers(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A broken user file must not stop a `--repo` write, and vice versa -- different files."""
    user_config_file(clean_env).parent.mkdir(parents=True)
    user_config_file(clean_env).write_text("{ not json")

    proc = run_bootstrap(["config", "block_severity", "high", "--repo"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(repo_config_file(git_repo).read_text()) == {"block_severity": "high"}


# --------------------------------------------------------------------------
# `model`'s reachability probe
# --------------------------------------------------------------------------


def test_model_probe_is_skipped_under_the_reviewer_seam(git_repo: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    proc = run_bootstrap(["config", "model", "anything/goes"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "warning" not in proc.stdout
    assert json.loads(user_config_file(env).read_text()) == {"model": "anything/goes"}


def test_model_probe_warns_but_does_not_refuse_when_opencode_is_unreachable(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = {**clean_env, "PATH": _path_without_opencode(tmp_path)}
    proc = run_bootstrap(["config", "model", "vendor/whatever"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "warning" in proc.stdout
    assert "opencode" in proc.stdout
    assert json.loads(user_config_file(env).read_text()) == {"model": "vendor/whatever"}


def test_model_probe_refuses_a_name_the_reviewer_does_not_report(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/other-model\\n'\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(["config", "model", "vendor/wanted-model"], cwd=git_repo, env=env)

    assert proc.returncode == 1
    assert "is not among the models OpenCode reports" in proc.stdout
    assert not user_config_file(env).exists()


def test_model_probe_warns_rather_than_trusts_a_non_zero_exit(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A model can be printed before the probe crashes; that is not a confirmed list.

    Before the fix, a non-zero exit code was ignored entirely: whatever had reached stdout
    was trusted as the complete model list, so this would incorrectly *refuse* a model that
    was, in fact, printed -- or accept one that happened to be printed before a real failure.
    """
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/wanted-model\\n'\nexit 1\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(["config", "model", "vendor/wanted-model"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "warning" in proc.stdout
    assert json.loads(user_config_file(env).read_text()) == {"model": "vendor/wanted-model"}


def test_model_force_skips_the_probe_entirely(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    bindir = Path(_path_without_opencode(tmp_path))
    fake = bindir / "opencode"
    fake.write_text("#!/usr/bin/env bash\nprintf 'vendor/other-model\\n'\n")
    fake.chmod(0o755)
    env = {**clean_env, "PATH": str(bindir)}

    proc = run_bootstrap(["config", "model", "vendor/wanted-model", "--force"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    assert "warning" not in proc.stdout
    assert json.loads(user_config_file(env).read_text()) == {"model": "vendor/wanted-model"}


# --------------------------------------------------------------------------
# The overlay reaching a real review invocation
# --------------------------------------------------------------------------


def test_a_configured_model_reaches_the_dry_run_argv(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """`config model` writes the user config; `dry-run` must read it back through `arm`."""
    env = armed_env(clean_env)
    run_bootstrap(["config", "model", "vendor/configured-model"], cwd=git_repo, env=env)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n\nphase one\n")
    run_bootstrap(["arm", "--session", "s1", "--plan", str(plan)], cwd=git_repo, env=env)
    (git_repo / "changed.txt").write_text("new work\n")

    proc = run_bootstrap(["dry-run"], cwd=git_repo, env=env)

    assert proc.returncode == 0, proc.stderr
    argv_block = proc.stdout.split("# argv (one element per line)\n", 1)[1].split("\n# the prompt argument\n", 1)[0]
    argv = argv_block.splitlines()
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "vendor/configured-model"


# --------------------------------------------------------------------------
# The activation overlay must not outlive the activation
# --------------------------------------------------------------------------


def test_show_reports_a_live_activations_overlay(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    env = armed_env(clean_env)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n\nphase one\n")
    run_bootstrap(["arm", "--session", "s1", "--args", f"{plan} --model vendor/activation-model"], cwd=git_repo, env=env)

    proc = run_bootstrap(["config"], cwd=git_repo, env=env)

    model_line = next(line for line in proc.stdout.splitlines() if line.startswith("model"))
    assert "vendor/activation-model" in model_line
    assert "(activation)" in model_line


def test_show_ignores_the_overlay_of_a_terminal_activation(git_repo: Path, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """A finished, stopped, failed or retired activation no longer governs the next run.

    Before the fix, `resolve_local_activation` returns any activation whose state loads,
    regardless of status, so a `--model` an old, now-terminal activation used would be shown
    as the layer deciding the *next* `implement`, when it is actually the user config's
    value that will apply.
    """
    env = armed_env(clean_env)
    run_bootstrap(["config", "model", "vendor/user-model"], cwd=git_repo, env=env)
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n\nphase one\n")
    run_bootstrap(["arm", "--session", "s1", "--args", f"{plan} --model vendor/activation-model"], cwd=git_repo, env=env)
    run_bootstrap(["deactivate"], cwd=git_repo, env=env)

    proc = run_bootstrap(["config"], cwd=git_repo, env=env)

    model_line = next(line for line in proc.stdout.splitlines() if line.startswith("model"))
    assert "vendor/user-model" in model_line
    assert "(user)" in model_line
    assert "vendor/activation-model" not in model_line


# --------------------------------------------------------------------------
# `--` ends option scanning, so a value may itself contain flag-looking tokens
# --------------------------------------------------------------------------


def test_a_value_with_a_flag_looking_token_needs_the_end_of_options_marker(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "verify_cmd", "pytest", "--maxfail=1"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 1
    assert "unrecognised flag" in proc.stdout
    assert not user_config_file(clean_env).exists()


def test_the_end_of_options_marker_lets_a_value_contain_flag_looking_tokens(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "verify_cmd", "--", "pytest", "--maxfail=1"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"verify_cmd": "pytest --maxfail=1"}


def test_flags_may_still_come_before_the_end_of_options_marker(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "verify_cmd", "--repo", "--", "npm", "test"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(repo_config_file(git_repo).read_text()) == {"verify_cmd": "npm test"}
    assert not user_config_file(clean_env).exists()


def test_only_the_first_end_of_options_marker_is_consumed(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A value that itself needs a literal `--` (npm's own args separator) must round-trip."""
    proc = run_bootstrap(["config", "verify_cmd", "--", "npm", "test", "--", "--runInBand"], cwd=git_repo, env=clean_env)

    assert proc.returncode == 0, proc.stderr
    assert json.loads(user_config_file(clean_env).read_text()) == {"verify_cmd": "npm test -- --runInBand"}


# --------------------------------------------------------------------------
# `show` must not display a value nothing actually reads
# --------------------------------------------------------------------------


def test_show_flags_an_unparseable_stored_int_rather_than_echoing_it(git_repo: Path, clean_env: dict[str, str]) -> None:
    """A hand-edited (or otherwise foreign-written) file can hold garbage `config set` itself
    would refuse -- `Config.as_int` silently falls back to the default for it everywhere
    else, so echoing the raw string here would show a value nothing actually reads."""
    path = user_config_file(clean_env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ttl_hours": "soon"}))

    proc = run_bootstrap(["config"], cwd=git_repo, env=clean_env)

    ttl_line = next(line for line in proc.stdout.splitlines() if line.startswith("ttl_hours"))
    assert "24" in ttl_line, "the effective value is the default Config.as_int falls back to"
    assert "soon" not in ttl_line
    assert "not an integer" in ttl_line


def test_show_flags_an_unrecognised_stored_block_severity(git_repo: Path, clean_env: dict[str, str]) -> None:
    """`config set` refuses this, but a file written some other way is not bound by it, and
    the gate treats an unrecognised threshold as rank 1 (`threshold_rank`) -- `show` must say
    so rather than echo the stored word as if it meant something the gate actually honours."""
    path = user_config_file(clean_env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"block_severity": "hihg"}))

    proc = run_bootstrap(["config"], cwd=git_repo, env=clean_env)

    severity_line = next(line for line in proc.stdout.splitlines() if line.startswith("block_severity"))
    assert "hihg" in severity_line
    assert "not a recognised severity" in severity_line


# --------------------------------------------------------------------------
# Locking a read-modify-write of the same target
# --------------------------------------------------------------------------


def test_locked_config_target_serialises_two_processes(git_repo: Path, clean_env: dict[str, str]) -> None:
    """The primitive `_set`/`_unset` rely on to avoid a lost update when two `config`
    invocations race a read-modify-write of the same file: their critical sections must
    never overlap."""
    target = repo_config_file(git_repo)
    target.write_text("{}")
    results = git_repo.parent / "results.txt"
    script = git_repo.parent / "hold_lock.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from pathlib import Path\n"
        "from ocrl import atomic, paths\n"
        "from ocrl.commands.configcmd import _lock_path\n"
        f"target = Path({str(target)!r})\n"
        "with atomic.locked(_lock_path(target), root=paths.state_root()):\n"
        "    start = time.monotonic()\n"
        "    time.sleep(0.3)\n"
        "    end = time.monotonic()\n"
        f"with open({str(results)!r}, 'a') as f:\n"
        "    f.write(f'{start} {end}\\n')\n"
    )

    procs = [subprocess.Popen([sys.executable, str(script)], env=clean_env) for _ in range(2)]
    for proc in procs:
        assert proc.wait() == 0

    lines = results.read_text().splitlines()
    assert len(lines) == 2
    intervals = sorted((float(a), float(b)) for a, b in (line.split() for line in lines))
    assert intervals[0][1] <= intervals[1][0], f"the two critical sections overlapped: {intervals}"


def test_an_unrecognised_late_block_severity_is_refused(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config", "late_block_severity", "hihg"], cwd=git_repo, env=clean_env)
    assert proc.returncode != 0
    assert "not a recognised severity" in proc.stderr + proc.stdout
    assert not user_config_file(clean_env).exists()


def test_the_config_listing_shows_late_block_severity_default_high(git_repo: Path, clean_env: dict[str, str]) -> None:
    proc = run_bootstrap(["config"], cwd=git_repo, env=clean_env)
    assert proc.returncode == 0, proc.stderr
    line = next(line for line in proc.stdout.splitlines() if line.startswith("late_block_severity"))
    assert "high" in line
    assert "(default)" in line
