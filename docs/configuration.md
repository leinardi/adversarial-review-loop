# Configuration

## Precedence

Five layers, lowest to highest priority:

```text
defaults  <  user config  <  repo config  <  activation overrides  <  OCRL_* environment
```

| Layer | Where | Set by |
| --- | --- | --- |
| defaults | built into `config.py` | nobody — the fallback |
| user config | `$XDG_CONFIG_HOME/opencode-review-loop/config.json` | `/opencode-review-loop:config <key> <value>` |
| repo config | `<repo>/.opencode-review-loop.json` | `/opencode-review-loop:config <key> <value> --repo`, or hand-edited |
| activation overrides | inside `state.json`, for the current activation only | `--model`/`--variant` on `implement` or `resume` |
| environment | the process environment | `OCRL_*` variables |

A concrete example: your user config sets `model` to `openai/gpt-5.6-sol`. The repo you're
working in sets it to `anthropic/claude-opus-5` in `.opencode-review-loop.json`. You run
`/opencode-review-loop:implement plan.md --model my-org/reviewer-model` for one specific
run. Someone has `OCRL_MODEL=debug/stub` set in their shell for testing. The model actually
used is `debug/stub` — environment always wins, activation overrides beat both config
files, and the repo file beats your personal one. Nothing here is silent: run
`/opencode-review-loop:config` with no arguments and it prints every key's resolved value
*and which layer set it*, computed by re-running the merge layer by layer rather than
guessing.

## Every key

| Key | Default | Purpose |
| --- | --- | --- |
| `model` | `openai/gpt-5.6-sol` | probed for reachability at arm time |
| `variant` | unset | reasoning effort (`high`, `max`, …) |
| `block_severity` | `medium` | blocks when `actionable=yes AND severity >= this` |
| `timeout_sec` | `900` | per review run |
| `max_failures` | `2` | op failures since the last approval before `needs-human` (transient failures excluded — see `max_transient_failures`) |
| `max_transient_failures` | `5` | timeouts/rate-limits/busy-review-slot failures since the last approval before `needs-human`; paced with backoff, no provider call spent while it waits |
| `max_stop_blocks` | `3` | **no-progress** Stop blocks before escalating |
| `max_defers` | `3` | pause escapes per activation |
| `verify_cmd` | unset | run by the hook, output attached as evidence |
| `pure` | `true` | pass `--pure` to OpenCode |
| `disable_project_config` | `false` | set `OPENCODE_DISABLE_PROJECT_CONFIG` |
| `chunk_diff_bytes` | `400000` | per-attachment diff chunk size |
| `hard_diff_ceiling` | `8388608` | above this → `needs-human` |
| `max_file_bytes` | `16777216` | oversized-file guard |
| `max_reason_bytes` | `32768` | prose cap in a denial message; `FINDING` lines are exempt |
| `max_findings` | `200` | above this → `needs-human`, never a trimmed list |
| `max_findings_bytes` | `65536` | same cap, measured by size instead of count |
| `max_clarifications` | `2` | `clarify` questions per run before it points at `accept` |
| `stall_rounds` | `2` | consecutive rounds a finding must persist before `needs-human`; `0` disables |
| `allow_dirty` | `false` | alternative to passing `--allow-dirty` every time |
| `ttl_hours` | `24` | after this, gates block and ask for a re-arm — `resume` is usually the right fix, not a fresh `implement` |
| `ignore_globs` | `[]` | paths whose sole change skips a review entirely |
| `final_review` | `false` | run the final cumulative review at `Stop` |

Environment variables are the upper-cased key with an `OCRL_` prefix — `OCRL_MODEL`,
`OCRL_BLOCK_SEVERITY`, `OCRL_MAX_FAILURES`, and so on. Since an environment variable is
always a string, each is coerced by the key's type: a boolean key checks against a set of
true-ish spellings, an integer key requires all-digit text and is otherwise **skipped
entirely** (a typo leaves the previous layer's value standing rather than silently becoming
`0`), and `OCRL_IGNORE_GLOBS` is comma-separated into a list. JSON config files need none of
this — their values are already the right type.

## Reading and writing it

```console
$ /opencode-review-loop:config
model                    openai/gpt-5.6-sol   (default)
variant                                       (default)
block_severity           medium               (default)
...
ttl_hours                72                   (user)
```

```console
$ /opencode-review-loop:config ttl_hours 72
ttl_hours: 24 -> 72
written to /home/you/.config/opencode-review-loop/config.json

$ /opencode-review-loop:config ttl_hours --unset
ttl_hours: 72 -> (unset)

$ /opencode-review-loop:config verify_cmd --repo -- pytest --maxfail=1
verify_cmd: (unset) -> pytest --maxfail=1
written to <repo>/.opencode-review-loop.json
```

Put `--` before a value that itself looks like a flag (a `verify_cmd` starting with `-`,
for instance) so it isn't parsed as one. `--repo` writes the *repository's* config instead
of your own — see "Repo config is not fully trusted," below, before reaching for it.
`config` is `disable-model-invocation: true` and registers no hooks: it works whether or
not anything is armed, and Claude can never invoke it itself.

Setting `model` additionally checks the name against `opencode models`. An unreachable
reviewer only warns at config time — arming will refuse on its own if it's still
unreachable when you actually start a run — but a name the reviewer actively rejects is
refused outright; pass `--force` to set it anyway.

## Per-run overrides

`--model X` and `--variant V` on `implement` or `resume` apply for that one activation
only, without touching either config file — useful for trying a different reviewer on one
run, or recovering an activation whose configured model has since become unreachable.
They're stored in the activation's own `overrides` and sit above both config files in the
precedence chain, but below the environment.

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

`.opencode-review-loop.json` lives inside the repository under review, which the rest of
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
- **`max_findings` / `max_findings_bytes` / `hard_diff_ceiling`, tuned high enough,** raise
  the bar at which a well-formed review result escalates to `needs-human` instead of being
  taken at face value. This is separate from operational fail-closed handling — a genuine
  timeout or malformed response still blocks or escalates no matter what config says; what
  moves is the threshold for a review that came back clean and well-formed.
- **`verify_cmd` is a different kind of risk again**: an actual shell command, run through
  `bash -lc` in the repository. Its output is only evidence — it can't itself approve a
  commit — but setting it *is* code execution, and the string can be anything, not only a
  command already present in the repo.

None of this needs the `config` command at all. `/opencode-review-loop:config --repo` is
the controlled, user-only way to write this file; the file itself is an ordinary one, and
Claude can rewrite it like any other file once mutations are permitted — the next config
load simply picks up whatever it now says. Treat every key here as something Claude could
set to whatever benefits Claude, not as a value that "can't weaken the gate." See
[security.md](security.md) for the full picture.

## Where config state lives

State — `state.json`, frozen plans, reports, bundles — never lives inside the repository.
It's under `$XDG_STATE_HOME/opencode-review-loop/`, one directory per `(worktree, session)`
pair. The **one** exception to "nothing is written inside the repo" is an explicit,
user-only `config <key> <value> --repo`, and even that never chmods anything beyond the one
file it writes.
