# Security

This is a threat-model page: what this plugin actually defends against, what it doesn't,
and why the line sits where it does. It's a summary aimed at someone deciding whether to
trust this in their workflow — anyone *changing* the plugin should treat
[`AGENTS.md`](../AGENTS.md) as canonical instead; it's more detailed and it's what a change
is actually reviewed against.

## What the failure mode actually is

> The failure that matters is not a crash — it is an unreviewed commit that looks reviewed.

Everything here is designed around that one sentence. A hook that denies too aggressively
is annoying. A hook that silently *passes* when it should have denied is the thing that
lets bad work look approved. Every design choice below picks "block, and say why" over
"guess, and hope."

## The threat model: an honest agent, not a hostile one

This is the load-bearing distinction, so it's worth being blunt about it: **this is a
quality gate for an agent that is trying to cooperate and might get something wrong — not
a sandbox for an agent that is actively trying to defeat it.**

If Claude is following the loop's own instructions, the gate is airtight: it cannot commit
anything the reviewer hasn't seen exactly, it cannot end its own escapes (`finish`,
`resume`, `config`), and it cannot silently disarm the mode. If Claude is deliberately
trying to get around it — writing a wrapper script that commits and then calls
`arl.sh deactivate`, or writing `status: "DISARMED"` straight into `state.json` — **it
can, and the design does not pretend otherwise.** Both of those are confirmed by
experiment, not theoretical:

- A wrapper script (`bash escape.sh`, where the script commits and then runs
  `arl.sh deactivate`) is recognised as neither a commit nor a user-only escape by the
  command classifier — because it *is* neither, as a shell command; the gate reads
  `tool_input.command` as a string, and that string is `bash escape.sh`. The commit inside
  it lands unreviewed and the mode ends.
- Editing `state.json` directly — the same file the gate itself trusts — ends the mode with
  no command to inspect at all. A `PreToolUse` guard refuses an *editing tool* aimed at the
  state root, but a shell command can still write the same bytes, because the gate runs as
  the same OS user and by the time it's asked about a command, that command is about to
  run.

No file-based mechanism inside the plugin can close either of these: any token this plugin
writes to disk, the model's own shell can write too. What *is* still enforceable in that
scenario is reporting — a HEAD whose tree no review ever approved is surfaced through
`additionalContext` on the tool result and a `systemMessage` at the end of the turn, aimed
at the human, not the model. Whether that message reliably reaches a human rather than only
the model transcript is one of the open items in `tests/STEP0.md` — see
[edge-cases.md](edge-cases.md#what-isnt-settled-without-a-live-session).

## The five rules

Everything else in the codebase is detail against these. Paraphrased from `AGENTS.md`:

1. **A gate that cannot prove it's running denies.** If the hooks fire at all, the skill
   was invoked. If arming itself then never executed — a refused sandbox, an unreadable
   script — the dispatcher records that failure itself and denies. Absence of state is
   never read as "nothing to enforce."
2. **Nothing converts a failure into an approval.** A timeout, malformed output, a
   non-zero exit, an unreadable state file — every one of these blocks or escalates.
   Operational uncertainty is never treated as "no findings."
3. **Nothing is written inside the repository under review, with one explicit exception.**
   All state lives outside the repo. The one exception — an explicit, user-only
   `config --repo` — is documented, never triggered by a hook, and never reachable from
   Claude.
4. **The user owns the exits.** `implement`, `finish`, `stop`, `resume`, `config`,
   `accept` and `pause` are unreachable through natural language — only the literal slash
   command invokes them — and Claude's own route to running them via `Bash` is denied. See
   the honest-agent caveat above for exactly what this does and doesn't guarantee against a
   hostile model.

   `pause` neither ends nor grants: it only moves the target that decides whether the Stop
   gate keeps asking for the remaining phases. It is here anyway, because in Claude's hands
   that is an unbounded escape from being asked for more work — a strictly better `defer`,
   which is deliberately bounded by `max_defers`.

   `accept` is the one exit that grants something rather than only ending or adjusting
   the activation, which is what makes it worth spelling out separately: it mints exactly
   the artifact a passing review mints — the current tree added to `approved_trees` —
   and nothing else. It does not advance the phase, complete the activation, or touch any
   tree but the one it names. That scope is what makes handing it to a user command safe:
   the grant is bound to one exact tree hash, so it cannot pre-approve future work, and
   editing anything afterward changes the hash and puts the commit right back under
   review. Both locks above apply to it exactly as they do to the other four.

(A fifth property, unnumbered in `AGENTS.md` but load-bearing throughout: hook stdout is
protocol. A hook entrypoint emits valid Claude hook JSON or nothing — never a stray
`print`, which would corrupt the response the same way a crash would.)

## Command classification: defense in depth, not a single point of failure

Whether a `Bash` command is allowed to run is decided by a deny-list of raw shell grammar
(`$`, backticks, `;`, `|`, redirection, subshells, unquoted globs, a bare `&` — all refused
before anything else runs) plus a real bash-grammar parser (a vendored copy of `bashlex`)
over whatever's left. The deny-list runs first and is the actual security boundary; the
parser only turns the small surviving language into words correctly — it doesn't widen what
the gate accepts.

A third check, `unresolved_expansion`, runs on **every** `Bash` call rather than only on the
commit path, and it is *not* the boundary. It exists so textual detection cannot go blind on
a command **name**: `$(printf git) commit` runs `git commit` and contains no `git` for the
detector to match, so a name an expansion decides is refused outright. It is scoped to that
and no wider — an expansion in an *argument* is allowed (`echo "exit=$?"` runs `echo`), and so
is anything inside a heredoc body whose delimiter is quoted, which bash does not expand at
all. Still refused beyond the name: an *unquoted* heredoc body, which bash does expand, and an
argument to a known exec wrapper such as `sh -c "$CMD"` — the latter a speed bump rather than a
boundary, since `python3 -c "$CODE"` walks through it exactly as `python3 script.py` always
did. **Narrowing that check did not move the deny-list**: on the commit path `$`, backticks,
`;`, `|`, redirection, subshells, globs and newlines are all still refused, `git commit -m
"$(x)"` is denied exactly as before, and a heredoc whose body contains the words `git commit`
is still detected and refused as a commit shape.

Two things make a bypass here recoverable rather than catastrophic — but
only the first of them is unconditional:

- `PostToolUse` independently re-verifies `HEAD^{tree}` against the approved tree and a
  clean worktree — but only on the *approved* path, where a pending approval exists and the
  command matches the one that was approved. A parser bypass has no pending approval to
  bind against, so it takes the other path, which asks a single, much weaker question: is
  `HEAD^{tree}` in the set of trees this activation has approved? The guarantee is
  therefore narrower than "bypasses are caught", and worth stating exactly: **a bypass
  whose HEAD tree is not in that set is detected and reported, and no configuration key
  can suppress that.**

  Two gaps sit outside it. A bypass landing a tree *already* in the set returns silently —
  no parent check, no cleanliness check — and an approved commit that failed still leaves
  its tree in the set, so a rewrite onto it passes unremarked. And membership in that set
  is not proof a model reviewed anything: the baseline tree is in it, and so is any tree
  where `ignore_globs` matched everything.

  Whether it is also *recoverable* depends on the activation's status: an active one enters
  `RECONCILE`, while one that is already finished, escalated, resumed or expired is reported
  and left alone, since those must not be reopened. That last case is reachable by config —
  lowering `ttl_hours` between the gate and the commit makes the activation `STALE` — so
  detection is unconditional, recovery is not.
- The final cumulative review covers the whole activation's end state regardless of what
  happened per commit — **when `final_review` is enabled, which since 0.6.0 it is not by
  default.** On a default install, the deny-list plus `PostToolUse` verification is the
  whole of the Stop path's protection, and the end-state pass exists only if someone asks
  for it: `final_review true`, or `/adversarial-review-loop:finish`. `finish` ignores
  `final_review` — but only that key; it still has to pass the ordinary finishability
  checks, and those are config-reachable too (a `ttl_hours` low enough to make the
  activation `STALE` refuses `finish` before any review runs). Once an activation is
  `COMPLETE`, both routes are closed for good.

`final_review` is deliberately an ordinary key: repository config may set it, exactly as it
may set every other key. That is not a new capability. A repository config can already set
`ignore_globs: ["**"]`, which bypasses *every* per-commit review — strictly worse, and
documented below. What `final_review` gives up is the cross-phase view (a later phase
quietly undoing an earlier one, dead ends, interface drift) and the second layer above; what
it cannot do is approve anything, reach `PostToolUse` or the deny-list, or affect `finish`.

## `state.json` is not a trust boundary

This is worth internalising if you're evaluating the design: the state file the gate
itself reads is not treated as inherently trustworthy input, because in the honest-agent
threat model it's still an ordinary file Claude can write to (once mutations are allowed).
Anywhere the gate uses a value read out of it to *do* something — most concretely, a
filename naming a frozen plan revision — that value is validated as if it came from outside
the system: checked as a single safe path component, resolved under the activation
directory with symlink-safe containment, and verified against a recorded SHA-256 hash
before its content is trusted for anything, including what gets shown to the reviewer. A
failure here is a hard escalation to `needs-human`, never a silent skip or a fallback to a
different file.

## Reviewer session continuity does not widen what `state.json` can do

Within one review label (`phase3`, or `final`) consecutive reviews continue the same
reviewer session where one can be safely established and claimed, rather than starting cold
every round — the `reviewer_session` pointer that makes this possible is itself a value read out of
`state.json`, which the previous section already establishes is not a trust boundary. So the
pointer is held to the same standard: it is never trusted to *authorize* anything. It cannot
be: it selects which conversation a review continues, and nothing more. A review's verdict
still has to come back through the same contract parse, and an actionable finding at or above
`block_severity` still blocks whatever the reviewer concluded.

**What continuity actually puts in front of the reviewer.** Two things, not one, and they are
**not** equally checkable — the difference matters more than the count, because the default
below turns on it.

- **`NNN-prior-rounds.txt` — bounded, and the gate's own words.** It carries earlier rounds'
  `FINDING` detail, and it is attached to a *fresh* invocation just as readily as to a continued
  one: session continuity is best-effort and drops silently (a listing failure, a generation
  bump, a held claim), while the prior-rounds attachment does not. It is not free-form model
  output smuggled back in. It is the gate's own rendering (`reviewer._prior_rounds_section`) of
  lines validated against `_FINDING_RE` before they were stored and re-validated before they are
  rendered, out of entries whose `verdict`, `seq` and `tree` are each type-checked on the way
  out; the section is bounded by `max_findings` lines and `max_findings_bytes` encoded bytes, so
  a tampered history degrades to a *shorter* attachment rather than to smuggled prose. It
  reaches the reviewer *inlined*, under either harness — through `-f` on OpenCode, in the stdin
  payload on Claude Code — from a directory outside the invocation's own read grants, so it
  cannot be re-opened by path.
- **A continued session (`-s`, or `--resume`) — unbounded, and the gate never sees it.** This
  one has no such story and it would be dishonest to give it one. Continuing a session hands the
  reviewer the entire earlier conversation: every earlier round's attachments, the repository content inside those diffs
  (which is where an injection would live), and the reviewer's own free prose across all of
  them. None of it is re-read, re-validated or size-bounded by the gate at the point it
  influences the next round — it lives in the reviewer CLI's own session store, which may also
  compact it into a lossy summary the gate cannot inspect either. A continued round's verdict can therefore be
  influenced by content the gate cannot enumerate, including content from a diff that this
  round's own bundle no longer contains.

**`max_session_rounds` bounds the second channel in the one dimension that was unbounded.**
A session carries at most `max_session_rounds` rounds (default `3`); the round after that
starts fresh, so the conversation `-s` hands the reviewer is at most two earlier rounds deep
rather than however many the phase needed. It was added for throughput — a long session gets
compacted by the provider, and a compaction landing mid-review has twice returned a malformed
findings block — and the security effect is a side benefit, not a guarantee: within the cap the
channel is exactly as unbounded and as uninspectable as described above, and a phase still runs
as many rounds as it takes, just across several sessions. `0` disables the cap. Do not quote it
as a mitigation for injection persistence; the label-keyed reset and `cold_confirm` are what
that argument rests on.

What is true of both channels, and is doing the real work: neither is an approval path on its
own. A verdict comes back only through the same contract parse, an actionable finding at or
above `block_severity` blocks whatever the reviewer concluded, no operational failure becomes an
approval, and the label-keyed reset (a new phase, or `final`, always starts a fresh session)
bounds any one poisoned session to a single phase.

### `cold_confirm`: the second, cold read — off by default

`cold_confirm` (default `false`) adds a stricter rule on top: **an approving verdict must come
from an invocation whose entire content the gate created.** With the key on, when a review that
held any model-influenced context returns `APPROVED` the gate does not act on it — it runs one
more review of the same bundle cold (no `-s`, no `context/` attachments, evidence built from
git, no memory of anything the earlier round said), and that cold review's verdict is the one
that counts. The stricter of the two always wins. It gates on *either* kind of context, not on
`-s` alone: gating on the session would exempt exactly the rounds that lost continuity but were
still shown `prior-rounds.txt`, which is the population the key exists to cover.

What the key buys, when it is on: a tampered `reviewer_session.id`, or a tampered
`round_history` rendered into `prior-rounds.txt`, can make the reviewer hold extra, possibly
misleading context and produce a verdict — but that verdict can never be an approval by itself.
At worst it denies, which is a stronger failure mode than most of this document's findings (a
denial-of-service, not a wrong grant), and the user's answer to it is
`/adversarial-review-loop:accept`.

**Why it is not the default, and what that costs — stated without averaging the two channels
together.** The cost of the key being *on* is a full second model call on every approving round
past the first, and a *full* one: the confirmation is session-less by construction, so it shares
no prefix with anything and reads nothing from the provider's prompt cache — measured against a
20k-token bundle, 20124 input tokens with 0 cached. Measured over a real 45-round, 7-phase run:
11 rounds were cold confirmations, and in every one of those the cold call raised *new*, medium
findings the warm round had not — a second read of the same evidence with strictly less of it,
disagreeing with itself rather than catching anything the first read had missed. Both manual
`accept`s in that run followed such an override. An invariant whose observed effect is to make a
converging phase stop converging is one worth paying for deliberately rather than by default.

The cost of the key being *off* is equally concrete, and it is **not** "a re-read of validated
`FINDING` lines is skipped". For a round that continued a session, it is that an approval may
have been produced by a reviewer holding the whole unbounded conversation described above — the
earlier diffs and any injection inside them, its own earlier prose, possibly compacted. Nothing
in the gate bounds or inspects that. What is left standing is the list at the end of the
previous section: the contract parse, `block_severity`, the deny-list, `confirm-commit`'s
ancestry evidence, `approval_is_current`, and the label-keyed reset that confines a poisoned
session to one phase. That is a real defence in depth and it is *not* the same guarantee as
"every approving verdict came from an invocation whose entire content the gate created". The
default gives that sentence up. Say so when quoting this section.

- **Injection persistence, and how the two settings differ on it.** A poisoned diff used to
  influence exactly one review; with continuity it can influence every remaining round of that
  phase, since the reviewer's session may hold it in context across rounds — including rounds
  whose own bundle no longer contains it. The label-keyed reset bounds the blast radius to one
  phase either way. With `cold_confirm` **on**, that persistence can only ever cause a
  *denial*, because the verdict acted on came from a call that never saw it. With it **off**,
  an approving verdict may have been shaped by it, and the backstops above are what remains. A
  review loop that will not converge — whether from injected content or an ordinary
  disagreement — is exactly what `/adversarial-review-loop:accept` exists to break out of.

Turn it on — `/adversarial-review-loop:config cold_confirm true`, or `ARL_COLD_CONFIRM=true` for
a single run — when the threat model is a tampered `state.json`, or a diff plausibly carrying an
injection, rather than a review loop that will not settle. Like every other key it is
repo-settable, and the note under "Repo config is not trusted for policy" applies: a repository
config can set it back to `false`, which is a strictly smaller lever than `ignore_globs: ["**"]`
already sitting open beside it.

And one thing continuity is *not*, so nobody spends the safety budget above chasing it: **it is
not a large token saving.** Measured against the same 20k-token bundle, a continued round reads
19968 tokens from the provider's prompt cache and pays 17128 uncached, against 20124 uncached for
the equivalent cold round — single digits, not an order of magnitude. The reason is that the
cached part is the conversation *history*, while the dominant cost is the diff, and the diff is
re-attached as a **new message** every round, where no prompt cache can reach it. That
re-attachment is deliberate and load-bearing, not an oversight to optimise away: `prompts/reviewer-phase.md`
tells the reviewer that `incremental.diff` "does not replace `changes.NN.diff`, which is still the
complete diff and still what your verdict is judged against", and to re-derive its findings "not
from memory of an earlier round's diff". Attaching only the incremental diff would move the
verdict's evidence into session memory, which the reviewer may compact into a lossy summary — trading
a few percent of tokens for exactly the kind of unverifiable, model-held context this whole
section exists to keep out of an approval. Continuity earns its place by keeping the reviewer
*oriented* across rounds, not by being cheap.

### What is attached to a reviewer call, and the window that cannot be closed

Every path the gate hands the reviewer is validated for containment before it reaches the argv
(or, on a harness that inlines from this process, before it is read): components checked, parents opened `O_NOFOLLOW` one level at a time, the leaf
`lstat`-ed rather than `stat`-ed (`atomic.verified_file`). That refuses a planted symlink at
any component, including the directory plant a per-file check cannot see — a symlinked
`bundles/<seq>/` leaves ordinary regular files beneath it.

**The attachment set is evidence, not a directory listing.** `build_bundle` writes a
`manifest` last: one row per attachment, in attachment order, each carrying the SHA-256 of the
bytes just written. Staging reads *that*, and the bundle directory is never consulted for what
to attach — not by glob, not by existence check, not by reading a `chunks` count back out of
it. Every one of those described the directory as it stands rather than the evidence that was
generated, so anyone able to write there could shorten or substitute the set and have the
reviewer judge it: rewrite `chunks` and delete the surplus diffs, drop the trailing plan
revisions, or replace a diff's bytes outright. **None of that needs a symlink**, so none of it
was caught by checking path shapes — which is what every earlier round of hardening here did.

The manifest alone would not be enough either, since anyone who can change a file can change
the manifest beside it. So its own SHA-256 is recorded **outside the directory it describes**:
on the active-review claim in `state.json` while the review is live, and on the `round_history`
entry afterwards, which is what `clarify` checks a long-since-finished bundle against. A
consistent rewrite of files *and* manifest now also has to reach a value held under the
activation lock.

**When the hashing happens is as load-bearing as the hashing.** `verify_cmd` is
repository-controlled configuration executed through a login shell, which makes it the one
piece of attacker-authored code that runs inside the gate's own process tree — with the gate's
privileges, and therefore with write access to the state root that `pretool` denies every
*tool* call. Hashing the evidence after it had run would have the manifest certify whatever it
left behind: a `verify_cmd` that overwrites `changes.00.diff` with benign bytes produces a
reviewer that approves the benign version while the tree being approved still holds the real
change. That is a complete bypass of the gate, and the hashes would have made it look verified.

So the canonical evidence is sealed *before* `verify_cmd` runs and re-checked afterwards; any
byte that moved is a hard failure, not something to record faithfully and review. Only
`verify.txt` — `verify_cmd`'s own output, disclosed to the reviewer as exactly that — is hashed
afterwards and appended, without re-reading anything already sealed.

One legitimate edit exists — `_downgrade_bundle_round` correcting `range.txt`'s round line
after a fallback to a fresh review — and it updates **only that row's** hash, carrying every
other row through byte for byte. Rehashing the whole manifest there would re-bless whatever
else had changed since the seal, which is the same "hash after the untrusted step" mistake in a
second place. It is also the only place a *new* trusted digest is minted, so it verifies before
minting one: the manifest against the digest this review was issued, and `range.txt` against
its own recorded row. Without the first, a wholesale replacement of the evidence and the
manifest would simply be re-signed and handed back as current; without the second, content
injected into `range.txt` would survive the substitution and be rehashed as legitimate. Either
mismatch declines to touch anything, and staging then refuses the bundle.

### What `verify_cmd` can still do, and what it cannot

`verify_cmd` runs with the gate's privileges, so what it leaves behind matters as much as what
it does while running. Three layers, and the third is a genuine limit rather than a mitigation:

- **While it runs**: the evidence is sealed before and re-checked after, so anything it edits
  fails the review rather than being certified (above).
- **On the way out**: the process group is killed after a normal exit, not only on timeout
  (`run_bounded(reap_group=True)`). Without that, `some-command &` returns promptly with a
  child still holding state-root write access; non-interactive bash runs without job control,
  so a backgrounded child stays in the group and is reaped.
- **A descendant that calls `setsid` for itself is not reached**, by that kill or by the
  timeout path — `_kill_group` documents the same limit. Such a process can overwrite a
  *staged* attachment after staging verified it, because `-f` hands OpenCode a pathname it
  opens for itself. The gate re-checks every staged attachment immediately before launching
  (`_confirm_staged_unchanged`), which moves the check as close to the open as this process can
  get and makes a swap an `OP_FAILURE` rather than a review of substituted bytes — but it does
  **not** close the window. An unpredictable staging path does not help either: the attacker is
  the same user and can list the directory.

The gate's own *instructions* live in that directory too, since a review's prompt is composed
per round and written to `raw/<label>-prompt.md` (`review_guide`, see below). Those are a
stronger primitive than the evidence: a substituted prompt telling the reviewer to emit no
findings yields a genuine `APPROVED`, because the verdict is recomputed from the `FINDING`
lines and there would be none. So the prompt is **not** re-read at invocation time — the bytes
this process composed are carried in memory and handed to the harness as text, so a swap
changes nothing about what the reviewer is told. `_confirm_prompt_unchanged` runs beside
`_confirm_staged_unchanged` as well, to *notice* the swap (`OP_FAILURE`) and to cover the
`ARL_REVIEWER_CMD` test seam, which is handed the pathname; that half carries the same racy
caveat as the staged-attachment check.

Closing that last one needs the child to receive a descriptor rather than a pathname, which
`-f` has no way to accept, or `verify_cmd` to run somewhere it cannot reach the state root at
all — a sandbox, which this gate has no portable way to build. Until then it is a known limit
of setting `verify_cmd` in repository config, and it sits alongside the larger one already
recorded here: `{"ignore_globs": ["**"]}` skips the reviewer outright.

Validation alone is not sufficient, because `-f` takes a *pathname* and OpenCode opens it
itself, minutes later. Two exposures live in that gap, and only one of them is closable:

- **Reading the wrong bytes — closed.** `atomic.read_verified_file` reads through the same
  descriptor walk that validated the path, so the bytes that leave are the bytes of the inode
  that was checked. There is no window, because the check and the read are one operation on
  one descriptor. A source that cannot be read that way fails the review; it is never a
  silently dropped attachment — quite apart from the evidence lost, dropping one could also
  talk the gate out of the cold confirmation an attached context requires when `cold_confirm`
  is on.
- **Handing over a pathname that later means something else — narrowed, not closed.** What
  `-f` names is a staged copy in a directory created fresh for that one invocation, with an
  unpredictable name, removed when the call returns (`reviewer.stage_attachments`). **Every**
  attachment is staged, not only the model-derived ones: staging the smaller channel while
  leaving the diffs the verdict is judged against on stable bundle paths would have hardened
  the lesser half and left the evidence exposed. That
  replaces a stable, long-lived, guessable path — `context/<seq>-prior-rounds.txt` persists
  for the whole round — with one that exists for the length of a single call. It does not make
  the swap impossible: anyone who can still write into the `0700` state root can unlink the
  staged file and leave a symlink at its name, and a random name does not stop them, since
  they can list the directory. Closing it entirely needs a descriptor passed to the child,
  which `-f` has no way to accept.

**Who is in that residual class matters, and the repository under review is not in it.**
`pretool` denies tool writes into the state root outright, so nothing Claude does on behalf of
the reviewed repository can reach these files. What remains is something running as the user
that does not go through the gate — a build script, a test, an MCP server — which is the same
class AGENTS.md already records under "Known environment hazards". A third, quieter gain from
staging: the staged bytes are the ones the gate already bounded by `max_findings_bytes`, so a
swap cannot turn a capped attachment into an unbounded one.

### The two harnesses, and which of these arguments is per-harness

The reviewer CLI is configurable (`harness`: `claude-code` by default, `opencode`). Most of
this page does not depend on which one runs — the contract parse, `block_severity`, the
cold-approval invariant, `confirm-commit`, the deny-list and `pretool` are all upstream of the
choice. Three things are per-harness, and each was measured rather than assumed
(`tests/STEP0.md` records the probes):

- **The evidence boundary holds under both, by the same mechanism.** Both inline every
  attachment: OpenCode through `-f`, Claude Code by concatenating them into the payload it
  writes to the child's stdin. So under both, a `context/` file exists only as bytes inside one
  invocation, never at a path the reviewer can re-open — which is what makes a cold
  confirmation, handed none of them, structurally unable to have seen model-authored prose.
  A path-based delivery channel would break that argument, and must not be introduced without
  replacing it. On Claude Code the read grants say the same thing a second way: `--add-dir`
  covers the repository and the bundles root, and `context/` is a sibling of `bundles/`,
  outside both.

- **Read coverage needs no verification, because delivery is complete.** The gate never has to
  establish that the reviewer opened a file it named: every byte was handed over. What
  *is* checked is the reverse — that the bytes handed over are the bytes that were staged.
  Inlining makes that check strictly stronger than the pathname case: `harness.claudecode.payload`
  re-reads each attachment through the same descriptor walk that validated it and compares a
  SHA-256 against the digest staging recorded, refusing the whole invocation on a mismatch.
  That closes, for this harness, the gap the section above describes as narrowed-not-closed —
  the check and the read are one operation in one process, so there is no window for a
  same-user process to swap a staged file in between. It closes it for the *attachments*; the
  `-f` case is unchanged, and remains a known limit.

- **A run that "succeeded" is not automatically an answer.** Measured on Claude Code: a turn
  whose tool call was denied still exits `0`, reports `is_error: false`, and produces a
  plausible-looking review — of less evidence than the gate believes it sent.
  `harness.claudecode.transcript` therefore requires the result event to state, positively,
  that nothing was denied (`permission_denials: []`) and that the turn was clean
  (`is_error: false`); a missing field is a refusal, not a pass. Each of these reaches the gate
  as an `OP_FAILURE`, which blocks (Rule 1). The reviewer's own isolation is the other half:
  `--tools Read,Grep,Glob` with `--strict-mcp-config` unconditionally — measured, `--tools`
  alone still left every connected MCP server's write-capable tools in the session — and
  `--safe-mode --disable-slash-commands` when `pure` is on.

One further consequence worth stating because it is a boundary rather than housekeeping: the
Claude Code harness runs from an **empty** directory it creates under the activation
(`<act_dir>/cwd`), not from the repository and not from the activation directory. In `-p` mode
the file tools are confined to the working directory plus each `--add-dir`, so whatever sits in
that directory is readable by the reviewer; pointing it at the activation directory would put
`context/` inside its reach at a stable path. Running from the repository instead would be safe
but rude — `claude -p` persists each session into a bucket keyed by its cwd, and that bucket is
what the interactive `/resume` picker lists, so every review round would land in the user's own
picker for the repository they are working in.

## Repo config is attacker-controlled input, full stop

`.adversarial-review-loop.json`, when present, lives inside the repository under review — the
same repository Claude is implementing a plan against, and an ordinary file Claude can
rewrite once mutations are permitted. Every key it sets is a lever, not only `verify_cmd`:

- **`{"ignore_globs": ["**"]}` is a full, silent review bypass**, not merely a relaxation.
  A commit whose every changed path matches `ignore_globs` skips the reviewer entirely and
  is approved with no model ever consulted — `gitsnap.all_paths_ignored`, exercised with
  exactly this input in `tests/selftest.sh`. This is the single most direct self-serving
  edit available: write that one line, and every subsequent commit passes unreviewed.
- **`block_severity` raised to `critical`** lets every medium and high finding through
  uncommented-on — the reviewer still runs and still writes the finding into the report,
  but nothing about it blocks the commit.
- **The caps and ceilings** (`max_findings`, `max_findings_bytes`, `hard_diff_ceiling`),
  raised far enough, move the point at which a real, well-formed review result escalates
  to `needs-human` instead of being accepted at face value. This is a different thing from
  the fail-closed handling of an *operationally* broken review (a timeout, a malformed
  response, an unreachable model) — that path is not config-controlled and stays
  fail-closed regardless of what the repo file says. What repo config controls is the
  threshold for a review that finished and answered normally.
- **`verify_cmd` is a further, distinct risk**: an actual shell command, run through
  `bash -lc` in the repository, whose output is attached to the review as evidence. It
  cannot itself approve a commit, but setting it *is* code execution — not sandboxed, not
  reviewed before it runs, and the string can be anything, not only a command already
  present in the repo.

None of the above needs the `config` command at all — `config`'s
`disable-model-invocation` blocks the *command* that writes this file in a controlled way,
but the file itself is not fenced off from an ordinary edit, and the next config reload
picks up whatever it now says. Treat every repo-config key as something an honest-but-wrong
or self-serving agent could set to its own advantage, exactly as you'd treat any other file
in the repository — because it *is* just another file in the repository. See
[configuration.md](configuration.md#repo-config-is-not-trusted-for-policy).

## Interpreter invocation is hardened against two specific, confirmed exploits

Both were found by experiment, not theory, and both look correct if reverted:

- **`python3 -m` or a relative path, instead of an absolute one.** Hooks run with the
  repository under review as the current directory. `-m` puts that directory at the front
  of `sys.path`, so a repository shipping `arl/__main__.py` — or merely `json.py` — would
  execute arbitrary code as the gate itself. The only sanctioned invocation is
  `python3 -I <absolute path to the bootstrap>`.
- **`uv run`, even with flags, on the hook path.** In a directory containing a
  `pyproject.toml` with a custom build backend, `uv run python -I <bootstrap>` ran that
  backend *before* the bootstrap started — the same class of exploit one layer up. A
  `.python-version` file in the reviewed repo also silently redirected the interpreter uv
  used, below the version floor this gate requires. `uv` is a developer convenience for
  running the test suite; it must never appear on the path a hook actually executes.

`sys.pycache_prefix` is likewise pointed somewhere verified to overlap neither the plugin
repo, the reviewed repo, nor the current directory — falling back to writing no bytecode at
all if that can't be confirmed, rather than defaulting to writing beside the source.

## Writes are durable and permission-scoped

Every state write goes through same-directory `os.replace` — a crash mid-write leaves the
last-good file untouched, never a truncated one. Everything under the state root
(directories, the lockfile, `state.json`, reports) is written `0700`/`0600`. The one writer
permitted to touch the repository under review (`config --repo`) chmods nothing beyond the
single file it writes, and never widens an existing file's permissions across a replace —
a config file you deliberately left at `0600` comes back `0600`, not the process umask
default.

## What this doesn't claim

- **Not a sandbox.** It doesn't isolate what Claude can read, run, or touch outside the
  commit gate itself — it's a checkpoint on one specific action (`git commit`), not a
  containment boundary around the agent.
- **Not proof against a hostile model.** See "The threat model," above. If you need that
  property, it isn't here.
- **The reviewer itself is read-only, but its context isn't sanitised.** Repository
  content, the plan, and commit messages are all labelled evidence rather than
  instructions in the fixed prompt, and the reviewer's own permissions deny everything
  except reading the repo and the review bundle — but a sufficiently adversarial *plan*
  file is still something a human chose to hand to the loop in the first place.
