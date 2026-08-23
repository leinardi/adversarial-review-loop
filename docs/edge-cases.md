# Edge cases

What happens when things aren't the straightforward case, and why it happens that way.
Every behaviour here is deliberate — if one of these looks like a bug, it's very likely the
gate working as designed against a scenario worth understanding before you hit it live.

## A stale activation

Every activation has `armed_at`. Once `ttl_hours` (default 24) has passed, every gate
answers `STALE` instead of the stored status — derived at read time, never a separate
timer, and it never silently disarms; it blocks and tells you what to do.

The fix is `/opencode-review-loop:resume`, not a fresh `implement`. `resume` refreshes
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

## Resume, retirement, and "which session is live"

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

## Submodules aren't diffed

A change inside a submodule is detected and declared in the review's header, not silently
missed — but its content isn't included in what the reviewer sees. Treat a submodule bump
as something to check by hand.

## `/clear` and crashes disarm the session, not the activation

The gate is session-scoped: a crash, `/clear`, or simply closing the terminal leaves the
*state* exactly as it was (nothing reverts), but the *session* that was enforcing it is
gone. The fix is the same as any other interruption — `resume` in the new session, which
picks the activation back up rather than starting over.

## Hooks registering twice

`implement` and `resume` both carry the identical hook-registration block — necessary so
that resuming in a session where `implement` never ran still enforces the gate from the
first tool call. If both happen to run in the *same* session (arm, then later
same-session `resume`), Claude Code registers the same four hooks a second time. The
handlers themselves look idempotent on inspection — a second `confirm-commit` firing on an
already-verified commit has nothing to do, a second `pretool` firing on an already-approved
tree takes the cache-hit path — but whether Claude Code actually *calls* a doubly-registered
hook twice per tool call, rather than once, is an open question tracked in
[`tests/STEP0.md`](../tests/STEP0.md) (item 15) rather than assumed safe.

## What isn't settled without a live session

A handful of things depend on exactly how Claude Code itself behaves and can't be verified
from a script — `tests/STEP0.md` is the runbook for those, and as of this writing five
items remain open: whether any host-provided signal can distinguish a user's own
`/opencode-review-loop:stop` from the identical command run inside a wrapper script (item
11), whether a `Stop`-hook's `systemMessage` is guaranteed to reach the user rather than
just the model (item 12), whether `{"decision":"block"}` has any effect on
`PostToolUse`/`PostToolUseFailure` (items 13–14), and whether a doubly-registered hook
fires once or twice per tool call (item 15 — the "Hooks registering twice" case just
above). None of the first four weaken the gate on the commit path itself — they bound how
strong the *reporting* is for the escapes documented in
[security.md](security.md).
