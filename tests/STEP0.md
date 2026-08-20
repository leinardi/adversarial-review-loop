# Step 0 — the live-session runbook

`tests/selftest.sh` covers everything reachable from a shell. It cannot cover how **Claude Code itself** loads the skill, expands its body, and dispatches its hooks. This document is the procedure for settling that, once, before the mode is trusted on real work.

Budget about an hour, plus a handful of real model calls in session A.

## Before you start

- `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` set to `40` in `settings.json`, and Claude Code restarted since. The variable is read at process start.
- The plugin installed:

  ```text
  /plugin marketplace add ~/Workspace/github/opencode-review-loop
  /plugin install opencode-review-loop
  ```

- **A working Bash sandbox, or none at all.** Arming runs through Claude Code's Bash sandbox at prompt-expansion time, so if the sandbox cannot start, `ocrl arm` never runs. On Ubuntu-family kernels `kernel.apparmor_restrict_unprivileged_userns=1` blocks the unprivileged user namespace the sandbox needs, and every command dies with `apply-seccomp: write /proc/self/setgroups`. Check with `unshare --user --map-root-user true`.

  Two independent things must hold: the sandbox must be able to start, **and** it must permit writes to the state root. A `denyWrite` covering `~/` blocks `$XDG_STATE_HOME/opencode-review-loop`, so arming fails on its first write even when the sandbox works.

- A throwaway fixture — never point this at work you care about, since the exercise is about provoking denials and bad commits:

  ```console
  tests/step0-fixture.sh          # creates ~/ocrl-step0
  ```

**Two escape hatches, worth knowing before you need them.** Once armed, the session's tool calls are gated, so if a run wedges: `/opencode-review-loop:stop` disarms it, and failing that `rm -rf ~/.local/state/opencode-review-loop` removes all state. `/clear` also disarms, because the hooks are session-scoped.

Already settled empirically against `opencode 1.18.18` and `openai/gpt-5.6-sol`, so they are not in the runbook: `-f` attachments outside the repo are reachable with the pattern-scoped `external_directory`, and OpenCode's permission patterns are last-match-wins. Static checks against Claude Code 2.1.235 confirm every frontmatter key and hook event used here exists in the binary.

---

## Session A — the main run

```console
cd ~/ocrl-step0/repo && claude
```

### A1. Arm, and watch what happens before Claude gets a turn

```text
/opencode-review-loop:implement ~/ocrl-step0/plan.md
```

**Expect** a block beginning `**opencode-review-loop is ARMED for this worktree.**`, naming the repository, a baseline tree, an activation commit, and the reviewer model — *before* Claude says anything.

This is **item 2, and it is load-bearing.** It is the one result that can invalidate the architecture.

**Settled 2026-08-19: it passes.** The expansion ran, `${CLAUDE_PLUGIN_ROOT}` resolved, and `${CLAUDE_SESSION_ID}` interpolated to a real session id. The evidence came from a *failure* of the arm command rather than a success: the error quoted the fully-substituted command back, which is itself proof the expansion happened. The architecture holds; re-check only if the harness changes.

- **Literal `` !`…` `` text appears instead** → expansion does not run in skill bodies. Stop the runbook and see "If A1 fails" below.
- **Nothing appears and Claude immediately can't do anything** → same conclusion. Arming did not run, but the hooks did register, so the dispatcher denies everything. That is the fail-closed design behaving correctly, not a bug.

Confirm the state landed:

```console
ls ~/.local/state/opencode-review-loop/worktrees/*/*/
```

You should see `state.json` and `plan.frozen.md` — that is **item 7**, a hook script reading a plan from outside the repo.

### A2. Hook registration — item 1

```text
/hooks
```

**Expect** four entries pointing at `scripts/ocrl.sh`: `PreToolUse` (no matcher), `PostToolUse` (`Bash`), `PostToolUseFailure` (`Bash`), `Stop`. That `${CLAUDE_PLUGIN_ROOT}` resolved at all is **item 5** — a path that failed to expand would show up as a missing command on every tool call.

If `PostToolUseFailure` is absent, the loop still holds: a stale pending approval is cleared by the next gate anyway. Drop the registration to avoid a dead entry.

### A3. The silent-pass check — item 4, the dangerous one

The phases are not frozen yet, so **every mutation must be denied**. Ask Claude, in plain language:

```text
Add a docstring to greet() in greet.py.
```

Read the outcome carefully, because three different things look similar:

| What you see | What it means |
| --- | --- |
| Denied, message names `set-phases` | **Correct.** State was found, so the expansion-time session id matches the hooks' `session_id` |
| Denied, message says the activation state **is missing** | Ids differ, or A1 silently failed. Investigate before continuing |
| **Claude edits the file** | **Critical.** The gate is not running at all. Stop and diagnose |

The third row is the failure this item exists to catch: a session-id mismatch makes the gate *silently pass* rather than fail loudly, which is the one failure mode the whole design is meant to exclude.

Cross-check the ids directly:

```text
/opencode-review-loop:status
```

The `session:` line must match the directory name under `worktrees/*/` from A1.

### A4. Run the loop for real

Let Claude do what the skill tells it: propose two phases, run `set-phases`, implement phase 1, commit.

**Expect**, in order: mutations become allowed once the phases are frozen; the commit is intercepted and takes a minute or two; either an approval and a phase advance, or a denial carrying findings. If it's denied, let Claude fix and re-commit — that cycle *is* the product.

Watch for two specific things:

- After a verified commit, Claude should be told to continue into phase 2 **without ending its turn**.
- After the last phase, ending the turn should trigger the final cumulative review.

Expect the reviewer to be strict. In my own end-to-end run it rejected two successive attempts on scope grounds before approving, which is the gate working, not a malfunction.

### A5. Isolation — item 10

While session A is still armed, open a second terminal in **any other repository** and run `claude` there. Do something that mutates a file.

**Expect** no gate messages, no denials, no latency. The dispatcher exits early when the session pointer is missing or the worktree does not match.

### A6. Close out

```text
/opencode-review-loop:stop
```

---

## Session B — argument handling (item 3)

One session, four invocations back to back. No `/stop` needed between them: re-arming overwrites the state for that session, and `implement` runs at expansion time so it is never gated. Press Esc to interrupt Claude between attempts — only the banner matters.

If the fixture has already been through an end-to-end run, rewind it first (`git -C ~/ocrl-step0/repo reset --hard <seed>`), or Claude will correctly report that the plan is already implemented and defer instead of working.

| Invocation | Expected |
| --- | --- |
| `/opencode-review-loop:implement ~/ocrl-step0/plan.md` | arms; banner reports `folded into phase 1: false` |
| `/opencode-review-loop:implement ~/ocrl-step0/plan.md --allow-dirty` | arms; banner reports `folded into phase 1: true` |
| `/opencode-review-loop:implement` (no argument) | `ARMING FAILED`, naming the missing plan |
| `` /opencode-review-loop:implement ~/ocrl-step0/pl`id`an.md `` | `ARMING FAILED … characters that are not safe` |

**Settled 2026-08-19, and it changed the design.** The skill body originally passed `"$1" "$2"`. Claude Code's positional substitution is **0-based** — `$N` resolves `s[N]` of a zero-indexed array, so `$1` is the *second* argument — and an out-of-range `$N` is left in the body verbatim, where the expansion shell then turns it into the empty string. A single-argument invocation therefore armed with an empty plan path and failed closed with "no plan path was supplied".

The body now passes `--args "$ARGUMENTS"` as one string and splits it in `ocrl_split_args`, which also keeps plan paths containing spaces intact. The same routine (`wPt`) confirms Claude Code does **not** shell-escape substituted arguments, which is why the character-set check in `cmd_arm` and the probe below both matter.

### The argument-safety probe, and what it established

`$ARGUMENTS` is substituted into the skill body **textually and without shell escaping**, and the body then runs through `eval`. Claude Code acknowledges this internally: an import-fallback message in the binary notes that Gemini shell-escapes `{{args}}` inside `!{…}` while Claude Code's `$ARGUMENTS` substitution does not.

Confirmed empirically on 2026-08-20, by reproducing the substitute-then-`eval` sequence:

| `$ARGUMENTS` | Result |
| --- | --- |
| `~/ocrl-step0/pl`​`id`​`an.md` | the backtick also closes the outer `` !`…` `` delimiter, truncating the command into a bash syntax error — the arm never runs |
| `x"; id; echo "` | **`id` executes.** The quote closes `--args "…"`, the semicolon starts a new command |

So the live backtick probe fails safe by accident, not by design: it breaks the syntax before it can run. The quote-and-semicolon form is the real test, and it injects.

**This is not fixable inside the plugin.** No quoting of `$ARGUMENTS` in the body helps — double quotes are escaped by `"`, single quotes by `'` — because the substitution happens before any shell sees it. It is a property of `` !`…` `` expansion that every plugin interpolating `$ARGUMENTS` into a command shares.

What bounds it here: `implement` is `disable-model-invocation: true`, so Claude cannot invoke the skill. The only party who can supply a hostile path is the person typing the slash command. The realistic risk is a pasted path from an untrusted source, not a compromised agent. The character-set check in `cmd_arm` still refuses such a path, but it runs *after* the injected command has already executed, so it limits the review loop's state rather than preventing execution.

Worth reporting upstream if you want it changed.

---

## Session C — the Stop-block cap (item 6)

Worth doing because of how the caps interact. Our own `max_stop_blocks` (default 3) counts only **no-progress** blocks, so a productive multi-phase run resets it constantly — but Claude Code's cap counts **consecutive** blocks regardless of progress. A five-phase run can therefore hit the host cap without ever tripping our escalation, and the host's override **ends the turn**, which reads as success.

1. Temporarily set `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` to `3`. Restart.
2. Arm, and end your turn repeatedly without doing any work.
3. **Expect** the override message (`A hook blocked the turn from ending N consecutive times`) at 3, not 8.
4. Set it back to `40`. Restart.

If it has no effect, the residual limit stands as documented in the README: our `max_stop_blocks` escalates to `needs-human` first in the no-progress case, so the loop reaches a loud stop rather than a silent one — but a long productive run stays exposed.

---

## Record the outcome

| Item | Check | Result |
| --- | --- | --- |
| 2 | `` !`…` `` expansion runs in a skill body | **pass** (2026-08-19) |
| 1 | all four hook events register | **pass** (2026-08-20) |
| 5 | `${CLAUDE_PLUGIN_ROOT}` resolves in a hook | **pass** (2026-08-19) |
| 7 | a hook reads a plan outside the repo | **pass** (2026-08-19) |
| 4 | session id matches; pre-phase mutation denied | **pass** (2026-08-20) |
| — | the loop runs end to end against a real model | **pass** (2026-08-20) |
| 10 | another repo in another session is untouched | outstanding |
| 3 | arguments, and a hostile path | **pass**, with a confirmed injection surface (2026-08-20) |
| 6 | the block cap responds to the setting | outstanding |

### The first clean end-to-end run, 2026-08-20

Two phases against `openai/gpt-5.6-sol`, roughly 80 seconds in total: arm at expansion, `set-phases` via the one permitted Bash call, mutations allowed only after the phases were frozen, both commits intercepted and reviewed, `confirm-commit` verifying each tree and advancing the phase, then the final cumulative review completing the activation and disarming the mode.

Three real reviews landed in `reports/` with their bundles. The reviewer globbed the repository, read `greet.py` and `README.md`, read the diff from the bundle under `$XDG_STATE_HOME`, and emitted the marker block correctly — worth checking `bundles/NNN/reviewer.out` yourself the first time, since an approval is only as good as the evidence the reviewer actually opened.

Items 1 and 4 fall out of this run: all four hooks fired, and the pre-phase `Read` of the frozen plan was permitted while the gate still denied mutations, which is only possible if the expansion-time session id matches the one the hooks receive.

## If A1 fails

Expansion not running in skill bodies is the only outcome that forces a redesign. The fallback:

1. Remove the `` !`…` `` line from `skills/implement/SKILL.md`.
2. Have the body instruct Claude to run `ocrl.sh arm …` as its first action.
3. Add a narrow `pretool` exception for exactly that command, mirroring the existing `set-phases` exception in `cmd_pretool`.

That reintroduces a one-command hole in the pre-activation guard — smaller than the original design's, but not zero, and it must be written to match only the exact arm command shape.

## What the first real run also taught

Anything that writes into the worktree between the gate and the commit — an editor writing back a buffer, an MCP server dropping a state directory, a file watcher — changes the tree out from under the approval and lands you in `RECONCILE`. The gate is behaving correctly when it does this. The fixture gitignores the usual offenders; add your own before arming rather than fighting the reconcile.
