"""Configuration resolution.

Precedence, lowest to highest::

    defaults  <  user config  <  repo .adversarial-review-loop.json  <  ARL_* environment

Repository config is attacker-controlled input: it travels with the tree under review. It
may select a model or widen an ignore list, but nothing here can execute code or turn a
failure into an approval -- every value is read as data by a typed accessor.

``-I`` implies ``-E``, which suppresses ``PYTHON*`` variables only; ``ARL_*`` is unaffected
and still reaches the engine.
"""

#  This file is part of adversarial-review-loop.
#
#  Copyright (c) 2026 Roberto Leinardi
#
#  adversarial-review-loop is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  adversarial-review-loop is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "BOOL_KEYS",
    "CONFIG_KEYS",
    "DEFAULTS",
    "EFFORT_LEVELS",
    "INT_KEYS",
    "LATE_DEFAULT_TIERS",
    "LIST_KEYS",
    "MODEL_TIERS",
    "MODEL_WORDS",
    "REPO_CONFIG_NAME",
    "SEVERITY_KEYS",
    "SEVERITY_LABELS",
    "Config",
    "ModelChoice",
    "for_round",
    "from_env",
    "late_threshold_rank",
    "load",
    "model_choice",
    "reviewer_model",
    "severity_rank",
    "threshold_rank",
    "user_config_path",
]

#: Every supported key, with its default.
#:
#: ``model`` and ``late_model`` take a tier word (``strong``, ``weak``), ``default`` (the built-in
#: choice) or a literal model id, because a provider-qualified id that is meaningful to one
#: reviewer CLI is meaningless to another: a tier names the harness's own model,
#: ``<harness>_strong_model`` or ``<harness>_weak_model``. Read them through
#: :func:`reviewer_model`, the one resolver every command shares. ``default`` (and an empty
#: ``model``) is ``strong``.
#:
#: ``late_model`` defaults to ``None``, not ``""``, because the two mean different things: ``None``
#: (the key set nowhere, or ``null``) is ``default``, the harness's late tier
#: (:data:`LATE_DEFAULT_TIERS`), while an explicit ``""`` is the user's opt-out, ``model`` on every
#: round.
#:
#: ``harness`` defaults to ``subagent``: the mod spawns a fresh Claude Code subagent per round, and
#: the engine only prepares and records. ``opencode`` is the one reviewer CLI the engine runs
#: itself (``review run-external``). The name never falls back: see ``arl.harness.UnknownHarness``.
#:
#: ``review_guide`` names one Markdown file spliced into every review prompt, and ``review_skill``
#: selects each root's own review skill (``auto``, a skill name, or ``off``). Both are read fresh
#: for every review; see ``arl.guidance``.
#: ``roots`` lists extra workspace roots every run in this directory starts with, besides the
#: launch directory, ``--root`` arguments and the paths the plan names (``arl init``).
DEFAULTS: Final[dict[str, Any]] = {
    "harness": "subagent",
    "model": "default",
    # The reviewer's model from the second round of a phase on: unset is `default`, the harness's
    # late tier (`LATE_DEFAULT_TIERS`); an explicit empty string means `model`. See `for_round`.
    "late_model": None,
    # What `strong` and `weak` name on each harness. The subagent takes only Claude Code's model
    # aliases: `$.agent.spawn` refuses a full id (tests/STEP0.md, L2).
    "subagent_strong_model": "opus",
    "subagent_weak_model": "sonnet",
    "opencode_strong_model": "openai/gpt-6.1-sol",
    "opencode_weak_model": "openai/gpt-6.1-terra",
    "variant": "",
    "block_severity": "medium",
    "late_block_severity": "high",
    "timeout_sec": 900,
    "max_failures": 2,
    "max_transient_failures": 5,
    # The command Claude is told to run before asking for a review. The engine never runs it:
    # what it reported reaches the reviewer as `verification.txt`.
    "verify_cmd": "",
    "disable_project_config": False,
    # One diff file of the bundle. Claude Code's Read returns at most 25,000 tokens of a file;
    # 40 KB of diff is about 17,000, so one read takes in a whole chunk.
    "chunk_diff_bytes": 40000,
    "hard_diff_ceiling": 8388608,
    "max_file_bytes": 16777216,
    "max_reason_bytes": 32768,
    "max_findings": 200,
    "max_findings_bytes": 65536,
    "stall_rounds": 5,
    "ignore_globs": [],
    "final_review": False,
    "review_guide": "",
    "review_skill": "auto",
    "roots": [],
    # The subagent reviewer's effort (`low` to `max`); empty for the agent default. opencode's
    # counterpart is `variant`.
    "effort": "",
    # Compact the conversation before each phase after an approval, so a long plan's later
    # phases do not run on a full context.
    "auto_compact": False,
}

BOOL_KEYS: Final = ("disable_project_config", "final_review", "auto_compact")

INT_KEYS: Final = (
    "timeout_sec",
    "max_failures",
    "max_transient_failures",
    "chunk_diff_bytes",
    "hard_diff_ceiling",
    "max_file_bytes",
    "max_reason_bytes",
    "max_findings",
    "max_findings_bytes",
    "stall_rounds",
)

LIST_KEYS: Final = ("ignore_globs", "roots")

#: The keys whose value is a severity label, validated and ranked through ``threshold_rank``.
SEVERITY_KEYS: Final = ("block_severity", "late_block_severity")

#: The levels ``effort`` may name, besides empty (the agent default).
EFFORT_LEVELS: Final = ("low", "medium", "high", "xhigh", "max")

#: The keys the environment may override, in the shell's order.
CONFIG_KEYS: Final = tuple(DEFAULTS)

#: Values the shell accepted as true. Deliberately exact, including the case variants:
#: anything else is false, so a typo never silently enables something.
_TRUE_VALUES: Final = frozenset({"1", "true", "TRUE", "yes", "on"})

REPO_CONFIG_NAME: Final = ".adversarial-review-loop.json"

#: Every label ``severity_rank``/``threshold_rank`` recognise, with its rank. The single
#: source both functions read from, so a label added to one can never drift from the other.
#: ``critical`` is a real, fifth tier -- the reviewer contract's own ``FINDING`` regex
#: (``contract.py``, ``severity=(?P<severity>info|low|medium|high|critical)``) accepts
#: exactly these five words and no others, so this dict has to agree with that regex, not
#: invent its own vocabulary.
_SEVERITY_RANK: Final[dict[str, int]] = {
    "info": 1,
    "trivial": 1,
    "nit": 1,
    "low": 2,
    "minor": 2,
    "medium": 3,
    "moderate": 3,
    "major": 3,
    "high": 4,
    "serious": 4,
    "critical": 5,
}

#: The labels a `block_severity` setting may be.
SEVERITY_LABELS: Final[frozenset[str]] = frozenset(_SEVERITY_RANK)


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CONFIG_HOME")
    base = xdg if xdg else f"{env.get('HOME', '')}/.config"
    return Path(base) / "adversarial-review-loop" / "config.json"


def from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Overrides built only from ``ARL_*`` variables that are **set**, empty included."""
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        name = f"ARL_{key.upper()}"
        if name not in env:
            continue
        raw = env[name]
        if key in BOOL_KEYS:
            out[key] = raw in _TRUE_VALUES
        elif key in INT_KEYS:
            # A non-numeric value is skipped entirely rather than coerced, so a typo
            # leaves the previous layer's value standing instead of becoming zero.
            if raw.isdigit():
                out[key] = int(raw)
        elif key in LIST_KEYS:
            # Comma-separated in the environment, a list everywhere else.
            out[key] = [part for part in raw.split(",") if part]
        else:
            out[key] = raw
    return out


class Config:
    """A merged configuration, read through typed accessors.

    The shell read every value through one ``jq`` filter whose ``// ""`` blanked any false
    boolean -- ``arl_cfg pure`` returned an empty string rather than ``"false"``. Every
    call site happened to compare against ``"true"``, so the bug was inert, but it is not
    reproduced here: the accessors below answer with the value's real type.
    """

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values: dict[str, Any] = dict(values)

    def __contains__(self, key: str) -> bool:
        return key in self.values

    def as_str(self, key: str) -> str:
        """Scalar value as text, with a list joined by commas (the shell's rendering)."""
        value = self.values.get(key)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    def as_int(self, key: str) -> int:
        """Integer value, falling back to the default when the config holds nonsense."""
        value = self.values.get(key)
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            fallback = DEFAULTS.get(key, 0)
            return int(fallback) if isinstance(fallback, int) else 0

    def as_bool(self, key: str) -> bool:
        value = self.values.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value in _TRUE_VALUES
        return bool(DEFAULTS.get(key, False))

    def as_list(self, key: str) -> list[str]:
        value = self.values.get(key)
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str) and value:
            return [part for part in value.split(",") if part]
        return []


#: The tiers ``model`` and ``late_model`` may name, each a ``<harness>_<tier>_model`` key.
MODEL_TIERS: Final = ("strong", "weak")

#: Every word ``model`` and ``late_model`` read as something other than a literal model id.
MODEL_WORDS: Final = ("default", *MODEL_TIERS)

#: The tier ``late_model``'s ``default`` names, per harness. Only the subagent has one: Sonnet matched
#: Opus on the round-2 benchmark at lower cost (``tests/bench/README.md``). OpenCode's models are the
#: provider's own and nothing was measured there, so a harness not listed keeps ``model`` on every
#: round (the strong model, unless ``model`` names another).
LATE_DEFAULT_TIERS: Final[Mapping[str, str]] = {"subagent": "weak"}


@dataclass(frozen=True)
class ModelChoice:
    """The model one review round runs on: the value configured, its tier and the id it names.

    ``tier`` is ``strong`` or ``weak`` for a tier word or ``default``, empty for a literal id.
    ``id`` is what the harness is given; empty only for a tier of a harness this build does not have.
    """

    value: str
    tier: str
    id: str

    def display(self) -> str:
        """``weak (sonnet)`` for a tier, the bare id for a literal."""
        return f"{self.tier} ({self.id})" if self.tier else self.id


def model_choice(config: Config, harness_name: str | None = None) -> ModelChoice:
    """What ``model`` names on ``harness_name`` (the configured harness when omitted).

    ``default`` and an empty or unset ``model`` are ``strong``; a tier word reads the harness's
    ``<harness>_<tier>_model`` key, its built-in value when a layer set it empty; anything else is
    a literal id, passed through.
    """
    name = config.as_str("harness") if harness_name is None else harness_name
    value = config.as_str("model") or "default"
    tier = "strong" if value == "default" else value if value in MODEL_TIERS else ""
    if not tier:
        return ModelChoice(value=value, tier="", id=value)
    key = f"{name}_{tier}_model"
    return ModelChoice(value=value, tier=tier, id=config.as_str(key) or str(DEFAULTS.get(key, "")))


def for_round(config: Config, *, round_number: int, final: bool) -> Config:
    """``config`` as one review round runs it: ``late_model`` in place of ``model`` from round 2.

    A later round of a phase checks a fix against findings already recorded, a narrower job than
    the first round's; the final review is one round over the whole run and always gets ``model``.
    ``late_model`` unset or ``default`` is the harness's late tier (:data:`LATE_DEFAULT_TIERS`), and
    ``model`` on a harness with none; an explicit ``""`` (the opt-out) changes nothing either. Read
    the result through :func:`model_choice`.
    """
    if final or round_number < 2:
        return config
    raw = config.values.get("late_model")
    late = "default" if raw is None else config.as_str("late_model")
    if late == "default":
        late = LATE_DEFAULT_TIERS.get(config.as_str("harness"), "")
    if not late:
        return config
    return Config({**config.values, "model": late})


def reviewer_model(config: Config, *, round_number: int = 1, final: bool = False) -> ModelChoice:
    """The model a review round runs on: :func:`for_round` read through :func:`model_choice`."""
    return model_choice(for_round(config, round_number=round_number, final=final))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load(
    repo: str,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Merge every layer into one configuration.

    Precedence, lowest to highest: defaults < user config < repo config < ``overrides`` <
    environment. ``overrides`` is a per-run overlay (for example a ``--model`` the user gave
    for this run) -- it beats the config
    files but not ``ARL_*``, so the environment still has the final word. Only keys already
    in :data:`DEFAULTS` are accepted from it; anything else is dropped, because the overlay
    is written into state and state is not a trust boundary the config layer should widen.

    A config file that cannot be read or parsed discards the *whole* file layer rather than
    applying half of it: the shell slurped all files through a single ``jq``, so one bad
    file left the defaults standing. That is preserved deliberately -- a partially applied
    security-relevant config is worse than an ignored one. A file that parses but is not a
    JSON object is skipped on its own, which is also what ``jq`` did.
    """
    env_overrides = from_env(environ)

    files: list[Path] = []
    user = user_config_path(environ)
    if user.is_file():
        files.append(user)
    if repo:
        repo_config = Path(repo) / REPO_CONFIG_NAME
        if repo_config.is_file():
            files.append(repo_config)

    merged: dict[str, Any] = dict(DEFAULTS)
    try:
        documents = [_read_json(path) for path in files]
    except (OSError, ValueError, RecursionError):
        merged = dict(DEFAULTS)
    else:
        for document in documents:
            if isinstance(document, dict):
                merged.update(document)

    if overrides:
        merged.update({key: value for key, value in overrides.items() if key in DEFAULTS})

    merged.update(env_overrides)
    return Config(merged)


def severity_rank(label: str) -> int:
    """Rank a reviewer-supplied severity label.

    An unrecognised label ranks as the most severe (5) -- the highest rank a real label ever
    gets is 4, so this guarantees an unparsable label clears any configured threshold. A
    severity the engine cannot parse must never be a way to slip past it (Rule 1).

    Not the right function for ranking ``block_severity`` itself -- see ``threshold_rank``.
    """
    return _SEVERITY_RANK.get(label.lower(), 5)


def threshold_rank(label: str) -> int:
    """Rank a configured ``block_severity`` threshold.

    ``severity_rank``'s "unrecognised ranks highest" rule is fail-closed for a *value being
    compared* -- an unparsable finding severity must clear any threshold. Applied to the
    *threshold itself* it is fail-open: ranking an unknown ``block_severity`` at 5 (above
    every real label) would make almost nothing meet it, so a typo'd or unrecognised
    threshold would silently block far less than the default, not more. Rule 1 requires the
    opposite direction here, so an unrecognised threshold ranks at 1 -- the floor a finding's
    rank clears most easily -- making the engine stricter on bad input, never looser.
    """
    return _SEVERITY_RANK.get(label.lower(), 1)


def late_threshold_rank(config: Config) -> int:
    """Rank the ``late_block_severity`` threshold, clamped up to ``block_severity``'s rank.

    From the second review round of a phase on, a finding that is new *and* outside the paths
    changed since the previous round blocks only when it reaches this rank
    (``contract.parse``). It can only ever **defer** a finding that ``block_severity`` would
    have blocked, never widen the blocking set: a ``late_block_severity`` set below
    ``block_severity`` is meaningless in that direction, so it is read as ``block_severity``
    itself. Both labels rank through :func:`threshold_rank`, so an unrecognised value ranks at
    the floor -- and the clamp then lifts it to ``block_severity``, restoring the ordinary rule
    rather than inventing a laxer one.
    """
    return max(threshold_rank(config.as_str("late_block_severity")), threshold_rank(config.as_str("block_severity")))
