# AGENTS.md

Project authority for `adversarial-review-loop`. Read this before changing anything here.

**Interim, v4 rewrite in progress.** This file describes the `v4` branch, which is being rebuilt
from v3's enforcement gate into a plan-and-go review loop. The plan is
`~/.claude/plans/magical-spinning-summit.md`, and its phases are landing one reviewed commit at a
time. v3's rules, invariant index and design notes describe code that no longer exists; they are
kept at the `adversarial-review-loop--v3.0.1` tag. Some docstrings still cite v3's rules ("Rule
1"), `docs/design/` notes or v3 modules; they are rewritten as each module is replaced, and a
stale reference is not an instruction. `README.md` and `docs/` are rewritten in phase 6.

For **how to review** a change to this repo, use the `adversarial-review` skill in
`.agents/skills/`.

## What this is

A Claude Code plugin that gives an implementing agent an external adversarial review per phase of
an agreed plan. The user provides a plan, Claude implements it phase by phase, and each phase is
reviewed before Claude moves on. A plan may span several git repositories and non-git folders.

v4 enforces nothing. It is not a security boundary: the agent being reviewed can bypass the loop,
and the design does not try to stop it. What it must get right is the review itself: the reviewer
sees exactly the phase's changes, its verdict is parsed strictly, and no failure is ever reported
as an approval.

Target architecture (the phases build it):

- **The mod** (`hooks/mod.ts`, phase 4 and 5): the orchestrator and UI. It registers the tools
  Claude calls (`set_phases`, `add_root`, `review`, `clarify`), the reviewer subagent, the slash
  commands, and the band and review pane.
- **The engine** (`scripts/arl/`, Python): workspace roots and snapshots, the review bundle,
  prompt composition, contract parsing and the verdict, stall detection, reports, and the run
  state under `<launch dir>/.arl/`. It is a CLI with JSON in and out, run by the mod.

## Rules that hold in every phase

1. **No failure is an approval.** A bundle that cannot be built, a reviewer that times out, exits
   non-zero, says nothing, breaks the output contract or gives an unknown verdict is
   `OP_FAILURE` or `NEEDS_HUMAN`, never `APPROVED`. The reviewer's own verdict is advisory: an
   actionable finding at or above `block_severity` blocks whatever it concluded.
2. **The reviewer sees the whole phase.** The evidence is the diff from the last approved snapshot
   to the current one, for every root, including untracked files. A path left out of the bundle
   is a change nobody reviewed.
3. **The engine's stdout is its answer.** Subcommands write their result to stdout; diagnostics go
   to stderr through `arl.util.log`. A stray `print` corrupts what the mod reads.
4. **Nothing is written inside a reviewed repository's tracked content.** Snapshots use a
   throwaway `GIT_INDEX_FILE`, and `sys.pycache_prefix` keeps `__pycache__` out of both the
   plugin and the reviewed tree.
5. **The engine runs only as `python3 -I "$PLUGIN_ROOT/scripts/arl-bootstrap.py"`.** Never
   `python3 -m arl` and never `uv run`: either would let the current directory's files shadow the
   engine's modules.

## Layout

| Path | What lives there |
| --- | --- |
| `hooks/mod.ts` | the mod: registers the tools, the reviewer agent and every hook; `hooks/hooks.json` names it |
| `hooks/flow.ts` | the loop: each tool's flow, a review round on either harness, asking the user, root registration, continuation |
| `hooks/reviewer.ts`, `engine.ts`, `text.ts` | the reviewer subagent (spawn, wait, permissions), running the engine, and the text Claude and the reviewer read |
| `hooks/commands.ts` | the person's slash commands, all prefixed `arl-` (Claude Code ships `/status`, `/resume` and `/config`) |
| `hooks/ui.tsx`, `types/index.d.ts` | the band and the review pane, and the session state they draw (its contract) |
| `scripts/arl.sh` | launcher: finds a Python 3.12 or newer and runs the bootstrap |
| `scripts/arl-bootstrap.py` | trusted absolute entrypoint; establishes `sys.path` and `sys.pycache_prefix` before `arl` is imported |
| `scripts/arl/cli.py` | subcommand dispatch: argv in, one JSON answer on stdout |
| `scripts/arl/commands.py` | the command table: `init`, `resume`, `set-phases`, `root-add`, `root-remove`, `root-baseline`, `attach`, `status`, `report`, `config`, `pause`, `stop`, `finish`, `accept`, `next-action`, and the `review`/`clarify` subcommands |
| `scripts/arl/configcmd.py` | `config`: every key with its layer, or set or unset one key |
| `scripts/arl/state.py` | the run state in `<launch dir>/.arl/state.json`: stages, approvals, tickets, `next_action` |
| `scripts/arl/workspace.py` | workspace roots: git and shadow snapshots, nested-repository discovery, late baselines |
| `scripts/arl/contract.py` | the reviewer output contract: `FINDING`/`SUPERSEDES` grammar, `parse`, the recomputed verdict, the late-round scope type, `parse_clarify` |
| `scripts/arl/review.py` | the review pipeline: `review prepare`/`record`/`run-external`, repair, failure budgets, stalls, `clarify` |
| `scripts/arl/bundle.py` | the review bundle: alias-prefixed diff chunks per root, `range.txt`, `verification.txt`, prior rounds, prompt composition |
| `scripts/arl/runner.py` | running the reviewer: the bounded runner, transcript reduction, failure classification |
| `scripts/arl/harness/`, `reviewer_probe.py` | the reviewer-CLI seam; `opencode.py` is the one external harness, and the probe lists its models |
| `scripts/arl/gitsnap.py` | temp-index snapshot, oversized guard, submodule detection |
| `scripts/arl/oscillation.py` | stall detection across rounds |
| `scripts/arl/guidance.py` | project review guidance: each root's review skill and the `review_guide` file |
| `scripts/arl/report.py` | report storage and the text Claude sees |
| `scripts/arl/atomic.py`, `config.py`, `globmatch.py`, `util.py`, `errors.py` | shared plumbing |
| `prompts/*.md` | the reviewer prompts |
| `tests/unit/` | pytest unit tests; shared fixtures in `conftest.py`, run fixtures in `run_common.py`, reviewer stand-ins in `reviewer_common.py` |
| `tests/mod/` | the mod's tests under `claude plugin test`; the scripted engine and user in `world.ts` |
| `tests/selftest.sh` | smoke test for `scripts/arl.sh` |
| `tests/STEP0.md` | live-session measurements of the Claude Code host; Session J covers the v4 mod surface |

## Generated: never hand-edit

- `.claude-plugin/types/`: the API types Claude Code writes when it loads the plugin from a
  checkout; untracked.
- `.mk/help.mk`, `.mk/pre-commit.mk` and `scripts/bootstrap-mk-common.sh`: fetched from
  `leinardi/make-common`, refreshed with `make mk-common-update`.

## Working on it

```console
make dev-deps     # install the pinned dev dependencies (once per checkout)
make test         # unit tests and the arl.sh smoke test; no model is called
make test-unit    # the pytest half only
make test-accept  # tests/selftest.sh only
make test-mod     # the mod under `claude plugin test`; needs the claude binary (from phase 4)
make check        # pre-commit: shellcheck, markdownlint, yamllint, actionlint, ruff, mypy, eslint, tsc, licence headers
make sync-pins    # propagate requirements-dev.txt into .pre-commit-config.yaml
```

`make test` must pass before any commit, and a behaviour change needs a test that fails on the old
code. The plugin's runtime needs only `python3` and the standard library; `requirements-dev.txt` is
for development.

**`make check` only sees files git already knows about.** Run `git add -N` on every new file before
`make check`. It also runs fix-capable hooks, so check `git status --short` afterwards.

Every Python, shell, TypeScript and JavaScript source, the Makefile and the repo-local `.mk`
fragments carry the GPL-3.0-or-later header (the IntelliJ "GNU GPLv3" profile in
`.idea/copyright/`; in Python, after the module docstring). `scripts/check-license-headers.sh`
enforces it. A new file takes the year of its first commit. YAML, JSON and Markdown are exempt.

### Python conventions

- Python 3.12, standard library only at runtime.
- `ruff` (`.ruff.toml`) and `mypy --strict` (`mypy.ini`) must both be clean.
- One module per concern; state is passed explicitly, never through a module-level global.
- `__all__` is declared on every module and is its actual public surface.

### Commits

All commits are Conventional Commits 1.0.0 **with a scope**: `<type>(<scope>)[!]: <description>`,
enforced by the `commit-msg` hook and CI. A change users receive (`scripts/`, `hooks/`,
`prompts/`, `.claude-plugin/`) is `feat` or `fix`; anything else (CI, tests, docs, tooling) is
another type. svu derives the release from these types, so a wrong type ships a wrong version; see
[`docs/release.md`](docs/release.md). The v4 work lands on the `v4` branch, one reviewed commit per
phase, and merges to `main` in one pull request.
