# Step 0 — harness assumptions that only a live session can settle

`tests/selftest.sh` covers everything reachable from a shell: the snapshot layer, the command-shape table, the state machine, the review contract, every failure path. It cannot cover how **Claude Code itself** loads the skill, expands its body, and dispatches its hooks — those need a real session with the plugin installed.

Work through this list once, on a throwaway branch, before trusting the mode on real work. Each item names what to do, what should happen, and what to fall back to if it does not.

Static verification already done against the shipped binary (`~/.local/share/claude/versions/2.1.235`): `PostToolUseFailure`, `statusMessage`, `disable-model-invocation`, `user-invocable`, `argument-hint`, `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_SESSION_ID}`, `additionalContext`, `permissionDecisionReason` and `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` all appear in it. Presence is not behaviour, so the checks below still stand.

---

## 1. All four hook events register

```text
> /plugin install opencode-review-loop
> /opencode-review-loop:implement /tmp/scratch-plan.md
> /hooks
```

**Expect** `PreToolUse` (no matcher), `PostToolUse` (`Bash`), `PostToolUseFailure` (`Bash`) and `Stop`, all pointing at `scripts/ocrl.sh`.

**If `PostToolUseFailure` is missing:** the pending approval is then only cleared by the next gate's stale-pending reconcile. That path exists and is tested, so the loop still holds; drop the registration from `skills/implement/SKILL.md` to avoid a dead entry.

## 2. `` !`…` `` expansion runs in a *skill* body

This is the load-bearing assumption. The arm output must appear in the transcript **before Claude's first turn**.

**Expect** the `**opencode-review-loop is ARMED…**` block in the expanded prompt, and a state directory under `$XDG_STATE_HOME/opencode-review-loop/worktrees/`.

**If the backtick block appears verbatim instead of its output**, expansion does not run in skill bodies. Fall back to: remove the `` !`…` `` line, have the body instruct Claude to run `ocrl.sh arm …` as its first action, and add a narrow `pretool` exception for exactly that command (mirroring the existing `set-phases` exception in `cmd_pretool`). That reintroduces a one-command hole in the pre-activation guard — smaller than the original, but not zero.

## 3. `$1` reaches the body, and an omitted `$2` is harmless

```text
> /opencode-review-loop:implement /tmp/scratch-plan.md
> /opencode-review-loop:implement /tmp/scratch-plan.md --allow-dirty
> /opencode-review-loop:implement "/tmp/a plan.md"
```

**Expect** the plan path in the arm output each time; the two-argument form to report `pre-existing uncommitted work folded into phase 1: true`; and the one-argument form **not** to fail on an unmatched `$2` (`cmd_arm` treats an empty positional as absent).

Then the adversarial paths — a path containing a quote, a backtick and a `$`:

```text
> /opencode-review-loop:implement "/tmp/pl\`an.md"
```

**Expect** `ARMING FAILED … characters that are not safe to pass through shell expansion`. The character-set check in `cmd_arm` is the guard; this confirms the harness does not mangle the argument in a way that bypasses it. The path comes from you, not from Claude, so residual risk here is self-inflicted.

## 4. The session id in the expansion equals the one the hooks receive

```text
> /opencode-review-loop:status
```

**Expect** the `session:` line to match the session directory the hooks are writing to — i.e. a mutation attempt is actually gated rather than silently passing.

Concretely: after arming and **before** `set-phases`, ask Claude to edit any file. It must be denied with the `set-phases` instructions. A silent pass means the ids differ, and the session pointer in `ocrl_pointer_read` is looking at the wrong key.

## 5. `${CLAUDE_PLUGIN_ROOT}` resolves inside a skill hook

Covered implicitly by 1 and 2: if it did not resolve, the hook command would not exist and every tool call would error. Confirm no `command not found` noise in `/hooks` output or the transcript.

## 6. `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` via settings `env` raises the cap

Set it to a small value (`3`) in `settings.json`, restart, arm, and end turns repeatedly without doing any work.

**Expect** the override message (`A hook blocked the turn from ending N consecutive times`) at 3 rather than at 8. Then set it to `40` for real use.

**If it does not take effect**, the residual limit stands as documented in the README: `max_stop_blocks` (default 3) escalates to `needs-human` well before the cap, so the loop reaches a loud stop rather than a silent one.

## 7. A hook script can read a plan under `~/.claude/plans/`

Arm with a plan that lives there. Activation reads it from bash, not through the `Read` tool, so no tool permission applies — but confirm the frozen copy exists:

```console
ls "$XDG_STATE_HOME/opencode-review-loop/worktrees/"*/*/plan.frozen.md
```

## 8. `opencode run -f` can attach a file outside the repository — **settled**

Verified against `opencode 1.18.18` with `openai/gpt-5.6-sol`: with `external_directory` denied everywhere except the bundle, the reviewer globbed `changes.*.diff` inside the bundle directory (under `$XDG_STATE_HOME`) and read repository files through `--dir`. Attachments outside the repo work.

**If a future OpenCode version blocks them**, widen `ocrl_review_permission` in `scripts/lib/reviewer.sh`: the bundle pattern is already there, so the next step is `"external_directory": "allow"`, or writing the bundle inside the repo under a gitignored path (which costs the "nothing is written inside the repo" property).

Related, and already fixed: `-f` is a yargs **array** option, so it greedily consumes a trailing positional as one more attachment path. The prompt is therefore passed immediately after `run`, before any flag. If you reorder `ocrl_review_argv`, keep it that way — the symptom is `Error: File not found: <the whole prompt>`.

## 9. OpenCode permission patterns are last-match-wins — **settled**

`ocrl_review_permission` puts the broad `"*": "deny"` first and the narrow bundle allow last, following OpenCode's own built-in `explore` agent. Confirmed in the same run: the reviewer read repository files and the bundle, and used only read/glob/grep tools.

## 10. A second session in another repository shows zero hook activity

Open Claude Code in an unrelated repo while the loop is armed here.

**Expect** no gate messages and no denials — `cmd_pretool` exits early when the session pointer is missing or the worktree does not match. The selftest covers both branches; this confirms it end to end.

---

## Then: the first real run

Throwaway branch, 2–3 small phases, the real model. Deliberately introduce a bug in one phase and check that the review catches it and the commit is denied — an approval on a phase you know is broken is the one result that invalidates the whole thing.

This has been done once already, against `openai/gpt-5.6-sol` on a scratch repository: a `get_user` that swallowed every exception and returned a truthy sentinel was denied with two actionable findings; the fixed version was approved; `confirm-commit` verified the tree and advanced the phase; the final cumulative review completed the activation and disarmed the mode.

That run also surfaced a real operating hazard worth knowing about: **anything that writes into the worktree between the gate and the commit lands you in `RECONCILE`.** In that run it was an editor/MCP tool dropping a `.serena/` directory into the repo. The gate behaved exactly as designed — it noticed the committed tree was not the reviewed tree and refused to advance — but the fix is to gitignore such tooling directories *before* arming, not to fight the reconcile.
