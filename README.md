# adversarial-review-loop

A Claude Code plugin that implements an agreed plan **phase by phase, with an external adversarial review as an enforcement gate**. Claude cannot commit a phase until a separate reviewer — a second `claude` process by default, or [OpenCode](https://opencode.ai) — has reviewed the exact tree it is about to commit and passed it.

The reviewer is *external* in the sense that matters: a fresh process, isolated from your ambient plugins, skills and MCP servers, given the diff as evidence and a fixed prompt, with no way to write anything and no memory of the session that wrote the code.

The review is not advice Claude may weigh up. It is a `PreToolUse` gate on the commit itself: findings come back as a denial, and the commit only proceeds once they are resolved.

```text
/adversarial-review-loop:implement plan.md
   -> arms (freezes the baseline and the plan) before Claude has a turn
   -> Claude proposes phases and freezes them
   -> phase N implemented -> git commit -> INTERCEPTED
        snapshot the whole working state into a tree
        the reviewer reads the delta since the last approved tree
        approved -> commit proceeds -> phase advances
        findings -> commit denied, findings returned inline
   -> all phases committed -> turn ends -> COMPLETE
        (plus a final cumulative review first, if final_review is on)
```

## 📋 Requirements

- Claude Code 2.1.x or newer (the plugin registers `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionStart` and `UserPromptSubmit` hooks in `hooks/hooks.json`)
- the reviewer CLI on `PATH` and authenticated: `claude` by default (you already have it), or [`opencode`](https://opencode.ai) with `harness` set to `opencode`. Arming refuses if the configured one is missing.
- `python3` 3.12 or newer — the gate itself. No install step: the standard library plus a vendored, lint-excluded copy of [bashlex](https://github.com/idank/bashlex) is everything it needs.
- `git`, `bash` 4.4+ and `timeout` (GNU or uutils coreutils)

## 📦 Install

Add this repository as a local plugin marketplace, then install the plugin:

```console
$ claude
> /plugin marketplace add ~/Workspace/github/adversarial-review-loop
> /plugin install adversarial-review-loop
```

Then raise the Stop-hook block cap so a long loop is not cut short. Claude Code caps consecutive Stop blocks (default 8) and **overrides by ending the turn**, which reads as success — so give the loop headroom in `settings.json` and restart Claude Code, since the variable is read at process start:

```json
{
  "env": {
    "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "40"
  }
}
```

## 🚀 Quick start

1. Write a plan as a Markdown file — whatever describes the work; there is no required format.
2. Run `/adversarial-review-loop:implement plan.md`. Arming happens before Claude gets a turn: the baseline and the plan are frozen, and the reviewer is probed for reachability.
3. Claude splits the plan into phases and freezes the list. Every mutation is denied until it does.
4. Claude implements phase 1 and runs `git add -A && git commit -m "…"`. That commit is intercepted, reviewed, and either allowed through or denied with the findings inline. Repeat until the phase passes, then on to phase 2.
5. `/adversarial-review-loop:status` at any time; `/adversarial-review-loop:report [n]` for a review in full.

Pause after phase 5 with `--until 5`, or mid-run with `/adversarial-review-loop:pause`. Pick a plan back up in a new session with `/adversarial-review-loop:resume` — never a second `implement`, which re-baselines and throws away every approval.

## 💻 Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/adversarial-review-loop:implement <plan.md> [--allow-dirty] [--until N] [--harness H] [--model X] [--variant V] [--guide <path>]` | you | Arms the loop for this worktree and starts the phased implementation |
| `/adversarial-review-loop:resume [--until N] [--plan <path>] [--guide <path>] [--replan] [--allow-dirty] [--abandon-pending] [--harness H] [--model X] [--variant V]` | you | Continues an armed activation — in a new session, or adjusts it in this one — without losing the baseline or any approval |
| `/adversarial-review-loop:status` | anyone | Current state: phase, baseline, approvals, counters, stored reports |
| `/adversarial-review-loop:report [n]` | anyone | Prints a stored review in full, untruncated |
| `/adversarial-review-loop:pause [N \| 0 \| all]` | you | Moves the pause target without a re-arm: with no argument, the loop finishes and commits the phase it is on and then stops instead of continuing |
| `/adversarial-review-loop:finish` | you | Runs the final cumulative review now, even with phases outstanding — and regardless of `final_review`, which makes it the way to get one on a default install |
| `/adversarial-review-loop:accept [reason]` | you | Manually approves the current working tree for the current phase, without a review |
| `/adversarial-review-loop:stop` | you | Leaves the mode. Nothing is reverted |
| `/adversarial-review-loop:config [<key> <value> [--repo]] [<key> --unset [--repo]]` | you | Reads or writes the review-loop configuration. Unrelated to any armed activation — never registers the gate |

Every command except `status` and `report` is `disable-model-invocation: true`: **Claude can never arm, resume, finish, stop, accept, pause, or run `config` itself** — only the exact slash command does, and no natural-language phrasing invokes any of them. A mode whose whole point is enforcement must not be self-enabling; the cost is that "use the review loop for this" does nothing. That stops the *command*, not ordinary file edits to what it writes — see the honest-agent bar under [Known limitations](#-known-limitations).

## 🔧 Configuration

Resolution order: `ARL_*` environment → repo `.adversarial-review-loop.json` → `$XDG_CONFIG_HOME/adversarial-review-loop/config.json` → defaults. Environment variables are the upper-cased key with an `ARL_` prefix (`ARL_BLOCK_SEVERITY`, `ARL_MODEL`, …).

The keys most people touch:

| Key | Default | Purpose |
| --- | --- | --- |
| `harness` | `claude-code` | which reviewer CLI runs the review — `claude-code` or `opencode` |
| `model` | the harness's own (`opus` for `claude-code`, `openai/gpt-5.6-sol` for `opencode`) | probed for reachability at arm time |
| `variant` | unset | reasoning effort — `--variant` on OpenCode, `--effort` (`low`…`max`) on Claude Code |
| `block_severity` | `medium` | blocks when `actionable=yes AND severity >= this` |
| `verify_cmd` | unset | run by the hook, output attached to the review as evidence |
| `review_guide` | unset | a Markdown file spliced into the reviewer's prompt as repo-specific guidance — see [configuration.md](docs/configuration.md#repo-specific-review-guidance) |
| `ignore_globs` | `[]` | paths whose sole change skips a review. A full bypass, not a relaxation |
| `final_review` | `false` | run the final cumulative review at `Stop` |
| `ttl_hours` | `24` | after this, gates block and ask for a re-arm — `resume` is usually the fix, not `implement` |
| `timeout_sec` | `900` | per review run |

```json
{
  "model": "openai/gpt-5.6-sol",
  "variant": "high",
  "verify_cmd": "make test",
  "ignore_globs": ["CHANGELOG.md", "docs/**"]
}
```

Every key, the full precedence rules, and what a review actually costs are in [docs/configuration.md](docs/configuration.md). State lives under `$XDG_STATE_HOME/adversarial-review-loop/`: **no hook, and nothing Claude can invoke itself, ever writes inside the repository under review** — the one exception is `config <key> <value> --repo`, an explicit user-only write.

## ❓ FAQ

Each question links to its full answer.

- **[How do I start?](docs/faq.md#how-do-i-start-using-the-plugin)** Install, write a plan `.md`, run `/adversarial-review-loop:implement plan.md`.
- **[How do I stop partway through?](docs/faq.md#how-do-i-stop-partway-through-a-plan)** <kbd>Esc</kbd> stops the turn at once, mid-phase. To stop at a *clean* boundary instead — current phase finished, reviewed and committed — press <kbd>Esc</kbd>, run `/adversarial-review-loop:pause`, then `continue`.
- **[I quit / rebooted / `/clear`ed mid-phase — how do I pick it back up?](docs/faq.md#picking-up-where-you-left-off)** Same session id (`claude --resume`, or `/resume` back to it): just `continue`. New session: `/adversarial-review-loop:resume --allow-dirty`. `/adversarial-review-loop:status` tells you which you're in.
- **[Can I implement only part of a plan?](docs/faq.md#can-i-implement-only-part-of-a-plan)** `--until N` on `implement` or `resume`; carry on later with `resume --until 0`.
- **[How do I customise what the reviewer looks for?](docs/faq.md#how-do-i-customise-what-the-reviewer-looks-for)** `review_guide` — a Markdown file added to the reviewer's prompt. It cannot change the contract or what blocks.
- **[The reviewer keeps finding new things and the phase never converges.](docs/faq.md#the-reviewer-keeps-finding-new-things-and-the-phase-never-converges-what-now)** `/adversarial-review-loop:accept [reason]` approves the current tree without another review, and records that it did.

More — `RECONCILE`, `NEEDS_HUMAN`, rate limits, cost, running your tests as evidence, revising a plan mid-run — in [docs/faq.md](docs/faq.md).

## 🚧 Known limitations

- **Honest-agent bar.** Claude could commit through a wrapper script, abuse `defer`, or edit `.adversarial-review-loop.json` directly — `disable-model-invocation` blocks the `config` *command*, not ordinary edits to the file it writes. Policy written there **can** weaken the gate silently: `ignore_globs: ["**"]` skips the reviewer call on every commit, a raised `block_severity` stops findings from blocking, and `final_review false` removes the cumulative backstop. None of these run unreviewed code; all of them change what the gate does. Nothing here defends against a deliberately hostile agent, and this design does not pretend to — see [security.md](docs/security.md).
- **The Stop-block cap is residual.** Continuation pressure from `PostToolUse`, progress-aware counting and `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` mitigate it, but a run that repeatedly ends its turn without progress can still exhaust the cap — and exhaustion ends the turn.
- **A hostile plan path can inject a shell command.** `$ARGUMENTS` is substituted into the skill body textually, without shell escaping, and the body is then `eval`ed — so a path like `x"; id; echo "` runs `id`. Confirmed empirically, and not fixable inside the plugin: the substitution happens before any shell sees it. What bounds it is that `implement` is `disable-model-invocation: true`, so only the person typing the slash command can supply the path. Do not paste plan paths from untrusted sources.
- **Binding is session-scoped.** An activation belongs to one session at a time, so `/clear`, a crash, or a fresh `claude` leaves you in a session that is not bound to it. Nothing is disarmed and nothing is lost — the activation stays armed and enforcing, and the unbound session is simply denied every mutation *in that worktree* until `/adversarial-review-loop:resume` binds it, carrying the baseline and every approval across. Returning to the same session (`claude --resume`) needs no command at all. The cost is that a second session resuming the worktree retires the first for good — see [edge-cases.md](docs/edge-cases.md#clear-crashes-and-quitting-unbind-a-session-they-do-not-disarm-an-activation).
- **Cost and latency.** Every phase costs at least one full-model review, and a denied commit blocks the session for the length of the review. The `PreToolUse` hook itself adds ~111 ms to *every* tool call — the measurements are in [architecture.md](docs/architecture.md#what-the-hot-path-costs).

## 📚 Documentation

| Page | For | Covers |
| --- | --- | --- |
| [how-it-works.md](docs/how-it-works.md) | anyone, no engineering background needed | What problem this solves and why, in plain language |
| [faq.md](docs/faq.md) | anyone using it day to day | The questions that come up first, answered short |
| [configuration.md](docs/configuration.md) | anyone running the loop day to day | Every setting, precedence, cost, examples |
| [architecture.md](docs/architecture.md) | engineers working on or integrating with the plugin | Components, data flow, the state machine, what blocks, on-disk layout |
| [edge-cases.md](docs/edge-cases.md) | anyone debugging unexpected behaviour | What happens when things go sideways, and why |
| [security.md](docs/security.md) | anyone assessing whether this is safe to trust | The threat model, what is and is not enforced, and why |

## 🔨 Development

```console
make test                    # 300+ acceptance assertions against scratch repos, plus the Python unit tests; no model is called
make test-filter FILTER=stop # one section
make dry-run                 # print the exact reviewer argv and prompt, without invoking it
make check                   # pre-commit (shellcheck, yamllint, markdownlint, …)
```

The selftest drives the hook entrypoints with synthetic payloads and replaces the reviewer with `tests/fixtures/fake-reviewer.sh` (`ARL_REVIEWER_CMD`), so loop logic costs nothing to iterate on. It covers the snapshot layer, the command-shape table, every arm-failure mode, the fail-closed guards, commit divergence and reconcile, the findings cap, the Stop accounting, and the TTL.

[`AGENTS.md`](AGENTS.md) is the contract any change to this project has to honour — the five non-negotiable rules, the invariants, and the hazards that silently reopen a closed hole if reverted. Before the first real run, work through [`tests/STEP0.md`](tests/STEP0.md): the harness assumptions that only a live Claude Code session can settle.

## 📄 Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

The gate parses commands with [bashlex](https://github.com/idank/bashlex), which is GPLv3 and is vendored under `scripts/arl/_vendor/` so the plugin works straight from a checkout with no install step. See [that directory's README](scripts/arl/_vendor/README.md) for the version, the upstream commit, and the one change made to it.
