---
name: pause
description: Move the review loop's pause target. With no argument the phase in flight is finished and committed as usual, then the turn ends instead of continuing into the next phase.
argument-hint: "[N | 0 | all]"
disable-model-invocation: true
user-invocable: true
---

# Pause at the end of a phase

!`${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh pause --args "$ARGUMENTS"`

The block above moved the pause target, which is the only thing this command does. With no argument it is the phase currently in flight; `N` sets it to phase `N`; `0` or `all` clears it so the loop runs to the end of the plan.

**The block is authoritative — read it before deciding anything below.** It says which target was actually set and what the loop will do with it, and those differ by case. Where this file and the block disagree, the block is right.

Whatever it says, this command **denies nothing and approves nothing**. The review gate on every commit is exactly as strict as it was, no tree became approved, and no phase was skipped.

## What to do next

Match the block to one of these, in order — the first that applies wins.

1. **It refused** ("Nothing was changed", "not armed in this worktree"). Nothing moved. Report the reason and carry on exactly as you were.

2. **It carries a `Note:` about `NEEDS_HUMAN`.** That outranks the pause: the activation is escalated, every mutation is denied, and only the user can clear it. **Do not implement, and do not attempt a commit.** Stop and tell the user the escalation is still standing.

3. **It carries a `Note:` about `RECONCILE`.** Do **not** stop — this one has a recovery you are meant to carry out, and stopping strands the phase until the user intervenes again. Follow the recovery the gate already prescribed: the bounded `git reset --soft` to the named parent, rebuild the phase's intended tree, then commit again with `git add -A && git commit -m "…"`, which goes through the normal review gate. **That recommit is the phase's commit** — do not implement the phase again or commit a second time on top of it. Once it is verified, go to "After a commit lands" below; it may well have reached the pause target already.

4. **It says "pause target cleared".** The opposite of a pause: there is no target left, so **no turn will end paused** and the loop runs to the end of the plan. Do not tell the user to expect a pause and do not offer them a resume. Carry on with the plan as normal, through the gate as always.

5. **It says the target is the last phase and "changes nothing on its own".** There is no pause here and there will be no paused turn — once every phase is committed the activation completes as usual. Say so plainly rather than promising a pause, and tell the user that stopping earlier means pausing on an earlier phase. Then carry on with the plan as normal.

6. **Otherwise it is an ordinary pause**, and the block names the phase it will stop after.
   - **Do not stop here.** Keep implementing the phase you are on, unchanged.
   - **Commit it the usual way** — `git add -A && git commit -m "…"` — through the review gate as always. A paused activation is still armed, and a phase still has to pass its review.
   - Then go to "After a commit lands".

## After a commit lands

Once a commit is verified the gate tells you what to do next, and **that message decides, not this file** — it knows which phase just landed and where the target is; a pause may have been set on the phase you were already finishing, so the very next commit can be the one that reaches it.

- **It says the pause target was reached** → end your turn. That is the signal to stop, and it is not an approval of the whole plan; the Stop gate ends the turn with a `paused` message. Tell the user which phase was committed, which is next, and that continuing is theirs to start with `/adversarial-review-loop:resume --until 0` (or `--until M` for a further target).
- **It tells you to continue straight into the next phase** → do that, without ending your turn. The target is further ahead than the phase that just landed.
- **It says every phase is committed** → end your turn; the activation completes as usual, with no pause involved.

Do not run `pause` yourself — the Bash route to it is denied, as it is for every other user-only command.

Check state at any time with `/adversarial-review-loop:status`, which prints the current `pause target`.
