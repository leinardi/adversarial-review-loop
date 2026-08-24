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
`ocrl.sh deactivate`, or writing `status: "DISARMED"` straight into `state.json` — **it
can, and the design does not pretend otherwise.** Both of those are confirmed by
experiment, not theoretical:

- A wrapper script (`bash escape.sh`, where the script commits and then runs
  `ocrl.sh deactivate`) is recognised as neither a commit nor a user-only escape by the
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
4. **The user owns the exits.** `implement`, `finish`, `stop`, `resume` and `config` are
   unreachable through natural language — only the literal slash command invokes them —
   and Claude's own route to running them via `Bash` is denied. See the honest-agent
   caveat above for exactly what this does and doesn't guarantee against a hostile model.

(A fifth property, unnumbered in `AGENTS.md` but load-bearing throughout: hook stdout is
protocol. A hook entrypoint emits valid Claude hook JSON or nothing — never a stray
`print`, which would corrupt the response the same way a crash would.)

## Command classification: defense in depth, not a single point of failure

Whether a `Bash` command is allowed to run is decided by a deny-list of raw shell grammar
(`$`, backticks, `;`, `|`, redirection, subshells, unquoted globs, a bare `&` — all refused
before anything else runs) plus a real bash-grammar parser (a vendored copy of `bashlex`)
over whatever's left. The deny-list runs first and is the actual security boundary; the
parser only turns the small surviving language into words correctly — it doesn't widen what
the gate accepts. Two things make a bypass here recoverable rather than catastrophic — but
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
  for it: `final_review true`, or `/opencode-review-loop:finish`. `finish` ignores
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

## Repo config is attacker-controlled input, full stop

`.opencode-review-loop.json`, when present, lives inside the repository under review — the
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
  of `sys.path`, so a repository shipping `ocrl/__main__.py` — or merely `json.py` — would
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
