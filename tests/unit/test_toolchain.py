"""Proof that the Python toolchain is wired up: ruff, mypy and pytest all see real input."""

from __future__ import annotations

import sys

import arl


def test_python_floor() -> None:
    """The port targets Ubuntu 24.04's system Python and never anything older."""
    assert sys.version_info >= (3, 12)


def test_plugin_root_is_the_checkout() -> None:
    assert (arl.PLUGIN_ROOT / "scripts" / "arl.sh").is_file()
    assert arl.PACKAGE_ROOT.parent.name == "scripts"


def test_prompt_path_resolves_shipped_prompts() -> None:
    for name in ("reviewer-phase", "reviewer-final"):
        assert arl.prompt_path(name).is_file()
