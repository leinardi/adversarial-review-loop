"""``list_models`` must never treat an incomplete run as a confirmed answer.

A model can be one line into printing the rest when a timeout fires, or the process can
crash after printing something plausible-looking. Neither is a completed answer, and a
caller (``arm``, ``config``) that trusted the partial output could accept a model the
reviewer does not actually support, or -- for ``config`` -- refuse a model it does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_commands_arm import _path_without_opencode

from arl import reviewer_probe


def _fake_opencode(bindir: Path, script: str) -> None:
    fake = bindir / "opencode"
    fake.write_text(f"#!/usr/bin/env bash\n{script}\n")
    fake.chmod(0o755)


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `_path_without_opencode` also symlinks `git` and `bash` in -- without them, the fake
    # `opencode` script's own `#!/usr/bin/env bash` shebang cannot resolve, and the process
    # fails with an unrelated "not found" before it ever reaches the behaviour under test.
    path = Path(_path_without_opencode(tmp_path))
    monkeypatch.setenv("PATH", str(path))
    return path


def test_a_completed_run_returns_the_reported_models(bindir: Path) -> None:
    _fake_opencode(bindir, "printf 'vendor/a\\nvendor/b\\n'")
    assert reviewer_probe.list_models() == ["vendor/a", "vendor/b"]


def test_a_non_zero_exit_is_not_trusted_even_with_output(bindir: Path) -> None:
    _fake_opencode(bindir, "printf 'vendor/model\\n'\nexit 1")
    with pytest.raises(reviewer_probe.ProbeFailed):
        reviewer_probe.list_models()


def test_a_timeout_is_not_trusted_even_with_partial_output(bindir: Path) -> None:
    _fake_opencode(bindir, "printf 'vendor/model\\n'\nsleep 5")
    with pytest.raises(reviewer_probe.ProbeFailed):
        reviewer_probe.list_models(timeout=0.2)


def test_empty_output_on_a_successful_exit_is_still_a_failure(bindir: Path) -> None:
    _fake_opencode(bindir, "exit 0")
    with pytest.raises(reviewer_probe.ProbeFailed):
        reviewer_probe.list_models()


def test_a_missing_binary_raises_probe_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(reviewer_probe.ProbeFailed):
        reviewer_probe.list_models()
