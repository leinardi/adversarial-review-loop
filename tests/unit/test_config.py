"""Config precedence and typed access."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocrl import config, harness


@pytest.fixture
def layers(tmp_path: Path, clean_env: dict[str, str]) -> dict[str, str]:
    """An environment with a user config directory and an empty repo, both under tmp."""
    env = dict(clean_env)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    (tmp_path / "repo").mkdir()
    return env


def write_user_config(env: dict[str, str], values: object) -> None:
    path = Path(env["XDG_CONFIG_HOME"]) / "opencode-review-loop" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(values if isinstance(values, str) else json.dumps(values))


def write_repo_config(repo: Path, values: object) -> None:
    (repo / config.REPO_CONFIG_NAME).write_text(values if isinstance(values, str) else json.dumps(values))


# -- precedence ------------------------------------------------------------


def test_defaults_stand_when_nothing_else_exists(layers: dict[str, str], tmp_path: Path) -> None:
    cfg = config.load(str(tmp_path / "repo"), layers)
    # The gate ships as a Claude Code plugin, so the reviewer every user already has installed
    # is the one it defaults to; `opencode` stays one config key away.
    assert cfg.as_str("harness") == "claude-code"
    # Empty, not a model name: `model`'s real default is the selected harness's own, resolved
    # through `harness.model` -- see `test_harness.py` for that half of the contract.
    assert cfg.as_str("model") == ""
    assert harness.model(cfg) == "opus"
    assert cfg.as_int("ttl_hours") == 24
    assert cfg.as_bool("pure") is True
    assert cfg.as_bool("final_review") is False
    assert cfg.as_bool("cold_confirm") is False
    assert cfg.as_list("ignore_globs") == []


def test_all_four_layers_in_order(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_user_config(layers, {"model": "user-model", "block_severity": "high", "ttl_hours": 5})
    write_repo_config(repo, {"model": "repo-model", "block_severity": "medium"})
    layers["OCRL_MODEL"] = "env-model"

    cfg = config.load(str(repo), layers)

    assert cfg.as_str("model") == "env-model", "environment must win"
    assert cfg.as_str("block_severity") == "medium", "repo must beat user"
    assert cfg.as_int("ttl_hours") == 5, "user must beat defaults"
    assert cfg.as_int("timeout_sec") == 900, "defaults must survive underneath"


def test_a_missing_repo_argument_skips_the_repo_layer(layers: dict[str, str], tmp_path: Path) -> None:
    write_repo_config(tmp_path / "repo", {"model": "repo-model"})
    assert config.load("", layers).as_str("model") == "", "the repo layer's model must not have been read"


# -- the activation overlay --------------------------------------------------


def test_overrides_beat_repo_but_lose_to_environment(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_user_config(layers, {"model": "user-model"})
    write_repo_config(repo, {"model": "repo-model"})
    layers["OCRL_MODEL"] = "env-model"

    cfg = config.load(str(repo), layers, overrides={"model": "override-model"})
    assert cfg.as_str("model") == "env-model", "the environment must still win over an activation override"

    del layers["OCRL_MODEL"]
    cfg = config.load(str(repo), layers, overrides={"model": "override-model"})
    assert cfg.as_str("model") == "override-model", "an override must beat the repo config"


def test_an_unknown_override_key_is_dropped(layers: dict[str, str], tmp_path: Path) -> None:
    """The overlay is written into ``state.json``, which is not a trust boundary (Rule 3)."""
    cfg = config.load(str(tmp_path / "repo"), layers, overrides={"model": "override-model", "totally_not_a_real_key": "x"})
    assert cfg.as_str("model") == "override-model", "a known key from the overlay is still accepted"
    assert "totally_not_a_real_key" not in cfg.values


def test_an_empty_overrides_mapping_changes_nothing(layers: dict[str, str], tmp_path: Path) -> None:
    write_user_config(layers, {"model": "user-model"})
    assert config.load(str(tmp_path / "repo"), layers, overrides={}).as_str("model") == "user-model"
    assert config.load(str(tmp_path / "repo"), layers, overrides=None).as_str("model") == "user-model"


# -- malformed input -------------------------------------------------------


def test_an_unparseable_file_drops_the_whole_file_layer(layers: dict[str, str], tmp_path: Path) -> None:
    """A partially applied security-relevant config is worse than an ignored one.

    The shell slurped every file through one jq, so a single bad file left the defaults
    standing rather than half a merge. That is preserved deliberately.
    """
    repo = tmp_path / "repo"
    write_user_config(layers, {"model": "user-model"})
    write_repo_config(repo, "{ not json")
    layers["OCRL_BLOCK_SEVERITY"] = "critical"

    cfg = config.load(str(repo), layers)

    assert cfg.as_str("model") == "", "the good file must not survive alone"
    assert cfg.as_str("block_severity") == "critical", "the environment still applies"


def test_a_non_object_file_is_skipped_on_its_own(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_user_config(layers, {"model": "user-model"})
    write_repo_config(repo, [1, 2, 3])
    assert config.load(str(repo), layers).as_str("model") == "user-model"


# -- environment coercion --------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True)])
def test_truthy_environment_values(raw: str, expected: bool, layers: dict[str, str]) -> None:
    layers["OCRL_PURE"] = raw
    assert config.load("", layers).as_bool("pure") is expected


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "True", "YES", "anything"])
def test_everything_else_is_false(raw: str, layers: dict[str, str]) -> None:
    """Exact matching, so a typo never silently enables something."""
    layers["OCRL_PURE"] = raw
    assert config.load("", layers).as_bool("pure") is False


def test_final_review_env_override(layers: dict[str, str]) -> None:
    assert config.load("", layers).as_bool("final_review") is False
    layers["OCRL_FINAL_REVIEW"] = "true"
    assert config.load("", layers).as_bool("final_review") is True


def test_final_review_precedence_across_all_four_layers(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert config.load(str(repo), layers).as_bool("final_review") is False, "defaults must stand alone"

    write_user_config(layers, {"final_review": True})
    assert config.load(str(repo), layers).as_bool("final_review") is True, "user must beat defaults"

    write_repo_config(repo, {"final_review": False})
    assert config.load(str(repo), layers).as_bool("final_review") is False, "repo must beat user"

    layers["OCRL_FINAL_REVIEW"] = "true"
    assert config.load(str(repo), layers).as_bool("final_review") is True, "environment must beat repo"


def test_cold_confirm_env_override(layers: dict[str, str]) -> None:
    """Off by default, and reachable for a single run without touching a config file."""
    assert config.load("", layers).as_bool("cold_confirm") is False
    layers["OCRL_COLD_CONFIRM"] = "true"
    assert config.load("", layers).as_bool("cold_confirm") is True


def test_cold_confirm_precedence_across_all_four_layers(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert config.load(str(repo), layers).as_bool("cold_confirm") is False, "defaults must stand alone"

    write_user_config(layers, {"cold_confirm": True})
    assert config.load(str(repo), layers).as_bool("cold_confirm") is True, "user must beat defaults"

    write_repo_config(repo, {"cold_confirm": False})
    assert config.load(str(repo), layers).as_bool("cold_confirm") is False, "repo must beat user"

    layers["OCRL_COLD_CONFIRM"] = "true"
    assert config.load(str(repo), layers).as_bool("cold_confirm") is True, "environment must beat repo"


def test_a_non_numeric_integer_override_is_skipped_not_zeroed(layers: dict[str, str]) -> None:
    write_user_config(layers, {"timeout_sec": 111})
    layers["OCRL_TIMEOUT_SEC"] = "not-a-number"
    assert config.load("", layers).as_int("timeout_sec") == 111


def test_max_session_rounds_defaults_to_three_and_takes_an_integer_override(layers: dict[str, str]) -> None:
    assert config.load("", layers).as_int("max_session_rounds") == 3
    layers["OCRL_MAX_SESSION_ROUNDS"] = "0"
    assert config.load("", layers).as_int("max_session_rounds") == 0, "0 disables the cap; it must not be read as unset"


def test_ignore_globs_is_comma_separated_in_the_environment(layers: dict[str, str]) -> None:
    layers["OCRL_IGNORE_GLOBS"] = "a/*,,b/**,"
    assert config.load("", layers).as_list("ignore_globs") == ["a/*", "b/**"]


def test_an_empty_string_override_still_counts_as_set(layers: dict[str, str]) -> None:
    """The shell tested `${!var+set}`, which is true for an empty value."""
    write_user_config(layers, {"variant": "from-user"})
    layers["OCRL_VARIANT"] = ""
    assert config.load("", layers).as_str("variant") == ""


# -- typed access ----------------------------------------------------------


def test_a_false_boolean_renders_as_false_not_empty(layers: dict[str, str]) -> None:
    """The shell's `.[$k] // ""` blanked every false boolean; this does not reproduce it."""
    assert config.load("", layers).as_str("pure") == "true"
    layers["OCRL_PURE"] = "0"
    assert config.load("", layers).as_str("pure") == "false"


def test_a_list_renders_comma_joined(layers: dict[str, str]) -> None:
    layers["OCRL_IGNORE_GLOBS"] = "a,b"
    assert config.load("", layers).as_str("ignore_globs") == "a,b"


@pytest.mark.parametrize(
    ("label", "rank"),
    [
        ("info", 1),
        ("TRIVIAL", 1),
        ("nit", 1),
        ("low", 2),
        ("Minor", 2),
        ("medium", 3),
        ("moderate", 3),
        ("major", 3),
        ("high", 4),
        ("serious", 4),
        ("critical", 5),
        ("blocker", 5),
        ("fatal", 5),
    ],
)
def test_severity_rank(label: str, rank: int) -> None:
    assert config.severity_rank(label) == rank


@pytest.mark.parametrize("label", ["", "unknown", "SEV-2", "probably fine"])
def test_an_unrecognised_severity_ranks_most_severe(label: str) -> None:
    """An unparsable label must never be a way to slip past the gate (Rule 1)."""
    assert config.severity_rank(label) == 5


@pytest.mark.parametrize(
    ("label", "rank"),
    [
        ("info", 1),
        ("low", 2),
        ("MEDIUM", 3),
        ("high", 4),
        ("critical", 5),
    ],
)
def test_threshold_rank_agrees_with_severity_rank_on_known_labels(label: str, rank: int) -> None:
    """``critical`` is a real fifth tier the reviewer contract's own `FINDING` regex accepts
    -- a `block_severity` of `critical` must rank 5, matching a critical finding exactly, not
    fall through as if it were a typo."""
    assert config.threshold_rank(label) == rank == config.severity_rank(label)


@pytest.mark.parametrize("label", ["", "unknown", "SEV-2", "hihg"])
def test_an_unrecognised_threshold_ranks_least_severe(label: str) -> None:
    """The opposite fallback direction from `severity_rank`: an unrecognised *threshold*
    must make the gate block on more, not less. Ranking it high (like a finding severity
    would) is fail-open here -- almost nothing would meet a threshold of 5."""
    assert config.threshold_rank(label) == 1


def test_severity_labels_is_exactly_what_both_rank_functions_recognise() -> None:
    """Every label in `SEVERITY_LABELS` must be a real dict hit for both functions -- if one
    were not, `severity_rank` would silently answer 5 (its unrecognised fallback) while
    `threshold_rank` answered 1 (its own), and this catches that mismatch directly."""
    for label in config.SEVERITY_LABELS:
        assert config.severity_rank(label) == config.threshold_rank(label)
    assert {"info", "trivial", "nit", "low", "minor", "medium", "moderate", "major", "high", "serious", "critical"} == config.SEVERITY_LABELS


# -- late_block_severity ----------------------------------------------------


def test_late_block_severity_defaults_to_high(layers: dict[str, str], tmp_path: Path) -> None:
    cfg = config.load(str(tmp_path / "repo"), layers)
    assert cfg.as_str("late_block_severity") == "high"
    assert config.late_threshold_rank(cfg) == config.threshold_rank("high")


def test_late_block_severity_env_override(layers: dict[str, str]) -> None:
    layers["OCRL_LATE_BLOCK_SEVERITY"] = "critical"
    assert config.load("", layers).as_str("late_block_severity") == "critical"


def test_late_block_severity_is_clamped_up_to_block_severity(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_repo_config(repo, {"block_severity": "high", "late_block_severity": "low"})
    cfg = config.load(str(repo), layers)
    assert config.late_threshold_rank(cfg) == config.threshold_rank("high"), "it can only defer, never widen"


def test_an_unrecognised_late_block_severity_falls_back_to_block_severity(layers: dict[str, str], tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_repo_config(repo, {"late_block_severity": "spicy"})
    cfg = config.load(str(repo), layers)
    assert config.late_threshold_rank(cfg) == config.threshold_rank("medium"), "unknown ranks at the floor, then the clamp restores the ordinary rule"


def test_the_harness_is_settable_from_the_environment(layers: dict[str, str], tmp_path: Path) -> None:
    """``harness`` is a plain string key, so `OCRL_HARNESS` reaches it through `from_env`'s
    string branch like any other -- which is what makes `OCRL_HARNESS=claude-code make dry-run`
    work without a config file."""
    layers["OCRL_HARNESS"] = "claude-code"

    cfg = config.load(str(tmp_path / "repo"), layers)

    assert cfg.as_str("harness") == "claude-code"
    assert harness.model(cfg) == harness.get("claude-code").default_model
