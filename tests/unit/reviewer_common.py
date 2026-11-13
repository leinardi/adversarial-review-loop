"""Helpers shared by the runner and contract tests: the stand-in reviewer and its modes."""

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

from conftest import FAKE_REVIEWER

from arl.contract import END_MARKER, FINDINGS_MARKER
from arl.runner import Invocation

#: What the ANSI-stripping test expects once the escapes are gone.
PLAIN_VERDICT = b"VERDICT APPROVED\n"


#: Every stand-in reviewer mode, with the verdict the engine must reach. Nothing here is
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


def bundle_with_diff(tmp_path: Path) -> Path:
    """A bundle directory holding one diff chunk: the least ``invoke`` launches a reviewer on."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(exist_ok=True)
    (bundle_dir / "changes.01.diff").write_text("diff --git a/ws/a.txt b/ws/a.txt\n")
    return bundle_dir


def invocation(tmp_path: Path, out_name: str = "reviewer.out") -> Invocation:
    return Invocation(bundle_dir=bundle_with_diff(tmp_path), prompt_file=tmp_path / "prompt.md", title="t", out_path=tmp_path / out_name)


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
