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
