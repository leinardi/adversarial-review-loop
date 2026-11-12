"""Fixtures and helpers shared by the ``test_reviewer_*`` modules.

Split out of the former single ``test_reviewer.py`` so each subsystem gets its own file
without copying the bundle builders, the scripted-reviewer drivers and the isolated
environment into eight of them.
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
import time
from pathlib import Path

from conftest import FAKE_REVIEWER, config_with, git

from arl import bundle, gitsnap, report
from arl import v3state as state
from arl.config import Config
from arl.contract import END_MARKER, FINDINGS_MARKER, Review, Target, records
from arl.runner import Invocation, run_invocation
from arl.util import now

#: What the ANSI-stripping test expects once the escapes are gone.
PLAIN_VERDICT = b"VERDICT APPROVED\n"


#: Every stand-in reviewer mode, with the verdict the gate must reach. Nothing here is
#: ``APPROVED`` unless the reviewer both said so and left no actionable finding behind.
MODE_VERDICTS = [
    ("approve", "APPROVED"),
    ("approve-with-nit", "APPROVED"),
    ("changes", "CHANGES_REQUIRED"),
    ("approve-with-critical", "CHANGES_REQUIRED"),
    ("critical-nonactionable", "APPROVED"),
    ("malformed", "OP_FAILURE"),
    ("no-verdict", "OP_FAILURE"),
    ("empty", "OP_FAILURE"),
    ("big-prose", "CHANGES_REQUIRED"),
    ("many", "CHANGES_REQUIRED"),
    # Blocks the contract does not allow. Each carries the reviewer's own APPROVED, and
    # each of them used to get it.
    ("bad-actionable", "OP_FAILURE"),
    ("bad-severity", "OP_FAILURE"),
    ("mangled-finding", "OP_FAILURE"),
    ("stray-end", "OP_FAILURE"),
    ("two-blocks", "OP_FAILURE"),
    ("two-verdicts", "OP_FAILURE"),
    ("chatty-block", "OP_FAILURE"),
    ("inline-start-marker", "OP_FAILURE"),
    ("suffixed-end-marker", "OP_FAILURE"),
    ("nul-byte", "OP_FAILURE"),
]


#: How long the spawner's descendant waits before touching its marker, and how long past
#: that a test waits before declaring it never did.
DESCENDANT_DELAY_SEC = 2.0


DESCENDANT_MARGIN_SEC = 1.0


def assert_descendant_never_ran(marker: Path, *, started: float) -> None:
    """Wait until the descendant's own deadline is well past, then require its silence.

    Timed from when the reviewer was *launched*, not from when the kill returned: what has
    to elapse is the descendant's ``sleep``, and sleeping a flat interval afterwards paid for
    the timeout and the kill grace twice over.
    """
    remaining = started + DESCENDANT_DELAY_SEC + DESCENDANT_MARGIN_SEC - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    assert not marker.exists(), "a descendant of the reviewer outlived the deadline"


def fake_reviewer_output(tmp_path: Path, mode: str, **env: str) -> Path:
    """Run the stand-in reviewer and keep its output, as ``invoke`` would have."""
    out = tmp_path / f"reviewer-{mode}.out"
    with out.open("wb") as sink:
        subprocess.run(
            [str(FAKE_REVIEWER), str(tmp_path), "prompt"],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env={**os.environ, "ARL_FAKE_MODE": mode, **env},
            check=False,
        )
    return out


def dirty(repo: Path, text: str = "phase one\n") -> str:
    (repo / "a.txt").write_text(text)
    return gitsnap.snapshot(str(repo)).tree


def target_for(repo: Path, *, scope: str = "phase", phase: int = 1) -> Target:
    """A review of the current working state against HEAD's tree."""
    return Target(repo=str(repo), base=git(repo, "rev-parse", "HEAD^{tree}"), head=dirty(repo), scope=scope, phase=phase)


def build(activation: state.State, repo: Path, dest: Path, config: Config | None = None, *, warnings: str = "") -> Path:
    bundle.build_bundle(target_for(repo), dest, state=activation, config=config or config_with(), warnings=warnings)
    return dest


def build_final(activation: state.State, repo: Path, dest: Path) -> Path:
    bundle.build_bundle(target_for(repo, scope="final"), dest, state=activation, config=config_with())
    return dest


def invocation(tmp_path: Path, out_name: str = "reviewer.out") -> Invocation:
    return Invocation(bundle_dir=tmp_path, prompt_file=Path("prompt.md"), title="t", out_path=tmp_path / out_name)


def spawner(tmp_path: Path, marker: Path, *, delay: float = 2.0, deaf: bool = False, name: str = "spawner.sh") -> Path:
    """A command that backgrounds a child outliving it, then blocks until killed.

    ``deaf`` makes the child ignore ``SIGTERM``, which is the case that survived a
    group-wide ``SIGTERM`` followed by a wait on the direct child: the parent dies on
    schedule and the descendant does not.
    """
    script = tmp_path / name
    trap = "trap '' TERM; " if deaf else ""
    script.write_text(f"#!/usr/bin/env bash\n( {trap}sleep {delay}; touch {marker!s} ) &\nsleep 30\n")
    script.chmod(0o755)
    return script


def contract_text(*lines: str) -> str:
    return "prose line\n\n" + FINDINGS_MARKER + "\n" + "".join(f"{line}\n" for line in lines) + END_MARKER + "\n"


def review_round(activation: state.State, target: Target, *, config: Config) -> Review:
    """One phase or final review, built, invoked and recorded the way the engine does it.

    Builds the bundle under the activation's ``bundles/<seq>``, runs the reviewer named by
    ``ARL_REVIEWER_CMD``, parses its answer under the late-round scope, and appends the parsed
    round to ``round_history`` so the next round sees it. Bundle failures come back as the
    verdicts the engine reports for them.
    """
    activation.load()
    seq = activation.get_int("report_seq") + 1
    activation.update(report_seq=seq)
    activation.save()
    label = f"{seq:03d}"
    bundle_dir = activation.act_dir / "bundles" / label
    raw_dir = activation.act_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        scope = bundle.late_scope(target, state=activation)
        _, previous_rounds = bundle._previous_round(activation, target)
        bundle.build_bundle(target, bundle_dir, state=activation, config=config, round_number=previous_rounds + 1, scope=scope)
    except (bundle.BundleTooLarge, bundle.PlanEvidenceCorrupted) as exc:
        return Review(verdict="NEEDS_HUMAN", error=str(exc))
    except bundle.BundleError as exc:
        return Review(verdict="OP_FAILURE", kind="bundle", error=str(exc))
    prompt_file, prompt_text, active = bundle.compose_prompt(activation, raw_dir, label, is_phase=target.is_phase)
    run = Invocation(
        bundle_dir=bundle_dir,
        prompt_file=prompt_file,
        prompt_text=prompt_text,
        title=f"arl {label}",
        out_path=raw_dir / f"{label}.out",
        context_files=tuple(bundle.context_attachments(bundle_dir)),
    )
    review, _ = run_invocation(target, run, config=config, scope=scope)
    review.seq = seq
    review.guide = bundle.guide_disclosure(active)
    report.store(review, target, seq=label, act_dir=activation.act_dir, config=config)
    if review.verdict in ("APPROVED", "CHANGES_REQUIRED"):
        history = list(activation.get_array_of_dicts("round_history"))
        history.append(
            {
                "seq": seq,
                "label": target.label,
                "phase": target.phase,
                "generation": activation.get_int("activation_generation"),
                "verdict": review.verdict,
                "tree": target.head,
                "base": target.base,
                "at": now(),
                "findings": [line for line in records(review.all_findings) if line],
                "supersedes": [line for line in records(review.supersedes) if line],
            }
        )
        activation.update(round_history=history)
        activation.save()
    return review


def execute_fake(activation: state.State, repo: Path, mode: str, *, config: Config | None = None, scope: str = "phase") -> Review:
    os.environ["ARL_REVIEWER_CMD"] = str(FAKE_REVIEWER)
    os.environ["ARL_FAKE_MODE"] = mode
    return review_round(activation, target_for(repo, scope=scope), config=config or config_with())


def _scripted_reviewer(tmp_path: Path, name: str, contract: str) -> None:
    """Install a reviewer stand-in whose output is a fixed script."""
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/usr/bin/env bash\nprintf '%b' '{contract}'\n")
    script.chmod(0o755)
    os.environ["ARL_REVIEWER_CMD"] = str(script)


def _run_scripted(activation: state.State, repo: Path, tmp_path: Path, name: str, contract: str) -> Review:
    """One round whose reviewer output is a fixed script -- ``execute_fake``'s canned modes
    all repeat the same finding every round, which cannot exercise a reversal."""
    _scripted_reviewer(tmp_path, name, contract)
    return review_round(activation, target_for(repo), config=config_with())


_ROUND_1 = "Looks off.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=warn.py:1 | needs warn-before\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


_ROUND_2 = "Different concern.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=other.py:1 | needs something else\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"


_ROUND_3 = "Back to the first concern.\\n\\n<<<ARL-FINDINGS>>>\\nFINDING severity=medium actionable=yes file=warn.py:9 | needs warn-before after all\\nVERDICT CHANGES_REQUIRED\\n<<<ARL-END>>>\\n"
