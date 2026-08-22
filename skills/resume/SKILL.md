---
name: resume
description: Continue an already-armed opencode-review-loop plan in a new session (or adjust the pause target, model, or plan in this one), without losing the original baseline or any approvals already recorded.
argument-hint: "[--until N] [--plan <path>] [--allow-dirty] [--abandon-pending] [--model X] [--variant V]"
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

1. **Read `plan.frozen.md`** in the activation directory the banner names. That copy — never the original file on disk — is what the reviewer is given as evidence, and it is *always* what the reviewer evaluates against, even when the banner reports a plan revision: the reviewer does not yet read revised plan content (that disclosure lands in a later phase of this feature). If the banner reports a revision, treat it as recorded but **not yet enforced** — do not implement against a `plan.rev<n>.md` file instead of `plan.frozen.md`, and tell the user the revision has no effect on what gets reviewed yet.
2. **If phases are not frozen yet** (the banner says so), split the plan and freeze it exactly as `/opencode-review-loop:implement` describes, then implement phase 1.
3. **Otherwise, continue straight into the phase the banner names.** The baseline, every prior approval, and the phase list are all carried forward from the original activation — nothing about them is reset by resuming.
4. **Commit each phase** the same way as always:

   ```
   git add -A && git commit -m "…"
   ```

   The commit is intercepted and reviewed exactly as under `/opencode-review-loop:implement`.
5. **If a pause target is set** (the banner names it), stop and report to the user once you reach it — do not continue past it on your own.
6. **When all phases are committed, end your turn.** The Stop gate runs the final cumulative review over the whole activation, from the *original* baseline to `HEAD`.

## Rules while the mode is active

Identical to `/opencode-review-loop:implement`: only `git commit`, or a chain of `git add` / `git status` / `git commit`, may create a commit; `--amend` and partial commits are denied; every phase leaves a clean worktree; you cannot end the mode yourself.

Check state at any time with `/opencode-review-loop:status`, and print any stored review in full with `/opencode-review-loop:report [n]`.
