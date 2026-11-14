# Reviewer benchmark

`bench.py` measures what a review costs and how many known bugs it finds, so a change to a
reviewer prompt, the bundle or a default can be compared with what came before it under the same
conditions. It is not part of `make test` or CI: `run`, `matrix` and `make bench-baseline` call
real models and cost money. `analyze` and `score` only read files that earlier runs left behind,
and `tests/unit/test_bench.py` covers them.

Everything the bench writes goes under `ARL_BENCH_DIR` (default `~/.local/share/arl-research`),
never inside this repository.

## What a cell is

A **phase** is one real commit of this repository, named in `phases.json`. It may also be
**seeded**: `seeds/<phase>.patch` plants six one-line bugs on top of the commit, and
`seeds/<phase>.truth.json` describes them.

A **case** is a scratch clone at `<commit>^`. A run is initialised in it through `scripts/arl.sh`,
then its tree moves to `<commit>` (seeded or not). The next `review` therefore sees exactly that
commit as its one phase. The bench keeps its own record of the case, `cases/<name>.bench.json`,
beside the clone and not inside it, because anything inside the clone would be reviewed as a
change.

A **cell** is one combination of phase, reviewer model, plugin variant and kind (seeded or
clean), run once in a fresh case by a headless session:

```console
claude -p '<call review once>' --plugin-dir <variant> --model haiku \
  --setting-sources project,local --strict-mcp-config
```

The reviewer's model is set with `ARL_MODEL`, and the main session is Haiku because it only calls
the tool. A **variant** is a plugin directory: this checkout by default, any other directory, or
`@<rev>` for this repository's tree at that revision (exported once into `plugins/rev-<sha>`).

`make bench-baseline` runs the round-1 matrix on this checkout: `medium` and `large1`, Opus and
Sonnet, seeded and clean, one run each. That makes 8 cells, 4 at a time. It writes
`runs/baseline.summary.json` and prints a table. Set `BENCH_PREFIX` to keep a second matrix apart
from the first. For more runs or other variants, call the script directly:

```console
python3 -B tests/bench/bench.py matrix --runs 3 --plugin before=@main --plugin after=. --kind seeded
```

## Subcommands

| Command | What it does |
| --- | --- |
| `case <commit> <name> [--seed P] [--phase N] [--plugin D]` | prepare a case |
| `run <case> --model M [--plugin D] [--tag T]` | one headless review; output in `runs/<tag>/` |
| `round2 <phase> <round-1 case> <name>` | copy a round-1 case that ended `CHANGES_REQUIRED` and apply `seeds/<phase>.round2.patch` |
| `analyze <case>...` | one JSON row per reviewer transcript: requests, tool calls, reads, tokens, cost units |
| `score <case>... [--round2]` | one JSON row per case: recall against its phase's truth |
| `matrix [...]` | `case`, `run`, `analyze` and `score` over a grid |

Case and run names are single path segments (letters, digits, `.`, `_`, `-`). `case`, `round2`
and `matrix` refuse to overwrite an existing case unless given `--replace`, and `round2` refuses
to use its source's name. `make bench-baseline` passes `BENCH_ARGS`, so a second baseline is
`make bench-baseline BENCH_PREFIX=other` or `BENCH_ARGS=--replace`.

Each `run` starts its session with a fresh `--session-id`, recorded in the case's `.bench.json`
before the session starts. `analyze` reads the reviewer subagents' transcripts under only those
sessions, in the bucket Claude Code keeps for the case directory (under `CLAUDE_CONFIG_DIR`,
default `~/.claude`). The bucket outlives the case: a case name used again by a later matrix
would otherwise add the earlier runs' cost to its own. Other subagents are skipped. A
transcript is joined to its `round_history` entry through its task description
(`Review <label>, round <n>`). The OpenCode harness leaves no transcript there.

## Cost units

Thousands of tokens, weighted by kind: input 1, cache write 1.25, cache read 0.1, output 5. Every
model gets the same weights, so a unit compares requests and context. It is not a bill: Opus's
per-token price is not applied.

## Scoring

A finding is a **candidate** for a defect when it cites the defect's file and either a line
within 5 of the defect's line or one of its `symbols` (a case-insensitive substring of the
finding's text). Both tests are needed because round-1 Claude reviewers often cite a line of the
bundle's diff file, not of the source.

A finding names at most one defect. When it is a candidate for several, only symbols can tell
them apart, because a line is shared by every defect near it. The defect with strictly the most
symbols in the finding wins. A tie, or several candidates matched by line alone, goes to
`ambiguous` with the candidates' ids, for a person to judge. The scorer does not guess, because a
guess between a fixed defect and a regression planted on the same line decides a round-2 figure.
A defect named by several findings is credited once: first a blocking finding, then one with a
near line, then the earlier one.

Two figures per run:

- **reported**: the defect was named at any severity;
- **blocking**: it was named by an `actionable=yes` finding at or above `block_severity`
  (`--block-severity`, default `medium`).

`unmatched_blocking` lists every blocking finding that named no defect and is not ambiguous. Read
these and `ambiguous` by hand. An unmatched finding is either a false positive or a real bug the
seed did not plant.

### Round 2

`seeds/<phase>.round2.patch` fixes some round-1 defects and plants one regression in a fix.
`seeds/<phase>.round2.truth.json` classifies every record:

| Class | Meaning |
| --- | --- |
| `open` | a round-1 defect the fix patch leaves in |
| `regression` | planted by the fix patch; its own file, line and symbols |
| `fixed` | a round-1 defect the patch removes; a finding on it is a **false re-raise** |
| `untouched-miss` | a round-1 defect in a file the patch does not change, which round 1 did not report |

`score --round2` reports both figures per class, and counts false re-raises in total and as
blocking. A regression planted on the line of a fixed defect shares that line, so each record's
`symbols` must name what is specific to it: in `large1`, `L1`'s inverted `now ==` and `LR`'s
`exists()`. A finding that names only the shared function is ambiguous. A finding near a fixed
defect's line, with nothing that names another defect, still counts as a re-raise, even when it
raises something new there.

The round-2 manifests assume a round 1 by Opus, which reported every defect except the sixth
(`M6`, `L6`). After a round 1 that found a different set, `untouched-miss` no longer means what
it says, so build round-2 cases from a run like that one (`score` it first).

## Adding a seeded phase

1. Add the commit to `phases.json` under a new name.
2. Write the bugs into a checkout at that commit and save `git diff` as `seeds/<name>.patch`. Keep
   each bug to one line, and write them at several levels of difficulty.
3. Write `seeds/<name>.truth.json`: one record per bug with `id`, `file`, `line` (in the seeded
   tree), `symbols` (the enclosing function and a distinctive token of the bug), `severity` and
   `what`. Choose symbols a reviewer would name for this bug and not for its neighbours. Then
   check them against real findings: score a few runs and read every `unmatched_blocking`.
4. For round 2, apply the seed, commit it in a scratch clone, revert some bugs, plant a
   regression, and save `git diff HEAD` as `seeds/<name>.round2.patch`. Then write
   `seeds/<name>.round2.truth.json` with a `class` on every record.

## Baseline

Measured 2026-11-13 on the plugin at 7524e30 (`base`) and with the efficiency prototype in the
reviewer system prompt (`eff`). Recall was recomputed with this scorer. Blocking recall equals
what was found by reading the findings, and reported recall differs in one run only. The full
notes are in `RESULTS.md` under the bench directory.

Phases: `small` = f6992b6, 24 KB diff; `medium` = 219159e, 93 KB; `large1` = 0b1bfc3, 141 KB.

### Round 1, clean phases, one run per cell

| Diff | Model | Prompt | Requests | Cache write | Cache read | Output | Units | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 24 KB | Sonnet | base | 5 | 28k | 85k | 2k | 52 | approved |
| 24 KB | Opus | base | 9 | 34k | 224k | 5k | 91 | approved |
| 93 KB | Sonnet | base | 6 | 58k | 208k | 4k | 114 | approved |
| 93 KB | Sonnet | eff | 5 | 40k | 130k | 2k | 77 | approved |
| 93 KB | Opus | base | 18 | 78k | 964k | 12k | 259 | approved |
| 93 KB | Opus | eff | 7 | 65k | 295k | 9k | 158 | changes required (1 medium, not verified) |
| 141 KB | Sonnet | base | 12 | 92k | 732k | 8k | 229 | approved |
| 141 KB | Sonnet | eff | 5 | 60k | 171k | 5k | 118 | approved |
| 141 KB | Opus | base | 33 | 111k | 2,847k | 22k | 534 | changes required (1 medium, real) |
| 141 KB | Opus | eff | 9 | 95k | 553k | 15k | 251 | approved (rated the same issue low) |

### Round 1, six seeded bugs, three runs per cell

| Diff | Model | Prompt | Blocking recall | Reported recall | Requests | Mean units |
| --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Opus | base | 5, 5, 5 | 5, 5, 5 | 6, 6, 7 | 123 |
| 93 KB | Opus | eff | 5, 4, 5 | 5, 4, 5 | 5, 5, 4 | 113 |
| 93 KB | Sonnet | base | 2, 3, 2 | 2, 3, 2 | 4, 4, 5 | 82 |
| 93 KB | Sonnet | eff | 3, 2, 2 | 4, 2, 2 | 4, 5, 5 | 86 |
| 141 KB | Opus | base | 5, 4, 5 | 5, 4, 5 | 8, 17, 6 | 187 |
| 141 KB | Opus | eff | 4, 4, 5 | 4, 4, 5 | 6, 5, 9 | 164 |
| 141 KB | Sonnet | base | 1, 2, 3 | 1, 2, 3 | 8, 8, 10 | 128 |
| 141 KB | Sonnet | eff | 3, 1, 3 | 3, 1, 3 | 5, 3, 8 | 119 |

None of the 24 runs had a blocking finding outside the seeded bugs. No run found the subtle bug
of either phase (`M6`, `L6`).

### Round 2 after an Opus round 1, one run per cell

`late` is `eff` plus a round-2 prototype prompt. Each cell shows blocking recall for the class.

| Diff | Model | Prompt | Units | Open | Regression | Untouched miss | False re-raises |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Opus | eff | 77 | 1/1 | 1/1 | 0/1 | 0 |
| 93 KB | Opus | late | 46 | 1/1 | 1/1 | 0/1 | 1 (medium) |
| 93 KB | Sonnet | eff | 43 | 1/1 | 1/1 | 0/1 | 0 |
| 93 KB | Sonnet | late | 37 | 1/1 | 1/1 | 0/1 | 0 |
| 141 KB | Opus | eff | 52 | 1/1 | 1/1 | 0/1 | 0 |
| 141 KB | Opus | late | 47 | 1/1 | 0/1 (rated low) | 0/1 | 1 (low) |
| 141 KB | Sonnet | eff | 29 | 1/1 | 0/1 (missed) | 0/1 | 0 |
| 141 KB | Sonnet | late | 41 | 1/1 | 1/1 | 0/1 | 0 |

Both re-raises cite the line of a fixed defect and raise something next to it: a fix only half
applied (`hooks/mod.ts:106`) and a test the fix did not add (`hooks/flow.ts:501`).

## After: the final tree

Measured 2026-11-14 on the plugin at `883a6e7`, with every change of this work in: efficiency
guidance, 40 KB diff chunks, the contract hardening, `done when`, finding ids and the later-round
prompt. Same cells and runs as the table under "Measured on this harness": Sonnet three runs per
cell, Opus one, every run reading its whole diff. Units are cost units (k), Sonnet's as the mean
with the range.

### Round 1

| Phase | Model | Kind | Baseline (`a9f8bc8`) requests | units | recall | Final requests | units | recall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Sonnet | clean | 8 | 129 |  | 3 to 5 | 101 (93 to 112) |  |
| 93 KB | Sonnet | seeded | 4 | 94 | 3/6 | 2 | 82 (79 to 84) | 4, 3, 4 |
| 141 KB | Sonnet | clean | 15 | 283 |  | 4 to 7 | 161 (143 to 176) |  |
| 141 KB | Sonnet | seeded | 6 | 146 | 3/6 | 3 to 4 | 126 (118 to 138) | 3, 2, 3 |
| 93 KB | Opus | clean | 13 | 229 |  | 7 | 181 |  |
| 93 KB | Opus | seeded | 8 | 138 | 5/6 | 4 | 119 | 5/6 |
| 141 KB | Opus | clean | 16 | 290 |  | 6 | 214 |  |
| 141 KB | Opus | seeded | 11 | 215 | 5/6 | 4 | 158 | 5/6 |

Clean round 1 costs 21 to 43% less, not the half the plan expected; seeded round 1 costs 13 to
27% less. Requests fell in every cell. Seeded recall is within the recorded spread in every cell.
The Opus 93 KB run found `M5` but cited the wrong line of `state.py`; `M5`'s record gained the
symbol `index < done`, the seeded comparison itself, and rescoring every earlier 93 KB seeded run
with it changed none of them. The one Opus clean phase that asked for changes raised real issues
of that commit, as the baseline did.

### Round 2

After the same Opus round 1 as under "Round 2 on this harness", three runs per cell, against the
plugin at `ccb6605` (before finding ids, dispositions and the later-round prompt). The bench
states no dispositions, so this measures the prompt, not what Claude says about the fixes.

| Phase | Model | `ccb6605` units | Final units | Final requests | Final verdicts | Open | Regression |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Opus | 98 to 104 | 56 to 67 | 5 | 3 changes required | `*` `*` `*` | `*` `*` `*` |
| 93 KB | Sonnet | 34 to 46 | 35 to 36 | 3 to 4 | 3 changes required | `*` `*` `*` | `*` `*` `*` |
| 141 KB | Opus | 131 to 133 | 41 to 50 | 4 to 5 | 3 changes required | `*` `*` `*` | low, low, low |
| 141 KB | Sonnet | 27 to 35 | 30 to 35 | 3 to 5 | 1 approved, 2 changes required | `*` `*` `-` | `-`, low, low |

Opus round 2 now costs about half what it did, with the same recall (9 of 12 blocking, the 141 KB
regression rated low every time) and no false re-raise, against one before. That leaves Sonnet
(30 to 36) about a third cheaper rather than two thirds, so `late_model` buys less than it
did. Sonnet is unchanged: 8 of 12, and again one run approved the 141 KB phase with `L4` open.
Every round 2 read the increment and none of the phase diff, as the later-round prompt asks.

## Measured on this harness

`make bench-baseline` cells on the 93 KB (`medium`) and 141 KB (`large1`) phases. The baseline is
the plugin at `a9f8bc8`, one run per cell. "Now" is the reviewer efficiency guidance with
40 KB diff chunks: Sonnet three runs per cell, Opus one. Units are cost units (k); "now" gives
the mean, and the range where there are three runs. A seeded cell shows blocking recall per run.
Every run in this table read its whole diff (`Diff read` in the matrix table). The clean cells'
blocking findings, where there are any, are real issues of those commits, fixed later.

| Phase | Model | Kind | Baseline requests | units | recall | Now requests | units | recall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Sonnet | clean | 8 | 129 |  | 3 to 7 | 108 (94 to 126) |  |
| 93 KB | Sonnet | seeded | 4 | 94 | 3/6 | 2 to 3 | 83 (80 to 88) | 3, 2, 3 |
| 141 KB | Sonnet | clean | 15 | 283 |  | 3 to 10 | 177 (139 to 219) |  |
| 141 KB | Sonnet | seeded | 6 | 146 | 3/6 | 3 | 123 (122 to 124) | 2, 3, 2 |
| 93 KB | Opus | clean | 13 | 229 |  | 7 | 189 |  |
| 93 KB | Opus | seeded | 8 | 138 | 5/6 | 3 | 108 | 5/6 |
| 141 KB | Opus | clean | 16 | 290 |  | 10 | 283 |  |
| 141 KB | Opus | seeded | 11 | 215 | 5/6 | 4 | 148 | 5/6 |

Requests fell in every cell, and units by 2 to 38%. Recall held: three more baseline runs of the
141 KB Sonnet seeded cell scored 2, 2 and 3, the same spread. The baseline is one run per cell
and run-to-run cost varies by up to half (the 141 KB Sonnet clean runs above), so a single cell's
saving is not a measurement; the direction across all eight is.

### Why the chunk size changed

Claude Code's `Read` returns at most 25,000 tokens of a file, about 1,000 lines of these diffs.
The 141 KB diff used to be one 2,827-line file, and its first read stopped at line 1,004 with a
note to read on. Sonnet often read one more page and then wrote its review: three of four
baseline runs of the 141 KB seeded cell read 1,699 to 1,859 lines, and a run with the guidance
that stopped at 1,704 wrote that it had read the whole diff. With 40 KB chunks a single read
takes in a whole file; the prompts still tell the reviewer to read on when a read stops short,
which a dense diff can still need.

### Historical, not validated

Earlier runs of this work, kept for the record only. They are not evidence of a saving, because
some read only part of the diff and a skipped part is cheap.

- First guidance wording (`never re-read a file`), 400 KB chunks, one run per cell: Sonnet read
  1,116 of 1,597, 1,257 of 1,599 and 2,008 of 2,827 diff lines in three of four cells.
- Revised wording, 400 KB chunks: all eight single runs read the whole diff, but seven 141 KB
  Sonnet seeded runs then read all of it in five (blocking recall 1, 2, 2, 2, 1) and stopped at
  1,751 and 1,704 lines in two (recall 2 and 3).

### Round 2 on this harness

Which model reviews from round 2 on (`late_model`). Each round 2 follows the same Opus round 1
(the `p3d` seeded cell, which reported every defect but the sixth), three runs per cell, with
the plugin at `ccb6605`. A cell lists, per run, the open defect and the regression: `*` when the
finding blocked, the severity given when it did not, `-` when it was not reported. "Full diff"
is the share of the whole phase diff (`changes.NN.diff`) the reviewer read.

| Phase | Model | Units (k) | Requests | Full diff | Verdicts | Open | Regression |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 93 KB | Opus | 98 to 104 | 3 | all, 3 of 3 | 3 changes required | `*` `*` `*` | `*` `*` `*` |
| 93 KB | Sonnet | 34 to 46 | 3 to 5 | none, 3 of 3 | 3 changes required | `*` `*` `*` | `*` `*` `*` |
| 141 KB | Opus | 131 to 133 | 3 | all, 3 of 3 | 3 changes required | `*` `*` `*` | low, low, low |
| 141 KB | Sonnet | 27 to 35 | 2 to 4 | none, 3 of 3 | 1 approved, 2 changes required | `-` `*` `*` | low, `-`, `-` |

Blocking recall on the open defect and the regression: Opus 9 of 12, Sonnet 8 of 12, within the
one miss the plan allowed. `late_model` still ships empty, because one Sonnet run approved the
141 KB phase with its open medium defect (`L4`) unreported. Every Sonnet round 2 read only
`incremental.diff` and `prior-rounds.txt`, as the round-2 prompt asked, and none of the phase
diff; Opus read the phase diff as well, which is most of the difference in cost. One Opus run
re-raised a fixed defect (`M4`, blocking). No untouched miss was found by either model.
