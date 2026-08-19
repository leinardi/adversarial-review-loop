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

Fresh session in the same repo. Run `/opencode-review-loop:stop` between each, since a failed arm persists `ARM_FAILED` deliberately.

| Invocation | Expected |
| --- | --- |
| `/opencode-review-loop:implement ~/ocrl-step0/plan.md` | arms; banner reports `folded into phase 1: false` |
| `/opencode-review-loop:implement ~/ocrl-step0/plan.md --allow-dirty` | arms; banner reports `folded into phase 1: true` |
| `/opencode-review-loop:implement` (no argument) | `ARMING FAILED`, naming the missing plan |
| `` /opencode-review-loop:implement ~/ocrl-step0/pl`id`an.md `` | `ARMING FAILED … characters that are not safe` |

The first row is the real test of an omitted `$2`: an unmatched positional must arrive as empty rather than breaking the command.

The last row is the **argument-safety probe**. The fixture creates a file whose name literally contains `` `id` ``. The expected result is a clean refusal by the character-set check in `cmd_arm`. If instead you see the output of `id` — a uid/gid line — anywhere in the banner, the harness let a backtick reach a shell. That is a finding worth stopping for, and it is the residual exposure the design documents rather than eliminates.

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
| 2 | `` !`…` `` expansion runs in a skill body |  |
| 1 | all four hook events register |  |
| 5 | `${CLAUDE_PLUGIN_ROOT}` resolves in a hook |  |
| 7 | a hook reads a plan outside the repo |  |
| 4 | session id matches; pre-phase mutation denied |  |
| — | the loop runs end to end against a real model |  |
| 10 | another repo in another session is untouched |  |
| 3 | `$1`, empty `$2`, and a hostile path |  |
| 6 | the block cap responds to the setting |  |

## If A1 fails

Expansion not running in skill bodies is the only outcome that forces a redesign. The fallback:

1. Remove the `` !`…` `` line from `skills/implement/SKILL.md`.
2. Have the body instruct Claude to run `ocrl.sh arm …` as its first action.
3. Add a narrow `pretool` exception for exactly that command, mirroring the existing `set-phases` exception in `cmd_pretool`.

That reintroduces a one-command hole in the pre-activation guard — smaller than the original design's, but not zero, and it must be written to match only the exact arm command shape.

## What the first real run also taught

Anything that writes into the worktree between the gate and the commit — an editor writing back a buffer, an MCP server dropping a state directory, a file watcher — changes the tree out from under the approval and lands you in `RECONCILE`. The gate is behaving correctly when it does this. The fixture gitignores the usual offenders; add your own before arming rather than fighting the reconcile.
