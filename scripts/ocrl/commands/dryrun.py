"""``dry-run`` -- build a real bundle and print the exact reviewer invocation, unrun.

Ports ``cmd_dry_run``. A development command: it is the only way to see the argv, the
permission document and the prompt the reviewer would receive without spending a model call,
and it is what a change to any of those three is checked against.

It builds a genuine bundle for the same reason -- an argv naming attachments that were never
produced would look right and be wrong.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import ocrl
from ocrl import commands, gitsnap, paths, planrev, reviewer
from ocrl import config as config_module
from ocrl.atomic import write_private_atomic
from ocrl.gitsnap import SnapshotError
from ocrl.state import State

__all__ = ["run"]

#: Session id for the throwaway activation a dry run creates when nothing is armed. Not a
#: real session: no pointer is written for it, so no hook can ever resolve it.
DRY_RUN_SESSION: str = "dry-run"


def _activation() -> tuple[str, State, config_module.Config] | None:
    """The live activation, or a scratch one good enough to build a bundle from.

    ``None`` means there is no repository here, which is the one thing a dry run cannot work
    around: the bundle is a diff between two trees.
    """
    root = paths.repo_root(os.getcwd())
    if not root:
        return None

    activation = commands.resolve_local_activation()
    if activation is not None:
        return activation.repo, activation.state, activation.config

    config = config_module.load(root)
    state = State(root, DRY_RUN_SESSION)
    with state.transaction(create=True):
        state.new()
        state.update(
            worktree=root,
            activation_commit=gitsnap.head_commit(root),
            baseline_tree=gitsnap.head_tree(root),
        )
    # Nothing armed this scratch activation, so there is no real frozen plan to disclose --
    # but `reviewer.build_bundle` now hard-fails on a missing one (Phase 4: a plan revision's
    # evidence is never a placeholder). A dry run is not a real review, so a placeholder file
    # is written here instead, the one place that is honest about what it is.
    write_private_atomic(
        state.act_dir / planrev.PLAN_FROZEN_NAME,
        "(dry run: no activation is armed in this worktree, so there is no real frozen plan)\n",
        root=paths.state_root(),
    )
    return root, state, config


def run(argv: list[str]) -> int:
    del argv
    resolved = _activation()
    if resolved is None:
        sys.stderr.write("not a git repository\n")
        return 1
    repo, state, config = resolved

    base = state.get("last_approved_tree") or gitsnap.head_tree(repo)
    try:
        snap = gitsnap.snapshot(repo)
    except SnapshotError as exc:
        sys.stderr.write(f"bundle build failed: {exc}\n")
        return 1

    dest = state.act_dir / "bundles" / "dry-run"
    target = reviewer.Target(repo=repo, base=base, head=snap.tree, scope="phase", phase=state.get_int("phase"))
    try:
        reviewer.build_bundle(target, dest, state=state, config=config, warnings=snap.warnings)
    except reviewer.BundleError as exc:
        sys.stderr.write(f"bundle build failed: {exc}\n")
        return 1

    out = [f"# OPENCODE_PERMISSION\n{reviewer.permission(dest)}\n\n# argv (one element per line)\n"]
    out.append("opencode\nrun\n<the prompt printed below, as a single argument>\n")
    out += [f"{element}\n" for element in reviewer.review_argv(repo, dest, "review-loop dry run", config=config)]
    out.append("\n# the prompt argument\n")
    out.append(_prompt_text())
    out.append(f"\n# bundle: {dest}\n")
    sys.stdout.write("".join(out))

    # `ls -la`, as the shell printed it: this is a developer's listing of what was actually
    # written, and reimplementing its columns would be a second thing to keep in step.
    sys.stdout.flush()
    subprocess.run(["ls", "-la", str(dest)], check=False)
    return 0


def _prompt_text() -> str:
    path: Path = ocrl.prompt_path("reviewer-phase")
    try:
        return path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError as exc:
        return f"(the prompt could not be read: {exc})\n"
