---
name: implement
description: Implement an agreed plan with an enforced OpenCode adversarial review loop. Every phase commit is gated on an external review that must pass before the commit proceeds.
argument-hint: "<path-to-plan.md> [--allow-dirty]"
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
          statusMessage: "OpenCode final review"
---

# Implement with an enforced review loop

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh arm --session "${CLAUDE_SESSION_ID}" --args "$ARGUMENTS"`

## What just happened

The block above is the output of arming, which ran **before you had a turn**. It is authoritative: it reports whether the review loop is armed, and it is the only place the baseline was frozen.

**If it says arming failed, stop.** The mode is not active, every mutation and every commit in this worktree is denied, and the plan must not be implemented. Report the reason to the user and let them re-run this command or run `/opencode-review-loop:stop`.

## Your job, in order

1. **Read the frozen plan** at the path named above (`plan.frozen.md`). Read it in full. That copy, not the original, is what the reviewer is given as evidence.

2. **Propose a phase split.** Each phase must be:
   - independently implementable and independently reviewable,
   - small enough that its diff is a sensible unit of review — a phase that produces hundreds of findings was scoped wrong,
   - complete on its own: it ends with one commit and a clean worktree.

   Phrase each phase description as what it delivers, faithfully to the plan. The reviewer is given both the plan and your phase descriptions, and is explicitly asked to flag a description that misrepresents the plan.

3. **Freeze the phases** by running exactly the `set-phases` command printed above, one `--phase "…"` per phase, in plan order. Until you do, every file mutation is denied — that is expected, not a malfunction.

4. **Implement phase 1**, then commit it:

   ```
   git add -A && git commit -m "…"
   ```

   The commit is intercepted. The whole working state — committed, staged, unstaged and untracked — is snapshotted into a tree, OpenCode reviews the delta since the last approved tree, and the commit proceeds only if the review passes. If it does not, the denial carries every blocking finding; fix them all and commit again.

5. **Keep going.** When a commit is verified you get a message telling you to continue straight into the next phase without ending your turn. Do that.

6. **When all phases are committed, end your turn.** The Stop gate runs a final cumulative review over the whole activation. Ending your turn earlier is fine but it will block with the outstanding phases.

## Rules while the mode is active

- Run builds, tests, formatters, and `git rm` as their **own** Bash calls. Commit commands may only be `git commit`, or a chain of `git add` / `git status` / `git commit` — anything that could change files after the snapshot is denied.
- `git commit --amend`, partial commits (pathspecs, `--only`, `--include`) and command substitution inside the commit command are denied. Commit the whole reviewed tree or nothing.
- Every phase leaves a clean worktree. Uncommitted leftovers block the turn.
- You cannot end the mode. `/opencode-review-loop:finish` and `/opencode-review-loop:stop` are the user's; running them yourself via Bash is denied.
- If you need to stop and ask the user something mid-phase, run `${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh defer --reason "…"` first, then end your turn. It is allowed a limited number of times.
- A failed or malformed review is never an approval. If a review fails, the commit stays denied and you retry it.

Check state at any time with `/opencode-review-loop:status`, and print any stored review in full with `/opencode-review-loop:report [n]`.
