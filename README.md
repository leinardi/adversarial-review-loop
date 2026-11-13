# adversarial-review-loop

A Claude Code plugin that implements an agreed plan **phase by phase, with an adversarial review of
each phase before Claude moves on**. You give it a plan; Claude records the plan's phases,
implements the first, asks for a review, fixes what the reviewer finds, and goes on to the next
phase only once the review approves. A plan may span several git repositories and plain folders.

The reviewer is a fresh agent every round: a hidden subagent by default, or
[OpenCode](https://opencode.ai). It reads the phase's whole change, every file in every workspace
root since the last approval, untracked files included. It is told what the earlier rounds found.
It never edits anything and never re-runs the checks Claude already ran.

```text
/arl-implement plan.md
   -> Claude reads the plan and records its phases (set_phases)
   -> phase N implemented, checks run -> review
        the engine snapshots every root and builds the bundle (the diff since the last approval)
        a fresh reviewer reads it and answers in a strict contract
        findings -> Claude fixes them and calls review again
        approved -> Claude commits each changed repository -> the loop prompts phase N+1
   -> last phase approved (plus a final cumulative review, if asked) -> the run is complete
```

**v4 enforces nothing.** It is a plan-and-go loop, not a gate: Claude could ignore a finding, and the
plugin does not try to stop it. What it gets right is the review itself. The reviewer sees exactly
the phase's changes, its verdict is parsed strictly, and no failure is ever reported as an approval.
[docs/philosophy.md](docs/philosophy.md) says why. v3, the enforcement gate, is kept at the
[`adversarial-review-loop--v3.0.1`](https://github.com/leinardi/adversarial-review-loop/tree/adversarial-review-loop--v3.0.1)
tag.

## 📋 Requirements

- Claude Code 2.1.323 or newer, with mods enabled. The plugin is a hooks module (`hooks/mod.ts`); a
  session that loads no mods (`--safe-mode`) leaves the plugin doing nothing.
- `python3` 3.12 or newer, which runs the engine with the standard library alone. **On macOS the
  bundled `/usr/bin/python3` is 3.9 and will not do**; install one from Homebrew or python.org.
- `git`.
- Only for `harness: opencode`: [`opencode`](https://opencode.ai) 2.2.7 or newer on `PATH`,
  authenticated. Earlier releases take flags 2.x removed, so every review there fails.

Claude Code's sandbox needs no exception: the mod and the engine run outside it.

## 📦 Install

```console
$ claude
> /plugin marketplace add leinardi/adversarial-review-loop
> /plugin install adversarial-review-loop
```

## 🚀 Quick start

1. Write the plan as a Markdown file, with its phases in order. Name the repositories it touches by
   path where you can: an existing git repository the plan names becomes a workspace root at the
   start. A plain folder joins with `--root`, the `roots` setting, or when Claude writes into it.
2. In the folder you want the run to live in, run `/arl-implement plan.md`.
3. Watch. The band above the prompt shows the phase, the round and what the loop is doing. The
   review pane shows the run in a few lines: the phases around the current one, the roots and
   the latest review's blocking findings. `/arl-status` opens it.

Claude asks for a review after each phase and commits each repository it changed once the review
approves. When the loop needs you (the same findings keep coming back, the reviewer keeps failing,
a file is too large to review, a folder joined with changes already in it), it asks, or headless it
tells Claude to stop and tell you.

## 💻 Commands

| Command | What it does |
| --- | --- |
| `/arl-implement [--new] [--root DIR]... <plan.md>` | start a run. `--root` adds a folder the plan does not name; `--new` archives the active run under `.arl/runs/` |
| `/arl-resume` | continue the run in this session: unpause, clear a decision the run waited on, abandon a review nothing is running |
| `/arl-status` | open the review pane, and show the run with every phase and every root's path |
| `/arl-phases` | list every phase of the run |
| `/arl-report [N]` | the newest stored review report, or the N-th, in a pane of its own |
| `/arl-pause` | stop the run after the current phase, until `/arl-resume`: the loop prompts nothing, and a review Claude asks for still runs and tells it to stop there |
| `/arl-accept` | approve the current phase (or the final review) as it stands, without a review |
| `/arl-finish` | require a final review of the whole run before it completes |
| `/arl-stop` | end the run |
| `/arl-config [<key> <value> [--repo] \| <key> --unset [--repo]]` | show the configuration, or set or unset one key |
| `/arl-remove-root <alias or path>` | drop a workspace root registered by mistake |

The `arl-` prefix is not decoration: Claude Code ships `/status`, `/resume` and `/config`, and
refuses a plugin command of the same name.

Claude itself uses four tools the plugin registers: `set_phases`, `add_root` (before it changes a
folder outside every root through Bash), `review` and `clarify` (one question to the reviewer about
its findings).

## 🔧 Configuration

The settings most people touch:

| Key | Default | What it does |
| --- | --- | --- |
| `harness` | `subagent` | who reviews: a Claude Code subagent, or `opencode` |
| `model` | the session's subagent default | the reviewer's model |
| `effort` | the agent's default | the subagent reviewer's effort, `low` to `max` |
| `verify_cmd` | unset | the checks Claude is told to run before each review, named in every prompt |
| `block_severity` | `medium` | an actionable finding at or above this blocks the approval |
| `final_review` | `false` | end every run with a review of all its changes |
| `auto_compact` | `false` | compact the conversation before each phase that follows an approval: Claude is told to end its turn after the approval, and the loop compacts, then prompts the next phase |

Every key, the layers (user file, the launch folder's `.adversarial-review-loop.json`, `ARL_*`
variables) and the project's own review skill are in [docs/configuration.md](docs/configuration.md).

## 🚧 Known limitations

- **Bash writes outside every root are not seen.** Edit and Write register the folder they write
  to; Claude is told to call `add_root` before changing anything else through Bash, but nothing
  checks that it did. A change nobody registered is a change nobody reviews.
- Paths the plan names become roots at the start only inside a git worktree under your home folder
  or beside the launch folder, with no hidden folder on the way (`/srv/api` does not count). Any
  other folder joins through `--root`, the `roots` setting, `add_root`, or a write into it.
- Launching inside a larger repository makes that whole repository a root.
- Repositories nested deeper than the scan depth, inside `node_modules`, or in a folder their
  container ignores are not discovered.
- A folder that joins after the run started is checked by timestamps: if anything in it changed
  since the start, you are asked for its baseline. `cp -p` and similar tools can evade the check.
  Writing a new subfolder of a folder outside every root registers the subfolder; a later write
  beside it registers the folder, and you are asked once for its baseline although nothing went
  unreviewed.
- Writes into scratch places (`/tmp`, `/var/tmp`, `$TMPDIR`, `~/.claude`) register nothing, unless
  they share a top-level folder with a root, or go through `add_root` or `/add-dir`.
- A repository nested in another root cannot be removed, only given a baseline.
- `/arl-config --repo` writes `.adversarial-review-loop.json` into the launch folder, so it shows
  up in that phase's diff.
- The failure budget makes the loop stop prompting Claude, but a `review` Claude calls anyway still
  runs.
- A subagent reviewer that times out cannot be stopped (Claude Code has no call for it): it runs on
  in the background, denied every tool, and its late answer is never recorded or shown to Claude.

## 📚 Documentation

[docs/](docs/README.md) has how it works, the configuration, the FAQ, the design philosophy and
the architecture.

## 🔨 Development

```console
make dev-deps     # the pinned development dependencies, once per checkout
make test         # the engine's unit tests and the launcher smoke test; no model is called
make test-mod     # the mod's tests under `claude plugin test`
make check        # pre-commit: ruff, mypy, eslint, tsc, shellcheck, markdownlint, licence headers, …
```

[`AGENTS.md`](AGENTS.md) is the contract a change to this project must honour, and
[`tests/STEP0.md`](tests/STEP0.md) records the Claude Code behaviour the design rests on, measured
in live sessions.

## 📄 Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
