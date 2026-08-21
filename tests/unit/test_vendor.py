"""The vendored parser: is it the one we shipped, and does it stay in its box?

A vendored dependency brings two questions no unit test of ``cmdshape`` can answer. Is the
parser the gate imports the copy in this repository, rather than one a machine happens to
have installed? And does importing and running it write anything into the tree -- because
the tree it lives in is a repository this gate reviews, and **Rule 3** says nothing is
written inside the repository under review.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import PLUGIN_ROOT

import ocrl
from ocrl._vendor import bashlex

VENDOR = PLUGIN_ROOT / "scripts" / "ocrl" / "_vendor"
BASHLEX = VENDOR / "bashlex"


def test_the_parser_is_the_vendored_copy() -> None:
    """Not a system install, not a virtualenv: the file in this repository."""
    assert Path(bashlex.__file__).resolve() == (BASHLEX / "__init__.py").resolve()
    assert bashlex.__name__ == "ocrl._vendor.bashlex"


def test_no_top_level_bashlex_is_introduced() -> None:
    """The vendor directory is not on ``sys.path``, so nothing else can satisfy the name.

    Putting it on ``sys.path`` would have avoided rewriting the imports, at the cost of a
    top-level ``bashlex`` that a system-installed copy could win -- and then the parser
    behind the gate would be whichever version the machine happened to have.
    """
    assert bashlex.__name__ == "ocrl._vendor.bashlex"
    assert str(VENDOR) not in sys.path
    assert "bashlex" not in sys.modules


def test_the_licence_ships_with_it() -> None:
    """GPL-3.0-or-later, which is why this repository is GPL-3.0-or-later."""
    assert (BASHLEX / "LICENSE").is_file()
    assert "GNU GENERAL PUBLIC LICENSE" in (BASHLEX / "LICENSE").read_text()
    assert "Version 3" in (BASHLEX / "LICENSE").read_text()
    assert (VENDOR / "README.md").is_file(), "the vendored version and commit must be recorded"


def test_the_upstream_version_is_recorded() -> None:
    readme = (VENDOR / "README.md").read_text()
    assert "idank/bashlex" in readme
    assert "0.18" in readme


def test_parsing_writes_nothing_into_the_vendor_directory(plugin_copy: Path, tmp_path: Path) -> None:
    """PLY writes its parse tables next to itself when it is built to.

    This copy of PLY has no table-writing code at all -- checked here rather than trusted,
    because a bump could reintroduce it and the file it would write lands inside the plugin
    repository, which this gate reviews (**Rule 3**).

    Run with ``-B`` so the only writes under test are the parser's own; where *bytecode*
    goes is a property of the bootstrap, asserted in ``test_bootstrap.py``.
    """
    vendored = plugin_copy / "ocrl" / "_vendor" / "bashlex"
    before = {p.name: p.stat().st_mtime_ns for p in vendored.iterdir()}

    script = (
        f"import sys; sys.path.insert(0, {str(plugin_copy)!r})\n"
        "from ocrl import cmdshape\n"
        "cmdshape.validate_commit('git add -A && git commit -m x')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    after = {p.name: p.stat().st_mtime_ns for p in vendored.iterdir()}
    assert set(after) == set(before), "the parser created or removed a file next to itself"
    assert after == before, "the parser rewrote a file next to itself"


def test_every_vendored_module_imports_itself_by_the_vendored_name() -> None:
    """The one modification to upstream, asserted so a bump cannot silently drop it.

    A missed rewrite is an ``ImportError`` at gate time, which fails closed -- but it fails
    closed on *every* commit, so it is worth catching here instead.
    """
    rewritten = set()
    for source_file in sorted(BASHLEX.glob("*.py")):
        source = source_file.read_text()
        assert "from bashlex import" not in source, f"{source_file.name} still imports the top-level name"
        assert "import bashlex" not in source, f"{source_file.name} still imports the top-level name"
        if "from ocrl._vendor.bashlex import" in source:
            rewritten.add(source_file.name)

    assert rewritten == {"__init__.py", "heredoc.py", "parser.py", "state.py", "subst.py", "tokenizer.py", "yacc.py"}


def test_the_plugin_declares_the_licence_it_inherits() -> None:
    manifest = json.loads((ocrl.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["license"] == "GPL-3.0-or-later"
    assert "GNU GENERAL PUBLIC LICENSE" in (ocrl.PLUGIN_ROOT / "LICENSE").read_text()
