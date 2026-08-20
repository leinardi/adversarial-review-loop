# AGENTS.md

Project authority for `opencode-review-loop`. Read this before changing anything here.

For **how to review** a change to this repo, use the `adversarial-review` skill in `.agents/skills/` — it owns the review procedure and the full invariant checklist. This file owns what the project *is* and the rules a change must not break.

## What this is

A Claude Code plugin that turns an external OpenCode review into an **enforcement gate** on `git commit`. Claude implements a plan phase by phase; each phase's commit is intercepted by a `PreToolUse` hook, the whole working state is snapshotted into a git tree, OpenCode reviews the delta, and the commit proceeds only if the review passes.

It is a security-shaped component. The failure that matters is not a crash — it is an **unreviewed commit that looks reviewed**.

## The five rules

Everything else is detail. These are not negotiable, and a change that weakens one is a defect even if every test passes.

0. **A gate that cannot prove it is running denies.** The hooks register when the `implement` skill is invoked, so a dispatcher that runs at all proves the skill was invoked. If it then finds no session pointer, arming never *executed* — a refused sandbox, an unreadable script, an unresolved `${CLAUDE_PLUGIN_ROOT}` — and `cmd_arm` cannot persist a failure to start. The dispatcher records `ARM_FAILED` itself and denies. Absence of state is never an opt-out. This is why `deactivate` leaves the session pointer in place and relies on `DISARMED` instead of deleting it.
1. **Nothing converts a failure into an approval.** Missing state, malformed JSON, a snapshot failure, a timeout, a non-zero reviewer exit, empty output, absent markers, an unknown verdict, an evidence ceiling — every one of them blocks or escalates. Operational uncertainty is never "no findings".
2. **Hook stdout is protocol.** Hook entrypoints (`pretool`, `confirm-commit`, `posttool-failure`, `gate-stop`) emit valid Claude hook JSON or nothing. Diagnostics go to stderr or a file. A stray `printf` in a library that runs under a hook corrupts the response — `ocrl_report_store` is deliberately silent for this reason. `EXIT` traps fail closed.
3. **Nothing is written inside the repository under review.** All state, frozen plans, bundles and reports live under `$XDG_STATE_HOME/opencode-review-loop/`. The snapshot uses a throwaway `GIT_INDEX_FILE` and never touches the real index or worktree.
4. **The user owns the exits.** `implement`, `finish` and `stop` are `disable-model-invocation: true`, and Claude's own route to them — Bash — is denied in `cmd_pretool`. Claude can never arm, finish, or disarm the mode.

## The parser is only safe because the deny-list is aggressive

`scripts/lib/cmdshape.sh` is a hand-rolled tokenizer, and it decides whether a commit command may run. It has a structural weakness worth stating plainly, because it is not visible from the code:

**It is a parser that must agree with a parser it does not control.** The gate reads `tool_input.command` as a string and decides; Claude Code then executes that same string through a real bash. Any place where the tokenizer's reading diverges from bash's actual grammar is a potential bypass. Rewriting the tokenizer in another language would not change this — only using a real shell parser (an AST from something like `mvdan.cc/sh/syntax`) would.

What makes the current implementation defensible is that `ocrl_cmd_tokenize` **rejects almost the entire grammar before tokenizing**: `$`, backticks, `;`, `|`, `<`, `>`, `(`, `)`, `{`, `}`, unquoted globs, a bare `&`, newlines and comments are all refused outright. What survives is a tiny language — words, two quoting forms, backslash escape, and `&&` — and agreeing with bash on *that* is a small claim rather than a large one.

**Therefore: the deny-list and the hand-rolled tokenizer are a single design, and relaxing one without replacing the other is the specific change that breaks this component.** If you want to accept any construct currently rejected up front — command substitution, redirection, process substitution, a pipeline, ANSI-C quoting — swap in a real parser first. Widening `ocrl_cmd_tokenize`'s accepted character set is not a small change, however small the diff looks.

Two things that make this defence in depth rather than a single point of failure, both of which must be preserved:

- `confirm-commit` independently verifies `HEAD^{tree} == pending_approved_tree` and a clean worktree, so a tokenizer bypass yields a *detected, recoverable* bad commit that enters `RECONCILE` — not a silent unreviewed one.
- The final cumulative review covers the end state regardless of what happened per commit.

### The argument channel is not escaped

`skills/implement/SKILL.md` passes `--args "$ARGUMENTS"`. Claude Code substitutes `$ARGUMENTS` textually, without shell escaping, and the body runs through `eval`. A plan path of `x"; id; echo "` therefore executes `id`. This is confirmed, not theoretical.

No change to the body fixes it — double quotes are broken by `"`, single quotes by `'`, and the substitution precedes any shell. The containment is `disable-model-invocation: true` on the skill, which means only the user can supply the path. **Do not remove that flag**, and do not add a second interpolated argument without re-reading this. `cmd_arm`'s character-set check is still worth keeping, but understand what it does and does not do: it runs after any injected command has already executed, so it protects the loop's state, not the machine.

## Hot-path rules

The `PreToolUse` dispatcher runs on **every** tool call, so cost there is multiplied by thousands. Two invariants hold it in place:

- **Read-only tools answer before config or state is loaded.** They are permitted in every state, so `cmd_pretool` hoists `ocrl_tool_is_readonly` above `ocrl_config_load`. If a future state ever needs to deny a read-only tool, **remove that hoist first** — otherwise the deny is unreachable.
- **One process per job on the hot path.** Hook fields come out of a single `jq` (`ocrl_hook_parse`), the config merge is a single `jq`, state load is a single `jq`, and `ocrl_effective_status` gets status, `armed_at` and the TTL together. `ocrl_now` and `ocrl_pointer_read` are builtins. Adding a per-field `jq` or a `date`/`head`/`cut` fork here is a measurable regression — the naive version cost 63 ms and 31 processes per call, against 12 ms and 3 today. Re-measure with `strace -f -e trace=execve` before and after.

## Layout

| Path | What lives there |
| --- | --- |
| `scripts/ocrl.sh` | single entrypoint; every hook and slash command routes through it |
| `scripts/lib/common.sh` | paths, hook JSON I/O, fail-closed decision emitters |
| `scripts/lib/config.sh` | config precedence: `OCRL_*` env → repo json → user json → defaults |
| `scripts/lib/state.sh` | session pointer, `state.json`, effective status incl. TTL |
| `scripts/lib/gitsnap.sh` | temp-index snapshot, oversized guard, submodule detection |
| `scripts/lib/cmdshape.sh` | commit-command tokenizer and flag allowlists |
| `scripts/lib/reviewer.sh` | bundle building, OpenCode invocation, contract parsing |
| `scripts/lib/report.sh` | report storage and the text Claude actually sees |
| `prompts/*.md` | the fixed reviewer prompts — Claude composes none of this |
| `skills/*/SKILL.md` | the five slash commands; `implement` carries the hook registrations |
| `tests/selftest.sh` | the whole suite; scratch repos, no model calls |
| `tests/STEP0.md` | runbook for the assumptions only a live session can settle |
| `tests/step0-fixture.sh` | builds the throwaway repo that runbook needs |

## Working on it

```console
make dev-deps                # install the pinned dev dependencies (once per checkout)
make test                    # full suite; no model is called
make test-unit               # the pytest half only
make test-accept             # tests/selftest.sh only
make test-filter FILTER=stop # one selftest section
make check                   # pre-commit: shellcheck, markdownlint, yamllint, actionlint, ruff, mypy
make dry-run                 # print the exact opencode argv and prompt without invoking it
```

`make test` needs pytest, which is pinned in `requirements-dev.txt` and installed by `make dev-deps`;
CI installs from the same file, so a local run and a CI run see the same version. That is a
*development* dependency only — the plugin's own runtime needs nothing beyond `python3` and the
standard library, and must keep working straight from a checkout.

`make test` must pass before any commit. A change to the gate needs a test that **fails on the old code** — a test that only asserts a helper's return value while the end-to-end bypass survives is not a regression test.

`make check` runs fix-capable hooks (markdownlint, prettier, end-of-file-fixer). Check `git status --short` afterwards so a formatter's edits are not mistaken for reviewed input.

### Why the gate does not run under uv

`make test` uses `uv` when it is installed — it resolves `requirements-dev.txt` into a cached environment with no virtualenv to manage, and it works where pip refuses to touch the system interpreter (PEP 668). That is a **developer convenience only**. The gate itself must keep running as `python3 -I "$PLUGIN_ROOT/scripts/ocrl-bootstrap.py"`, and `uv run` must never appear on the hook path. Two measured reasons, both about the same thing — under a hook, the current directory is the repository under review:

- **`uv run` executes code from the reviewed repository.** In a directory containing a `pyproject.toml` with a `backend-path` build backend, `uv run python -I <trusted bootstrap>` ran that backend *before* the bootstrap started. That is the `python3 -m` exploit reintroduced one level up, and the bootstrap's hardening cannot see it.
- **`.python-version` in the reviewed repository redirects the interpreter.** Even with `--no-project`, a repo shipping `.python-version` made uv download CPython 3.9 (27 MB, over the network, inside a hook) and run the gate on it — below the 3.12 floor.

Closing both needs `uv run --no-project --no-config --python <absolute interpreter>` with `UV_PYTHON_DOWNLOADS=never` — at which point uv contributes nothing except latency, and the latency is not small: 122 ms per invocation against 20 ms for `python3 -I`, on a path that runs on every tool call.

**`make check` only sees files git already knows about.** pre-commit enumerates `git ls-files`, so a brand-new file that has never been added is skipped in silence — ruff, mypy and shellcheck report clean on code they never read, and the first real run is the commit hook, after the change has already been reviewed. Run `git add -N` on every new file (intent-to-add is enough; it needs no staged content) before `make check`, and treat a green run that skipped a new module as no run at all.

### Shell conventions

- Bash 4.4+, GNU coreutils. `split -C`, `stat -c`, `sed -i` are relied on and declared in the README.
- `shellcheck -x` must be clean. `.shellcheckrc` sets `source-path=SCRIPTDIR` and disables `SC1091`.
- Libraries are sourced into one process and communicate through globals (`OCRL_STATE`, `OCRL_REVIEW_*`, `OCRL_CMD_ERROR`, `OCRL_SNAP_*`). Shellcheck cannot see across files, so cross-file globals carry an explicit `SC2034` disable — do not "fix" them by making them local.
- Functions communicate through five channels: globals, stdout, exit status, files, and subshell boundaries. A helper called inside `$( )` cannot set a global for its caller; the selftest keeps the last hook payload in a file for exactly this reason.
- `set -u` is on; `set -e` is deliberately off, because the gate must handle failures rather than die on them.

### Adding config

New keys go in `ocrl_config_defaults`, in the `ocrl_config_from_env` key list with the right type branch, and in the README table. Treat repository config as attacker-controlled: a config change must not be able to execute unreviewed code or silently weaken the active gate.

### Commits

Conventional Commits with a mandatory scope (`conventional-pre-commit --force-scope`), e.g. `fix(cmdshape): reject git commit --only`.

### The install cache, and what it means for iterating

A local marketplace install **copies** the plugin to `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, but sets `${CLAUDE_PLUGIN_ROOT}` to the `installLocation` — this repository. The two are served from different places:

| What changed | Takes effect |
| --- | --- |
| anything under `scripts/` | **immediately** — hooks run `${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh`, which is the working tree |
| `skills/*/SKILL.md` body or frontmatter | only after the cache is refreshed |
| `prompts/*.md` | immediately, for the same reason as `scripts/` |

`/plugin marketplace update` refreshes the marketplace record but **not** the cached copy when the version is unchanged. To pick up a skill-body change, bump `version` in `.claude-plugin/plugin.json`, then reinstall and restart. A stale body is easy to misdiagnose because the script it invokes is current — the giveaway is an error quoting a command line you no longer have in the repo.

## Host integration

Some behaviour cannot be tested from a shell — skill-hook registration, `` !`…` `` expansion inside a skill body, `${CLAUDE_SESSION_ID}` equality with the hooks' `session_id`, the Stop-hook block cap. These live in `tests/STEP0.md` with an expected result and a fallback each. **Do not claim shell tests cover them**, and do not change the arming path or the skill frontmatter without re-running the relevant STEP0 item.

Verified against Claude Code 2.1.235 and `opencode 1.18.18`.

## Known environment hazards

- `--pure` removes OpenCode **plugins**, not global skills (`~/.config/opencode/skills`) or a global `~/.config/opencode/AGENTS.md`. Both still reach the reviewer; the fixed prompts tell it to ignore ambient style directives and not to invoke a skill for the review. A reformatted review fails the contract, which blocks — it never approves.
- Anything that writes into the worktree between the gate and the commit (an editor writing back a buffer, an MCP server dropping a state directory, a file watcher) changes the tree out from under the approval and lands you in `RECONCILE`. Gitignore such paths before arming.
