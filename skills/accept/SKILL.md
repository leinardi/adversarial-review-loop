---
name: accept
description: Manually approve the current working tree for this phase, overriding a review loop that will not converge, without leaving the mode.
argument-hint: "[reason]"
disable-model-invocation: true
user-invocable: true
---

# Manually accept the current tree

!`${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh accept --reason "$ARGUMENTS"`

## What just happened

The block above is the output of accepting, which ran **before you had a turn**. It grants exactly one thing: the current working tree is added to `approved_trees`, the same record a passing review would have written. It does not advance the phase and does not complete the activation — the next commit still goes through the ordinary review gate, and the tree it commits must still be this exact one for the gate to let it through without a review.

**If it says nothing was accepted, stop.** The activation could not be accepted into for the reason given — read it and follow the alternative it names (usually `set-phases`, `/adversarial-review-loop:resume`, or a fresh `/adversarial-review-loop:implement <plan.md>`).

**If it accepted a `NEEDS_HUMAN` escalation**, the activation is `ACTIVE` again and commits are gated normally from here.

**If it accepted during a `RECONCILE`**, that reconcile still stands — the acceptance did not clear it. The outstanding recovery reset is still required before the mode will complete.

This command exists for one situation: a review loop that keeps raising new findings without converging. It is not a way to skip review as a matter of course — every other phase, and every tree that changes after this one, is reviewed exactly as before.
