---
name: stop
description: Leave the OpenCode review loop for this worktree. Commits and file changes stop being gated. Nothing is reverted.
disable-model-invocation: true
user-invocable: true
---

# Stop the review loop

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh deactivate --session "${CLAUDE_SESSION_ID}"`

The review loop has been switched off for this worktree, or was not active to begin with — the block above says which.

Nothing was reverted and nothing was committed. Whatever phase the work had reached is exactly where it still is, and the recorded state and review reports are kept at the path shown above.

Tell the user what state the work was left in, and do not resume implementing a gated plan unless they ask.
