---
name: implement
description: Implement an agreed plan with an enforced adversarial review loop. Every phase commit is gated on an external review that must pass before the commit proceeds.
argument-hint: "<path-to-plan.md> [--allow-dirty] [--until N] [--harness H] [--model X] [--variant V]"
disable-model-invocation: true
user-invocable: true
---

# Implement with an enforced review loop

!`${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh arm --session "${CLAUDE_SESSION_ID}" --args "$ARGUMENTS"`

## What just happened

The block above is the output of arming, which ran **before you had a turn**. It is authoritative: it reports whether the review loop is armed, and it is the only place the baseline was frozen.

**If it says arming failed, stop.** The mode is not active, every mutation and every commit in this worktree is denied, and the plan must not be implemented. Report the reason to the user and let them re-run this command or run `/adversarial-review-loop:stop`.

## Your job, in order

1. **Read the frozen plan** at the path named above (`plan.frozen.md`). Read it in full. That copy, not the original, is what the reviewer is given as evidence.

2. **Propose a phase split.** Each phase must be:
   - independently implementable and independently reviewable,
   - small enough that its diff is a sensible unit of review — a phase that produces hundreds of findings was scoped wrong,
   - complete on its own: it ends with one commit and a clean worktree.

   Phrase each phase description as what it delivers, faithfully to the plan. A description must carry **every clause** of the plan section it covers, trailing qualifier clauses included — an "Integration: …" or "…, keeping X unchanged" tail is scope, not decoration, and a description that drops it is the scope the phase will not deliver. Descriptions are frozen: you implement against the description, not the plan, so a clause missing there is lost until a reviewer notices its absence rounds later. The reviewer is given both the plan and your phase descriptions, and is explicitly asked to flag a description that misrepresents the plan — dropping a clause included.

3. **Freeze the phases** by running exactly the `set-phases` command printed above, one `--phase "…"` per phase, in plan order. Until you do, every file mutation is denied — that is expected, not a malfunction.

   **Never probe this command with placeholder or shortened phases to check the syntax works.** The freeze is one-shot: the first successful `set-phases` call locks in whatever list it was given, and a second call is refused outright ("the phase list is already frozen"). If a real call errors, fix the invocation and re-run it with the real phases — don't substitute a throwaway list to test the mechanics first. If a placeholder list does get frozen by mistake and no phase has been committed yet, recover with `resume --session <id> --replan` followed by the real `set-phases` call — see AGENTS.md, "the replan fence".

4. **Implement phase 1**, then commit it:

   ```
   git add -A && git commit -m "…"
   ```

   The commit is intercepted. The whole working state — committed, staged, unstaged and untracked — is snapshotted into a tree, the reviewer reviews the delta since the last approved tree, and the commit proceeds only if the review passes. If it does not, the denial carries every blocking finding; fix them all and commit again.

5. **Keep going.** When a commit is verified you get a message telling you to continue straight into the next phase without ending your turn. Do that.

6. **When all phases are committed, end your turn.** The Stop gate sweeps anything unreviewed and then completes the activation — running a final cumulative review over the whole activation first only if `final_review` is enabled, which by default it is not. Ending your turn earlier is fine but it will block with the outstanding phases.

## Rules while the mode is active

- Run builds, tests, formatters, and `git rm` as their **own** Bash calls. Commit commands may only be `git commit`, or a chain of `git add` / `git status` / `git commit` — anything that could change files after the snapshot is denied.
- `git commit --amend`, partial commits (pathspecs, `--only`, `--include`) and command substitution inside the commit command are denied. Commit the whole reviewed tree or nothing.
- A multi-paragraph commit message is written as repeated `-m`, one per paragraph (`-m "subject" -m "body"`) — git joins them with a blank line. A real newline inside a single `-m` is refused by the deny-list, so this is the way to write one, not a workaround.
- Every phase leaves a clean worktree. Uncommitted leftovers block the turn.
- You cannot end the mode. `/adversarial-review-loop:finish` and `/adversarial-review-loop:stop` are the user's; running them yourself via Bash is denied.
- If a blocking finding is ambiguous, or contradicts an earlier round of the same phase, ask the reviewer **before** guessing: `${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh clarify --question "…"`. It is one prose question against the review that just ran — no new commit attempt, no new round, and it changes no state you depend on. A wrong guess costs a whole round; a question costs one of a small allowance, and the denial itself tells you how many are left. Prefer it over re-running the same fix twice.
- If you need to stop and ask the user something mid-phase, run `${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh defer --reason "…"` first, then end your turn. It is allowed a limited number of times — each call permanently spends one of them, so never run it just to see what it does.
- A failed or malformed review is never an approval. If a review fails, the commit stays denied and you retry it.

Check state at any time with `/adversarial-review-loop:status`, and print any stored review in full with `/adversarial-review-loop:report [n]`.
