# Configuration

## Precedence

Five layers, lowest to highest priority:

```text
defaults  <  user config  <  repo config  <  activation overrides  <  ARL_* environment
```

| Layer | Where | Set by |
| --- | --- | --- |
| defaults | built into `config.py` | nobody — the fallback |
| user config | `$XDG_CONFIG_HOME/adversarial-review-loop/config.json` | `/adversarial-review-loop:config <key> <value>` |
| repo config | `<repo>/.adversarial-review-loop.json` | `/adversarial-review-loop:config <key> <value> --repo`, or hand-edited |
| activation overrides | inside `state.json`, for the current activation only | `--harness`/`--model`/`--variant` on `implement` or `resume` |
| environment | the process environment | `ARL_*` variables |

A concrete example: your user config sets `model` to `openai/gpt-5.6-sol`. The repo you're
working in sets it to `anthropic/claude-opus-5` in `.adversarial-review-loop.json`. You run
`/adversarial-review-loop:implement plan.md --model my-org/reviewer-model` for one specific
run. Someone has `ARL_MODEL=debug/stub` set in their shell for testing. The model actually
used is `debug/stub` — environment always wins, activation overrides beat both config
files, and the repo file beats your personal one. Nothing here is silent: run
`/adversarial-review-loop:config` with no arguments and it prints every key's resolved value
*and which layer set it*, computed by re-running the merge layer by layer rather than
guessing.

## Every key

| Key | Default | Purpose |
| --- | --- | --- |
| `harness` | `claude-code` | which reviewer CLI runs the review — `claude-code` or `opencode`. It defaults to Claude Code because that is the CLI this plugin already runs inside; an unimplemented name is refused when you arm, never quietly replaced with the default |
| `model` | the harness's own (`opus` for `claude-code`, `openai/gpt-5.6-sol` for `opencode`) | probed for reachability at arm time, for a harness that can enumerate its models |
| `variant` | unset | reasoning effort — `--variant` on OpenCode, `--effort` (`low`…`max`) on Claude Code |
| `block_severity` | `medium` | blocks when `actionable=yes AND severity >= this` |
| `late_block_severity` | `high` | from round 2 of a phase on, a new finding outside the paths changed since the previous round blocks only at or above this; never below `block_severity` |
| `timeout_sec` | `900` | per review run |
| `max_failures` | `2` | op failures since the last approval before `needs-human` (transient failures excluded — see `max_transient_failures`) |
| `max_transient_failures` | `5` | timeouts/rate-limits/busy-review-slot failures since the last approval before `needs-human`; paced with backoff, no provider call spent while it waits |
| `max_stop_blocks` | `3` | **no-progress** Stop blocks before escalating |
| `max_defers` | `3` | pause escapes per activation |
| `verify_cmd` | unset | run by the hook, output attached as evidence |
| `pure` | `true` | run the reviewer without its ambient extensions — `--pure` on OpenCode, `--safe-mode --disable-slash-commands` on Claude Code |
| `disable_project_config` | `false` | ignore the repository's own agent config — `OPENCODE_DISABLE_PROJECT_CONFIG` on OpenCode, `--setting-sources user` on Claude Code |
| `chunk_diff_bytes` | `400000` | per-attachment diff chunk size |
| `hard_diff_ceiling` | `8388608` | above this → `needs-human` |
| `max_file_bytes` | `16777216` | oversized-file guard |
| `max_reason_bytes` | `32768` | prose cap in a denial message; `FINDING` lines are exempt |
| `max_findings` | `200` | above this → `needs-human`, never a trimmed list |
| `max_findings_bytes` | `65536` | same cap, measured by size instead of count |
| `max_clarifications` | `2` | `clarify` questions per run before it points at `accept` |
| `stall_rounds` | `3` | consecutive rounds a **blocking** finding (`actionable=yes`, at or above `block_severity`) must persist before `needs-human`; `0` disables |
| `max_session_rounds` | `3` | rounds one reviewer session may carry before the next round starts a fresh one; `0` never resets |
| `allow_dirty` | `false` | alternative to passing `--allow-dirty` every time |
| `ttl_hours` | `24` | after this, gates block and ask for a re-arm — `resume` is usually the right fix, not a fresh `implement` |
| `ignore_globs` | `[]` | paths whose sole change skips a review entirely |
| `final_review` | `false` | run the final cumulative review at `Stop` |
| `cold_confirm` | `false` | re-review an approving round cold — no session, no prior-round attachment — and act on that verdict instead |

Environment variables are the upper-cased key with an `ARL_` prefix — `ARL_MODEL`,
`ARL_BLOCK_SEVERITY`, `ARL_MAX_FAILURES`, and so on. Since an environment variable is
always a string, each is coerced by the key's type: a boolean key checks against a set of
true-ish spellings, an integer key requires all-digit text and is otherwise **skipped
entirely** (a typo leaves the previous layer's value standing rather than silently becoming
`0`), and `ARL_IGNORE_GLOBS` is comma-separated into a list. JSON config files need none of
this — their values are already the right type.

## Reading and writing it

```console
$ /adversarial-review-loop:config
harness                  claude-code          (default)
model                    opus                 (default: claude-code)
variant                                       (default)
block_severity           medium               (default)
...
ttl_hours                72                   (user)
```

`model` prints `(default: <harness>)` rather than plain `(default)` because its default is
not a constant: it is whatever the selected harness calls its own, so the value shown is
what a run would actually pass and the layer says which harness decided it.

```console
$ /adversarial-review-loop:config ttl_hours 72
ttl_hours: 24 -> 72
written to /home/you/.config/adversarial-review-loop/config.json

$ /adversarial-review-loop:config ttl_hours --unset
ttl_hours: 72 -> (unset)

$ /adversarial-review-loop:config verify_cmd --repo -- pytest --maxfail=1
verify_cmd: (unset) -> pytest --maxfail=1
written to <repo>/.adversarial-review-loop.json
```

Put `--` before a value that itself looks like a flag (a `verify_cmd` starting with `-`,
for instance) so it isn't parsed as one. `--repo` writes the *repository's* config instead
of your own — see "Repo config is not fully trusted," below, before reaching for it.
`config` is `disable-model-invocation: true` and registers no hooks: it works whether or
not anything is armed, and Claude can never invoke it itself.

Setting `model` additionally checks the name against the selected harness's model list
(`opencode models`). An unreachable reviewer only warns at config time — arming will refuse
on its own if it's still unreachable when you actually start a run — but a name the
reviewer actively rejects is refused outright; pass `--force` to set it anyway. A harness
that cannot enumerate its models at all (Claude Code has no such subcommand) says so and
validates nothing: a name it does not know exits non-zero at review time, which blocks.

Setting `harness` is checked against the harnesses this build implements, and an
unimplemented name is refused with no `--force` escape — that list is a fact about the
build, not something a probe can be inconclusive about.

## Per-run overrides

`--harness H`, `--model X` and `--variant V` on `implement` or `resume` apply for that one
activation only, without touching either config file — useful for trying a different
reviewer on one run, or recovering an activation whose configured model has since become
unreachable. They're stored in the activation's own `overrides` and sit above both config
files in the precedence chain, but below the environment.

`harness` is the one key that is **pinned** into `overrides` when you arm, whether or not
you passed `--harness`. `model` and `variant` are not: they keep resolving through the
config layers on every round. The difference is that `harness` is what decides which binary
has to exist, so it is the only one whose drift silently voids the reachability check
arming just did — a `.adversarial-review-loop.json` edited to another harness mid-activation
would leave every later review failing with "that binary is not on PATH", and that file
travels with the tree under review and is not a trust boundary.

So an activation keeps the harness it was armed with, and switching it is explicit: pass
`--harness` to `resume`, or set `ARL_HARNESS`, which still outranks the overlay. A
`resume` that names a harness has its binary and model list checked against the one being
switched *to*. Continuity does not carry across a switch: a session pointer minted by one
harness is not presentable to another, so the next review simply starts fresh.

What gets pinned is the harness that was actually **probed**, not the one you typed. Since
`ARL_HARNESS` outranks the overlay, `ARL_HARNESS=claude-code … --harness opencode` checks
*claude-code* — so claude-code is what is recorded, and the armed banner says so. Recording the
flag instead would pin a reviewer nothing verified, and the activation would start running
it the moment the variable left your environment.

Two `resume` calls running at once are refused rather than combined: each checks the
reviewer against the overrides it read, so merging their writes could store a
harness/model pair neither one checked. The first to write wins; the other reports that the
overrides moved and writes nothing. Run it again and it checks the combination against
what is now on disk.

## Example repo config

```json
{
  "model": "openai/gpt-5.6-sol",
  "variant": "high",
  "verify_cmd": "make test",
  "ignore_globs": ["CHANGELOG.md", "docs/**"]
}
```

## Repo config is not trusted for policy

`.adversarial-review-loop.json` lives inside the repository under review, which the rest of
this system treats as content an agent could have written — because it can be, once
mutations are allowed, whether through `config --repo` (user-only) or a plain file edit
(not user-only at all). Every key it can set is a real lever, and none of them are fenced
off from a self-serving edit:

- **`ignore_globs` is a full bypass, not a relaxation.** `{"ignore_globs": ["**"]}` makes
  every changed path match, and a commit whose every path is ignored skips the reviewer
  entirely — approved outright, with no model ever consulted
  (`gitsnap.all_paths_ignored`, exercised with exactly this shape in `tests/selftest.sh`).
- **`block_severity` raised to `critical`** means only a critical finding blocks; every
  medium or high finding still shows up in the report, but nothing stops the commit.
- **`late_block_severity` raised to `critical`** widens what a later round may defer: from
  round 2 on, a new high finding in a file untouched since the previous round would then be
  recorded rather than block. It cannot go the other way — a value below `block_severity`
  is read as `block_severity` — and it never reaches round 1, a `final` review, a changed
  path, or a file an earlier round already raised a finding in.
- **`max_findings` / `max_findings_bytes` / `hard_diff_ceiling`, tuned high enough,** raise
  the bar at which a well-formed review result escalates to `needs-human` instead of being
  taken at face value. This is separate from operational fail-closed handling — a genuine
  timeout or malformed response still blocks or escalates no matter what config says; what
  moves is the threshold for a review that came back clean and well-formed.
- **`verify_cmd` is a different kind of risk again**: an actual shell command, run through
  `bash -lc` in the repository. Its output is only evidence — it can't itself approve a
  commit — but setting it *is* code execution, and the string can be anything, not only a
  command already present in the repo.

None of this needs the `config` command at all. `/adversarial-review-loop:config --repo` is
the controlled, user-only way to write this file; the file itself is an ordinary one, and
Claude can rewrite it like any other file once mutations are permitted — the next config
load simply picks up whatever it now says. Treat every key here as something Claude could
set to whatever benefits Claude, not as a value that "can't weaken the gate." See
[security.md](security.md) for the full picture.

## Cost

A review is an agentic run, and an agentic run's bill is roughly **context size × turns**:
every tool call is a turn, and every turn re-reads the whole context. Two real rounds of the
same phase under `harness=claude-code`, `model=opus`:

| round | turns | tool calls | cache read | cache created | output | cost |
| --- | --- | --- | --- | --- | --- | --- |
| 1 (fresh session) | 50 | 49 | 5.65M | 189k | 32k | $5.58 |
| 2 (resumed session) | 28 | 27 | 6.33M | 152k | 24k | $5.28 |

The cache-read column is the bill. The payload itself was ~230 KB; it was re-read 50 times.

**A resumed session costs *more* per turn under Claude Code, not less.** `--resume` replays
every earlier round's attachments and tool results, so round 2 carried roughly twice the
context per turn (~226k vs ~113k) and cost about the same as round 1 despite doing half the
work. Continuity buys the reviewer's memory of the earlier round; it does not buy a discount.
`prior-rounds.txt` and `incremental.diff` carry that memory anyway, as bounded, gate-rendered
evidence — which is why `max_session_rounds 1` is a reasonable setting and not a loss.

### The levers, roughly in order of effect

| Lever | Effect |
| --- | --- |
| `model` | the largest single one. A smaller model is several times cheaper per token, and the token count barely moves |
| `max_session_rounds 1` | never resume; each round pays fresh-session context instead of cumulative |
| `variant low`/`medium` | `--effort` on Claude Code: less thinking output, and usually fewer turns |
| a shorter plan | the frozen plan is capped at 64 KiB and sent **every round**, so plan length is a per-round tax |
| `cold_confirm` off (the default) | on, it adds a second full model call to every approving round |
| `final_review` off (the default) | on, it adds one cumulative review of the whole activation |
| `ignore_globs` | a commit whose every changed path matches skips the review entirely |

### What the gate already does for you

Two things happen without any configuration, both aimed at the per-turn context that the
table above shows is the real driver:

- **The frozen plan is sent once per reviewer session, not once per round.** A plan cannot be
  revised without ending the session it was sent in, so rounds 2 and 3 are told it is already
  in their context instead of receiving another copy — ~16k tokens per round at the 64 KiB
  cap. A cold confirmation, a fresh session and the rare mid-review loss of session ownership
  all still get the full plan; with `cold_confirm` on it ships every time, since a cold call
  reads the same bundle.
- **The working guidance rides in the system prompt.** `prompts/reviewer-efficiency.md` — batch
  independent tool calls, grep to locate then read with `offset`/`limit`, never re-open a file
  — is passed as `--append-system-prompt` on Claude Code rather than buried in the prompt
  payload, because measured across seven real rounds the same words inside a ~100 KB user
  message produced zero batched tool calls. OpenCode has no system-prompt flag, so there it is
  appended to the prompt.

### Where to read the numbers

For a harness that reports its accounting (Claude Code does; OpenCode has no machine-readable
output mode, so nothing is shown rather than something estimated):

- each stored report carries a `cost:` line per invocation — under `cold_confirm` the round
  with context and the cold confirmation each get their own, which is the argument for or
  against that setting in one place;
- `/adversarial-review-loop:status` totals the activation and the current phase;
- the CLI's own output is kept verbatim beside each transcript as `raw/NNN-*.out.envelope`.

None of these figures feed any decision the gate makes. They are reported, never read.

## Where config state lives

State — `state.json`, frozen plans, reports, bundles — never lives inside the repository.
It's under `$XDG_STATE_HOME/adversarial-review-loop/`, one directory per `(worktree, session)`
pair. The **one** exception to "nothing is written inside the repo" is an explicit,
user-only `config <key> <value> --repo`, and even that never chmods anything beyond the one
file it writes.
