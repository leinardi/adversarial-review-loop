# Architecture

This is the engineering-level map: what runs when, what talks to what, and where state
lives. For the "why," see [`AGENTS.md`](../AGENTS.md) in the repo root — it's the
canonical authority for invariants a change must not break. This page describes the system
as it stands; it doesn't repeat the reasoning behind every design choice.

## The shape of it

```text
 Claude Code                          scripts/ocrl.sh (guarded Bash shim)
 ┌──────────────────┐                 ┌──────────────────────────────────┐
 │ skill invocation   ──!`…`─────────▶│  probes python3, wraps it in       │
 │ (prompt-expansion)  │               │  `timeout`, discards partial       │
 │                     │               │  output on any non-zero exit       │
 │ PreToolUse hook    ─────────────────▶  scripts/ocrl-bootstrap.py         │
 │ PostToolUse hook   ─────────────────▶    -I, absolute path, sets         │
 │ PostToolUseFailure ─────────────────▶    sys.pycache_prefix before       │
 │ Stop hook          ─────────────────▶    ocrl is ever imported           │
 └──────────────────┘                 └──────────────┬───────────────────┘
                                                       ▼
                                        scripts/ocrl/cli.py (subcommand dispatch)
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
                                   gitsnap.py                       reviewer.py
                              (throwaway-index                (bundle building, OpenCode
                               tree snapshot)                   invocation, contract parsing)
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

## The hook lifecycle

Four Claude Code hook events are registered, identically, by both `skills/implement/SKILL.md`
and `skills/resume/SKILL.md` (this duplication is deliberate — see
[edge-cases.md](edge-cases.md#hooks-registering-twice)):

| Event | Subcommand | Fires on | Can deny? |
| --- | --- | --- | --- |
| `PreToolUse` | `pretool` | every tool call | yes — this is the gate |
| `PostToolUse` | `confirm-commit` | after a `Bash` call returns | no — the tool already ran; it reports |
| `PostToolUseFailure` | `posttool-failure` | after a failed `Bash` call | no — it only clears stale pending state |
| `Stop` | `gate-stop` | when Claude tries to end its turn | yes — blocks the turn from ending |

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
| `NEEDS_HUMAN` | escalated; every mutation denied until the user intervenes | no — `stop` clears it |
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

`ocrl.sh clarify --question "…"` asks the reviewer one prose question about the review it
just gave, without attempting a commit or spending a new round. It is Claude-invocable —
it parses no `VERDICT`, writes nothing that can approve anything, and reaches
`hook.pass_()` under `ACTIVE` without any gate change. It runs **cold and session-less**,
against `bundles/<seq>/` for the most recent `round_history` entry of the current phase —
never the `reviewer_session` continuity pointer, which may name a session whose continued
`APPROVED` was overridden by a cold `CHANGES_REQUIRED` the acting verdict came from. The
question is written to `context/<n>-question.txt` (a sibling of `bundles/`, inlined with
`-f`, never re-openable by path) wrapped in an evidence-not-instruction fence. Two counters
bound and number it: `clarifications` (capped by `max_clarifications`, reset by `resume`)
and `clarify_seq` (carried across a `resume` like `report_seq`, since it names files under
the copied-forward `context/`). A clarify leaves every `hooks.Activation` field and
`round_history` byte-identical.

## One commit, start to finish

```text
1. Claude runs: git add -A && git commit -m "…"
2. PreToolUse (pretool) intercepts it:
     - classify the command (cmdshape.py) — must be one of a small allowed shape
     - snapshot the prospective tree with a throwaway git index (gitsnap.py) —
       committed + staged + unstaged + non-ignored untracked, real index untouched
     - same tree as the last approval? → allow immediately, no review (cache hit)
     - otherwise: build a review bundle (diff chunks, the frozen plan, prior
       revisions, verify output) and invoke OpenCode (reviewer.py)
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

## Resume and plan revision

`resume` is a second arming path — it continues an activation instead of starting one.
Two shapes:

- **Cross-session** (a new Claude Code session picks the plan back up): the previous
  activation is retired into a blocking `RESUMED` status *before* the new one is
  materialised — see the retire-first ordering and why it has no rollback in `AGENTS.md`.
  Exactly one activation may ever be live per worktree.
- **Same-session** (re-running `resume` to change `--until`, the model, or the plan):
  the live document is mutated in place; nothing is retired.

Either way, the baseline tree and every already-approved tree carry forward untouched —
the successor is built by copying the whole predecessor document and resetting a *named*
set of fields (session identity, pending state, counters), never by re-listing what to
keep. A plan revision writes an immutable, additively-numbered copy
(`plan.rev<n>.md`) rather than editing anything already frozen, and `--replan` grants a
one-shot, fenced permission to redefine phases from the current one onward through the
ordinary `set-phases` command.

## On disk

```text
$XDG_STATE_HOME/opencode-review-loop/
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
        ├── raw/NNN-clarify.out      a clarify exchange's transcript (NNN is clarify_seq, not report_seq)
        ├── context/NNN-prior-rounds.txt  earlier rounds' verdicts+findings for report NNN — a sibling of bundles/, passed with -f and never re-openable by path; omitted from a cold confirmation
        ├── context/NNN-question.txt  a Claude-composed clarify question (NNN is clarify_seq) — same sibling directory, same -f-only channel
        └── bundles/NNN/             gate-generated evidence shown to the reviewer for report NNN — no model output, ever
```

Nothing here lives inside the repository under review, with one narrow exception —
`config <key> <value> --repo`, an explicit user-only write to the repo's own
`.opencode-review-loop.json`. See [security.md](security.md) for why that boundary matters
and exactly what does and doesn't cross it.

## Module map

| Path | Owns |
| --- | --- |
| `scripts/ocrl.sh` | the guarded shim every hook actually invokes — interpreter probe, `timeout` wrapping, fail-closed fallback on any non-zero exit |
| `scripts/ocrl-bootstrap.py` | trusted absolute entrypoint; sets up `sys.path` and `sys.pycache_prefix` before anything else imports |
| `scripts/ocrl/cli.py` | subcommand dispatch |
| `scripts/ocrl/paths.py` | state-directory layout, path safety checks |
| `scripts/ocrl/atomic.py` | durable, private writes — `write_private_atomic` (state root) and `write_atomic` (the one repo-writing path) |
| `scripts/ocrl/hookio.py` | hook payload parsing and the fail-closed decision emitters |
| `scripts/ocrl/config.py` | precedence: env → activation overrides → repo file → user file → defaults |
| `scripts/ocrl/state.py` | `state.json`, session pointers, `STATE_VERSION` migration, effective status |
| `scripts/ocrl/gitsnap.py` | the throwaway-index tree snapshot, oversized-file guard, submodule detection |
| `scripts/ocrl/cmdshape.py` | deny-list plus a real bash parser (vendored bashlex) deciding whether a commit command may run |
| `scripts/ocrl/globmatch.py` | glob matching for `ignore_globs`, reimplemented rather than shelled out |
| `scripts/ocrl/reviewer.py` | bundle building, the OpenCode invocation, output-contract parsing |
| `scripts/ocrl/reviewer_probe.py` | the `opencode models` reachability probe, shared by `arm`, `resume` and `config` |
| `scripts/ocrl/planrev.py` | plan-revision bookkeeping — backfilling revision 0, path/hash verification, the active revision |
| `scripts/ocrl/report.py` | report storage; the text Claude actually sees |
| `scripts/ocrl/commands/` | one module per subcommand — `arm`, `resume`, `phases`, `session`, `configcmd`, `completion`, `dryrun`, plus the four hook entrypoints |
| `scripts/ocrl/_vendor/bashlex/` | vendored parser, kept diffable against upstream |
| `prompts/*.md` | the fixed reviewer prompts |
| `skills/*/SKILL.md` | the seven slash commands |

## Testing

`tests/selftest.sh` drives the hook entrypoints with synthetic payloads against scratch
git repos, with the reviewer replaced by `tests/fixtures/fake-reviewer.sh` — no model is
ever called. `tests/unit/` is the pytest suite for the Python modules directly.
`tests/STEP0.md` is a separate runbook for the handful of things only a live Claude Code
session can settle (skill-hook registration, prompt-expansion, argument handling) — see
[edge-cases.md](edge-cases.md) for what's still open there.
