"""``model``: the reviewer's model for this run alone (``/arl-model``), never written to a config file.

A run may hold its own ``model`` and ``late_model`` (:attr:`arl.state.RunState.model_overlay`):
they beat the config files but not ``ARL_*`` (``config.load``'s ``overrides``). ``init`` writes
one from ``/arl-implement --model``, ``model set`` changes it and ``model reset`` removes it;
``resume`` and a replan keep it, and a new run starts without one. A change takes effect at the
next ``review prepare``: a review already prepared keeps the model its ticket recorded.

``set ... default`` stores the word ``default``, so the run uses the built-in choice whatever a
config file says; ``reset`` removes the entry, so the run inherits the config layers again.

A literal model is checked before it is stored, and refused when it cannot be: on the subagent it
must be one of Claude Code's aliases (``$.agent.spawn`` refuses anything else, tests/STEP0.md L2);
on OpenCode its base id (before any ``#variant``) must be one ``opencode models`` lists. A probe
that does not answer refuses the setting.
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

import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

from arl import config, configcmd, harness, reviewer_probe
from arl.state import RunError, RunPaths, RunState, locked_state, run_overlay

__all__ = ["SUBAGENT_ALIASES", "USAGE", "WHICH", "check_value", "cmd_model", "overlay_for"]

#: The models a subagent reviewer may be spawned on: ``$.agent.spawn`` takes these and nothing else.
SUBAGENT_ALIASES: Final = ("opus", "sonnet", "haiku", "fable")

#: Which rounds a setting covers, and the keys each writes. ``first`` is round 1 of every phase and
#: the final review (``model``); ``late`` is every later round (``late_model``).
WHICH: Final[Mapping[str, tuple[str, ...]]] = {"all": ("model", "late_model"), "first": ("model",), "late": ("late_model",)}

USAGE: Final = (
    "usage: /arl-model                                 the reviewer's model for each kind of round, and who set it\n"
    "       /arl-model [all|first|late] <model>         this run's model: default, strong, weak or a model id\n"
    "       /arl-model reset [all|first|late]           drop this run's choice; the config files decide again\n"
    "first is round 1 of each phase and the final review; late is every later round; all (the default) is both."
)

#: The environment variables that beat a run's choice, by overlay key.
_ENV: Final[Mapping[str, str]] = {"model": "ARL_MODEL", "late_model": "ARL_LATE_MODEL"}


def check_value(value: str, cfg: config.Config) -> None:
    """Refuse a model value this run's reviewer cannot be given (``bad-model``)."""
    if not value:
        raise RunError("bad-model", "name a model: default, strong, weak or a model id")
    if value in config.MODEL_WORDS:
        return
    name = cfg.as_str("harness")
    if name == harness.SUBAGENT:
        if value not in SUBAGENT_ALIASES:
            raise RunError(
                "bad-model",
                f"the subagent reviewer runs on one of Claude Code's aliases ({', '.join(SUBAGENT_ALIASES)}), not {value!r}; "
                "or use default, strong or weak",
            )
        return
    try:
        implementation = harness.get(name)
    except harness.UnknownHarness as exc:
        raise RunError("unknown-harness", str(exc)) from exc
    base, hashed, variant = value.partition("#")
    if hashed and not (base and variant):
        raise RunError("bad-model", f"{value!r} is not provider/model or provider/model#variant")
    if hashed and cfg.as_str("variant"):
        raise RunError("bad-model", f"model {value!r} already names a variant; unset variant ({cfg.as_str('variant')!r}) or drop it from the model")
    try:
        listed = implementation.probe_models(reviewer_probe.MODELS_PROBE_TIMEOUT)
    except reviewer_probe.ProbeFailed as exc:
        raise RunError("bad-model", f"{value!r} cannot be checked, so it is not set: {exc}") from exc
    if listed is None:
        raise RunError("bad-model", f"{name} cannot list its models, so {value!r} cannot be checked and is not set")
    if base not in listed:
        raise RunError("bad-model", f"{name} does not list {base!r}: `{implementation.binary} models` names the models it has")


def overlay_for(which: str, value: str, current: Mapping[str, str]) -> dict[str, str]:
    """``current`` with ``which``'s keys set to ``value``."""
    return {**current, **dict.fromkeys(WHICH[which], value)}


def _which(word: str) -> str:
    if word not in WHICH:
        raise RunError("bad-model", f"{word!r} is not all, first or late\n{USAGE}")
    return word


def _show(run: RunState, cfg: config.Config, overlay: Mapping[str, str]) -> str:
    source = configcmd.layers(run.paths.launch_dir, overlay)
    first = config.reviewer_model(cfg, round_number=1).display()
    late = "the same as the first round" if cfg.values.get("late_model") == "" else config.reviewer_model(cfg, round_number=2).display()
    lines = [
        f"Reviewer model for this run ({cfg.as_str('harness')}):",
        f"  first round of a phase, and the final review: {first}  ({source['model']})",
        f"  later rounds: {late}  ({source['late_model']})",
    ]
    return "\n".join(lines)


def _warnings(keys: Sequence[str]) -> list[str]:
    return [f"{_ENV[key]} is set, so it beats this run's {key}" for key in keys if _ENV[key] in os.environ]


def _parse(words: Sequence[str]) -> tuple[str, str, str]:
    """``(action, which, value)``: ``show``, ``reset`` or ``set``."""
    if not words:
        return "show", "", ""
    action, rest = words[0], list(words[1:])
    if action == "reset" and len(rest) <= 1:
        return "reset", _which(rest[0] if rest else "all"), ""
    if action == "set" and len(rest) == 2:
        return "set", _which(rest[0]), rest[1]
    if action in ("reset", "set"):
        raise RunError("bad-model", USAGE)
    raise RunError("bad-model", f"unknown action {action!r}\n{USAGE}")


def cmd_model(paths: RunPaths, args: Mapping[str, Any]) -> dict[str, Any]:
    """``model [set <which> <value> | reset [which]]``; with no words, the current choice."""
    action, which, value = _parse([str(word) for word in args.get("words") or []])
    launch = str(paths.launch_dir)
    if action == "set":
        # Before the lock: an OpenCode check runs `opencode models`, which may take a while.
        check_value(value, config.load(launch, overrides=run_overlay(paths)))
    with locked_state(paths) as loaded:
        if loaded is None:
            raise RunError("no-run", "no run here: /arl-implement <plan> starts one")
        run = loaded
        overlay = run.model_overlay
        if action == "show":
            return {"ok": True, "text": _show(run, config.load(launch, overrides=overlay), overlay), "overlay": overlay}
        run.require_status("active", "paused")
        keys = WHICH[which]
        updated = {key: stored for key, stored in overlay.items() if key not in keys} if action == "reset" else overlay_for(which, value, overlay)
        run.set_model_overlay(updated)
        run.save()
        cfg = config.load(launch, overrides=updated)
        return {"ok": True, "text": _show(run, cfg, updated), "overlay": updated, "warnings": _warnings(keys)}
