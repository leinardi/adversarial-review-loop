"""``config`` -- read and write the gate's own configuration, outside any activation.

Two things make this module different from every other command here: it is a **user-only**
escape (``skills/config/SKILL.md`` carries no ``hooks:`` block and ``disable-model-invocation:
true``, and ``cmdshape.is_escape`` denies it from a running session -- see
``pretool.ESCAPE_DENIED``), and it is the one place the plugin writes *inside* the repository
under review, via an explicit ``--repo``. Neither of those weakens Rule 3: the file is the
user's own project config, already documented in the README, written only on that explicit
flag, and never by a hook.

``config`` with no arguments never writes anything -- it only reads, so it re-derives the
provenance of every key by walking the same layers ``config.load`` merges, rather than
asking the caller to trust a cached guess.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Final

from ocrl import atomic, commands, paths, reviewer_probe
from ocrl import config as config_module
from ocrl.config import Config

__all__ = ["run"]

_TRUE_TOKENS: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS: Final = frozenset({"0", "false", "no", "off"})

#: Statuses under which an activation no longer governs this worktree, so its `overrides`
#: must not be shown as the layer that will decide the *next* run's configuration.
_TERMINAL_STATUSES: Final = frozenset({"COMPLETE", "DISARMED", "ARM_FAILED", "RESUMED"})

USAGE: Final = """\
usage: ocrl.sh config                                    show every key, its value and layer
       ocrl.sh config <key> <value> [--repo] [--force]    set a key
       ocrl.sh config <key> --unset [--repo]               remove a key from that layer

A value containing its own "--"-looking tokens (a verify_cmd like `pytest --maxfail=1`)
needs `--` before it: `config verify_cmd --repo -- pytest --maxfail=1`.
"""


class _ConfigFailure(Exception):
    """A refusal that is reported and nothing else -- ``config`` never persists a failure."""


def _display(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(item) for item in value) if value else "(empty)"
    return str(value)


def _coerce(key: str, raw: str) -> Any:
    """Parse ``raw`` for ``key``'s type, refusing anything that does not parse cleanly.

    ``Config.as_int`` falls back to a default for nonsense; that is right for a value the
    gate merely *reads*, wrong for a value the user just typed -- silently writing ``0``
    for a typo'd integer would be a config change nobody asked for.
    """
    if key in config_module.BOOL_KEYS:
        lowered = raw.lower()
        if lowered in _TRUE_TOKENS:
            return True
        if lowered in _FALSE_TOKENS:
            return False
        raise _ConfigFailure(f'"{raw}" is not a boolean for {key}; use one of {sorted(_TRUE_TOKENS | _FALSE_TOKENS)}')
    if key in config_module.INT_KEYS:
        if not raw.isdigit():
            raise _ConfigFailure(f'"{raw}" is not a non-negative integer for {key}')
        return int(raw)
    if key in config_module.LIST_KEYS:
        return [part for part in raw.split(",") if part]
    if key in config_module.SEVERITY_KEYS:
        # A word `config.threshold_rank` does not recognise silently ranks at 1 -- the
        # *laxest* threshold, not a refusal -- so a typo here would not error, it would
        # quietly make the gate block on (almost) everything. Rejecting it here is strictly
        # better than either silent direction: the user sees the mistake immediately.
        lowered = raw.lower()
        if lowered not in config_module.SEVERITY_LABELS:
            raise _ConfigFailure(f'"{raw}" is not a recognised severity; use one of {sorted(config_module.SEVERITY_LABELS)}')
        return lowered
    return raw


def _validate_model(model: str, *, force: bool) -> str | None:
    """Returns a warning to print, or ``None``. Raises ``_ConfigFailure`` on a bad name.

    Unlike ``arm._check_reviewer`` this never refuses over an unreachable reviewer: an
    unreachable ``opencode`` at config time is not the same failure as an unreachable one at
    arm time, and arming will refuse on its own if it is still unreachable then. It refuses
    only when the reviewer answered *completely* and the name given is not in what it
    reported -- a probe that timed out, exited non-zero or never started is exactly as
    inconclusive as one that printed nothing, so it warns rather than trusting whatever
    partial output it happened to produce (``reviewer_probe.ProbeFailed``).
    """
    if force or os.environ.get("OCRL_REVIEWER_CMD"):
        return None
    if not paths.have("opencode"):
        return "warning: `opencode` is not on PATH; the model name was not validated."
    try:
        models = reviewer_probe.list_models(reviewer_probe.MODELS_PROBE_TIMEOUT)
    except reviewer_probe.ProbeFailed as exc:
        return f"warning: could not list OpenCode models ({exc}); the model name was not validated."
    if model not in models:
        raise _ConfigFailure(f'"{model}" is not among the models OpenCode reports; pass --force to set it anyway.')
    return None


def _target(repo: str, *, repo_flag: bool) -> Path:
    return (Path(repo) / config_module.REPO_CONFIG_NAME) if repo_flag else config_module.user_config_path()


def _lock_path(target: Path) -> Path:
    """A private lock for a read-modify-write of ``target``, keyed by its canonical path.

    ``target`` may itself be inside the repository under review (``--repo``); the lock must
    not be. Rule 3 permits exactly one file to be written inside that repository, and it is
    ``target`` -- via ``write_atomic``, which is the one writer that never touches state root
    -- not a second, untracked file left beside it. Resolving first, rather than hashing the
    string as typed, means two spellings of the same file (a relative path and its absolute
    form, or one reached through a symlink) serialise against the same lock instead of two
    that never see each other.
    """
    key = paths.sha256_hex(str(target.resolve()))
    return paths.state_root() / "config-locks" / f"{key}.lock"


def _read_existing(path: Path) -> dict[str, Any]:
    """The JSON object at ``path``, or ``{}`` if it does not exist yet.

    A file that exists but cannot be parsed is refused rather than overwritten: it may hold
    settings this command does not know how to reproduce, and clobbering it with a
    single-key document would silently discard the rest.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _ConfigFailure(f"{path} exists but could not be parsed as JSON ({exc}); refusing to overwrite it") from exc
    if not isinstance(data, dict):
        raise _ConfigFailure(f"{path} does not contain a JSON object; refusing to overwrite it")
    return data


def _write(target: Path, document: dict[str, Any]) -> None:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    atomic.write_atomic(target, text)


def _set(key: str, raw: str, *, repo_flag: bool, force: bool) -> int:
    value = _coerce(key, raw)
    warning = _validate_model(value, force=force) if key == "model" else None

    repo = commands.current_repo()
    target = _target(repo, repo_flag=repo_flag)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Locked across read, mutate and write: two concurrent `config` invocations targeting
    # the same file would otherwise race read-modify-write, and the loser's `os.replace`
    # would silently discard whatever key the winner had just set. The lock itself lives
    # under the state root (`_lock_path`), never beside `target` -- `target` can be inside
    # the repository under review, and Rule 3 permits exactly one file to be written there.
    with atomic.locked(_lock_path(target), root=paths.state_root()):
        document = _read_existing(target)
        old = document.get(key, config_module.DEFAULTS[key])
        document[key] = value
        _write(target, document)

    lines = [f"{key}: {_display(old)} -> {_display(value)}", f"written to {target}"]
    if warning:
        lines.append(warning)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _unset(key: str, *, repo_flag: bool) -> int:
    repo = commands.current_repo()
    target = _target(repo, repo_flag=repo_flag)
    target.parent.mkdir(parents=True, exist_ok=True)
    with atomic.locked(_lock_path(target), root=paths.state_root()):
        document = _read_existing(target)
        if key not in document:
            sys.stdout.write(f"{key} is not set in {target}; nothing to do.\n")
            return 0
        old = document.pop(key)
        _write(target, document)
    sys.stdout.write(f"{key}: {_display(old)} -> (unset)\nwritten to {target}\n")
    return 0


def _file_layers(repo: str) -> list[tuple[str, dict[str, Any]]]:
    """``(layer name, values)`` for each config file, replicating ``config.load``'s reading.

    A file that fails to parse discards *every* file layer, exactly as ``config.load`` does
    -- both files are read into one list before either is merged there, so one bad file
    blanks the whole file layer rather than just itself.
    """
    user_path = config_module.user_config_path()
    candidates: list[tuple[str, Path]] = []
    if user_path.is_file():
        candidates.append(("user", user_path))
    if repo:
        repo_path = Path(repo) / config_module.REPO_CONFIG_NAME
        if repo_path.is_file():
            candidates.append(("repo", repo_path))

    try:
        documents = [(name, json.loads(path.read_text(encoding="utf-8"))) for name, path in candidates]
    except (OSError, ValueError, RecursionError):
        return []
    return [(name, doc) for name, doc in documents if isinstance(doc, dict)]


def _typed_value(final: Config, key: str) -> Any:
    """The value the gate will actually read for ``key``, via the same typed accessors every
    other command reads it through -- not the raw JSON, which can silently disagree with it.
    """
    if key in config_module.BOOL_KEYS:
        return final.as_bool(key)
    if key in config_module.INT_KEYS:
        return final.as_int(key)
    if key in config_module.LIST_KEYS:
        return final.as_list(key)
    return final.as_str(key)


def _invalid_note(final: Config, key: str) -> str:
    """A note when the stored value could not be parsed and a default silently stood in.

    ``Config.as_int`` falls back to ``DEFAULTS[key]`` for anything that will not ``int()``,
    so an unparseable stored ``"ttl_hours": "soon"`` is *not* what a real run uses. Showing
    the value from ``_typed_value`` without this note would show a number nothing actually
    reads without saying so. The severity keys get the same treatment for the same reason,
    but the silent fallback there is ``threshold_rank``'s rank ``1`` rather than a value in
    ``DEFAULTS`` -- ``_coerce`` refuses an unrecognised word when it comes through
    ``config set``, but a file written some other way (hand-edited, or set before that
    validation existed) is not bound by it.
    """
    if key in config_module.INT_KEYS:
        raw = final.values.get(key)
        try:
            int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "stored value is not an integer; showing the default"
    elif key in config_module.SEVERITY_KEYS and final.as_str(key).lower() not in config_module.SEVERITY_LABELS:
        return "not a recognised severity; the gate treats it as the laxest threshold"
    return ""


def _show() -> int:
    repo = commands.current_repo()
    activation = commands.resolve_local_activation()
    overrides: dict[str, Any] = {}
    # Only a *live* activation's override still governs anything: one that has finished,
    # been stopped, failed to arm, or been retired by a resume no longer gates this
    # worktree, so its `--model`/`--variant` would mislabel the next `implement` or `resume`
    # as inheriting a choice that in fact came from the user or repo config.
    if activation is not None and activation.state.get("status") not in _TERMINAL_STATUSES:
        raw = activation.state.data.get("overrides")
        if isinstance(raw, dict):
            overrides = {key: value for key, value in raw.items() if key in config_module.DEFAULTS}
    env_overrides = config_module.from_env()

    # The layer that most recently touched each key, walked in the same precedence
    # `config.load` merges: defaults < user < repo < activation overrides < env.
    source = dict.fromkeys(config_module.DEFAULTS, "default")
    for name, values in _file_layers(repo):
        for key in values:
            if key in config_module.DEFAULTS:
                source[key] = name
    for key in overrides:
        source[key] = "activation"
    for key in env_overrides:
        source[key] = "env"

    # The merged document comes from `config.load` itself -- the single authority for what a
    # real run would actually see -- so a bug in the layering replication above could
    # mislabel a source but can never mislabel a value.
    final = config_module.load(repo, overrides=overrides)

    width = max(len(key) for key in config_module.DEFAULTS)
    lines = ["opencode-review-loop configuration", "-----------------------------------"]
    for key in sorted(config_module.DEFAULTS):
        value = _typed_value(final, key)
        note = _invalid_note(final, key)
        suffix = f" [{note}]" if note else ""
        lines.append(f"{key.ljust(width)}  {_display(value):<28} ({source[key]}){suffix}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _parse(argv: list[str]) -> tuple[str, list[str], bool, bool, bool]:
    """``(key, positionals, repo_flag, force, unset)``.

    A bare ``--`` ends option scanning, exactly as ``getopt`` treats it: every flag
    (``--repo``, ``--force``, ``--unset``) has to come before it, and everything after is
    taken verbatim into the value, including tokens that themselves look like a flag -- a
    ``verify_cmd`` such as ``pytest --maxfail=1`` or ``npm test -- --runInBand`` would
    otherwise be unwritable, since every ``--xxx`` token would be read as one of ours. Only
    the *first* ``--`` is consumed as the marker; any later one is data, so a value that
    itself needs a literal ``--`` (``npm test -- --runInBand``) still round-trips.
    """
    key = argv[0]
    if key.startswith("--"):
        raise _ConfigFailure(f'expected a config key, got "{key}"\n\n{USAGE}')
    if key not in config_module.DEFAULTS:
        raise _ConfigFailure(f'unknown key "{key}"; known keys are {", ".join(sorted(config_module.DEFAULTS))}')

    repo_flag = False
    force = False
    unset = False
    positionals: list[str] = []
    end_of_options = False
    for token in argv[1:]:
        if end_of_options:
            positionals.append(token)
        elif token == "--":
            end_of_options = True
        elif token == "--repo":
            repo_flag = True
        elif token == "--force":
            force = True
        elif token == "--unset":
            unset = True
        elif token.startswith("--"):
            raise _ConfigFailure(f'unrecognised flag "{token}"; put `--` before the value if it needs to contain one\n\n{USAGE}')
        else:
            positionals.append(token)
    return key, positionals, repo_flag, force, unset


def _run(argv: list[str]) -> int:
    if not argv:
        return _show()

    key, positionals, repo_flag, force, unset = _parse(argv)

    if unset:
        if positionals:
            raise _ConfigFailure(f'"--unset" takes no value\n\n{USAGE}')
        return _unset(key, repo_flag=repo_flag)

    if not positionals:
        raise _ConfigFailure(f'"config {key}" needs a value, or --unset to remove it\n\n{USAGE}')
    # Everything that is not a recognised flag is the value, rejoined with a single space --
    # the shim receives `$ARGUMENTS` unquoted (AGENTS.md, "The argument channel is not
    # escaped"), so a multi-word value like a `verify_cmd` arrives as several tokens rather
    # than one quoted string.
    return _set(key, " ".join(positionals), repo_flag=repo_flag, force=force)


def run(argv: list[str]) -> int:
    try:
        return _run(argv)
    except _ConfigFailure as exc:
        sys.stdout.write(f"opencode-review-loop: config failed -- {exc}\n")
        return 1
