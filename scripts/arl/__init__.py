"""Python implementation of the ``adversarial-review-loop`` gate.

The package is never imported with ``python3 -m``: hooks run with the repository under
review as cwd, and ``-m`` would put that repository at the front of ``sys.path``. The only
supported entrypoint is ``scripts/arl-bootstrap.py``, run by absolute path under
``python3 -I``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PACKAGE_ROOT", "PLUGIN_ROOT", "prompt_path"]

#: Directory holding this package, i.e. ``<plugin>/scripts/arl``.
PACKAGE_ROOT: Path = Path(__file__).resolve().parent

#: Plugin checkout root, the value Claude Code exposes as ``${CLAUDE_PLUGIN_ROOT}``.
PLUGIN_ROOT: Path = PACKAGE_ROOT.parent.parent


def prompt_path(name: str) -> Path:
    """Return the path of a fixed reviewer prompt shipped with the plugin.

    Prompts are read from the plugin checkout rather than composed at runtime, so the
    reviewer's instructions are never model-controlled.
    """
    return PLUGIN_ROOT / "prompts" / f"{name}.md"
