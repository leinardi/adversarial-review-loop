# opencode-review-loop

A Claude Code plugin that implements an agreed plan **phase by phase, with an external adversarial review as an enforcement gate**. Claude cannot commit a phase until [OpenCode](https://opencode.ai) has reviewed the exact tree it is about to commit and passed it.

The review is not advice Claude may weigh up. It is a `PreToolUse` gate on the commit itself: findings come back as a denial, and the commit only proceeds once they are resolved.

```text
/opencode-review-loop:implement plan.md
   -> arms (freezes the baseline and the plan) before Claude has a turn
   -> Claude proposes phases and freezes them
   -> phase N implemented -> git commit -> INTERCEPTED
        snapshot the whole working state into a tree
        OpenCode reviews the delta since the last approved tree
        approved -> commit proceeds -> phase advances
        findings -> commit denied, findings returned inline
   -> all phases committed -> turn ends -> COMPLETE
        (plus a final cumulative review first, if final_review is on)
```

## Requirements

- Claude Code 2.1.x or newer (the plugin uses `PreToolUse`, `PostToolUse`, `PostToolUseFailure` and `Stop` skill hooks)
- [`opencode`](https://opencode.ai) on `PATH`, authenticated, with the configured model available
- `python3` 3.12 or newer — the gate itself. No install step: the standard library plus a vendored, lint-excluded copy of [bashlex](https://github.com/idank/bashlex) is everything it needs.
- `git`, `bash` 4.4+ and `timeout` (GNU or uutils coreutils) — `scripts/ocrl.sh` is a thin guarded shim over the interpreter above; see "Interpreter invocation" in `AGENTS.md` for why it exists and what it guarantees.

`jq` and GNU-specific coreutils (`split -C`, `stat -c`, `sed -i`) are no longer runtime dependencies — the port replaced every one of them with the standard library.

## Install

Add this repository as a local plugin marketplace, then install the plugin:

```console
$ claude
> /plugin marketplace add ~/Workspace/github/opencode-review-loop
> /plugin install opencode-review-loop
```

Raise the Stop-hook block cap so a long loop is not cut short. Claude Code caps consecutive Stop blocks (default 8) and **overrides by ending the turn**, which reads as success — so give the loop headroom in `settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "40"
  }
}
```

The variable is read at process start, so restart Claude Code after setting it. The number is how many consecutive blocks are *tolerated* — the override fires on the next one after that. Verified against a cap of `3`, which overrode on the fourth block.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/opencode-review-loop:implement <plan.md> [--allow-dirty] [--until N] [--model X] [--variant V]` | you | Arms the loop for this worktree and starts the phased implementation |
| `/opencode-review-loop:resume [--until N] [--plan <path>] [--replan] [--allow-dirty] [--abandon-pending] [--model X] [--variant V]` | you | Continues an armed activation — in a new session, or adjusts it in this one — without losing the baseline or any approval. See "Running a long plan across sessions" below |
| `/opencode-review-loop:status` | anyone | Current state: phase, baseline, approvals, counters, stored reports |
| `/opencode-review-loop:report [n]` | anyone | Prints a stored review in full, untruncated |
| `/opencode-review-loop:finish` | you | Runs the final cumulative review now, even with phases outstanding — and regardless of `final_review`, which makes it the way to get one on a default install |
| `/opencode-review-loop:accept [reason]` | you | Manually approves the current working tree for the current phase, without a review — see "Breaking a stuck review loop" below |
| `/opencode-review-loop:stop` | you | Leaves the mode. Nothing is reverted |
| `/opencode-review-loop:config [<key> <value> [--repo]] [<key> --unset [--repo]]` | you | Reads or writes the review-loop configuration. Unrelated to any armed activation — never registers the gate |

`implement`, `finish`, `stop`, `resume`, `config` and `accept` are `disable-model-invocation: true`. **Claude can never arm, resume, finish, stop, accept, or run the `config` command itself** — no natural-language phrasing invokes any of them, only the exact slash command. That stops the *command*; it does not make `.opencode-review-loop.json` off-limits to ordinary file edits — see "Known limitations" below. That is deliberate: a mode whose whole point is enforcement must not be self-enabling, and the cost is that "use the review loop for this" does nothing.

## Breaking a stuck review loop

Some phases will not converge — OpenCode keeps raising a fresh finding every round instead of closing the earlier ones. `/opencode-review-loop:stop` gets you out, but it turns enforcement off for the rest of the session; `/opencode-review-loop:finish` runs a review that is just as likely to keep finding things. `/opencode-review-loop:accept [reason]` is the middle option: it puts the current working tree — exactly as it stands — into the set of approved trees, the same record a passing review would have written. Nothing else changes: the phase does not advance, the activation does not complete, and any further edit changes the tree hash and puts the commit right back under review. It also clears a `NEEDS_HUMAN` escalation, which is otherwise something only a human can do — `/opencode-review-loop:resume` deliberately refuses to.

**When a finding actually repeats, the loop notices on its own.** `stall_rounds` (default `2`) consecutive rounds raising the same finding unchanged, or one that reappears after being absent, or is reversed (`SUPERSEDES`) more than once, escalates straight to `NEEDS_HUMAN` — the reviewer is not called again for it, and `/opencode-review-loop:status` shows the persisting finding. That does not cover every stuck phase: a reviewer that raises a genuinely new, non-repeating objection every round never trips it, and `accept` is still the only bound for that case — see [edge-cases.md](docs/edge-cases.md#a-phase-that-never-converges).

**A timeout or a rate limit is not the same failure as a missing binary.** `max_failures` still governs every operational, contract or bundle failure exactly as before, with no pacing — retrying immediately is the right move for those. A timeout, a matched rate/usage-limit signal, or contention with another review of the same phase already in flight is counted separately, against `max_transient_failures` (default `5`), and paced with backoff (`30s`, doubling, capped at `300s`): the next commit attempt is denied with the remaining wait rather than spending another provider call on a limit that has not reset yet. Both counters run independently and neither resets the other, so they bound the *total* number of failing attempts since the last approval, not strictly-consecutive runs of one kind — a stuck phase alternating between the two still escalates, just possibly after `max_failures + max_transient_failures` attempts rather than either limit alone. Both budgets, exhausted, escalate to `NEEDS_HUMAN` the same way.

Every acceptance is recorded: in `/opencode-review-loop:status`, as its own numbered report visible through `/opencode-review-loop:report`, and in a `## Manually accepted phases` section shown to every later review of that activation — including the final cumulative one — so nothing downstream mistakes an accepted phase for one that actually passed a gate.

Before accepting, though: when a finding is just *unclear* — or two rounds seem to contradict each other — Claude can run `ocrl.sh clarify --question "…"` to get one prose answer about the review that already ran, with no new commit attempt and no new round. It is not a slash command (Claude invokes it directly), it changes nothing, and it is capped at `max_clarifications` per run. A genuine standing disagreement still ends at `accept`.

## Running a long plan across sessions

`implement` always starts fresh: a new baseline, an empty phase list, no approvals carried over. Right for a new plan, wrong for picking an old one back up tomorrow — the 24-hour `ttl_hours` default makes that happen sooner than you'd think. `resume` is the second arming path for exactly that: it continues the *same* activation, in a new session or the current one, without moving the baseline or losing anything already approved.

- **Cross-session** (the normal case — a fresh session picks the plan back up) retires the previous activation into a blocking `RESUMED` status before the new one exists. Only one activation may write to a worktree at a time, so from that moment the old session denies every mutation, naming the session that took over.
- **Same-session** (re-running `resume` to change `--until`, the model, or the plan without starting a new session) mutates the live activation in place; nothing is retired.
- The baseline tree and every approved tree carry forward untouched. If a final cumulative review runs at all — `final_review` is off by default, and `finish` runs one regardless — it covers the whole plan from its *original* baseline, not from wherever the most recent resume happened to start.
- A commit landed since the last approval but was never reviewed? Resume warns rather than silently treating it as approved, and folds it into the next review.

**Pausing with `--until N`.** Add it to `implement` or `resume` and the loop stops asking for more once phase `N` is committed — the turn ends with a "paused, not an approval of the whole plan" message instead of demanding the next phase. `--until 0` or `--until all` clears the target. It is soft, not a fence: nothing stops Claude from continuing past it if told to, but by default the loop does not push forward on its own.

**Revising the plan mid-run.** Pass `--plan <path>` to `resume`, or just edit the plan file resume already has on record — if its bytes changed, resume freezes a new, timestamped copy (`plan.rev<n>.md`) alongside every earlier revision; nothing already frozen is ever edited or replaced. Add `--replan` and the phases from the *current* one onward may be redefined with `set-phases`, exactly as at first arming — every phase already committed stays immutable, since its description was the evidence a review already ran against. Both a decided revision and `--replan` require a clean worktree, regardless of `--allow-dirty`: that boundary is what keeps "redescribe the work to fit the code that's already there" from happening. From that point on, every reviewer is shown the full revision history and every revision file, not just the diff from the last hop — so a phase reviewed under an earlier revision is still explainable to a review of a later one.

Resume also blunts `ttl_hours`: a TTL-expired activation is `STALE` and still blocks, but the fix is now `resume` — which refreshes the activation rather than resetting it — not a full re-arm that would throw away progress. `ttl_hours` mainly guards a worktree nobody is actively driving anymore.

## How it works

**Arming happens at prompt-expansion time.** The skill body starts with a `` !`…` `` command, so the dirty check, the baseline freeze, the plan freeze and the model probe all run *before Claude has a turn*. Nothing needs to be written to disk by a tool call, so the pre-activation guard can deny everything without deadlocking on the command that would lift it.

**The commit is the phase boundary.** No snapshot bookkeeping, no state machine spanning turns: `HEAD` is the baseline and each phase ends in one commit. Approvals are keyed by tree SHA, which buys idempotence, skip-when-unchanged, and duplicate-review suppression for free.

**The snapshot is a real git tree.** Committed, staged, unstaged and non-ignored untracked content go into a throwaway index, and the resulting tree id is what gets reviewed:

```bash
GIT_INDEX_FILE=$tmp git read-tree HEAD
GIT_INDEX_FILE=$tmp git add -A     # respects .gitignore; the real index is untouched
tree=$(GIT_INDEX_FILE=$tmp git write-tree)
```

**Approval and commit are separate events.** `PreToolUse` permits the Bash call; `PostToolUse` then verifies that a new commit exists, that its parent is the pre-command `HEAD` (which rejects `--amend`), that `HEAD^{tree}` is exactly the approved tree, and that the worktree is clean. Only then does the phase advance. Anything else enters `RECONCILE` with a prescribed, non-automatic recovery.

**`Stop` is a backstop, not the driver.** It sweeps unreviewed work, enforces outstanding phases, and runs the final cumulative review before completing when `final_review` is on **or a `finish` was requested earlier** — a `finish` whose review found problems leaves that request standing, so the next turn end re-runs the review rather than completing quietly. Blocks are counted only when no progress happened in between, so alternating work and blocks never escalates.

### What blocks

One rule: **`actionable=yes` AND `severity >= block_severity`**. The default `block_severity` is `low`, so every actionable finding blocks; raising it is a deliberate relaxation.

The reviewer's own `VERDICT` line is advisory. The gate recomputes the verdict from the `FINDING` lines and the stricter of the two wins — an `APPROVE` alongside an actionable critical finding still blocks.

**Nothing converts a failure into an approval.** Missing contract markers, a missing verdict, a non-zero exit, a timeout, an empty response, a diff above the hard ceiling, or more findings than the cap — every one of those blocks or escalates to `needs-human`. Findings are never silently trimmed: above `max_findings` the gate escalates and keeps the full report on disk rather than showing you a shortened list.

The findings block is parsed strictly, and anything else in it is a failed review rather than a line to skip. `severity` must be one of `info`, `low`, `medium`, `high`, `critical` and `actionable` must be `yes` or `no`; there must be exactly one marker pair, in order, holding exactly one `VERDICT`. The gate cannot tell a reviewer's typo from a finding it failed to understand, so `actionable=maybe` on a critical finding blocks instead of quietly not counting.

Every `FINDING` line comes back inline in the denial. Prose is what truncates, never the actionable set.

### Accepted commit commands

A snapshot taken before a compound command is meaningless if the command then changes files, so classification inspects **arguments**, not just the executable:

| Accepted | Denied |
| --- | --- |
| `git commit -m "…"` | `make build && git commit -m x` (mutates after the snapshot) |
| `git add -A && git commit -m "…"` | `git rm f && git commit -m x` (deletes from the worktree) |
| `git add -A && git status && git commit -m "…"` | `git diff --output=… && git commit -m x` (writes files) |
| `git commit -am "…"` | `git commit --amend`, `--only`, `--include`, pathspecs |
|  | `git -C … commit`, `$(…)`, backticks, pipes, redirection |

Each subcommand is matched against a flag allowlist with **default-deny on unknown options**. Run builds, tests and `git rm` as their own Bash calls; the next snapshot picks up the result.

### The reviewer

```bash
OPENCODE_PERMISSION='{"*":"deny","read":"allow","grep":"allow","glob":"allow","list":"allow",
                      "external_directory":{"*":"deny","<act_dir>/bundles/**":"allow"}}' \
opencode run --pure --dir "$repo" -m "$model" --title "review-loop phase N [<hash>/<seq>]" \
  -f range.txt -f changes.00.diff [-f …] "$(cat prompts/reviewer-phase.md)"
```

- `--pure` neutralises your global OpenCode **plugins**, which would otherwise rewrite the output and break the contract. It does *not* remove global skills (`~/.config/opencode/skills`) or a global `~/.config/opencode/AGENTS.md`, both of which still reach the reviewer — the prompt explicitly tells it to ignore ambient style directives and not to invoke a skill for the review. A review that comes back reformatted anyway fails the contract, which blocks; it never approves
- `OPENCODE_PERMISSION` makes the reviewer structurally read-only and repo-scoped; `external_directory` is denied everywhere except the **bundles root** — every bundle in this activation, never the activation directory itself, which also holds `state.json`, the frozen plan and the reports
- the diff is **chunked across as many attachments as needed, never truncated** — a truncated diff hides deletions, and approving on a partial view is approving what was never seen
- project OpenCode config stays enabled, so repo-local review skills load

The reviewer cannot run anything. Set `verify_cmd` and the hook runs it and attaches the output as evidence.

Repo text and the frozen plan are labelled **evidence, not instructions**, and the reviewer is explicitly asked to flag a phase description that misrepresents the frozen plan.

**Consecutive reviews of one phase continue the same OpenCode session** where one can be found and safely claimed — `-s <id>` instead of a fresh `--title` — so round 2 can see what round 1 already flagged instead of starting cold every time. A new phase, or the final cumulative review, always starts fresh. This is a pure convenience: an approving verdict from a continued session is never acted on by itself — the gate runs one more, cold confirmation of the same evidence first, and that cold verdict is what decides. See [security.md](docs/security.md) for the full argument and its costs (one extra model call per approving phase, and diff/context growth across rounds — `/opencode-review-loop:accept` is the answer when a phase will not converge either way).

## Configuration

Resolution order: `OCRL_*` environment → repo `.opencode-review-loop.json` → `$XDG_CONFIG_HOME/opencode-review-loop/config.json` → defaults.

| Key | Default | Purpose |
| --- | --- | --- |
| `model` | `openai/gpt-5.6-sol` | probed for reachability at arm time |
| `variant` | unset | reasoning effort (`high`, `max`, …) |
| `block_severity` | `low` | blocks when `actionable=yes AND severity >= this` |
| `timeout_sec` | `900` | per review run |
| `max_failures` | `2` | op failures since the last approval before `needs-human` (transient failures excluded — see `max_transient_failures`) |
| `max_transient_failures` | `5` | timeouts/rate-limits/busy-review-slot failures since the last approval before `needs-human`; paced with backoff |
| `max_stop_blocks` | `3` | **no-progress** Stop blocks before escalating |
| `max_defers` | `3` | pause escapes per activation |
| `verify_cmd` | unset | run by the hook, output attached as evidence |
| `pure` | `true` | pass `--pure` |
| `disable_project_config` | `false` | set `OPENCODE_DISABLE_PROJECT_CONFIG` |
| `chunk_diff_bytes` | `400000` | per-attachment chunk size |
| `hard_diff_ceiling` | `8388608` | above this → `needs-human` |
| `max_file_bytes` | `16777216` | oversized-file guard |
| `max_reason_bytes` | `32768` | prose cap; `FINDING` lines exempt |
| `max_findings` | `200` | above this → `needs-human`, never a trimmed list |
| `max_findings_bytes` | `65536` | same, by size |
| `max_clarifications` | `2` | `clarify` questions per run before it points at `accept` |
| `stall_rounds` | `2` | consecutive rounds a finding must persist before `needs-human`; `0` disables |
| `allow_dirty` | `false` | alternative to passing `--allow-dirty` |
| `ttl_hours` | `24` | after this, gates block and ask for a re-arm — `resume` is usually the fix, not `implement`; see "Running a long plan across sessions" |
| `ignore_globs` | `[]` | paths whose sole change skips a review |
| `final_review` | `false` | run the final cumulative review at `Stop` |

Environment variables are the upper-cased key with an `OCRL_` prefix (`OCRL_BLOCK_SEVERITY`, `OCRL_MODEL`, …); `OCRL_IGNORE_GLOBS` is comma-separated.

Example `.opencode-review-loop.json`:

```json
{
  "model": "openai/gpt-5.6-sol",
  "variant": "high",
  "verify_cmd": "make test",
  "ignore_globs": ["CHANGELOG.md", "docs/**"]
}
```

State lives in `$XDG_STATE_HOME/opencode-review-loop/worktrees/<sha256(worktree)>/<session-id>/` — `state.json`, `plan.frozen.md`, `phases.frozen`, `reports/`, `bundles/`. **No hook, and nothing Claude can invoke itself, ever writes inside the repository under review.** The one exception is `/opencode-review-loop:config <key> <value> --repo`, an explicit, user-only write to the repository's own `.opencode-review-loop.json`.

## Failure behaviour

| Condition | Behaviour |
| --- | --- |
| Dirty worktree at arm time | `ARM_FAILED`; `--allow-dirty` folds the dirt into phase 1's review |
| `opencode` missing or model unreachable | `ARM_FAILED`, naming the failure |
| Any arm failure | Persisted **before** exit; all mutations and commits denied until re-armed or stopped |
| Arming never executes (refused sandbox, unreadable script) | The dispatcher records `ARM_FAILED` itself and denies; a missing pointer is never read as "not armed" |
| Mutation before `set-phases` | Denied, with the exact command to run |
| Turn ends while `ARM_FAILED` or phases unset | `Stop` blocks with instructions; OpenCode is never called |
| Timeout, malformed output, missing verdict, non-zero exit | `OP_FAILURE` → deny; never an approval |
| `OP_FAILURE` past `max_failures` | `NEEDS_HUMAN`; OpenCode is no longer invoked |
| Findings past `max_findings` / `max_findings_bytes` | `NEEDS_HUMAN`, full report retained; never trimmed, never approved |
| Diff above `hard_diff_ceiling` | `NEEDS_HUMAN` |
| Commit lands ≠ reviewed tree, or is an amend | `RECONCILE` with a prescribed, non-automatic recovery |
| `git reset --soft` before the activation commit | Denied (equal to it is allowed — that is the phase-1 recovery) |
| Activation older than `ttl_hours` | `STALE`: gates block and ask for a re-arm. **Never a silent disarm** |
| No-progress `Stop` blocks past `max_stop_blocks` | `NEEDS_HUMAN`, loud system message. **Not** an approval |
| Claude tries `ocrl finish` / `deactivate` / `resume` / `config` via Bash | Denied — user-only |
| Any mutation against a `RESUMED` activation | Denied, naming the session that took over; re-arm with `implement` is the only way out |
| A resume retires its predecessor, then fails before publishing the successor | No automatic rollback: predecessor stays `RESUMED`, successor is `ARM_FAILED` — both deny, and the recovery is `implement` |
| `resume` with an approval still pending confirmation | Refused, unless `--abandon-pending` |
| `resume` after history was rewritten (the activation commit is no longer an ancestor of `HEAD`) | Refused; re-arm |
| A decided plan revision, or `--replan`, on a dirty worktree | Refused — **not** waived by `--allow-dirty` |
| `--replan` before the phase list has ever been frozen | Refused; there is nothing yet to replan |
| A `state.json` predating this feature | Migrated on first resume; `ARM_FAILED` (not a crash) if its `plan.frozen.md` is missing |
| A recorded plan revision whose file fails a path or hash check | `NEEDS_HUMAN` — never attached to a review, never silently skipped |

## Development

```console
make test                    # 300+ acceptance assertions against scratch repos, plus the Python unit tests; no model is called
make test-filter FILTER=stop # one section
make dry-run                 # print the exact opencode argv and prompt, without invoking it
make check                   # pre-commit (shellcheck, yamllint, markdownlint, …)
```

The selftest drives the hook entrypoints with synthetic payloads and replaces the reviewer with `tests/fixtures/fake-reviewer.sh` (`OCRL_REVIEWER_CMD`), so loop logic costs nothing to iterate on. It covers the snapshot layer, the command-shape table, every arm-failure mode, the fail-closed guards, commit divergence and reconcile, the findings cap, the Stop accounting, and the TTL.

Before the first real run, work through [`tests/STEP0.md`](tests/STEP0.md): the harness assumptions that only a live Claude Code session can settle.

## Upgrading to 0.6

**The final cumulative review no longer runs by default.** In 0.5.x, ending the turn with every phase committed always ran a review of the whole activation (baseline → `HEAD`) and reached `COMPLETE` only if it approved. In 0.6.0 that review is behind `final_review`, which defaults to `false`: the Stop gate completes the activation directly instead. One exception survives — if a `finish` was requested and its review did not approve, that request stands, and the next turn end re-runs the cumulative review even with the key off. A failed `finish` never becomes a quiet `COMPLETE`.

To keep 0.5.x behaviour, set it once:

```console
/opencode-review-loop:config final_review true
```

That writes user-level config, which is the **lowest**-precedence source above the defaults. `OCRL_FINAL_REVIEW` in the environment and `final_review` in the repository's own `.opencode-review-loop.json` both override it, so if either says `false` the review still will not run. Confirm what actually resolved with `/opencode-review-loop:config` — it prints the effective value of every key and where it came from. `/opencode-review-loop:status` does **not** show this key, so it cannot tell you whether the review will run.

`OCRL_FINAL_REVIEW=true` covers a single run, and `/opencode-review-loop:finish` runs the review whatever the key says. Do not read that as "`finish` always gets you a review": it ignores `final_review` and nothing else. It still refuses, before running any review, on a dirty worktree and on any status outside its allow-list — `STALE`, `NEEDS_HUMAN`, `DISARMED`, `ARM_FAILED` and `RESUMED` all get a refusal rather than a review, and a `STALE` one is reachable just by `ttl_hours` elapsing. The two kinds of refusal differ in what they leave behind, which matters if you are relying on one. An ineligible *status* is checked before the request is recorded, so that refusal is a clean no-op. A dirty worktree is checked after, so the request stands — and once the worktree is clean (Stop blocks on that first), the next turn end runs the cumulative review, skipping the outstanding-phase check as it does, even with `final_review` off. And once an activation is `COMPLETE` it can never be reviewed cumulatively; `finish` and `resume` both refuse it. If you want the pass, run it while the activation is healthy and the worktree is clean.

**Why.** The review covers the whole activation in one pass. Past roughly 40 phases that diff can exceed `hard_diff_ceiling` outright — escalating to `needs-human` with completion wedged — and well before that it exceeds what a model can meaningfully read at once, so you pay for a skim that reads as assurance. The prompt already tells the reviewer not to re-litigate findings a phase review accepted.

**If you have an activation already in flight, finish it before upgrading — or turn the key on.** Completing without a review is not a free pass: the Stop gate first proves phase progress against git, requiring one recorded commit per frozen phase (`phase_commits`, written by `confirm-commit`), all distinct, in ancestry order, each one *moving the tree*, and the last of them being `HEAD` itself — a commit landing after the final phase refuses the completion, even if its tree was approved earlier. An activation armed by 0.5.x has no such record and cannot produce one retroactively, so it escalates to `needs-human` instead of completing — and that escalation also closes the `finish` remedy, since `finish` refuses a `NEEDS_HUMAN` activation. Two other shapes land the same way: a plan with an empty phase commit in it, and any activation armed on a repository with **no commits yet** — its `activation_commit` is legitimately empty, but that field is what anchors the evidence to this activation and nothing outside `state.json` can confirm the claim, so it is refused rather than trusted. For any of these, either set `final_review true` for that activation or run `/opencode-review-loop:finish` before the last turn ends.

**What you lose by leaving it off.** Every phase commit still passed the per-commit gate, and the unreviewed-work sweep still runs at turn end. But "passed the gate" is weaker than "was reviewed": an already-approved tree, or one where `ignore_globs` matched everything, passes without any model call. Under 0.5.x the cumulative review was the one pass that would still have looked at that content; with the key off, nothing does. What else goes is the cross-phase view — phase 7 quietly undoing phase 2, dead ends, interface drift between phases — and one of the two layers `AGENTS.md` names as making the command deny-list defence-in-depth. Worth stating plainly: cross-phase drift is likeliest on exactly the large plans where this review was least able to catch it.

**Also in 0.6.0:** `MAX_PHASES` is 64, up from 30.

## Known limitations

- **Honest-agent bar.** Claude could commit through a wrapper script, abuse `defer`, or edit `.opencode-review-loop.json` directly — `disable-model-invocation` blocks the `config` *command*, not ordinary edits to the file it writes, and nothing else marks that path special. Repo config is treated as attacker-controlled input for exactly this reason (see "Adding config" in `AGENTS.md`) — but treat that as "validated as untrusted input", not as "harmless". It is policy, and policy written there **can** weaken the gate silently: `ignore_globs: ["**"]` skips the reviewer call on every commit, a raised `block_severity` stops findings from blocking, and `final_review false` removes the cumulative backstop. None of these run unreviewed code; all of them change what the gate does, and nothing fences the file off from Claude. Commit-level dodges are backstopped by the final cumulative review **only if `final_review` is on**, which by default it is not; on a default install the per-commit gate and `confirm-commit`'s reporting are what you have. Nothing here defends against a deliberately hostile agent, and this design does not pretend to.
- **The Stop-block cap is residual.** Continuation pressure from `PostToolUse`, progress-aware counting and `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` mitigate it, but a run that repeatedly ends its turn without progress can still exhaust the cap — and exhaustion ends the turn.
- **A `PreToolUse` hook runs on every tool call**, and it is not free. Measured on Linux with warm caches, through `scripts/ocrl.sh` end to end (the Python port; these numbers supersede the Bash-era ones this table used to carry):

  | Path | Latency | Processes |
  | --- | --- | --- |
  | tool call outside the armed worktree | ~111 ms | 5 |
  | read-only tool (`Read`, `Grep`, `Glob`, …) | ~111 ms | 4 |
  | mutating tool (`Edit`, `Write`, MCP) | ~111 ms | 4 |
  | `Bash` (non-commit) | ~111 ms | 4 |

  Processes counted the same way `tests/selftest.sh`'s own hot-path check counts them: every successful `execve` under `strace -f`, end to end through `scripts/ocrl.sh`. Four is the shim's fixed floor regardless of branch — the shebang's `env`, the `bash` shim itself, the `timeout` wrapper, and `python3` — plus one more `git rev-parse` when `cwd` is outside the armed worktree and has to be placed. Under Python, config and state are in-process file I/O rather than `jq` subprocesses, so *that* part of the process count no longer varies by branch the way it did under Bash. The read-only hoist still exists and still matters (it is what makes a future read-only deny unreachable without deliberately undoing it) — it just shows up as less work inside the one Python process, not as fewer processes around it.

  Latency did not drop with the process count, and the reason is `scripts/ocrl.sh` itself: every hook call is wrapped in `timeout <N>` (below the timeout Claude Code enforces — see "Interpreter invocation" in `AGENTS.md`), so a hung parser still denies before the host tears the hook down with nothing. On this machine that wrapper alone measured **~90 ms**, flat, on every call — `timeout` here is `uutils-coreutils`' Rust reimplementation, and it appears to poll on a fixed interval rather than waking when the child exits (confirmed with `strace`: the child exits immediately, `timeout` still sleeps out the rest of its interval). The raw interpreter cost underneath it, `python3 -I` running the port directly with no wrapper, measured **~45 ms**. Neither the wrapper's cost nor its presence is optional — Rule 1's "a hung parser must deny" needs it — but which number you see depends on which `timeout` your system runs: GNU coreutils' does not have this behaviour. A commit pays two things more. The bash parser builds its LALR tables at import, measured at **~55 ms**, and it is imported only when a command already looks like a commit — a `Read` never pays for it. And then the review itself, which is measured in minutes, not milliseconds.
- **Strict commit hygiene is enforced.** Phases must commit all their work and leave a clean worktree. The payoff is that every commit in the history is a tree that was actually reviewed.
- **Background writers land you in `RECONCILE`.** Anything that touches the worktree between the gate and the commit — an editor writing back a buffer, an MCP server dropping a state directory, a file watcher — changes the tree out from under the approval. That is the gate working, but gitignore such tooling paths before arming rather than fighting it.
- **A hostile plan path can inject a shell command.** `$ARGUMENTS` is substituted into the skill body textually, without shell escaping, and the body is then `eval`ed — so a path like `x"; id; echo "` runs `id`. Confirmed empirically, and not fixable inside the plugin: the substitution happens before any shell sees it, so no quoting of `$ARGUMENTS` helps. What bounds it is that `implement` is `disable-model-invocation: true`, so only the person typing the slash command can supply the path — Claude cannot invoke it. Do not paste plan paths from untrusted sources.
- **Session-scoped.** `/clear` or a crash disarms the mode; state goes stale and blocks rather than vanishing. Recovery is re-arming, which re-baselines from the current `HEAD`.
- **Submodule content is not diffed** — it is detected and declared in the report header.
- **Cost and latency.** Every phase costs at least one full-model review, and a denied commit blocks the session for the length of the review. That is what a gate costs.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).

The gate parses commands with [bashlex](https://github.com/idank/bashlex), which is GPLv3
and is vendored under `scripts/ocrl/_vendor/` so the plugin works straight from a checkout
with no install step. See [that directory's README](scripts/ocrl/_vendor/README.md) for the
version, the upstream commit, and the one change made to it.
