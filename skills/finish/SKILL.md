---
name: finish
description: Run the final cumulative OpenCode review now — regardless of final_review, even with phases outstanding — and complete the activation if it passes.
disable-model-invocation: true
user-invocable: true
---

# Final cumulative review

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh finish`

This ran the final cumulative review over the whole activation — baseline to the current state — regardless of how many phases were left, **and regardless of `final_review`**. Since 0.6.0 that key is `false` by default, so the Stop gate no longer runs this review on its own: this command is the only guaranteed route to one, and only while the activation is still open. A `COMPLETE` activation can never be reviewed cumulatively afterwards.

- **If it passed**, the mode disarmed itself and further commits are ungated. Say so and summarise what shipped.
- **If it did not pass**, the mode is still armed and the block above carries the findings. Do not treat a failed final review as completion. Report the findings to the user; if they want them fixed, fix them and commit as usual — the per-commit gate is still in force. The request to finish stands, so the next turn end re-runs this review rather than completing quietly, even with `final_review` off.
- **If the worktree was not clean**, nothing was reviewed. The outstanding work has to be committed first so that every phase lands in a reviewed commit.
