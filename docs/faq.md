# FAQ

Short answers to the things that come up first. Every one links to the page that goes
deeper.

## Getting started

### How do I start using the plugin?

Install it (see the [README](../README.md#-install)), write a plan as a Markdown file, and
run `/adversarial-review-loop:implement plan.md`. Arming runs *before* Claude gets a turn:
it freezes the baseline commit and a copy of the plan, and checks the reviewer CLI is
reachable. Claude then proposes a phase split, freezes it, and starts implementing — every
mutation is denied until that list is frozen.

### Do I need a plan file?

Yes. `implement` takes a path and refuses without one; the frozen copy of that file is what
the reviewer is shown as evidence on every round. There is no required format — any Markdown
describing the work will do. It is capped at 64 KiB, and since it is re-sent to the reviewer
every session, a shorter plan is a cheaper one.

### Do I need OpenCode?

No. The default `harness` is `claude-code`, which is the CLI you are already running in.
`opencode` is one config key away if you want a second opinion from a different vendor's
model — see [configuration.md](configuration.md#per-run-overrides).

### Does it write anything into my repository?

No. `state.json`, the frozen plan, every stored report and every review bundle live under
`$XDG_STATE_HOME/adversarial-review-loop/`, keyed by worktree and session. The single
exception is `/adversarial-review-loop:config <key> <value> --repo`, an explicit user-only
write to the repository's own `.adversarial-review-loop.json`.

## Picking up where you left off

This is the part people get wrong, so read the table before reaching for a command.

**Nothing you do to a *conversation* disarms the loop.** The activation lives on disk, keyed
by *(worktree, session id)*, and survives Esc, quitting, `/clear`, a crash and a reboot.
Exactly four things change a binding: `/adversarial-review-loop:stop`, `ttl_hours` expiring,
a `resume` run from a *different* session (which retires yours), and an arm failure.

What you need depends on one thing only — whether you come back under the **same session id**:

| How you come back | Bound? | What to run |
| --- | --- | --- |
| `claude --resume` / `claude -c`, same session | yes | just `continue` |
| `/clear`, then `/resume` back to the original session | yes | just `continue` |
| a fresh `claude` in the worktree | no | `/adversarial-review-loop:resume --allow-dirty` |
| any of the above, but more than `ttl_hours` (24h) elapsed | `STALE` | `/adversarial-review-loop:resume --allow-dirty` |
| another session ran `resume` here while you were away | no, and permanently | carry on in *that* session; yours is `RESUMED` |

**When in doubt, run `/adversarial-review-loop:status` first.** It is read-only, works in any
session, and settles it: `ACTIVE` at the phase you expect means you are bound and can just
continue. A denial naming another session means you are not, and resume is the fix.

And never `implement` to pick something back up — it re-baselines from the current `HEAD` and
discards the phase list and every approval already earned.

### I quit Claude mid-phase with uncommitted work, and rebooted. Now what?

Your work is untouched and the loop is still armed. Which command you need depends on how you
restart:

**`claude --resume` (same session id) — type `continue`, nothing else.** The session is still
bound, and a `SessionStart` hook re-orients Claude automatically: which phase is in progress,
its frozen description, the frozen plan path with an instruction to re-read the relevant part,
what the last review of that phase concluded and the report number to read it, and the commit
rules still in force.

The dirty worktree needs no flag and no special handling. It *is* the phase in progress. The
snapshot taken when Claude finally commits covers committed, staged, unstaged and non-ignored
untracked content, so the work you left half-finished is reviewed as part of that phase's
commit exactly as if you had never quit.

**A fresh `claude` (new session id) — `/adversarial-review-loop:resume --allow-dirty`.** A
session with no pointer is *unbound*: every mutation and every commit in that worktree is
denied, naming the activation that holds it, until resume binds the session. `--allow-dirty`
is required, because resume otherwise refuses a dirty worktree — with it, the uncommitted work
is folded into the next phase's review rather than ignored. This retires the old session into
`RESUMED`, which is expected, not an error.

One thing `--allow-dirty` will *not* waive: a plan revision and `--replan` both require a
genuinely clean worktree. Redescribing a phase while half-finished work for it sits in the
tree is the exact failure that refusal exists to block.

### I ran `/clear`, worked elsewhere, then `/resume`d back to the original session

Just `continue`. You do not need `/adversarial-review-loop:resume`, and you do not need
`--allow-dirty`.

`/clear` starts a new conversation; it does not touch your activation, and no hook even fires
on it. While you were in the cleared session, that session was unbound — which is why it would
have denied any mutation *in the armed worktree* (working in a different repository is
unaffected). Coming back restores the original session id, and both its own pointer and the
worktree's `latest` still name it: bound, `ACTIVE`, gate live, with the same `SessionStart`
re-orientation as above. Your uncommitted work is still the phase in progress.

**The one way this breaks:** if the intervening session ran `/adversarial-review-loop:resume`
in that same worktree, it took ownership and retired the original to `RESUMED`. Going back
then denies everything, naming the session that took over, and there is no route back — you
continue in the newer session instead. Working in a *different* repository in the meantime is
always safe.

### I ran `/clear` and I'm staying in the new session. Is my progress gone?

No. The gate is session-scoped but the activation is not: it lives on disk, keyed by worktree,
and nothing about it reverts. Run `/adversarial-review-loop:resume` in the new session — it
binds it, keeps the original baseline and every approval already earned, and refreshes the
TTL. Add `--allow-dirty` if you left uncommitted work.

`/clear` between phases is in fact the recommended way to run a very long plan: pause at a
phase boundary, clear, and resume with a fresh context rather than relying on compaction
([how-it-works.md](how-it-works.md#long-plans-and-the-context-window)).

### It says `STALE`. What happened?

`ttl_hours` (default 24) elapsed since the activation was armed. It never silently disarms —
every mutation is denied, and each turn ends with a message naming the fix. That message is
`/adversarial-review-loop:resume`, which refreshes
`armed_at`, **not** a fresh `implement`, which re-baselines from the current `HEAD` and throws
away the phase list and every approval
([edge-cases.md](edge-cases.md#a-stale-activation)).

A `STALE` activation needs resume even under the same session id. If you come back with
`claude --resume` after more than a day, the re-orientation hook notices and tells Claude to
stop and hand it back to you rather than keep implementing.

## Running a long plan

### Can I implement only part of a plan?

Yes. `--until N` on `implement` or `resume` stops the loop asking for more once phase `N` is
committed — the turn ends with a "paused, not an approval of the whole plan" message. Carry
on later with `/adversarial-review-loop:resume --until 0`, which clears the target. The
target is soft: it changes what the Stop gate insists on, not what the commit gate enforces
([edge-cases.md](edge-cases.md#pausing-is-a-soft-target-not-a-fence)).

### How do I stop partway through a plan?

Two different things, and it matters which one you want:

| Want | Do | Where it leaves you |
| --- | --- | --- |
| stop **now** | <kbd>Esc</kbd> | mid-phase: dirty worktree, phase uncommitted and unreviewed |
| stop at a **clean boundary** | <kbd>Esc</kbd>, then `/adversarial-review-loop:pause`, then `continue` | phase finished, reviewed and committed; worktree clean; turn ends paused |

**<kbd>Esc</kbd> on its own is already a pause.** The turn stops, nothing further runs, and
none of the loop's state changes. Type `continue` whenever you like and it picks up where it
was.

**`/adversarial-review-loop:pause` does not stop anything.** It sets the pause target, so the
loop stops *asking for the next phase* once the current one is committed. <kbd>Esc</kbd>
appears in that recipe only because you need the prompt back to type a slash command — the
`continue` afterwards is what lets Claude finish and commit the phase it was on.

Prefer the second one before shutting the machine down, upgrading the plugin, or leaving it
for a day: it is the difference between coming back to a half-written phase and coming back to
a committed one. After a bare <kbd>Esc</kbd> the phase is uncommitted, so returning in a *new*
session puts you in the `resume --allow-dirty` case above.

Telling Claude "pause after this phase" in prose does **not** work, and never did. The pause
target is user-only, Claude has no route to it, and the Stop gate will send it straight back
into the next phase. The command is that route.

To start again afterwards, clear the target: `/adversarial-review-loop:resume --until 0`. A
reached target stays set, so a bare `resume` continues the activation but still ends every
turn paused ([edge-cases.md](edge-cases.md#pausing-is-a-soft-target-not-a-fence)).

### Can I change the plan partway through?

Yes, for phases that have not started. Pass `--plan <path>` to `resume`, or just edit the
file resume already has on record — if its bytes changed, resume freezes a new, timestamped
revision (`plan.rev<n>.md`) beside every earlier one; nothing already frozen is edited.
Add `--replan` and phases from the current one onward may be redefined with `set-phases`.
Committed phases are immutable: their descriptions were the evidence a review already ran
against ([edge-cases.md](edge-cases.md#revising-the-plan-mid-run)).

## When the review blocks you

### The reviewer keeps finding new things and the phase never converges. What now?

`/adversarial-review-loop:accept [reason]`. It puts the current working tree, exactly as it
stands, into the set of approved trees — the same record a passing review would have written
— without calling the reviewer. The phase does not advance and the activation does not
complete, so any further edit puts the commit right back under review.

Nothing is hidden by it: the acceptance shows up in `status`, gets its own numbered report,
and is named in a `## Manually accepted phases` section shown to every later review of the
activation. The *repeating* case is caught automatically — `stall_rounds` (default 3)
escalates a blocking finding that persists, reappears or gets reversed — but a reviewer
raising a genuinely new objection every round never trips that, and `accept` is the only
bound for it ([edge-cases.md](edge-cases.md#a-phase-that-never-converges)).

### The reviewer said `APPROVE` but my commit was denied. Why?

The reviewer's own `VERDICT` line is advisory. The gate recomputes the verdict from the
`FINDING` lines and the stricter of the two wins, so an `APPROVE` alongside an actionable
finding at or above `block_severity` still blocks
([architecture.md](architecture.md#what-blocks-a-commit)).

### Why was my `git commit --amend` — or `make build && git commit` — denied?

A snapshot taken before a compound command means nothing if the command then changes files,
so the gate accepts only a small set of commit shapes: `git commit`, optionally chained with
`git add` and `git status`. `--amend`, pathspecs, `--only`/`--include`, `git -C`, pipes,
redirection and command substitution are all denied. Run builds, tests and `git rm` as their
own Bash calls — the next snapshot picks up the result
([security.md](security.md#the-accepted-shapes)).

### How do I write a multi-paragraph commit message?

Repeated `-m`, one per paragraph — git joins them with a blank line:

```console
git add -A && git commit -m "feat(x): subject" -m "First paragraph." -m "Second paragraph."
```

The two obvious alternatives are both denied. A real newline inside `-m "…"` is refused
before the parser even runs, because a newline is a statement separator in shell and the
deny-list will not try to tell the two uses apart. `git commit -F msg.txt` is refused
because it reads the message from a path that may change after the snapshot. Repeated `-m`
is the supported form, not a workaround.

### The gate says `RECONCILE`. What do I do?

A commit landed that is not the one that was approved: `HEAD` moved more than once, its
parent was not the pre-command `HEAD` (an amend), its tree is not the approved tree, or the
worktree was left dirty. `RECONCILE` names the specific mismatch and gives one exact,
non-automatic recovery — usually `git reset --soft <last known-good commit>`, the only
mutation it permits. Nothing auto-corrects your worktree.

The usual cause is a background writer: an editor saving a buffer, an MCP server dropping a
state directory, a file watcher. Gitignore those paths before arming rather than fighting the
reconcile ([edge-cases.md](edge-cases.md#reconcile--a-commit-didnt-land-the-way-it-was-approved)).

### The gate says `NEEDS_HUMAN`. What do I do?

That is the escalation: the reviewer is no longer invoked and every mutation is denied until
a person intervenes. It is reached by exhausting `max_failures` or `max_transient_failures`,
by a review whose findings exceed `max_findings`, by a diff above `hard_diff_ceiling`, by a
stalled or oscillating blocking finding, or by too many no-progress Stop blocks.

Two routes out, both user-only: `/adversarial-review-loop:accept [reason]`, which clears the
escalation and leaves the mode armed, or `/adversarial-review-loop:stop`, which leaves the
mode entirely. `resume` deliberately refuses — an escalation that a resume could clear would
not be an escalation.

A `STALE` activation never lands here: the Stop gate ends those turns rather than counting
them, precisely so the TTL cannot manufacture an escalation only `accept` could clear
([edge-cases.md](edge-cases.md#a-stale-activation)).

### The review timed out, or hit a rate limit

Those are counted separately from ordinary failures, against `max_transient_failures`
(default 5), and paced with backoff (30s, doubling, capped at 300s). The next commit attempt
is denied with the remaining wait rather than spending another provider call on a limit that
has not reset. Raise `timeout_sec` if a large phase genuinely needs longer than 15 minutes
([edge-cases.md](edge-cases.md#transient-failures-are-counted-and-paced-separately)).

### Where do I read what the reviewer actually said?

`/adversarial-review-loop:report` for the latest, `/adversarial-review-loop:report <n>` for a
specific one. Reports are stored in full and never deleted — the denial message truncates
prose, never the `FINDING` lines, and the stored report truncates nothing at all.

## Cost, models, and customisation

### How do I make reviews cheaper or faster?

In order of effect: a smaller `model`, `max_session_rounds 1` (never resume a reviewer
session — under Claude Code a resumed one replays every earlier attachment and costs *more*
per turn), `variant low`, and a shorter plan, since the frozen plan is re-sent every session.
Each round's actual cost is printed in its stored report, and `status` totals the activation
([configuration.md](configuration.md#cost)).

### How do I customise what the reviewer looks for?

Set `review_guide` to a Markdown file — the invariants that matter in your repo, the
subsystems where a regression is expensive, the classes of finding that are noise here. Its
content is spliced into the phase and final prompts as an additive extension of "what to look
for":

```console
> /adversarial-review-loop:config review_guide .arl/review-guide.md --repo
> /adversarial-review-loop:implement plan.md --guide docs/review-guide.md
```

It is bounded rather than trusted: it may add areas of concern, and may not change the output
contract, the severity rubric, what blocks, or ask for an approval — any attempt to is
reported as a `high` finding against the guide file itself. It is frozen and hashed when you
arm, so editing the source file mid-activation changes nothing; `resume --guide <path>`
replaces it, recording a new revision. The prompts in `prompts/` are plugin files: changing
those means forking ([configuration.md](configuration.md#repo-specific-review-guidance)).

### Can I make the reviewer run my tests?

Not the reviewer — it is structurally read-only and cannot execute anything. Set `verify_cmd`
instead (`"verify_cmd": "make test"`) and the *hook* runs it, attaching the output to the
bundle as evidence. Note that this is real code execution from a config file the repository
under review can contain, which is why repo config is treated as untrusted input
([security.md](security.md#repo-config-is-attacker-controlled-input-full-stop)).

### Can I skip review for docs-only commits?

`ignore_globs` does that — a commit whose changed paths *all* match skips the reviewer
entirely. Be clear-eyed about it: this is a full bypass, not a relaxation. `["**"]` disables
every per-commit review with no model ever consulted. A commit touching even one non-ignored
file still gets a full review of the whole diff, ignored paths included
([edge-cases.md](edge-cases.md#oversized-files-and-ignore_globs)).

### Does the final whole-plan review run?

Not by default. `final_review` is `false`, so the Stop gate completes the activation directly
once every phase is committed and the sweep finds nothing unreviewed. Turn it on with
`/adversarial-review-loop:config final_review true`, or get one for a single plan with
`/adversarial-review-loop:finish`, which ignores the key.

It has to happen *before* the activation closes: a `COMPLETE` activation can never be
reviewed cumulatively, and both `finish` and `resume` refuse one. What you give up by leaving
it off is the cross-phase view — phase 7 quietly undoing phase 2, dead ends, interface drift
— and on a long plan that is also where the cumulative diff is least readable in one pass
([edge-cases.md](edge-cases.md#completing-without-a-final-cumulative-review)).

### Does it work in a git worktree, or on a repo with no commits yet?

Worktrees, yes — state is keyed by worktree path, and exactly one activation may be live per
worktree at a time. A repository with *no commits at all* arms, but cannot complete without a
review: the activation has no baseline commit to anchor its phase evidence to, so the Stop
gate escalates rather than trusting `state.json`. Make one commit first, or run `finish`.
