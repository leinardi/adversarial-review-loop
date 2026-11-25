# How it works

## The problem

An agent that writes code and then judges its own work is a poor judge: it knows what it meant, and
it reads its diff in that light. A second reader catches what the first one missed, but only if it
sees all of the change, gets no say from the author on what to look at, and is read strictly when
it answers.

This plugin puts that second reader between the phases of a plan. Claude implements one phase; a
reviewer that did not write it reads the whole change and says what is wrong with it; Claude fixes
that and asks again; only an approval moves the run to the next phase.

## A run, start to finish

1. **You start it.** `/arl-implement plan.md` copies the plan into `.arl/plan.md` and takes a
   snapshot of every workspace root: the launch folder, the git repositories the plan names, any
   `--root`, and the `roots` setting.
   Those snapshots are the run's baseline. Then Claude is told what the loop expects of it.
2. **Claude records the phases** with the `set_phases` tool, one short title each. It may revise
   the list until a phase is approved, and later only as an explicit replan.
3. **Claude implements a phase.** When it writes with Edit or Write into a folder outside every
   root, the plugin registers that folder as a root first, so its content before the change is
   the baseline. Before it changes anything else through Bash, Claude calls `add_root` itself.
4. **Claude runs the project's checks and calls `review`** with what they reported.
5. **The engine builds the bundle.** It snapshots every root again and writes the diff from the
   last approved snapshot to this one, for every root, untracked files included. Each path is
   prefixed with its root's name (`api/src/main.py`), so two roots' `src/main.py` never mix. The
   bundle also holds `range.txt` (what is under review and how it is judged), the checks Claude
   reported, the earlier rounds of this phase, and the project's own review skill when it has one.
6. **A fresh reviewer reads it.** By default a subagent the plugin registers and hides from Claude.
   Round 1 of a phase reads the whole diff; a later round reads what changed since the previous
   round and the earlier rounds' findings, and searches the whole diff, still in the bundle, as a
   finding or a fix requires. A later round runs on `late_model`, the weak model (Sonnet) by
   default on the subagent (set it to `""` to keep `model`); round 1 and the final review always
   run on `model`, the strong model (Opus) by default. `/arl-implement --model weak` or
   `/arl-model` picks the model for one run alone, from the next review on
   ([configuration](configuration.md#choosing-the-reviewers-model)).
   It may read files and run read-only commands, nothing else. It answers in a strict contract:
   one `FINDING` line per problem, with severity, whether it is actionable, and the file. A
   finding that blocks ends with a suggested check (`|| done when: ...`) that would show it is
   fixed; Claude sees it under the finding, and the next round judges the defect itself.
7. **The engine decides the verdict**, not the reviewer. An actionable finding at or above
   `block_severity` blocks, whatever the reviewer concluded, even when it left out its `VERDICT`
   line. An answer that breaks the contract (a missing `VERDICT` with nothing blocking included)
   gets one repair attempt, then counts as a failure. A failure is never an approval. A finding
   that cites a line past the end of its file still stands, with a note telling Claude to find
   it by its description. From round 2 of a phase, an approval must also leave no earlier
   blocking finding of the phase open. A finding is closed only by a line that names exactly it:
   `RESOLVED id=r1.3 | <what in the code shows it>`, or a `SUPERSEDES` giving its round and its
   exact `file=` value. Raising a defect again gives it a new id, and both must be closed. The
   reviewer sees the open ids under "Still open" in `prior-rounds.txt`, computed the way the
   engine checks them, and how each earlier finding was closed. An approval that leaves one open
   breaks the contract, and its repair call is told which ids to
   account for. That repair may approve, since the review behind it was read; any other repair
   never does, and may copy a `RESOLVED` line only from the answer it repairs.
8. **Claude reads the outcome.** Findings, each with an id (`r1.3`): fix them and call `review`
   again, saying for each one whether it was fixed or is rejected, with a note. The next reviewer
   is told what this one found and what Claude said, and accounts for every finding: fixed, still
   open, or withdrawn. A rejection that cites no file and line stays open. Dispositions sent to the
   first review of a stage have nothing to answer: they are ignored, and the answer says so.
   Approval: commit each changed repository by its own conventions.
9. **The loop continues.** When Claude's turn ends, the plugin asks the engine whether work
   remains and, if so, prompts Claude with the next phase. After the last approval (and the final
   review, if one is required) the run is complete.

## When the loop needs you

The loop stops prompting Claude and asks you (or, headless, tells Claude to stop and tell you) when:

- the same blocking findings persist for `stall_rounds` rounds;
- the reviewer keeps failing (`max_failures`, `max_transient_failures`);
- the reviewer itself asks for a person;
- the reviewer's model cannot be used (below): at once, with nothing retried;
- a changed file or the whole diff is too large to review;
- a folder joined the run with changes already in it, so its baseline cannot be known. You pick:
  the changes since a commit the engine offers, everything in it is new, or its current content
  is the baseline. For a clone or a worktree made during the run it offers both where the
  checkouts right after it left HEAD (a guess at where the work began) and the commit it was
  cloned or created at. A folder
  registered before it existed, then filled by a clone or a worktree, is asked about the same way
  when Claude registers it again.

You can accept the stage as it stands (`/arl-accept`), keep going (`/arl-resume`), or stop
(`/arl-stop`).

### The reviewer's model

A review never runs on a model other than the one chosen for it, and never quietly on a fallback.
The checks, and what each costs:

- **On the subagent**, the model must be one of Claude Code's aliases (`opus`, `sonnet`, `haiku`,
  `fable`). Before a model's first review in a session, the plugin sends it one tiny request
  ("Reply OK.", one output token) through the session's own credentials. A model the API refuses
  (400, 401, 403, 404) or the session does not allow stops the run; a rate limit, a server error
  or no response counts as a transient failure, and the check runs again next time. After the
  spawn, the model the session reports must be the alias's own: an allowlist that silently runs
  another model stops the run too, and that reviewer's answer is never read.
- **On OpenCode**, the model (the part before any `#variant`) must be one `opencode models`
  lists, checked before each run. A run the provider refuses (an unknown model, no credentials, a
  key it rejects) stops the run; a network failure is transient. `opencode models` lists models
  whether or not you have credentials, so a missing key is caught by the run itself, a few
  seconds in.

When the model cannot be used, the loop says which model, for which round, and why, and asks you
to choose another, accept the stage, keep going on the same model, or stop. Choosing ends the wait.
The model offered is never the refused one: `/arl-model default` (every round back on the built-in
models) when those differ from it, else the other tier for the rounds that failed (`/arl-model
first weak` when the built-in `strong` was refused); `/arl-model <model>` names any other. Headless,
Claude is told to stop and tell you.

In auto mode the session tells the reviewer to deliver its report with `SubagentHandback`, and
only auto mode's own classifier may allow that call. The loop leaves it to the classifier, and
reads the report the call delivered as the reviewer's answer, since the reviewer's last message is
then often only a note about the handback. A handback the session refuses delivers nothing, and
the reviewer's last message is read as before. A handback whose report has no findings block is
refused by the loop itself, with a reason that asks for the report again, whole: up to twice per
reviewer, after which it goes through and the round fails on the missing block.

A git root's starting base follows its checkout to another line of history. Say a repository
was on a fix branch when the run started, and the work then went on a new branch from `main`:
diffed from the fix branch, its fixes would read as removed by the run. When the starting base was
a clean checkout of a commit, and that commit is no longer in `HEAD`'s history, the base moves to
the commit the two lines share. A phase moves it while nothing in that root has been approved
since; the final review moves it whenever. A switch to a branch built on the base changes nothing.

Nothing else moves. An approved base is the run's own work: when the checkout leaves it for a line
without it, that work reads as removed, as it is. A starting base with uncommitted changes, an
empty base (`--empty`, or a root the run created), and a line with no shared history stay too.
When a base stays while the checkout changed branch (and the approved snapshot is not in `HEAD`'s
history), or shares no history with `HEAD`, the root is named in `range.txt` (under "Snapshot
warnings") and to Claude, as is every move, in every review until the base changes.

## What it does not do

It does not enforce anything. Claude can commit without a review, ignore a finding, or write
somewhere nobody registered. The plugin's job is to make the review good, not to police the agent;
[philosophy.md](philosophy.md) explains the choice.
