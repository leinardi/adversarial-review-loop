---
name: resume
description: Continue an already-armed adversarial-review-loop plan in a new session (or adjust the pause target, model, or plan in this one), without losing the original baseline or any approvals already recorded.
argument-hint: "[--until N] [--plan <path>] [--guide <path>] [--replan] [--allow-dirty] [--abandon-pending] [--harness H] [--model X] [--variant V]"
disable-model-invocation: true
user-invocable: true
---

# Resume the review loop

!`${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh resume --session "${CLAUDE_SESSION_ID}" --args "$ARGUMENTS"`

## What just happened

The block above is the output of resuming, which ran **before you had a turn**. It is authoritative: it reports whether an activation for this worktree was picked back up, what phase it is on, and whether the plan changed since it was last frozen.

**If it says resume failed, stop.** Every file mutation and every commit in this worktree is denied until the reason is resolved. Report it to the user; do not implement anything.

The gate's hooks are registered by the plugin itself, in every Claude Code session, whether or not the resume succeeds — a failed resume leaves this worktree denied, it never leaves it ungated.

## Your job, in order

1. **Read the frozen plan the banner names** — `frozen plan:` in the banner, inside the activation directory. That copy — never the original file on disk — is what the reviewer is given as evidence. When the banner reports more than one plan revision, the file it names is the **active** one (a `plan.rev<n>.md`, not necessarily `plan.frozen.md`), and it is what the reviewer actually evaluates against from this resume onward; every earlier revision is still disclosed to the reviewer for context. Always implement against the file the banner names, whichever revision it is.
2. **If phases are not frozen yet** (the banner says so), split the plan and freeze it exactly as `/adversarial-review-loop:implement` describes, then implement phase 1.
3. **If the banner says `--replan` was granted**, redefine only the phases it names (the current one onward — earlier phases are immutable and already committed) by reading the active plan and running `set-phases` with one `--phase` per replacement phase, exactly as the banner's command shows. Every other mutation is denied until that command has run. Then continue into the (possibly renamed) current phase.
4. **Otherwise, continue straight into the phase the banner names.** The baseline, every prior approval, and the phase list are all carried forward from the original activation — nothing about them is reset by resuming.
5. **If the banner's `review guide:` line says the guide changed just now** (`--guide <path>` was passed), read the frozen copy it names: from here on, every review is composed with that guidance instead of the one earlier phases were judged under, and the reviewer is told as much. It is guidance to the reviewer, not to you — implement against the plan.
6. **Commit each phase** the same way as always:

   ```
   git add -A && git commit -m "…"
   ```

   The commit is intercepted and reviewed exactly as under `/adversarial-review-loop:implement`.
7. **If a pause target is set** (the banner names it), stop and report to the user once you reach it — do not continue past it on your own.
8. **When all phases are committed, end your turn.** The Stop gate sweeps anything unreviewed and then completes the activation. If `final_review` is enabled — by default it is not — it first runs a cumulative review over the whole activation, from the *original* baseline to `HEAD`.

## Rules while the mode is active

Identical to `/adversarial-review-loop:implement`: only `git commit`, or a chain of `git add` / `git status` / `git commit`, may create a commit; `--amend` and partial commits are denied; every phase leaves a clean worktree; you cannot end the mode yourself.

When a blocking finding is ambiguous or contradicts an earlier round, ask `${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh clarify --question "…"` instead of guessing — one prose question against the review that just ran, no new round, and the denial names how many questions are left.

Check state at any time with `/adversarial-review-loop:status`, and print any stored review in full with `/adversarial-review-loop:report [n]`.
