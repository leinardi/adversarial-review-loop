---
name: resume
description: Continue an already-armed opencode-review-loop plan in a new session (or adjust the pause target, model, or plan in this one), without losing the original baseline or any approvals already recorded.
argument-hint: "[--until N] [--plan <path>] [--replan] [--allow-dirty] [--abandon-pending] [--harness H] [--model X] [--variant V]"
disable-model-invocation: true
user-invocable: true
hooks:
  PreToolUse:
    - hooks:
        - type: command
          command: "${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh"
          args: ["pretool"]
          timeout: 1200
          statusMessage: "OpenCode review gate"
  PostToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh"
          args: ["confirm-commit"]
          timeout: 60
  PostToolUseFailure:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh"
          args: ["posttool-failure"]
          timeout: 30
  Stop:
    - hooks:
        - type: command
          command: "${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh"
          args: ["gate-stop"]
          timeout: 1800
          statusMessage: "OpenCode review loop: end-of-turn gate"
---

# Resume the review loop

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh resume --session "${CLAUDE_SESSION_ID}" --args "$ARGUMENTS"`

## What just happened

The block above is the output of resuming, which ran **before you had a turn**. It is authoritative: it reports whether an activation for this worktree was picked back up, what phase it is on, and whether the plan changed since it was last frozen.

**If it says resume failed, stop.** Every file mutation and every commit in this worktree is denied until the reason is resolved. Report it to the user; do not implement anything.

Hooks identical to `/opencode-review-loop:implement` are registered for this session the moment this skill runs, whether or not the resume itself succeeds — the gate is enforcing from your very first tool call either way.

## Your job, in order

1. **Read the frozen plan the banner names** — `frozen plan:` in the banner, inside the activation directory. That copy — never the original file on disk — is what the reviewer is given as evidence. When the banner reports more than one plan revision, the file it names is the **active** one (a `plan.rev<n>.md`, not necessarily `plan.frozen.md`), and it is what the reviewer actually evaluates against from this resume onward; every earlier revision is still disclosed to the reviewer for context. Always implement against the file the banner names, whichever revision it is.
2. **If phases are not frozen yet** (the banner says so), split the plan and freeze it exactly as `/opencode-review-loop:implement` describes, then implement phase 1.
3. **If the banner says `--replan` was granted**, redefine only the phases it names (the current one onward — earlier phases are immutable and already committed) by reading the active plan and running `set-phases` with one `--phase` per replacement phase, exactly as the banner's command shows. Every other mutation is denied until that command has run. Then continue into the (possibly renamed) current phase.
4. **Otherwise, continue straight into the phase the banner names.** The baseline, every prior approval, and the phase list are all carried forward from the original activation — nothing about them is reset by resuming.
5. **Commit each phase** the same way as always:

   ```
   git add -A && git commit -m "…"
   ```

   The commit is intercepted and reviewed exactly as under `/opencode-review-loop:implement`.
6. **If a pause target is set** (the banner names it), stop and report to the user once you reach it — do not continue past it on your own.
7. **When all phases are committed, end your turn.** The Stop gate sweeps anything unreviewed and then completes the activation. If `final_review` is enabled — by default it is not — it first runs a cumulative review over the whole activation, from the *original* baseline to `HEAD`.

## Rules while the mode is active

Identical to `/opencode-review-loop:implement`: only `git commit`, or a chain of `git add` / `git status` / `git commit`, may create a commit; `--amend` and partial commits are denied; every phase leaves a clean worktree; you cannot end the mode yourself.

When a blocking finding is ambiguous or contradicts an earlier round, ask `${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh clarify --question "…"` instead of guessing — one prose question against the review that just ran, no new round, and the denial names how many questions are left.

Check state at any time with `/opencode-review-loop:status`, and print any stored review in full with `/opencode-review-loop:report [n]`.
