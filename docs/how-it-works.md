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
   It may read files and run read-only commands, nothing else. It answers in a strict contract:
   one `FINDING` line per problem, with severity, whether it is actionable, and the file.
7. **The engine decides the verdict**, not the reviewer. An actionable finding at or above
   `block_severity` blocks, whatever the reviewer concluded. An answer that breaks the contract
   gets one repair attempt, then counts as a failure. A failure is never an approval.
8. **Claude reads the outcome.** Findings: fix them and call `review` again; the next reviewer is
   told what this one found. Approval: commit each changed repository by its own conventions.
9. **The loop continues.** When Claude's turn ends, the plugin asks the engine whether work
   remains and, if so, prompts Claude with the next phase. After the last approval (and the final
   review, if one is required) the run is complete.

## When the loop needs you

The loop stops prompting Claude and asks you (or, headless, tells Claude to stop and tell you) when:

- the same blocking findings persist for `stall_rounds` rounds;
- the reviewer keeps failing (`max_failures`, `max_transient_failures`);
- the reviewer itself asks for a person;
- a changed file or the whole diff is too large to review;
- a folder joined the run with changes already in it, so its baseline cannot be known. You pick:
  the changes since a proposed commit, everything in it is new, or its current content is the
  baseline.

You can accept the stage as it stands (`/arl-accept`), keep going (`/arl-resume`), or stop
(`/arl-stop`).

## What it does not do

It does not enforce anything. Claude can commit without a review, ignore a finding, or write
somewhere nobody registered. The plugin's job is to make the review good, not to police the agent;
[philosophy.md](philosophy.md) explains the choice.
