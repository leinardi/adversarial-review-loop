---
name: finish
description: Run the final cumulative OpenCode review now, even with phases outstanding, and complete the activation if it passes.
disable-model-invocation: true
user-invocable: true
---

# Final cumulative review

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh finish`

This ran the final cumulative review over the whole activation — baseline to the current state — regardless of how many phases were left.

- **If it passed**, the mode disarmed itself and further commits are ungated. Say so and summarise what shipped.
- **If it did not pass**, the mode is still armed and the block above carries the findings. Do not treat a failed final review as completion. Report the findings to the user; if they want them fixed, fix them and commit as usual — the per-commit gate is still in force.
- **If the worktree was not clean**, nothing was reviewed. The outstanding work has to be committed first so that every phase lands in a reviewed commit.
