# Architecture

This is the engineering-level map: what runs when, what talks to what, and where state
lives. For the "why," see [`AGENTS.md`](../AGENTS.md) in the repo root — it's the
canonical authority for invariants a change must not break. This page describes the system
as it stands; it doesn't repeat the reasoning behind every design choice.

## The shape of it

```text
 Claude Code                          scripts/arl.sh (guarded Bash shim)
 ┌──────────────────┐                 ┌──────────────────────────────────┐
 │ skill invocation   ──!`…`─────────▶│  probes python3, wraps it in       │
 │ (prompt-expansion)  │               │  `timeout`, discards partial       │
 │                     │               │  output on any non-zero exit       │
 │ PreToolUse hook    ─────────────────▶  scripts/arl-bootstrap.py         │
 │ PostToolUse hook   ─────────────────▶    -I, absolute path, sets         │
 │ PostToolUseFailure ─────────────────▶    sys.pycache_prefix before       │
 │ Stop hook          ─────────────────▶    arl is ever imported           │
 └──────────────────┘                 └──────────────┬───────────────────┘
                                                       ▼
                                        scripts/arl/cli.py (subcommand dispatch)
                                                       │
                       ┌───────────────────────────────┼───────────────────────────────┐
                       ▼                                ▼                                ▼
              commands/pretool.py             commands/posttool.py              commands/stop.py
              (PreToolUse: gate every           (PostToolUse: verify a           (Stop: sweep, enforce
               mutating tool call)               commit landed as approved,       outstanding phases, run
                                                  PostToolUseFailure: clear        the final review if on)
                                                  a stale pending approval)
                       │                                │                                │
                       └────────────────┬───────────────┴────────────────┬───────────────┘
                                         ▼                                ▼
                                   gitsnap.py                       reviewer.py ──▶ harness/
                              (throwaway-index                (bundle building,     (which reviewer CLI,
                               tree snapshot)                   running the           and how its command
                                                                reviewer,             is spelled)
                                                                contract parsing)
                                         │                                │
                                         └──────────────┬─────────────────┘
                                                         ▼
                                                    state.py
                                          (state.json, session pointers,
                                           the activation's on-disk directory)
```

`commands/arm.py` and `commands/resume.py` sit alongside this, reached from `implement` and
`resume`'s own `` !`…` `` prompt-expansion lines rather than from a hook — arming has to
finish, and deny or permit accordingly, *before Claude gets a turn at all*.

## Four load-bearing choices

**Arming happens at prompt-expansion time.** The skill body starts with a `` !`…` ``
command, so the dirty check, the baseline freeze, the plan freeze and the model probe all
run *before Claude has a turn*. Nothing needs to be written to disk by a tool call, so the
pre-activation guard can deny everything without deadlocking on the command that would lift
it.

**The commit is the phase boundary.** No snapshot bookkeeping, no state machine spanning
turns: `HEAD` is the baseline and each phase ends in one commit. Approvals are keyed by tree
SHA, which buys idempotence, skip-when-unchanged, and duplicate-review suppression for free.

**The snapshot is a real git tree.** Committed, staged, unstaged and non-ignored untracked
content go into a throwaway index, and the resulting tree id is what gets reviewed:

```bash
GIT_INDEX_FILE=$tmp git read-tree HEAD
GIT_INDEX_FILE=$tmp git add -A     # respects .gitignore; the real index is untouched
tree=$(GIT_INDEX_FILE=$tmp git write-tree)
```

**Approval and commit are separate events.** `PreToolUse` permits the Bash call;
`PostToolUse` then verifies that a new commit exists, that its parent is the pre-command
`HEAD` (which rejects `--amend`), that `HEAD^{tree}` is exactly the approved tree, and that
the worktree is clean. Only then does the phase advance. Anything else enters `RECONCILE`
with a prescribed, non-automatic recovery.

## The hook lifecycle

Six Claude Code hook events are registered by `hooks/hooks.json`, at plugin load, in every
Claude Code process the plugin is enabled in — deliberately *not* by the skills, whose hooks
would register per process and vanish on `claude --resume` (see
[edge-cases.md](edge-cases.md#hooks-are-plugin-level-not-skill-level)):

| Event | Subcommand | Fires on | Can deny? |
| --- | --- | --- | --- |
| `PreToolUse` | `pretool` | every tool call | yes — this is the gate |
| `PostToolUse` | `confirm-commit` | after a `Bash` call returns | no — the tool already ran; it reports |
| `PostToolUseFailure` | `posttool-failure` | after a failed `Bash` call | no — it only clears stale pending state |
| `Stop` | `gate-stop` | when Claude tries to end its turn | yes — blocks the turn from ending |
| `SessionStart` | `reorient` | after a compaction or a resume | no — plain-text context for Claude, never a decision |
| `UserPromptSubmit` | `intent` | every prompt, before any skill expands | only an arming prompt whose request could not be recorded |

A single phase's commit touches three of these in order: `pretool` reviews and
conditionally allows the `git commit` to run; `confirm-commit` (or `posttool-failure`, if
the commit itself failed) verifies what actually landed; `gate-stop` is the backstop that
catches anything left unreviewed when Claude tries to stop. Whether `gate-stop` also runs a
cumulative review of the whole activation before completing depends on `final_review` (off by
default) **or** on a `finish` having been requested and not yet satisfied; the sweep and the
outstanding-phase check run either way.

## The state machine

Every activation (one armed session, one worktree) has a `status`. These are the only
values that exist:

| Status | Meaning | Terminal? |
| --- | --- | --- |
| `ARMED` | baseline and plan frozen, phases not yet split | no |
| `ACTIVE` | phases frozen, implementing/reviewing normally | no |
| `RECONCILE` | a commit landed that doesn't match what was approved | no — has a prescribed recovery |
| `NEEDS_HUMAN` | escalated; every mutation denied until the user intervenes | no — `accept` clears it and keeps the loop going; `stop` leaves the mode |
| `RESUMED` | retired by a `resume` into a successor session | **yes** |
| `COMPLETE` | the activation is closed and the mode disarmed. Reached three ways: the Stop gate with every phase committed, either directly or after an approving cumulative review when `final_review` is on; the Stop gate following through on a standing `finish_requested`, which skips the outstanding-phase check and so can complete with phases left; or a user-invoked `finish`, which always reviews and likewise need not have every phase committed. The `reason` field records which | **yes** |
| `ARM_FAILED` | arming (or resuming) failed; nothing was frozen | no — re-arm fixes it |
| `DISARMED` | the user ran `stop` | no — re-arming starts fresh |

`STALE` is not a stored value — it's *derived* from `armed_at` plus `ttl_hours` at read
time (`state.effective_status`). An activation can be stored as `ACTIVE` and answer `STALE`
today, then answer `ACTIVE` again tomorrow if `resume` refreshes `armed_at`. See
[edge-cases.md](edge-cases.md#a-stale-activation) for what that means in practice.

### `round_history`

`STATE_VERSION` 3 adds `round_history`: one entry per *parsed* review (`APPROVED` or
`CHANGES_REQUIRED` only — an `OP_FAILURE` or a `NEEDS_HUMAN` is not a round), appended by
`reviewer.execute` under a fingerprint-guarded transaction. Each entry records the
sequence number, label, phase, `activation_generation`, round number, verdict, the tree
and base it was judged against, and the review's `FINDING` lines. It is **evidence, not a
counter** — carried across a `resume` untouched, like the stored reports and
`manual_accepts`. It changes no behaviour on its own; later work reads it to give the
reviewer memory of its own prior verdicts, to detect an oscillating finding, and to
escalate a phase that is demonstrably stuck. Every consumer treats `state.json` as
untrusted: stored finding lines are re-validated before rendering, and any tree id from an
entry is resolved through `gitsnap.checked_tree` before it reaches a git command line.

### The clarify channel

`arl.sh clarify --question "…"` asks the reviewer one prose question about the review it
just gave, without attempting a commit or spending a new round. It is Claude-invocable —
it parses no `VERDICT`, writes nothing that can approve anything, and reaches
`hook.pass_()` under `ACTIVE` without any gate change. It runs **cold and session-less**,
against `bundles/<seq>/` for the most recent `round_history` entry of the current phase —
never the `reviewer_session` continuity pointer, which under `cold_confirm` may name a session
whose continued `APPROVED` was overridden by a cold `CHANGES_REQUIRED` the acting verdict came
from. The
question is written to `context/<n>-question.txt` (a sibling of `bundles/`, inlined with
`-f`, never re-openable by path) wrapped in an evidence-not-instruction fence. Two counters
bound and number it: `clarifications` (capped by `max_clarifications`, reset by `resume`)
and `clarify_seq` (carried across a `resume` like `report_seq`, since it names files under
the copied-forward `context/`). A clarify leaves every `hooks.Activation` field and
`round_history` byte-identical.

Discovery is the hard part: across two full activations Claude reached for it zero times.
So every **phase-scoped** blocking verdict — the commit gate's `CHANGES_REQUIRED` and the
Stop sweep's — carries `report.with_clarify_hint`, a paragraph of its own naming the exact
command and what is left of the allowance ("Clarifications left: N of M"), and both
`/adversarial-review-loop:implement` and `:resume` name it in "Rules while the mode is active".
The hint is omitted once nothing is left, and never appears on the **final** cumulative
review: `clarify` binds to the latest `round_history` entry of the current phase's label,
and a final review writes none.

## One commit, start to finish

```text
1. Claude runs: git add -A && git commit -m "…"
2. PreToolUse (pretool) intercepts it:
     - classify the command (cmdshape.py) — must be one of a small allowed shape
     - snapshot the prospective tree with a throwaway git index (gitsnap.py) —
       committed + staged + unstaged + non-ignored untracked, real index untouched
     - same tree as the last approval? → allow immediately, no review (cache hit)
     - otherwise: build a review bundle (diff chunks, the frozen plan, prior
       revisions, verify output) and run the reviewer (reviewer.py,
       through the configured harness)
     - reviewer approves → record `pending_approved_tree`, allow the commit
     - reviewer finds something, or the run fails operationally → deny,
       findings (or the operational reason) go back to Claude inline
3. The commit actually runs (or doesn't — that's between Claude and git)
4. PostToolUse (confirm-commit) fires:
     - HEAD moved exactly once, parent == pre-command HEAD (rules out --amend),
       HEAD^{tree} == the tree that was approved, worktree clean →
       phase advances, `last_approved_tree` updates
     - anything else → RECONCILE, with the specific mismatch and a prescribed,
       non-automatic recovery (never an automatic `git reset`)
5. (If the Bash call itself failed, PostToolUseFailure clears the now-stale
   pending approval instead of leaving it around for a different commit to
   consume.)
```

## What blocks a commit

One rule: **`actionable=yes` AND `severity >= block_severity`**. The default
`block_severity` is `medium`, so an actionable `low` finding is recorded but does not block;
lowering it to `low` restores the stricter behaviour, raising it further is a deliberate
relaxation.

**From the second round of a phase on, one more condition applies.** A finding that clears
`block_severity` blocks only if its path is in *Changed since round N-1* (the paths that
moved between the previous round's tree and this one), or an earlier round already raised a
finding in that file, or its severity is at or above `late_block_severity` (default `high`).
Anything else is **deferred**: still reported, still recorded in the round history, shown in
full on the approval message and in the stored report — but it does not block *that*
approval. Deferral is one approval's grace, not a dismissal: if the same phase is reviewed
again (a Stop sweep approved, then more edits, then the commit), that file is now a known
finding and it blocks. The point is a reviewer that raises a brand-new medium in an
untouched file on round 4 of a converging phase — measured at 11 of 45 rounds in a real run,
and behind both manual `accept`s — no longer stalls the phase over it; `late_block_severity
medium` restores the stricter behaviour, and it can only ever defer, never widen (set below
`block_severity` it reads as `block_severity`). The scope fails closed: round 1, a `final`
review, a previous-round tree that no longer resolves, or a round history that does not
validate line by line all mean the ordinary rule — every finding at or above
`block_severity` blocks — and a `git diff` that cannot list the changed paths is a bundle
failure, never an approval.

The reviewer's own `VERDICT` line is advisory. The gate recomputes the verdict from the
`FINDING` lines and the stricter of the two wins — an `APPROVE` alongside an actionable
critical finding still blocks.

**Nothing converts a failure into an approval.** Missing contract markers, a missing
verdict, a non-zero exit, a timeout, an empty response, a diff above the hard ceiling, or
more findings than the cap — every one of those blocks or escalates to `needs-human`.
Findings are never silently trimmed: above `max_findings` the gate escalates and keeps the
full report on disk rather than showing a shortened list.

The findings block is parsed strictly, and anything else in it is a failed review rather
than a line to skip. `severity` must be one of `info`, `low`, `medium`, `high`, `critical`
and `actionable` must be `yes` or `no`; there must be exactly one marker pair, in order,
holding exactly one `VERDICT`. The gate cannot tell a reviewer's typo from a finding it
failed to understand, so `actionable=maybe` on a critical finding blocks instead of quietly
not counting.

Every `FINDING` line comes back inline in the denial. Prose is what truncates, never the
actionable set.

## The reviewer harness

Which CLI actually performs the review is a seam, not a hard-coded name. `harness/__init__.py`
holds the `Harness` and `SessionStrategy` protocols and a registry keyed by the `harness`
config value; `harness/opencode.py` and `harness/claudecode.py` are the two implementations.
The default is `claude-code` — the gate ships as a Claude Code plugin, so that is the reviewer
every user already has — and `opencode` is one config key away.

Everything that decides an *outcome* stays in `reviewer.py` and never learns which CLI ran:
bundle building, staging and manifest verification, the `FINDING`/`VERDICT` contract, the
cold-approval invariant, `round_history`, the retry classes. A harness answers with a
`Command` — argv, environment *overrides*, optional stdin, optional working directory — and
`reviewer.py` runs it. It reads no verdict and writes no state.

The two implementations differ in three visible ways, and each difference is measured rather
than assumed (`tests/STEP0.md`):

|  | OpenCode | Claude Code |
| --- | --- | --- |
| prompt and attachments | one argv element, attachments inlined by `-f` | one payload on stdin, attachments inlined between per-run fences |
| read access | `OPENCODE_PERMISSION`, `--dir <repo>` | `--tools Read,Grep,Glob`, `--strict-mcp-config`, `--add-dir <repo>` |
| sessions | *discovered* after the run, by matching a unique `--title` in `session list` | *assigned* before it, `--session-id` / `--resume` |

**Both inline their attachments, and that is load-bearing rather than incidental.** It is what
keeps `context/` — the only model-derived evidence the gate ever produces — from existing at a
path the reviewer could re-open, which is what makes a cold confirmation structurally unable to
have seen model-authored prose. See [security.md](security.md).

### What each one actually runs

Both spellings below are what the gate composes; `make dry-run` prints the exact one for
your configuration — argv, env overrides, cwd and stdin — without spending a model call, and
is the cheapest way to inspect a change to either.

**`claude-code` (the default).** The prompt and every attachment go in on **stdin**; nothing
repo-derived or bundle-derived is named in the argv.

```bash
claude -p --output-format json --model "$model" \
  --tools "Read,Grep,Glob" --strict-mcp-config --safe-mode --disable-slash-commands \
  --session-id "$uuid" --add-dir "$repo" --add-dir "<act_dir>/bundles" \
  < "prompts/reviewer-phase.md + range.txt + changes.00.diff [+ …], fenced"
```

- `--tools` plus `--strict-mcp-config` make the reviewer structurally read-only: measured,
  `--tools` on its own still left every connected MCP server's tools in the session,
  write-capable ones included. Those two are unconditional; `--safe-mode
  --disable-slash-commands` is what `pure` selects, and it takes your `CLAUDE.md`, hooks,
  plugins, agents and skills out of the review
- `--add-dir` grants read of the repository and of this activation's **bundles root** —
  never the activation directory itself, which also holds `state.json`, the frozen plan and
  the reports. The run's own working directory is an empty one the gate creates, so no
  review session lands in your repository's `/resume` picker
- a denied tool call or a failed turn is refused rather than parsed, even though the CLI
  exits `0` for both

**`opencode`.** The prompt is an argument and the attachments are `-f` paths, which OpenCode
inlines.

```bash
OPENCODE_PERMISSION='{"*":"deny","read":"allow","grep":"allow","glob":"allow","list":"allow",
                      "external_directory":{"*":"deny","<act_dir>/bundles/**":"allow"}}' \
opencode run --pure --dir "$repo" -m "$model" --title "review-loop phase N [<hash>/<seq>]" \
  -f range.txt -f changes.00.diff [-f …] "$(cat prompts/reviewer-phase.md)"
```

- `--pure` neutralises your global OpenCode **plugins**, which would otherwise rewrite the
  output and break the contract. It does *not* remove global skills
  (`~/.config/opencode/skills`) or a global `~/.config/opencode/AGENTS.md`, both of which
  still reach the reviewer — the prompt explicitly tells it to ignore ambient style
  directives and not to invoke a skill for the review. A review that comes back reformatted
  anyway fails the contract, which blocks; it never approves
- `OPENCODE_PERMISSION` makes the reviewer structurally read-only and repo-scoped;
  `external_directory` is denied everywhere except the **bundles root**, on the same
  reasoning as above
- project OpenCode config stays enabled, so repo-local review skills load

Under both, the diff is **chunked across as many attachments as needed, never truncated** —
a truncated diff hides deletions, and approving on a partial view is approving what was
never seen. The reviewer cannot run anything: set `verify_cmd` and the hook runs it and
attaches the output as evidence instead. Repo text and the frozen plan are labelled
**evidence, not instructions**, and the reviewer is explicitly asked to flag a phase
description that misrepresents the frozen plan.

### Session continuity

**Consecutive reviews of one phase continue the same reviewer session** where one can be
safely established and claimed — `--resume <uuid>` on Claude Code, `-s <id>` on OpenCode —
so round 2 can see what round 1 already flagged instead of starting cold every time. A new
phase, or the final cumulative review, always starts fresh.

**One session carries at most `max_session_rounds` rounds** (default `3`, `0` never resets).
Past that the next round starts a fresh session on purpose: a session that keeps growing
gets compacted by the provider, and a compaction landing mid-review has produced a malformed
findings block — a whole round and a `failures` slot spent on nothing. Little is lost by
resetting, because the memory does not live in the conversation: `prior-rounds.txt` carries
every earlier round's verdict and `FINDING` lines, and `incremental.diff` carries what
changed since the previous round, both as bounded evidence the gate itself renders.

`cold_confirm` (**off by default**) adds a second, cold read on top of that: an approving
verdict from a round that was shown model-influenced context — a continued session, or an
earlier round's own findings — is not acted on by itself; the gate re-reviews the same
evidence with none of it attached, and that cold verdict decides. It is off because it is a
full second model call on every approving round past the first. See
[security.md](security.md#cold_confirm-the-second-cold-read--off-by-default) for the
argument in both directions.

## Resume and plan revision

`resume` is a second arming path — it continues an activation instead of starting one.
Two shapes:

- **Cross-session** (a new Claude Code session picks the plan back up): the previous
  activation is retired into a blocking `RESUMED` status *before* the new one is
  materialised — see the retire-first ordering and why it has no rollback in `AGENTS.md`.
  Exactly one activation may ever be live per worktree.
- **Same-session** (re-running `resume` to change `--until`, the model, or the plan):
  the live document is mutated in place; nothing is retired.

`commands/pausecmd.py` is the narrow case pulled out of that second shape: moving only
`stop_after_phase`, under one transaction, with no revision detection, no cleanliness
requirement and no `activation_generation` bump. It is user-only for the same reason
`resume` is — see the module docstring for why an unbounded, Claude-reachable pause would be
a strictly better `defer`.

Either way, the baseline tree and every already-approved tree carry forward untouched —
the successor is built by copying the whole predecessor document and resetting a *named*
set of fields (session identity, pending state, counters), never by re-listing what to
keep. A plan revision writes an immutable, additively-numbered copy
(`plan.rev<n>.md`) rather than editing anything already frozen, and `--replan` grants a
one-shot, fenced permission to redefine phases from the current one onward through the
ordinary `set-phases` command.

## On disk

```text
$XDG_STATE_HOME/adversarial-review-loop/
├── sessions/<session-id>            → which worktree this session armed
└── worktrees/<sha256(worktree)>/
    ├── latest                       → which session owns this worktree right now
    └── <session-id>/
        ├── state.json               the activation's whole state
        ├── plan.frozen.md           revision 0, exactly as arm wrote it
        ├── plan.rev<n>.md           later revisions, each immutable once written
        ├── phases.frozen            the split phase list
        ├── reports/NNN-*.md         every review, in full, never deleted
        ├── raw/NNN-<label>[-cold].out  the reviewer's own transcript for report NNN — never inside bundles/
        ├── raw/NNN-<label>-repair.out  a contract-repair call's transcript, when the review's own block was malformed
        ├── raw/NNN-clarify.out      a clarify exchange's transcript (NNN is clarify_seq, not report_seq)
        ├── context/NNN-prior-rounds.txt  earlier rounds' verdicts+findings for report NNN — a sibling of bundles/, passed with -f and never re-openable by path; omitted from a cold confirmation
        ├── context/NNN-question.txt  a Claude-composed clarify question (NNN is clarify_seq) — same sibling directory, same -f-only channel
        ├── context/NNN-repair.txt   the fenced tail of a malformed transcript, the only thing a repair call is shown besides range.txt
        └── bundles/NNN/             gate-generated evidence shown to the reviewer for report NNN — no model output, ever
```

A bundle holds `range.txt`, the `changes.NN.diff` chunks, `incremental.diff` from round 2 on,
`verify.txt` when a `verify_cmd` is configured, and `plan.rev<n>.md` **only once the plan has
actually been revised** — `range.txt` already inlines the active revision in full, so with an
unrevised plan a `plan.rev0.md` would be a byte-identical second copy of it in the same
payload, re-read on every agentic turn of the review. See [configuration.md](configuration.md#cost).

Nothing here lives inside the repository under review, with one narrow exception —
`config <key> <value> --repo`, an explicit user-only write to the repo's own
`.adversarial-review-loop.json`. See [security.md](security.md) for why that boundary matters
and exactly what does and doesn't cross it.

## What the hot path costs

A `PreToolUse` hook runs on **every** tool call, and it is not free. Measured on Linux with
warm caches, through `scripts/arl.sh` end to end:

| Path | Latency | Processes |
| --- | --- | --- |
| tool call outside the armed worktree | ~111 ms | 5 |
| read-only tool (`Read`, `Grep`, `Glob`, …) | ~111 ms | 4 |
| mutating tool (`Edit`, `Write`, MCP) | ~111 ms | 4 |
| `Bash` (non-commit) | ~111 ms | 4 |

Processes are counted the way `tests/selftest.sh`'s own hot-path check counts them: every
successful `execve` under `strace -f`, end to end through `scripts/arl.sh`. Four is the
shim's fixed floor regardless of branch — the shebang's `env`, the `bash` shim itself, the
`timeout` wrapper, and `python3` — plus one more `git rev-parse` when `cwd` is outside the
armed worktree and has to be placed. Config and state are in-process file I/O, so that part
of the process count does not vary by branch. The read-only hoist still exists and still
matters (it is what makes a future read-only deny unreachable without deliberately undoing
it) — it just shows up as less work inside the one Python process, not as fewer processes
around it.

Latency does not track the process count, and the reason is `scripts/arl.sh` itself: every
hook call is wrapped in `timeout <N>` (below the timeout Claude Code enforces — see
"Interpreter invocation" in [`AGENTS.md`](../AGENTS.md)), so a hung parser still denies
before the host tears the hook down with nothing. On this machine that wrapper alone
measured **~90 ms**, flat, on every call — `timeout` here is `uutils-coreutils`' Rust
reimplementation, and it appears to poll on a fixed interval rather than waking when the
child exits (confirmed with `strace`: the child exits immediately, `timeout` still sleeps
out the rest of its interval). The raw interpreter cost underneath it, `python3 -I` running
the gate directly with no wrapper, measured **~45 ms**. Neither the wrapper's cost nor its
presence is optional — Rule 1's "a hung parser must deny" needs it — but which number you
see depends on which `timeout` your system runs: GNU coreutils' does not have this
behaviour.

A commit pays two things more. The bash parser builds its LALR tables at import, measured at
**~55 ms**, and it is imported only when a command already looks like a commit — a `Read`
never pays for it. And then the review itself, which is measured in minutes, not
milliseconds.

## Module map

| Path | Owns |
| --- | --- |
| `scripts/arl.sh` | the guarded shim every hook actually invokes — interpreter probe, `timeout` wrapping, fail-closed fallback on any non-zero exit |
| `scripts/arl-bootstrap.py` | trusted absolute entrypoint; sets up `sys.path` and `sys.pycache_prefix` before anything else imports |
| `scripts/arl/cli.py` | subcommand dispatch |
| `scripts/arl/paths.py` | state-directory layout, path safety checks |
| `scripts/arl/atomic.py` | durable, private writes — `write_private_atomic` (state root) and `write_atomic` (the one repo-writing path) |
| `scripts/arl/hookio.py` | hook payload parsing and the fail-closed decision emitters |
| `scripts/arl/config.py` | precedence: env → activation overrides → repo file → user file → defaults |
| `scripts/arl/state.py` | `state.json`, session pointers, `STATE_VERSION` migration, effective status |
| `scripts/arl/gitsnap.py` | the throwaway-index tree snapshot, oversized-file guard, submodule detection |
| `scripts/arl/cmdshape.py` | deny-list plus a real bash parser (vendored bashlex) deciding whether a commit command may run |
| `scripts/arl/globmatch.py` | glob matching for `ignore_globs`, reimplemented rather than shelled out |
| `scripts/arl/reviewer.py` | bundle building, staging, running the reviewer command, output-contract parsing |
| `scripts/arl/harness/` | the reviewer-CLI seam — the `Harness`/`SessionStrategy` protocols and the registry, plus one module per implementation (`opencode.py`, `claudecode.py`) |
| `scripts/arl/reviewer_probe.py` | the `opencode models` reachability probe, reached through the OpenCode harness; a CLI that cannot enumerate its models has none |
| `scripts/arl/planrev.py` | plan-revision bookkeeping — backfilling revision 0, path/hash verification, the active revision |
| `scripts/arl/report.py` | report storage; the text Claude actually sees |
| `scripts/arl/commands/` | one module per subcommand — `arm`, `resume`, `phases`, `session`, `configcmd`, `completion`, `dryrun`, `accept`, `clarify`, `pausecmd`, plus the four hook entrypoints |
| `scripts/arl/_vendor/bashlex/` | vendored parser, kept diffable against upstream |
| `prompts/*.md` | the fixed reviewer prompts — one per review kind, plus `reviewer-efficiency.md`, the working guidance each harness delivers its own way (a system prompt where the CLI has one) |
| `skills/*/SKILL.md` | the nine slash commands |

## Testing

`tests/selftest.sh` drives the hook entrypoints with synthetic payloads against scratch
git repos, with the reviewer replaced by `tests/fixtures/fake-reviewer.sh` — no model is
ever called. `tests/unit/` is the pytest suite for the Python modules directly.
`tests/STEP0.md` is a separate runbook for the handful of things only a live Claude Code
session can settle (skill-hook registration, prompt-expansion, argument handling) — see
[edge-cases.md](edge-cases.md) for what's still open there.
