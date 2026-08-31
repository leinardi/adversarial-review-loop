"""``dry-run`` -- build a real bundle and print the exact reviewer invocation, unrun.

Ports ``cmd_dry_run``. A development command: it is the only way to see the argv, the
environment overrides and the prompt the reviewer would receive without spending a model
call, and it is what a change to any of those three is checked against.

It builds a genuine bundle for the same reason -- an argv naming attachments that were never
produced would look right and be wrong.

**It prints the command the harness actually composed, never a reconstruction of it.** The
whole invocation goes through :meth:`arl.harness.Harness.review_command`, exactly as
``reviewer.invoke`` does, and what is rendered below is the :class:`arl.harness.Command`
that came back -- argv, environment overrides, working directory and stdin. A dry run that
re-derived any of those from the harness's parts could print an invocation that no review
would ever make, which would make this command worse than useless on the day it mattered:
the one thing it exists to show is the thing it would have got wrong. It is also why
inspecting a *new* harness costs nothing here -- there is no per-harness rendering to add.
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
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import arl
from arl import commands, gitsnap, guide, harness, paths, planrev, reviewer
from arl import config as config_module
from arl.atomic import write_private_atomic
from arl.gitsnap import SnapshotError
from arl.state import State

__all__ = ["run"]

#: Session id for the throwaway activation a dry run creates when nothing is armed. Not a
#: real session: no pointer is written for it, so no hook can ever resolve it.
DRY_RUN_SESSION: str = "dry-run"

#: The title a dry run's invocation carries. A real review's is ``reviewer._unique_title``,
#: which is built to be matched against a session listing afterwards; nothing is ever matched
#: against this one, because nothing is ever run.
DRY_RUN_TITLE: Final = "review-loop dry run"

#: What the ``cwd`` line says for a harness that names the repository with a flag instead, and
#: so leaves :attr:`arl.harness.Command.cwd` unset -- the child then inherits the gate's own.
_INHERITED_CWD: Final = "(the gate's own)"


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
        digest = reviewer.build_bundle(target, dest, state=state, config=config, warnings=snap.warnings)
    except reviewer.BundleError as exc:
        sys.stderr.write(f"bundle build failed: {exc}\n")
        return 1

    # The real path attaches *staged copies*, so the dry run stages too -- otherwise it would
    # print an argv that differs from the one a review actually builds, which is the single
    # thing this command exists to show. The staging directory is left in place deliberately:
    # a developer inspecting the dry run wants the files the printed argv names to exist.
    staging_dir = reviewer.staging_dir_for(state.act_dir, "dry-run")
    try:
        attachments, _context = reviewer.stage_invocation(dest, state.act_dir, digest, staging_dir, include_context=True)
    except (reviewer.BundleError, OSError) as exc:
        sys.stderr.write(f"bundle build failed: {exc}\n")
        return 1

    try:
        implementation, command = _compose(repo, state, dest, attachments, config=config)
    except (harness.UnknownHarness, harness.PayloadError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    sys.stdout.write(_render(implementation, command, bundle_dir=dest))

    # `ls -la`, as the shell printed it: this is a developer's listing of what was actually
    # written, and reimplementing its columns would be a second thing to keep in step.
    sys.stdout.flush()
    subprocess.run(["ls", "-la", str(dest)], check=False)
    return 0


def _compose(
    repo: str,
    state: State,
    bundle_dir: Path,
    attachments: Sequence[tuple[Path, str]],
    *,
    config: config_module.Config,
) -> tuple[harness.Harness, harness.Command]:
    """The harness this configuration selects, and the review command it composes.

    Both refusals travel to the caller as they are: an unimplemented ``harness`` is exactly the
    hard refusal every other command makes of it (``arl.harness.UnknownHarness``), and falling
    back to the default's invocation would print a command nobody configured -- the one output
    a dry run must never produce. ``PayloadError`` reaches the caller for the same reason it
    stops a real review: an attachment that cannot be vouched for is not something to render
    around.
    """
    implementation = harness.selected(config)
    spec = harness.ReviewSpec(
        repo=repo,
        prompt_text=_prompt_text(state).rstrip("\n"),
        # The real path passes this, so the dry run must too -- its whole contract is that
        # what it prints is the invocation a review would actually make.
        system_prompt=reviewer.efficiency_text(),
        title=DRY_RUN_TITLE,
        bundle_dir=bundle_dir,
        act_dir=state.act_dir,
        config=config,
        attachments=tuple(harness.Attachment(path, digest) for path, digest in attachments),
        # A fresh run's id, minted the same way a real one is ("" for a harness that cannot
        # pre-assign). Printing a *resumed* invocation instead would need a session that this
        # command, which spends no model call, has no way to have created.
        new_session_id=implementation.sessions().mint(),
    )
    return implementation, implementation.review_command(spec)


def _render(implementation: harness.Harness, command: harness.Command, *, bundle_dir: Path) -> str:
    """One composed :class:`arl.harness.Command`, as a developer reads it.

    Every section is generic over the harness, on purpose: the two implementations deliver the
    same review through different channels -- OpenCode puts the prompt in an argv element and
    the permission document in the environment, Claude Code puts prompt and attachments alike
    on stdin -- and a renderer with a branch per harness would be one more thing to keep in
    step with the harnesses themselves.

    **Nothing is summarised or truncated.** The point of the command is to answer questions of
    the form "would this run really send that?", and an elision is exactly where the answer
    would hide. A multi-line argv element is the one thing moved, not shortened: printed in
    place it would break the one-element-per-line rule that makes the argv readable at all, so
    it is printed in full below the argv under the index it came from.
    """
    argv, deferred = _argv_lines(command.argv)
    out = [
        f"# harness: {implementation.name}\n",
        f"# cwd: {command.cwd or _INHERITED_CWD}\n",
        "\n# env overrides (one KEY=value per line, layered onto the gate's environment)\n",
        *(f"{key}={value}\n" for key, value in sorted(command.env.items())),
    ]
    if not command.env:
        out.append("(none)\n")
    out.append("\n# argv (one element per line)\n")
    out += argv
    for index, element in deferred:
        out.append(f"\n# argv element {index}, in full\n")
        out.append(element if element.endswith("\n") else f"{element}\n")
    out.append(_stdin_section(command.stdin))
    out.append(f"\n# bundle: {bundle_dir}\n")
    return "".join(out)


def _argv_lines(argv: list[str]) -> tuple[list[str], list[tuple[int, str]]]:
    """The argv one element per line, with multi-line elements deferred to their own sections.

    The deferred list is ``(index, element)`` and the placeholder names the same index, so a
    reader can put the argv back together exactly -- which is the difference between moving an
    element and losing track of one.
    """
    lines: list[str] = []
    deferred: list[tuple[int, str]] = []
    for index, element in enumerate(argv):
        if "\n" in element:
            deferred.append((index, element))
            lines.append(f"<element {index}: {len(element.encode('utf-8', 'surrogateescape'))} bytes, printed in full below>\n")
        else:
            lines.append(f"{element}\n")
    return lines, deferred


def _stdin_section(stdin: bytes | None) -> str:
    """What the child reads on standard input, in full, or the note that it reads nothing.

    Decoded with ``backslashreplace`` rather than ``surrogateescape``: the payload carries
    attachment bytes verbatim, a diff of the repository under review can hold sequences that
    are not valid UTF-8, and a surrogate reaching ``sys.stdout`` would abort the whole listing
    with an encoding error. Escaping the byte shows what was actually there and prints.
    """
    if stdin is None:
        return "\n# stdin: nothing -- this harness reads no standard input\n"
    text = stdin.decode("utf-8", "backslashreplace")
    return f"\n# stdin ({len(stdin)} bytes, verbatim)\n{text}" + ("" if text.endswith("\n") else "\n")


def _prompt_text(state: State) -> str:
    """The phase prompt as a real review would compose it, guide and all.

    Composed rather than read straight off disk, for the reason the whole command exists: what
    it prints has to be the invocation a review would actually make. Against a live activation
    with a guide armed, that includes the guide -- and against one without, it includes the
    placeholder being stripped, so the dry run also shows there is no residue.

    Degrades to a note rather than raising, like the read failure below it: this command spends
    no model call and decides nothing, so an unreadable prompt or an unverifiable guide is
    something to *print*, not something to escalate.
    """
    path: Path = arl.prompt_path("reviewer-phase")
    try:
        text = path.read_bytes().decode("utf-8", "surrogateescape")
    except OSError as exc:
        return f"(the prompt could not be read: {exc})\n"
    try:
        active = reviewer.active_guide(state)
    except reviewer.PlanEvidenceCorrupted as exc:
        return f"(the frozen review guide could not be verified, so this prompt is not what a review would run: {exc})\n{text}"
    return guide.compose(text, guide=active.content, path=active.path, sha256=active.sha256)
