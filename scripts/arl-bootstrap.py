#!/usr/bin/env python3
"""Trusted entrypoint for the adversarial-review-loop gate.

Must be invoked by absolute path under ``python3 -I``::

    python3 -I "$PLUGIN_ROOT/scripts/arl-bootstrap.py" "$@"

Never ``python3 -m arl``. Hooks run with the repository under review as cwd, and ``-m``
puts cwd at ``sys.path[0]`` ahead of ``PYTHONPATH``, so a repository containing
``arl/__main__.py`` -- or merely ``json.py`` -- would execute arbitrary code *as the
gate*. That is confirmed by experiment, not theory.

``-I`` implies ``-P`` (neither cwd nor the script's own directory joins ``sys.path``),
``-E`` (no ``PYTHON*`` environment influence, ``PYTHONPATH`` included) and ``-s`` (no user
site-packages). Because ``-P`` also drops the script directory, this file must establish
``sys.path`` itself, from its own absolute location -- which is trusted precisely because
the shim passes it as an absolute path.

``-I`` likewise makes the interpreter ignore ``PYTHONPYCACHEPREFIX`` (measured:
``sys.pycache_prefix`` is ``None``), so the cache directory is set here at runtime, before
``arl`` is imported. Rule 3 -- nothing is written inside the repository under review --
applies to ``__pycache__`` as much as to anything else, and the plugin gates work on
itself. ``-B`` would also keep the tree clean but throws the bytecode cache away on every
hook, which is the hot path.
"""

from __future__ import annotations

import os
import sys

# This file lives in <plugin>/scripts/, alongside the arl package.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _bytecode_cache_dir() -> str:
    """Return where ``.pyc`` files should be written, before it has been vetted."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = xdg if xdg else os.path.join(os.environ.get("HOME", ""), ".cache")
    return os.path.join(base, "adversarial-review-loop", "pycache")


def _overlaps(a: str, b: str) -> bool:
    """True if either path contains the other, or they are the same path.

    Containment is checked in *both* directions on purpose. ``sys.pycache_prefix`` mirrors
    each source file's absolute path underneath the prefix, so a prefix that is merely an
    *ancestor* of a source tree still writes into it: with a prefix of ``/``, the bytecode
    for ``/plugin/scripts/arl/x.py`` lands at ``/plugin/scripts/arl/x.pyc``.
    """
    a = os.path.realpath(a)
    b = os.path.realpath(b)
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        # Not comparable (one relative, or different roots); the isabs check upstream
        # already rejects the relative case.
        return False
    # Deliberately not a `startswith(other + os.sep)` test: that answers False for a root
    # of "/", because "/" + os.sep is "//" and no path starts with that.
    return common in (a, b)


def _enclosing_repo_root(start: str) -> str:
    """Walk up from ``start`` to the repository that contains it. Empty if there is none.

    Deliberately a bounded ``.git`` probe rather than a ``git`` subprocess: this runs on the
    hot path, before anything else is loaded. It answers only about the tree the gate was
    invoked in -- under a hook, the repository under review.
    """
    current = os.path.realpath(start)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent


def _cache_dir_is_safe(cache_dir: str) -> bool:
    """Is it safe to write bytecode under ``cache_dir``? (Rule 3.)

    ``XDG_CACHE_HOME`` is ordinary environment, not a trusted input: a value of
    ``$PWD/.cache`` is perfectly absolute and lands squarely inside the repository under
    review. Absoluteness is necessary but nowhere near sufficient.
    """
    if not os.path.isabs(cache_dir):
        # A relative prefix resolves against cwd, which under a hook is the repo.
        return False
    try:
        cwd = os.getcwd()
    except OSError:
        return False
    plugin_root = os.path.dirname(_SCRIPTS_DIR)
    forbidden = [cwd, plugin_root]
    repo_root = _enclosing_repo_root(cwd)
    if repo_root:
        forbidden.append(repo_root)
    return not any(_overlaps(cache_dir, path) for path in forbidden)


def _configure_bytecode_cache() -> None:
    """Point the bytecode cache outside every repository, or disable it outright.

    The fallback is deliberately ``dont_write_bytecode`` rather than "write beside the
    source". Losing the cache costs a few milliseconds per hook; writing into the tree
    under review breaks Rule 3, and the plugin gates work on itself.
    """
    cache_dir = _bytecode_cache_dir()
    if _cache_dir_is_safe(cache_dir):
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass
        else:
            sys.pycache_prefix = cache_dir
            return
    sys.dont_write_bytecode = True


def main(argv: list[str]) -> int:
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    _configure_bytecode_cache()

    # Deliberately not at module scope: importing arl before sys.path is established
    # would resolve it against whatever the interpreter starts with. This import is the
    # first moment the package may be trusted, and PLC0415 must not "fix" it upward.
    from arl.cli import main as cli_main  # noqa: PLC0415

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
