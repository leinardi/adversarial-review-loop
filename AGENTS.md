# AGENTS.md

Project authority for `opencode-review-loop`. Read this before changing anything here.

For **how to review** a change to this repo, use the `adversarial-review` skill in `.agents/skills/` — it owns the review procedure and the full invariant checklist. This file owns what the project *is* and the rules a change must not break.

## What this is

A Claude Code plugin that turns an external OpenCode review into an **enforcement gate** on `git commit`. Claude implements a plan phase by phase; each phase's commit is intercepted by a `PreToolUse` hook, the whole working state is snapshotted into a git tree, OpenCode reviews the delta, and the commit proceeds only if the review passes.

It is a security-shaped component. The failure that matters is not a crash — it is an **unreviewed commit that looks reviewed**.

## The five rules

Everything else is detail. These are not negotiable, and a change that weakens one is a defect even if every test passes.

0. **A gate that cannot prove it is running denies.** The hooks register when the `implement` skill is invoked, so a dispatcher that runs at all proves the skill was invoked. If it then finds no session pointer, arming never *executed* — a refused sandbox, an unreadable script, an unresolved `${CLAUDE_PLUGIN_ROOT}` — and `commands/arm.py` cannot persist a failure to start. `hooks.record_unstarted_arm` records `ARM_FAILED` itself and denies. Absence of state is never an opt-out. This is why `deactivate` leaves the session pointer in place and relies on `DISARMED` instead of deleting it.
1. **Nothing converts a failure into an approval.** Missing state, malformed JSON, a snapshot failure, a timeout, a non-zero reviewer exit, empty output, absent markers, an unknown verdict, an evidence ceiling — every one of them blocks or escalates. Operational uncertainty is never "no findings".
2. **Hook stdout is protocol.** Hook entrypoints (`pretool`, `confirm-commit`, `posttool-failure`, `gate-stop`) emit valid Claude hook JSON or nothing. Diagnostics go to stderr only, through `ocrl.util.log`. A stray `print` in a module that runs under a hook corrupts the response — `report.store` is deliberately silent on stdout for this reason. An uncaught exception at the top level still emits that event's fallback JSON before exiting 0; see "Interpreter invocation" below for the full contract.
3. **Nothing is written inside the repository under review.** All state, frozen plans, bundles and reports live under `$XDG_STATE_HOME/opencode-review-loop/`. The snapshot uses a throwaway `GIT_INDEX_FILE` and never touches the real index or worktree. `sys.pycache_prefix` keeps `__pycache__` out of both the plugin repo and the reviewed one; see "Interpreter invocation" below.
4. **The user owns the exits.** `implement`, `finish` and `stop` are `disable-model-invocation: true`, and Claude's own route to them — Bash — is denied in `commands/pretool.py`. See below for what that does and does not guarantee.

### What Rule 4 does and does not guarantee

The denial reads `tool_input.command` as a string, so it stops the direct route and nothing more. Two things defeat it, both confirmed by experiment:

- **A wrapper.** `bash escape.sh`, where the script commits and then runs `ocrl.sh deactivate`, is recognised as neither a commit nor an escape. The commit lands unreviewed and the mode ends.
- **Editing the state.** `status: DISARMED` written straight into `state.json` ends the mode with no command to inspect at all. The `PreToolUse` gate now refuses an *editing tool* aimed at the state root, but a shell command can still write the same file — the gate runs as the same user, and by the time it is asked about a command, that command is about to run.

**So Rule 4 is not enforceable against a deliberately adversarial model.** It holds against mistakes, drift, and a model following the loop's instructions, which is what the component is for. No file-based mechanism can do better: any token this plugin writes to disk, the model's shell can write too.

What *is* enforceable is reporting. `confirm-commit` reports a HEAD whose tree no review approved, and the Stop gate repeats it through `systemMessage`. The value of that second channel rests on `systemMessage` reaching the *user*, so that relaying it is not the model's decision — **that is assumed, not verified**, and it is `tests/STEP0.md` item 12. If it turns out to be model-visible only, this paragraph is wrong and the escape has no reliable reader at all. Closing the rule properly needs a host-only signal that a real user typed the slash command: item 11. Until both are settled, the limit stands as documented rather than fixed.

## The deny-list is still the security boundary

`scripts/ocrl/cmdshape.py` decides whether a commit command may run, and since the bashlex vendoring it does so with a real bash parser rather than a hand-rolled tokenizer. That change does **not** move the security boundary, and the reason is worth stating plainly, because it is not visible from a diff:

**The gate is still a parser that must agree with a parser it does not control.** It reads `tool_input.command` as a string and decides; Claude Code then executes that same string through a real bash. bashlex parses the *whole* shell grammar, so on its own it would happily hand back a clean AST for a pipeline, a subshell or a redirection — the gate would then have to decide, node by node, which constructs cannot reach the filesystem after the snapshot. That is the same policy question as before, re-expressed against a grammar large enough to hide a mistake in.

**`_deny_shell_grammar` runs first and is unchanged in substance from `cmdshape.sh`.** `$`, backticks, `;`, `|`, `<`, `>`, `(`, `)`, `{`, `}`, unquoted globs, a bare `&`, newlines and comments are all refused before bashlex ever sees the command. What survives is the same tiny language as before — words, two quoting forms, backslash escape, and `&&` — and *that* is what bashlex now turns into words (`tokenize` in `cmdshape.py`).

**What the parser bought is word-splitting and quote handling, not a wider grammar.** Splitting, quote removal and backslash handling are now bash's own rules as bashlex implements them, not as re-derived by a loop of this repository's own — and a command bashlex cannot parse (a trailing unescaped backslash, an unterminated quote) is refused rather than tokenized on a guess, which the hand-rolled version sometimes did.

**Therefore: the deny-list and the parser are still a single design, and relaxing the deny-list without re-reading `_words` is the specific change that breaks this component.** If you want to accept any construct currently rejected up front — command substitution, redirection, process substitution, a pipeline, ANSI-C quoting — the parser can now represent it, but deciding it is *safe* to accept is a separate change with its own evidence. Widening `_deny_shell_grammar`'s accepted character set is not a small change, however small the diff looks.

Two things that make this defence in depth rather than a single point of failure, both of which must be preserved:

- `confirm-commit` independently verifies `HEAD^{tree} == pending_approved_tree` and a clean worktree, so a parser bypass yields a *detected, recoverable* bad commit that enters `RECONCILE` — not a silent unreviewed one.
- The final cumulative review covers the end state regardless of what happened per commit.

A parser failure mode unique to bashlex: a command it cannot parse, or hangs on, must deny — never fall through to an approval. `CommandShapeTimeout` and the `signal.setitimer` deadline in `cmdshape.py` exist for exactly this; see "Interpreter invocation" below.

### The argument channel is not escaped

`skills/implement/SKILL.md` passes `--args "$ARGUMENTS"`. Claude Code substitutes `$ARGUMENTS` textually, without shell escaping, and the body runs through `eval`. A plan path of `x"; id; echo "` therefore executes `id`. This is confirmed, not theoretical.

No change to the body fixes it — double quotes are broken by `"`, single quotes by `'`, and the substitution precedes any shell. The containment is `disable-model-invocation: true` on the skill, which means only the user can supply the path. **Do not remove that flag**, and do not add a second interpolated argument without re-reading this. `commands/arm.py`'s character-set check is still worth keeping, but understand what it does and does not do: it runs after any injected command has already executed, so it protects the loop's state, not the machine.

## Interpreter invocation

These are non-obvious and silently reversible: each one reverts to something that looks correct, passes a casual reading, and reopens a hole the port closed.

1. **Never `python3 -m ocrl`, never a relative path.** Hooks run with the repository under review as `cwd`. `-m` puts `cwd` at `sys.path[0]`, ahead of `PYTHONPATH`, so a repo containing `ocrl/__main__.py` — or merely `json.py` — executes arbitrary code as the gate. Confirmed by experiment. The only sanctioned invocation is `python3 -I "$PLUGIN_ROOT/scripts/ocrl-bootstrap.py" "$@"`, an absolute, trusted path.
2. **`-I` is load-bearing, not a style choice.** It implies `-P` (no cwd or script-directory on `sys.path`), `-E` (no `PYTHON*` environment influence, `PYTHONPATH` included) and `-s` (no user site-packages). Because `-P` drops the script directory too, `ocrl-bootstrap.py` re-derives `sys.path` itself from its own `os.path.abspath(__file__)` — trusted precisely because the shim always passes that path absolute.
3. **`sys.pycache_prefix` is set at runtime, before `ocrl` is imported.** `-I` makes the interpreter ignore `PYTHONPYCACHEPREFIX` (`sys.pycache_prefix` is `None` under `-I`), and `-B` would keep the tree clean at the cost of the bytecode cache on every hook. The bootstrap instead points `sys.pycache_prefix` at `$XDG_CACHE_HOME/opencode-review-loop/pycache` after verifying the directory does not overlap the plugin repo, the reviewed repo, or `cwd` (`_cache_dir_is_safe` in `ocrl-bootstrap.py`). If the directory cannot be created or the check fails, `sys.dont_write_bytecode = True` — never a silent fall-back to writing beside the source.
4. **The shim (`scripts/ocrl.sh`) never `exec`s a hook subcommand.** For `PreToolUse`, Claude Code treats exit 2 as blocking and *any other* non-zero exit as non-blocking — the tool proceeds. A naive `exec python3 …` fails open on a missing interpreter (exit 127, no output) and on an uncaught exception (exit 1, no output). Instead `ocrl_hook_run` captures stdout into a shell variable (never a temp file — nothing extra to place outside the reviewed repo), and only forwards it when the process exited exactly `0`.
5. **On any non-zero exit, everything captured is discarded**, and the shim emits that event's own fallback (`ocrl_hook_fallback`) — never Python's partial output. Appending a fallback after a partial write would yield two concatenated JSON objects, which is not valid JSON, and an unparseable `PreToolUse` response is not a denial. The discriminator is exit status, never empty stdout: `pass` legitimately emits zero bytes and exits 0, and it is the commonest hot-path outcome.
6. **The fallback shape is per-event, not one shape reused everywhere:**

   | Entrypoint | Event | Fallback on shim/interpreter failure |
   | --- | --- | --- |
   | `pretool` | PreToolUse | `permissionDecision: "deny"` |
   | `gate-stop` | Stop | `{"decision":"block","reason":…}` |
   | `confirm-commit` | PostToolUse | `additionalContext` describing the failure — a post-hook cannot deny, the tool call already ran |
   | `posttool-failure` | PostToolUseFailure | nothing at all, matching the entrypoint's own silent behaviour; its only job is clearing a pending approval, and a crash here leaves that pending tree stale rather than granting anything |

   Python mirrors this: an uncaught exception at the top level emits the same fallback for its event, through `hookio`'s `Decided` / `OutputFailure` control-flow exceptions, before exiting 0.
7. **A hung parser must still deny, in two layers.** Inside Python, `cmdshape.py` runs the bashlex parse under a `signal.setitimer` deadline (`PARSE_TIMEOUT_SECONDS`) whose handler raises `CommandShapeTimeout`, which the gate treats as a denial. bashlex is pure Python, so `SIGALRM` is delivered between bytecodes and the parse is genuinely interruptible. Outside Python, the shim runs every hook subcommand under `timeout <N>`, `N` fixed below the timeout Claude Code itself enforces per entrypoint (`skills/implement/SKILL.md`) and only reducible for tests via `OCRL_SHIM_TIMEOUT_*`, never raisable past the ceiling (`ocrl_bounded_timeout`). If the in-process alarm never fires — the interpreter wedged in C — `timeout` returns 124, non-zero, and lands in the same discard-and-fallback path as any other failure.
8. **Every state write is same-directory `os.replace`, never a direct write.** `atomic.py` writes to a temp file in the destination directory and renames over the target only after the full document is serialised and flushed — a failed write leaves the last-good file untouched, unlike the old `jq ... | mv -f`, which could truncate a state file and then unconditionally overwrite good state with the failure. The state root, activation directories, and lockfiles get `0700`; state, pointer and report files get `0600`, applied by comparing-then-`fchmod`/opening with an explicit mode rather than trusting `umask` — including tightening a pre-existing `0755` root left over from before this hardening landed. Mutating paths take an `fcntl.flock` around load → mutate → save, so a `PostToolUse` hook overlapping a user-run `defer` cannot lose an update even though the rename alone only prevents a torn file.

## Hot-path rules

The `PreToolUse` dispatcher runs on **every** tool call, so cost there is multiplied by thousands. Two invariants hold it in place:

- **Read-only tools answer before config or state is loaded.** They are permitted in every state, so `commands/pretool.py` hoists `hooks.tool_is_readonly` above loading config and state. The shell repeated this check inside each denying branch too; those repeats were unreachable and are not reproduced in the port. **If a future state ever needs to deny a read-only tool, remove that hoist first** — and put the per-branch checks back with it, or the deny is unreachable in the other direction.
- **One process per job on the hot path.** Config, state and hook-payload parsing are now in-process `json` calls rather than `jq` subprocesses — that whole axis of process count no longer varies by branch the way it did under Bash. What remains fixed per hook is the shim's own floor: the shebang's `env`, the `bash` shim, the `timeout` wrapper, and `python3` — four processes regardless of branch, plus one more `git rev-parse` when `cwd` is outside the armed worktree. Re-measure with `strace -f -e trace=execve` before and after any change that might add a subprocess here; the README's latency table has the current baseline.

## Layout

| Path | What lives there |
| --- | --- |
| `scripts/ocrl.sh` | the guarded shim registered as every hook's command; probes the interpreter, runs it, fails closed — see "Interpreter invocation" |
| `scripts/ocrl-bootstrap.py` | trusted absolute entrypoint; establishes `sys.path` and `sys.pycache_prefix` before `ocrl` is imported |
| `scripts/ocrl/cli.py` | subcommand dispatch, reached only through the bootstrap |
| `scripts/ocrl/paths.py` | state-directory layout and repository resolution |
| `scripts/ocrl/atomic.py` | durable, private writes for everything the gate persists — same-directory `os.replace`, `0700`/`0600` permissions, `flock` |
| `scripts/ocrl/hookio.py` | hook input parsing and the fail-closed decision emitters (**Rule 2** lives here) |
| `scripts/ocrl/config.py` | config precedence: `OCRL_*` env → repo json → user json → defaults |
| `scripts/ocrl/state.py` | session pointer, `state.json`, effective status incl. TTL |
| `scripts/ocrl/gitsnap.py` | temp-index snapshot, oversized guard, submodule detection |
| `scripts/ocrl/cmdshape.py` | deny-list plus bashlex AST walk deciding whether a commit command may run |
| `scripts/ocrl/globmatch.py` | `[[ $path == $glob ]]` semantics, reimplemented rather than shelled out to |
| `scripts/ocrl/reviewer.py` | bundle building, OpenCode invocation, contract parsing |
| `scripts/ocrl/report.py` | report storage and the text Claude actually sees |
| `scripts/ocrl/commands/` | one module per subcommand group — `arm`, `phases`, `session`, `completion`, `dryrun`, and the four hook entrypoints in `pretool.py`, `posttool.py`, `stop.py`, `hooks.py` |
| `scripts/ocrl/_vendor/bashlex/` | vendored parser; lint-excluded, `_vendor/README.md` records the upstream version and commit |
| `prompts/*.md` | the fixed reviewer prompts — Claude composes none of this |
| `skills/*/SKILL.md` | the five slash commands; `implement` carries the hook registrations |
| `tests/selftest.sh` | the whole black-box acceptance suite; scratch repos, no model calls, language-agnostic on purpose |
| `tests/unit/` | pytest unit tests for the Python modules |
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
*development* dependency only — the plugin's own runtime needs nothing beyond `python3`, the
standard library, and the vendored bashlex under `scripts/ocrl/_vendor/`, and must keep working
straight from a checkout with no install step.

`make test` must pass before any commit. A change to the gate needs a test that **fails on the old code** — a test that only asserts a helper's return value while the end-to-end bypass survives is not a regression test.

`make check` runs fix-capable hooks (markdownlint, prettier, end-of-file-fixer). Check `git status --short` afterwards so a formatter's edits are not mistaken for reviewed input.

### Why the gate does not run under uv

`make test` uses `uv` when it is installed — it resolves `requirements-dev.txt` into a cached environment with no virtualenv to manage, and it works where pip refuses to touch the system interpreter (PEP 668). That is a **developer convenience only**. The gate itself must keep running as `python3 -I "$PLUGIN_ROOT/scripts/ocrl-bootstrap.py"`, and `uv run` must never appear on the hook path. Two measured reasons, both about the same thing — under a hook, the current directory is the repository under review:

- **`uv run` executes code from the reviewed repository.** In a directory containing a `pyproject.toml` with a `backend-path` build backend, `uv run python -I <trusted bootstrap>` ran that backend *before* the bootstrap started. That is the `python3 -m` exploit reintroduced one level up, and the bootstrap's hardening cannot see it.
- **`.python-version` in the reviewed repository redirects the interpreter.** Even with `--no-project`, a repo shipping `.python-version` made uv download CPython 3.9 (27 MB, over the network, inside a hook) and run the gate on it — below the 3.12 floor.

Closing both needs `uv run --no-project --no-config --python <absolute interpreter>` with `UV_PYTHON_DOWNLOADS=never` — at which point uv contributes nothing except latency, and the latency is not small: 122 ms per invocation against 20 ms for `python3 -I`, on a path that runs on every tool call.

**`make check` only sees files git already knows about.** pre-commit enumerates `git ls-files`, so a brand-new file that has never been added is skipped in silence — ruff, mypy and shellcheck report clean on code they never read, and the first real run is the commit hook, after the change has already been reviewed. Run `git add -N` on every new file (intent-to-add is enough; it needs no staged content) before `make check`, and treat a green run that skipped a new module as no run at all.

### Python conventions

- Python 3.12, standard library only at runtime. `requirements-dev.txt` (pytest and friends) is a *development* dependency; the gate itself must keep working straight from a checkout, with no install step.
- `ruff` (`.ruff.toml`, `line-length = 150`, `target-version = "py312"`) and `mypy` (`mypy.ini`, `strict = True`) must both be clean. Both exclude `scripts/ocrl/_vendor` — it is kept diffable against upstream bashlex, not reformatted or typed to this repo's standard.
- `mypy --strict`: every function is typed, including its return type. `disallow_untyped_defs` and `check_untyped_defs` are both on, so an untyped helper is a lint failure, not a gap that slips through on an existing function.
- One module per concern, not one process sourcing globals — `hookio.py`, `config.py`, `state.py`, `cmdshape.py`, `gitsnap.py`, `reviewer.py`, `report.py` each own one layer, and `commands/` holds one module per subcommand group. Cross-module state is passed explicitly (a `Config`, a `State`, a `Hook`), never a module-level global mutated by a caller.
- Hook entrypoints unwind through `hookio`'s `Decided` / `OutputFailure` control-flow exceptions (deliberately `BaseException` subclasses, not `Exception` — see the class docstrings in `hookio.py`), not through a shell-style `exit 0` scattered across branches.
- `__all__` is declared on every module that has one, and is the module's actual public surface — an import from outside that list is a sign the boundary is wrong, not that the list needs extending.

### Adding config

New keys go in `config.DEFAULTS`, in `config.from_env`'s key list with the right type branch, and in the README table. Treat repository config as attacker-controlled: a config change must not be able to execute unreviewed code or silently weaken the active gate.

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
