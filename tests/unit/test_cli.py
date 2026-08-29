"""The dispatcher: which name reaches which command, and what an unknown name does."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from conftest import PLUGIN_ROOT, run_bootstrap

from ocrl import cli


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"]])
def test_help_is_the_only_thing_a_bare_invocation_does(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == cli.USAGE


def test_an_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """Two, not one: a usage error is not a gate decision, and nothing has been enforced."""
    assert cli.main(["not-a-subcommand"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == cli.USAGE


def test_selftest_hands_over_to_the_acceptance_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    """It stays Bash, so this is an exec rather than a call -- pin the path and the argv."""
    seen: dict[str, Any] = {}

    def fake_execv(path: str, argv: list[str]) -> None:
        seen["path"] = path
        seen["argv"] = argv

    monkeypatch.setattr(os, "execv", fake_execv)
    cli.main(["selftest", "stop"])

    assert seen["path"] == str(PLUGIN_ROOT / "tests" / "selftest.sh")
    assert seen["argv"] == [seen["path"], "stop"]


@pytest.mark.parametrize(
    "sub",
    ["arm", "set-phases", "defer", "status", "report", "finish", "deactivate", "dry-run"],
)
def test_every_user_facing_subcommand_is_wired(sub: str, tmp_path: Path, clean_env: dict[str, str]) -> None:
    """Not a behaviour test: it asserts the name reaches *something* other than usage.

    A subcommand missing from the table exits 2 with usage on stderr, which is exactly what a
    typo looks like -- so the distinction is worth pinning separately from what each command
    then does.
    """
    proc = run_bootstrap([sub], cwd=tmp_path, env=clean_env)
    assert proc.returncode != 2, proc.stderr
    assert not proc.stderr.startswith("usage: ocrl.sh")


# --------------------------------------------------------------------------
# The whole-hook deadline
# --------------------------------------------------------------------------

#: ``pretool``'s ceiling, which is what `scripts/ocrl.sh` gives its own ``timeout``.
PRETOOL_CEILING = 1150.0

#: A shrunk shim timeout, the shape ``OCRL_SHIM_TIMEOUT_PRETOOL`` produces in a test.
SHRUNK_TIMEOUT = 30.0


def test_a_non_hook_subcommand_has_no_deadline() -> None:
    """``finish``, ``status``, ``clarify`` and the rest run under no shim ``timeout`` at all,
    so there is no budget to run out of and nothing optional to withhold."""
    assert cli._hook_deadline("finish") is None
    assert cli._hook_deadline("") is None


@pytest.mark.parametrize(("sub", "ceiling"), [("pretool", 1150.0), ("gate-stop", 1750.0), ("confirm-commit", 50.0), ("posttool-failure", 20.0)])
def test_each_hook_entrypoint_falls_back_to_its_own_ceiling(sub: str, ceiling: float, monkeypatch: pytest.MonkeyPatch) -> None:
    """The numbers must match ``scripts/ocrl.sh``'s own ``timeout`` values."""
    monkeypatch.delenv("OCRL_HOOK_DEADLINE_SEC", raising=False)
    assert cli._hook_deadline(sub) == ceiling


@pytest.mark.parametrize("raw", ["", "0", "00", "-5", "1150.5", " 600", "600 ", "abc", "1151", "9" * 30, "٦٠٠"])
def test_an_unusable_deadline_override_falls_back_to_the_ceiling(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment is not a trust boundary, and this clamp is the same one
    ``ocrl_bounded_timeout`` applies on the shell side -- including its refusal of every
    spelling of zero, which means "no limit" to ``timeout``. Non-ASCII digits are rejected
    too: ``str.isdigit`` alone accepts them and ``int`` then parses them, so a value the
    shell could never have written would be honoured here."""
    monkeypatch.setenv("OCRL_HOOK_DEADLINE_SEC", raw)
    assert cli._hook_deadline("pretool") == PRETOOL_CEILING


def test_a_shrunk_shim_timeout_shrinks_the_gates_own_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shim passes the same number it gives ``timeout``, so the two cannot disagree."""
    monkeypatch.setenv("OCRL_HOOK_DEADLINE_SEC", "30")
    assert cli._hook_deadline("pretool") == SHRUNK_TIMEOUT


def test_dispatch_stamps_the_clock_for_a_hook_and_clears_it_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` is where the clock starts, so ``reviewer.remaining_budget`` measures the run."""
    monkeypatch.delenv("OCRL_HOOK_DEADLINE_SEC", raising=False)
    monkeypatch.setattr(cli, "HOOK_DEADLINE_SEC", 1.0)

    cli.main(["help"])
    assert cli.HOOK_DEADLINE_SEC is None

    before = cli.HOOK_STARTED
    cli._start_clock("pretool")
    assert cli.HOOK_DEADLINE_SEC == PRETOOL_CEILING
    assert before <= cli.HOOK_STARTED
