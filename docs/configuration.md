# Configuration

## Precedence

Lowest to highest:

1. the defaults below;
2. the user config, `$XDG_CONFIG_HOME/adversarial-review-loop/config.json` (by default
   `~/.config/adversarial-review-loop/config.json`);
3. the launch folder's `.adversarial-review-loop.json`, the folder you ran `/arl-implement` in;
4. `ARL_*` environment variables: the upper-cased key with an `ARL_` prefix (`ARL_MODEL`,
   `ARL_BLOCK_SEVERITY`).

A config file that cannot be parsed discards the whole file layer, not just itself, so a broken
file leaves the defaults standing rather than half of a configuration. In the environment, a
boolean key is true only for `1`, `true`, `TRUE`, `yes` or `on`; an integer key that is not all
digits is skipped, so a typo leaves the previous layer's value standing; a list key is
comma-separated.

Every value is read fresh for each review and each continuation prompt, except `final_review` and
`roots`, which a run takes when it starts (use `/arl-finish` to ask for the final review mid-run,
`add_root` for a folder), and `effort`, which the reviewer is registered with at session start and
again when `/arl-config` sets it.

## Every key

| Key | Default | Purpose |
| --- | --- | --- |
| `harness` | `subagent` | who reviews: `subagent`, a hidden Claude Code subagent the plugin registers, or `opencode`, which the engine runs itself. An unknown name is refused, never replaced by the default |
| `model` | empty: the session's subagent default, or `openai/gpt-5.6-sol` for `opencode` | the reviewer's model |
| `effort` | empty: the agent's default | the subagent reviewer's effort: `low`, `medium`, `high`, `xhigh` or `max`. A value that is no level is ignored, and said at session start |
| `variant` | empty | OpenCode only: the model variant, passed as `-m <model>#<variant>`. A `model` containing `#` and a `variant` together are refused |
| `disable_project_config` | `false` | OpenCode only: ignore the repository's own OpenCode config (`OPENCODE_DISABLE_PROJECT_CONFIG`) |
| `verify_cmd` | empty | the command Claude is told to run before each review, named in the start prompt and every continuation prompt. The engine never runs it: what Claude reports reaches the reviewer as `verification.txt`, labelled as the implementer's report |
| `block_severity` | `medium` | an actionable finding at or above this blocks the approval, whatever verdict the reviewer gave |
| `late_block_severity` | `high` | from round 2 of a phase on, a new finding in a path untouched since the previous round blocks only at or above this; never below `block_severity` |
| `timeout_sec` | `900` | one review run |
| `max_failures` | `2` | reviewer failures (crash, broken contract) tolerated in a row: the next one makes the loop stop prompting Claude and ask you. Any answered review resets the count, and a `review` Claude calls anyway still runs |
| `max_transient_failures` | `5` | timeouts and rate limits tolerated in a row, likewise |
| `stall_rounds` | `5` | rounds a blocking finding may persist before the loop asks you; `0` disables |
| `max_clarifications` | `2` | questions Claude may ask the reviewer about one review |
| `ignore_globs` | `[]` | paths (inside their root) whose sole change approves a stage without a review. A change to `.adversarial-review-loop.json` itself is never ignored |
| `final_review` | `false` | end every run with a review of all its changes, every root from where it joined the run |
| `review_skill` | `auto` | each root's own review skill, attached as guidance for that root: `auto`, a skill name, or `off`. See below |
| `review_guide` | empty | one Markdown file (relative to the launch folder) spliced into every review prompt |
| `roots` | `[]` | folders every run in this launch folder starts with, besides the launch folder, `--root` and the paths the plan names |
| `auto_compact` | `false` | compact the conversation before the phase that follows an approval. An approval tells Claude to commit and end its turn; the loop then compacts and prompts the next phase. A turn that goes on into the next phase cancels it: by calling review there, or by writing a file in a root with Edit, Write or NotebookEdit before it ends (a change made only through Bash does not count, and a turn with one still compacts). Never headless |
| `chunk_diff_bytes` | `400000` | the size of one diff file in the bundle |
| `hard_diff_ceiling` | `8388608` | a diff larger than this makes the loop ask you rather than review a cut |
| `max_file_bytes` | `16777216` | one changed file larger than this makes the loop ask you |
| `max_reason_bytes` | `32768` | the reviewer's prose kept in the text Claude reads; findings are exempt |
| `max_findings` | `200` | more findings than this makes the loop ask you (the phase is too large to judge), never a trimmed list |
| `max_findings_bytes` | `65536` | the same cap, by size |

## Reading and writing it

```console
> /arl-config
adversarial-review-loop configuration for /home/you/work
  auto_compact            false  (default)
  block_severity          medium  (default)
  ...
> /arl-config effort high
effort: "" -> high (wrote high to /home/you/.config/adversarial-review-loop/config.json)
> /arl-config verify_cmd --repo -- make test
> /arl-config effort --unset
```

The arguments are split as a shell would. Flags come before a bare `--`, which ends them, so a value
can hold its own flags (`-- pytest --maxfail=1`). A value of several words keeps its quoting. A value
that does not parse for its key is refused, never coerced, and a config file that does not parse is
never overwritten. When another layer overrides the key you set, the answer says so.

## The project's own review skill

For each root a review covers, the engine looks in `.claude/skills/` and then `.agents/skills/` for
a skill named `adversarial-review`, then any skill whose name holds `review` (`review_skill: auto`).
A name picks that skill; `off` uses none. The skill's text is attached to the review as guidance
scoped to that root, and the prompt says what replaces its own steps: the bundle replaces "establish
the diff", the checks are not re-run, and the plugin's output contract replaces the skill's report
format. `review_guide` is attached first, for every root. Guidance past 64 KiB in all is left out,
and the review says what was dropped.

## The launch folder's config is not trusted for policy

`.adversarial-review-loop.json` lives in the launch folder, which is usually a root under review.
Claude can edit it like any other file, and every key is a real lever. The config is read when the
review is prepared, so an edit takes effect on the very review that shows it: a `block_severity`
of `critical` lets every high finding through, that one included, and `review_guide` is text the
reviewer reads as instruction. The edit itself is in the phase's diff, and a change to the file is
never ignored, so `ignore_globs: ["**"]` cannot approve its own phase without a reviewer, but it
skips the review of every later phase that leaves the file alone. v4 does not try to stop such an
edit (see [philosophy.md](philosophy.md)).

## Where the run lives

Everything about a run is in `<launch folder>/.arl/`, never in a reviewed repository's tracked
content:

| Path | What |
| --- | --- |
| `state.json` | the run: phases, roots and their baselines, approvals, the pending review |
| `plan.md` | the copy of the plan the run started with |
| `reviews/<run>-<seq>/` | each review's prompt, bundle and answer |
| `reports/` | one report per review round, read with `/arl-report` |
| `shadow/` | the snapshot store of each plain-folder root |
| `runs/<id>/` | earlier runs: archived by `/arl-implement --new`, or by any `/arl-implement` once stopped or complete (`corrupt-<time>` for one that could not be read) |

A git root that contains `.arl/` gets it (`/.arl/`, or `/<path>/.arl/` when the launch folder is
deeper) in its `.git/info/exclude`, never in a tracked file.
