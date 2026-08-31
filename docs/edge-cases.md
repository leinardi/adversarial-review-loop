# Edge cases

What happens when things aren't the straightforward case, and why it happens that way.
Every behaviour here is deliberate — if one of these looks like a bug, it's very likely the
gate working as designed against a scenario worth understanding before you hit it live.

## At a glance

| Condition | Behaviour |
| --- | --- |
| Dirty worktree at arm time | `ARM_FAILED`; `--allow-dirty` folds the dirt into phase 1's review |
| `opencode` missing or model unreachable | `ARM_FAILED`, naming the failure |
| Any arm failure | Persisted **before** exit; all mutations and commits denied until re-armed or stopped |
| Arming never executes (refused sandbox, unreadable script) | The `UserPromptSubmit` hook recorded that arming was asked for; the next hook call records `ARM_FAILED` itself and denies |
| Git cannot be run from the hook, or the working directory is gone | Denied: the gate cannot tell whether an armed worktree guards the call, and does not guess |
| A session that never ran `implement`/`resume` opens an armed worktree (a fresh `claude`, a resumed session under a new id) | Every mutation and commit is denied until `/adversarial-review-loop:resume` binds the session; the other session's activation is left untouched |
| Mutation before `set-phases` | Denied, with the exact command to run |
| Turn ends while `ARM_FAILED` or phases unset | `Stop` blocks with instructions; the reviewer is never called |
| Timeout, malformed output, missing verdict, non-zero exit | `OP_FAILURE` → deny; never an approval |
| `OP_FAILURE` past `max_failures` | `NEEDS_HUMAN`; the reviewer is no longer invoked |
| Findings past `max_findings` / `max_findings_bytes` | `NEEDS_HUMAN`, full report retained; never trimmed, never approved |
| Diff above `hard_diff_ceiling` | `NEEDS_HUMAN` |
| Commit lands ≠ reviewed tree, or is an amend | `RECONCILE` with a prescribed, non-automatic recovery |
| `git reset --soft` before the activation commit | Denied (equal to it is allowed — that is the phase-1 recovery) |
| Activation older than `ttl_hours` | `STALE`: gates block and ask for a re-arm. **Never a silent disarm** |
| No-progress `Stop` blocks past `max_stop_blocks` | `NEEDS_HUMAN`, loud system message. **Not** an approval |
| Claude tries `arl finish` / `deactivate` / `resume` / `config` via Bash | Denied — user-only |
| Any mutation against a `RESUMED` activation | Denied, naming the session that took over; re-arm with `implement` is the only way out |
| A resume retires its predecessor, then fails before publishing the successor | No automatic rollback: predecessor stays `RESUMED`, successor is `ARM_FAILED` — both deny, and the recovery is `implement` |
| `resume` with an approval still pending confirmation | Refused, unless `--abandon-pending` |
| `resume` after history was rewritten (the activation commit is no longer an ancestor of `HEAD`) | Refused; re-arm |
| A decided plan revision, or `--replan`, on a dirty worktree | Refused — **not** waived by `--allow-dirty` |
| `--replan` before the phase list has ever been frozen | Refused; there is nothing yet to replan |
| A `state.json` predating the plan-revision feature | Migrated on first resume; `ARM_FAILED` (not a crash) if its `plan.frozen.md` is missing |
| A recorded plan revision whose file fails a path or hash check | `NEEDS_HUMAN` — never attached to a review, never silently skipped |

## A stale activation

Every activation has `armed_at`. Once `ttl_hours` (default 24) has passed, every gate
answers `STALE` instead of the stored status — derived at read time, never a separate
timer, and it never silently disarms; it blocks and tells you what to do.

The fix is `/adversarial-review-loop:resume`, not a fresh `implement`. `resume` refreshes
`armed_at` — which is what un-stales it — while `implement` re-baselines from scratch,
throwing away the phase list and every approval already earned. `resume` is explicitly
designed to continue a `STALE` activation: it re-verifies the things that actually matter
(the worktree, whether history was rewritten, whether anything landed unreviewed) rather
than blindly trusting old state. `ttl_hours` mainly exists to catch a worktree nobody is
actively driving anymore — a long-running plan you're actually coming back to is exactly
what `resume` is for.

## RECONCILE — a commit didn't land the way it was approved

`PreToolUse` approves a *tree*, not a commit — the actual `git commit` still has to run,
and anything can happen to a worktree between approval and verification: a partial `git
add`, an `--amend`, a background process writing a file back. `PostToolUse` checks four
things after a commit-shaped command runs: `HEAD` moved exactly once, its parent is the
`HEAD` from before the command, its tree is exactly the approved tree, and the worktree is
clean. If any of those fail, the activation enters `RECONCILE`.

`RECONCILE` is not a re-review — it names the specific mismatch and gives an exact,
non-automatic recovery (typically `git reset --soft <the last known-good commit>`, which is
the only mutation `RECONCILE` permits, and only to that exact target). Nothing here
auto-corrects the worktree; a bad commit sitting there is safer than a "helpful" reset
running unattended and possibly discarding something.

**A background writer will put you here.** An editor auto-saving a buffer, an MCP server
dropping a state directory, a file watcher — anything that touches the worktree between the
gate's approval and the commit landing changes the tree out from under the approval. This
is the gate working correctly, not a false positive; the fix is to `.gitignore` the
offending paths before arming, not to fight the reconcile after the fact.

## Pausing is a soft target, not a fence

`--until N` (on `implement` or `resume`) stops the loop from *asking* for more once phase
`N` lands — the turn ends with a "paused, not an approval of the whole plan" message
instead of demanding the next phase. It adds no new denial: if Claude is told to keep
going anyway, nothing stops it. The gate on every commit is exactly as strict either side
of the pause target; only the Stop-hook's insistence on outstanding phases changes.

`/adversarial-review-loop:pause` names the same target mid-flight, and writes nothing else.
Note that it does not *stop* anything: <kbd>Esc</kbd> is the immediate stop, and it leaves the
phase half-written. The command only moves the target, so what it buys is a stop at a clean
boundary — the phase finished, reviewed and committed first. That is the one you want when you
decide partway through a long plan that you'd like to shut the machine down, or upgrade the
plugin, rather than leaving off mid-phase:

```console
  … Claude is halfway through phase 3 of 9 …
Esc
$ /adversarial-review-loop:pause                  # pause after phase 3
$ continue
  … phase 3 is finished, reviewed, committed; the turn ends paused …
```

With no argument it targets the phase in flight; `N` targets phase `N` (clamped to the last
phase); `0` or `all` clears the target. Continue whenever you like with
`/adversarial-review-loop:resume --until 0`.

**A target that has been reached stays set, and that is not a one-off.** The Stop gate's
check is `phase <= target`, and `phase` only ever increases, so once the phase pointer moves
past the target it can never fire again — every later turn end takes the pause branch
instead. A bare `resume` therefore continues the activation but keeps stopping at each turn
end; clearing the target with `--until 0` (or naming a further one with `--until M`) is what
restarts the loop. That stickiness is deliberate: the target is a user-only control
precisely so Claude cannot move it, and a `resume` that quietly cleared it would make the
most-run command discard the fence. Since it is easy to misread, both `status` and the resume
banner mark a spent target as `already reached` rather than showing it like a pending one.

Telling Claude "pause after this phase" in plain prose does **not** work: the target is
user-only, Claude has no route to it, and the Stop gate will keep sending it back into the
next phase. This command is that route.

Because it grants nothing it needs no clean worktree, unlike `resume --until N` — there is
no uncommitted work for it to fold into a review. Two consequences of it being that small
are worth knowing. It does **not** bump
`activation_generation`, deliberately: that bump exists to invalidate a decision whose
evidence moved underneath a long review, and a pause target is not evidence — bumping it
would land an in-flight final review as `SUPERSEDED` and discard a real verdict for nothing.
And the Stop gate reads `stop_after_phase` once, near the top of its run, so a pause that
races a turn end takes effect at the *next* turn end rather than the one already in
progress. Both are benign; neither can turn into an approval.

## Revising the plan mid-run

Editing the plan file, or passing `--plan` to `resume`, only takes effect for phases that
haven't started — phase descriptions already committed against are immutable, because
they were the evidence a completed review already ran against. `--replan` grants a
one-shot permission to redefine phases from the current one onward via the ordinary
`set-phases` command; while that permission is outstanding, **every other mutation is
denied** — the same fence `ARMED` applies before the very first freeze. This closes a
specific failure mode: without the fence, Claude could start implementing (or even get a
phase reviewed) *before* the replaced phase descriptions exist, which is the
redescribe-the-work-to-fit-the-code problem arriving from the other direction.

Both a decided revision and `--replan` require a **clean** worktree, and `--allow-dirty`
does not waive it — not even when the revision was triggered automatically (you edited the
plan file, no flag typed at all). Mid-phase work sitting in the worktree while the phase
that describes it gets silently redefined is exactly the failure this refusal exists to
block.

## Completing without a final cumulative review

`final_review` is `false` by default, so ending the turn with every phase committed reaches
`COMPLETE` without a final cumulative review. That is not the same as "no reviewer call":
the unreviewed-work sweep runs first as it always did, and if it finds a tree no review has
approved it calls the reviewer for that, and blocks on findings. What is skipped is the
baseline-to-`HEAD` pass over the whole activation. Everything else about the Stop gate is
unchanged: the unreviewed-work sweep still runs, outstanding phases still block, a pause
target still holds, a dirty worktree still blocks, and a `RECONCILE` activation still cannot
complete this way — `finish` (which always reviews) is its only route out.

**One exception to "no review runs": a `finish` already requested.** `finish` sets
`finish_requested` before its review runs and does not clear it when the review returns
findings — it just returns 1 with the mode still armed. The Stop gate honours that request
on the next turn end regardless of `final_review`, so a `finish` that found problems is
re-reviewed rather than quietly completed. Where the flag is written relative to each
precondition decides what a *refused* `finish` leaves behind: the status allow-list is checked
first, so a `STALE` or `NEEDS_HUMAN` refusal is a clean no-op, while the worktree-cleanliness
check comes after, so a `finish` refused for a dirty worktree still leaves the request
standing. That does not make the next turn end run the review while the worktree is still
dirty — Stop checks cleanliness first and blocks — but once it is clean, the review runs,
skipping the outstanding-phase check, so it can complete with phases still left.

**Completing this way has to be earned, and the evidence is git's, not `state.json`'s.**
With no reviewer in the loop, `phase == len(phases) + 1` proves only that an integer was
incremented, so the gate proves phase progress against the repository instead:
`confirm-commit` records the SHA it verified for each phase in `phase_commits`, and
completion requires one per frozen phase, all distinct canonical object IDs, forming an
ancestry chain from the activation commit through each phase in order, **each one moving the
tree**, ending *at* `HEAD` rather than merely below it. The last clause is what stops `git commit --allow-empty` per phase from
walking an unimplemented plan to `COMPLETE` through the unchanged-tree cache, which never
calls the reviewer.

It fails closed, and three shapes land on that side of the line. An activation armed before
`phase_commits` existed has no record and cannot produce one after the fact. A plan containing
an empty phase commit has a step that does not move the tree. And an activation armed on a
repository with **no commits at all** has an empty `activation_commit` — which is honest, but
it is also the field that anchors the chain to this activation rather than to inherited
history, and nothing outside `state.json` can confirm the claim. Asking git about the shape of
phase 1 does not help: "a non-empty root commit" is exactly what any seeded repository's own
first commit looks like. So the empty-repository case is refused rather than special-cased.

All three escalate to `NEEDS_HUMAN` instead of completing, and escalation closes the `finish`
remedy as well — `finish` refuses a `NEEDS_HUMAN` activation. For any of them, set
`final_review true` or run `finish` **before** the last turn ends; both put a reviewer back in
the loop, which is where this evidence cannot reach.

Two further consequences worth knowing.

**There is no remedy afterwards.** A `COMPLETE` activation can never be reviewed
cumulatively: `finish` refuses one, and so does `resume`. The decision is made at the moment
the turn ends, not later. If you want the pass, ask for it *before* that —
`/adversarial-review-loop:finish`, which ignores `final_review`.

**Refusals on this path escalate much faster.** When a completion is refused (the activation
moved underneath it, the TTL shrank mid-turn, the recorded phase evidence doesn't hold up),
that becomes a counted no-progress block. With the review enabled there was a minutes-long
reviewer call between attempts; without it, a worktree something else is rewriting can burn
through `max_stop_blocks` in seconds and escalate to `NEEDS_HUMAN`. That escalation is the
fail-closed direction and is working as intended — but it arrives sooner than it would with
the cumulative review enabled.

## The phase cap

`set-phases` refuses more than `MAX_PHASES` (64) phases in one frozen list — a bound
against a runaway list, not a judgment on how finely a plan is decomposed. Nothing in the
gate scales badly with phase count (each is one line in `phases.frozen`, one array entry in
`state.json`), so the number exists only to catch a model proposing hundreds of phases
where grouping them is clearly the right call. A `--replan` counts the same way: the
immutable committed prefix plus the new tail must together stay at or under the cap.

## Resume, retirement, and "which session is live"

`implement` always starts fresh: a new baseline, an empty phase list, no approvals carried
over. Right for a new plan, wrong for picking an old one back up tomorrow — and the 24-hour
`ttl_hours` default makes that happen sooner than you'd think. `resume` is the second arming
path for exactly that: it continues the *same* activation, in a new session or the current
one, without moving the baseline or losing anything already approved. The baseline tree and
every approved tree carry forward untouched, so a final cumulative review — if one runs at
all — still covers the whole plan from its *original* baseline, not from wherever the most
recent resume happened to start. A commit that landed since the last approval but was never
reviewed is not silently treated as approved either: resume warns, and folds it into the next
review.

Exactly one activation may be live per worktree. When you `resume` in a *new* session, the
previous one is retired into a blocking `RESUMED` status before the new one exists — any
attempt to mutate anything in the old session afterward is denied, naming the session that
took over. There is no automatic rollback if a resume fails partway through: if the
predecessor is already retired when that happens, both the predecessor (`RESUMED`) and the
new attempt (`ARM_FAILED`) deny, and the recovery is a fresh `implement`.

A resume with an approval still pending (a commit that was approved but hasn't landed or
failed yet) is refused unless you pass `--abandon-pending` — the assumption being that
session is simply gone (crashed, closed) rather than mid-commit. If you abandon a pending
approval and that commit *does* land later (a killed session recovering, or landing out of
band), the gate still notices: it enters `RECONCILE` rather than silently accepting a
commit nobody's current activation ever reviewed.

## History rewritten under a resume

If the commit `implement` originally froze the baseline against is no longer an ancestor
of `HEAD` — a rebase, a hard reset, a force-push someone pulled — `resume` refuses outright
rather than resuming against a baseline that no longer describes the repository. The
recovery is a fresh `implement`.

## A phase that never converges

There is no cap on how many rounds one phase may take. A phase escalates to `needs-human`
only on evidence it is genuinely stuck: `stall_rounds` (default `3`) consecutive rounds
raising the same **blocking** finding, unchanged, or a blocking finding that reappears after
being absent, or is reversed (`SUPERSEDES`) in two or more separate rounds. Either signal, and
the next commit attempt (or Stop gate sweep) does not call the reviewer at all — it escalates
straight away, with the persisting or oscillating findings quoted verbatim.

**Only a finding that could block counts.** Both signals ask whether the loop is stuck, and a
loop can only be stuck on something that stops a commit — so a finding raises an anchor here
exactly when it is `actionable=yes` *and* at or above `block_severity`, the same test that
fills the blocking list. A `severity=info actionable=no` remark repeated every round, or a
`low` note under the default threshold, is the reviewer restating itself, not a standing
disagreement, and it does not spend a human interrupt. Retirement is unaffected: a
`SUPERSEDES` still resolves against every finding of the round it names, blocking or not.

**A reversal the reviewer declares is believed.** `SUPERSEDES round=N file=F` retires the
round-`N` finding whose location is exactly `F`, and a retired finding is not "raised" for
either check — so a round that retracts a finding and raises a genuinely different one in the
same file is converging, not stuck, whether it does so in the next round or three rounds
later. This cannot be used to dodge the check: a proper retraction counts towards the
reversal signal instead (reversing one file in two separate rounds escalates on its own),
and a finding dropped *silently* and raised again later still counts as a reappearance. Retirement is deliberately strict about what
it will believe: a round number that names no earlier round, a location matching no finding
of that round, a location matching *two* of them (which was reversed is unknowable), and a
reversal already claimed by an earlier round all retire nothing. Every one of those was
observed as a false escalation before the rule existed — most often the last one, since
`prior-rounds.txt` keeps showing a reversal the reviewer already made, and restating it is
not a second change of mind.

**Accepted consequence:** a reviewer that raises a genuinely new, non-repeating objection
every round never trips this. Nothing here distinguishes real, ongoing progress from an
unlucky sequence of distinct findings that never happens to repeat — both keep the loop
running indefinitely. `/adversarial-review-loop:accept` is the only bound in that case: it
approves the current tree without another review and continues, regardless of how many
rounds ran or what they disagreed about. If a phase is taking an unreasonable number of
rounds, that is the exit, not a config knob to make the reviewer stop trying.

### `accept` is the middle option, and it is recorded

`/adversarial-review-loop:stop` gets you out of a stuck phase too, but it turns enforcement
off for the rest of the session; `/adversarial-review-loop:finish` runs a review that is just
as likely to keep finding things. `accept [reason]` sits between them: it puts the current
working tree — exactly as it stands — into the set of approved trees, the same record a
passing review would have written. Nothing else changes. The phase does not advance, the
activation does not complete, and any further edit changes the tree hash and puts the commit
right back under review. It also clears a `NEEDS_HUMAN` escalation, which is otherwise
something only a human can do — `resume` deliberately refuses to.

Every acceptance is recorded: in `/adversarial-review-loop:status`, as its own numbered
report visible through `/adversarial-review-loop:report`, and in a
`## Manually accepted phases` section shown to every later review of that activation — including the final
cumulative one — so nothing downstream mistakes an accepted phase for one that actually
passed a gate.

Before accepting, though: when a finding is just *unclear*, or two rounds seem to contradict
each other, Claude can run `arl.sh clarify --question "…"` to get one prose answer about the
review that already ran, with no new commit attempt and no new round. It is not a slash
command (Claude invokes it directly), it changes nothing, and it is capped at
`max_clarifications` per run. A genuine standing disagreement still ends at `accept`.

### Transient failures are counted and paced separately

A timeout or a rate limit is not the same failure as a missing binary. `max_failures`
governs every operational, contract or bundle failure, with no pacing — retrying immediately
is the right move for those. A timeout, a matched rate/usage-limit signal, or contention with
another review of the same phase already in flight is counted separately, against
`max_transient_failures` (default `5`), and paced with backoff (`30s`, doubling, capped at
`300s`): the next commit attempt is denied with the remaining wait rather than spending
another provider call on a limit that has not reset yet.

Both counters run independently and neither resets the other, so they bound the *total*
number of failing attempts since the last approval, not strictly-consecutive runs of one
kind — a stuck phase alternating between the two still escalates, just possibly after
`max_failures + max_transient_failures` attempts rather than either limit alone. Both
budgets, exhausted, escalate to `NEEDS_HUMAN` the same way.

The check applies to phase reviews only — a `final` cumulative review has no `round_history`
label of its own to stall on. Set `stall_rounds` to `0` to disable the check entirely and
fall back to no automatic bound at all beyond `accept`.

**Two reviews of the same phase never run at the same time.** The commit gate and the Stop
gate's own unreviewed-work sweep can genuinely overlap, and if both were allowed to invoke
the reviewer concurrently, whichever finished first could act on a verdict decided blind to
the other's still-running result — an approval that never saw a repeated finding land a
moment later is not a timing quirk, it is the standing disagreement slipping through. A
second, overlapping attempt is refused outright instead, before it ever builds a bundle or
calls the reviewer — it denies as an ordinary operational failure and simply needs retrying
once the first review has finished.

## The findings cap escalates, it never trims

If a review comes back with more findings than `max_findings` (or their combined size
exceeds `max_findings_bytes`), the gate does **not** show you a shortened list and call it
approved-with-caveats. It escalates straight to `needs-human` and keeps the full report on
disk — a trimmed list you didn't get to see the rest of is a worse outcome than an honest
"this needs a person."

## Oversized files and `ignore_globs`

A staged file larger than `max_file_bytes` blocks the commit outright rather than being
silently dropped from the snapshot — a file the reviewer never saw is not the same as a
file that was reviewed and found fine. `ignore_globs` is the opposite tool: paths matching
it are excluded from triggering a review at all, so a commit touching *only* ignored paths
(a changelog, generated docs) is a cache hit with no reviewer call — but any change that
touches even one non-ignored file still gets a full review of everything in the diff,
ignored paths included.

## Empty diffs are cache hits, not free passes

If the tree about to be committed is byte-identical to the last approved tree — nothing
actually changed, or the change is confined entirely to `ignore_globs` — the commit is
allowed immediately, with no reviewer call. This is deliberate: reviewing "no change" would
either be a meaningless pass or, worse, a place a misconfigured reviewer could rubber-stamp
something that was never actually looked at. It only ever fires on a genuinely unchanged
tree, verified by comparing git tree hashes, not by trusting a flag.

## The Stop-hook block cap is a different thing from `max_stop_blocks`

Claude Code itself caps *consecutive* Stop-hook blocks (default 8) and, on exceeding it,
**overrides and ends the turn anyway** — which reads as success from the outside. This
plugin's own `max_stop_blocks` (default 3) counts only *no-progress* blocks and resets on
any forward motion, so a productive multi-phase run resets it constantly — but Claude
Code's cap counts every block regardless of progress. A long, genuinely productive run can
still exhaust the host's cap purely on volume. Raise
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` in `settings.json` (see the top-level README) to give a
long loop headroom; this residual gap is documented, not fixed, because it isn't something
this plugin can control from inside a hook.

The variable is read at process start, so Claude Code has to be restarted after setting it.
The number is how many consecutive blocks are *tolerated* — the override fires on the next
one after that, verified against a cap of `3`, which overrode on the fourth block.

## Submodules aren't diffed

A change inside a submodule is detected and declared in the review's header, not silently
missed — but its content isn't included in what the reviewer sees. Treat a submodule bump
as something to check by hand.

## `/clear`, crashes and quitting unbind a session, they do not disarm an activation

The gate is session-scoped: a crash, `/clear`, quitting, or simply closing the terminal
leaves the *state* exactly as it was (nothing reverts), but the *session* that was enforcing
it may no longer be the one you are in.

The distinction that matters is **binding**, not arming. An activation is keyed by
`(worktree, session id)`, and only four things change a binding: `stop`, `ttl_hours`
expiring, a `resume` from a different session (which retires the predecessor to `RESUMED`),
and an arm failure. Nothing you do to a *conversation* is on that list.

So the question on coming back is only ever "am I under the same session id?":

- **Yes** — `claude --resume`, or a `/resume` back to a session you left earlier. The session
  pointer and the worktree's `latest` both still name it, so it is bound, the gate is live,
  and the `SessionStart(compact|resume)` re-orientation fires. Nothing needs to be run:
  telling Claude to continue is enough. Uncommitted work is simply the phase in progress, and
  the snapshot at commit time picks it up.
- **No** — a fresh `claude` in the worktree, or the new conversation `/clear` leaves you in.
  That session is *unbound*: every mutation and commit is denied, naming the activation, until
  `/adversarial-review-loop:resume` binds it. With uncommitted work in the tree that needs
  `--allow-dirty`, which folds it into the next phase's review.

An unbound session is only denied **in the worktree the live activation guards**. Working in
another repository meanwhile is unaffected, and passes silently.

**One irreversible case.** If a *second* session runs `resume` in that worktree while you are
away, it takes ownership and retires the first to `RESUMED`. Returning to the original session
then denies every mutation, naming the successor, and there is no route back into it — the
work continues in the newer session. This is the "exactly one live activation per worktree"
rule doing its job, not a failure, but it is the one interruption that cannot be undone by
coming back.

`/adversarial-review-loop:status` answers "bound or not" without changing anything, and is
worth running before trusting either branch.

## Hooks are plugin-level, not skill-level

Every hook is registered by `hooks/hooks.json` at plugin load, so the gate exists in every
Claude Code process the plugin is enabled in. The original design registered them from the
`implement` and `resume` skills' frontmatter instead, and that had a hole: skill hooks
register *per process*, on invocation. Interrupt a run, quit, `claude --resume` the next
day, type `continue` — the resumed process has the same session id, `state.json` says
`ACTIVE`, `/adversarial-review-loop:status` agrees, and not one `arl` hook is registered.
Every commit lands ungated while the state claims enforcement (measured 2026-08-30; see
`tests/STEP0.md`).

The cost is that the dispatcher now runs in sessions that never armed anything. That path is
deliberately cheap and silent: no session pointer, no live activation for the worktree the
call is about, nothing written, exit 0 in well under a second. The path that is *not* silent
is a session with no pointer in a worktree whose `latest` activation is still live — a fresh
`claude` opened there, or a resumed session that came back under a new id. That session is
**unbound**: every mutation is denied, naming the activation and telling the user to run
`/adversarial-review-loop:resume`, which binds the session and keeps every approval. The other
session's document is never touched.

Two more things are needed for that to be fail-closed rather than merely convenient. First,
"nothing to enforce" is proven, not defaulted to: the worktree is resolved with a git call
that distinguishes "not a repository" from "git could not be run", and only the former
passes — a `git` missing from the hook's PATH denies. Second, an `arm` that never started
is still caught. The skills arm from a prompt-expansion line, and when that line cannot
run, Claude Code aborts the skill and Claude gets no turn — the skill body's own warning
never reaches it, and nothing has been persisted for the gate to find. So a
`UserPromptSubmit` hook (`intent`) records a marker the moment a prompt *starting with*
`/adversarial-review-loop:implement` or `:resume` is submitted, before any expansion, naming
the worktree it was submitted from. A successful (or failed-but-recorded) arm supersedes it
by writing the session pointer, which carries the marker's own token; an *unanswered* marker is read as "arming never ran"
*whatever pointer the session held before* — an earlier activation that ended is still a
pointer, and a re-arm whose expansion failed must not hide behind it — so it is checked
ahead of the pointer, recorded as `ARM_FAILED` for the worktree it names, and denied until
the user re-arms or stops. Calls from any other repository leave it untouched. Prose that
merely mentions the command records nothing.

Because the marker outranks the pointer, the two are *bound* rather than ordered: `intent`
mints a token, `pointer_write` publishes the pointer carrying that token, and a marker whose
token the pointer already names is answered — inert, cleaned up on the next check. Publishing
first is what closes both crash windows: die after it and the leftover marker is harmless;
die before it and the request is still pending, so the next mutation records `ARM_FAILED`.
Unlinking first would have opened a window with nothing on disk at all — no marker, no
pointer, no `latest` — in which a saved `ACTIVE` activation went ungated.

The marker's write order is not guaranteed either way — measured live, it can land *after*
the expansion it announces has already completed — so a marker whose worktree already has a
live, gating activation bound to the same session is also read as answered, never as a
failed arm: whichever side wrote last, the gate is enforcing.

A marker that exists but cannot be read, names no absolute worktree, or carries no valid
token is never "no intent" — but neither is it assigned to whatever repository the call
happens to be in. It denies *everywhere*, records nothing and consumes nothing, and only
`/adversarial-review-loop:stop` (which now passes `--session`) discards it. Otherwise a
corrupted marker for repository A would be consumed by a call in repository B, and A would
go ungated.

"Proven" cuts the other way too. A bound session whose call comes from a *subdirectory* of
the armed worktree needs git to place it; if git cannot run from the hook's PATH, the old
lenient resolution read that as "another repository" and passed the call — including a
commit run through an absolute `/usr/bin/git`. Every hook now treats an unanswerable
resolution as a denial (`pretool`), a block (`gate-stop`) or an explicit "NOT confirmed"
(`confirm-commit`); only git's own "not a repository" is a pass.

It also settles item 15 in `tests/STEP0.md`: with one registration per process there is
nothing left to register twice when `implement` and a same-session `resume` both run.

## What isn't settled without a live session

A handful of things depend on exactly how Claude Code itself behaves and can't be verified
from a script — `tests/STEP0.md` is the runbook for those, and as of this writing five
items remain open: whether any host-provided signal can distinguish a user's own
`/adversarial-review-loop:stop` from the identical command run inside a wrapper script (item
11), whether a `Stop`-hook's `systemMessage` is guaranteed to reach the user rather than
just the model (item 12), whether `{"decision":"block"}` has any effect on
`PostToolUse`/`PostToolUseFailure` (items 13–14), and whether a doubly-registered hook
fires once or twice per tool call (item 15 — the "Hooks registering twice" case just
above). None of the first four weaken the gate on the commit path itself — they bound how
strong the *reporting* is for the escapes documented in
[security.md](security.md).
