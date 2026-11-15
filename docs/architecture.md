# Architecture

## The shape of it

```text
hooks/mod.ts (Claude Code mod: orchestrator, UI)    scripts/arl/ (engine: Python CLI, JSON in and out)
  tools     set_phases, add_root, review              run state in <launch>/.arl/state.json, one lock
  agent     adversarial-review-loop:reviewer          workspace roots, git and shadow snapshots
  commands  /arl-implement ... /arl-remove-root       the bundle, prompt composition, review skills
  hooks     tool.call, tool.check, turn.complete,     contract parsing and the recomputed verdict
            session.start, ui.render, ...             stall detection, failure budgets, reports
  UI        band, review pane, log, notify, ask       opencode harness (run by the engine)
```

The mod decides nothing about the run. Each step is one engine command, run as
`python3 -I scripts/arl-bootstrap.py --dir <launch folder> <command>` through `scripts/arl.sh`,
answering one JSON object on stdout. The mod runs things in order, shows what the engine answered,
and asks the person when the engine says the run waits for them.

## The mod

| File | What |
| --- | --- |
| `hooks/mod.ts` | registers everything and every hook |
| `hooks/flow.ts` | each tool's flow, a review round on either harness, asking the user, root registration, continuation |
| `hooks/reviewer.ts` | the reviewer agent type: registration, spawn, waiting for its answer, its permissions |
| `hooks/commands.ts` | the `/arl-*` commands |
| `hooks/ui.tsx`, `types/index.d.ts` | the band and the pane, and the session state they draw |
| `hooks/engine.ts`, `hooks/text.ts` | running the engine; the text Claude and the reviewer read |

- **The reviewer** is registered at session start (`$.agent.register`, `omitClaudeMd`, tools Read,
  Grep, Glob and Bash), hidden from Claude by an `agent.offer` hook, and spawned once per round. A
  `tool.check` hook allows it reads under the bundle and the roots and read-only Bash, and denies
  everything else, and everything at all once its review is over.
- **Waiting for it.** A hook's own time budget runs while it awaits a plain promise, so the review
  tool waits for the subagent's `turn.complete` while a `$.process.spawn(['sleep', timeout])` is in
  flight: the spawn stops the clock, and its exit is the review's timeout.
- **Roots before writes.** A `tool.call` hook on Edit, Write and NotebookEdit registers the folder
  a file is written to, unless the session's rules deny the write. Once the write is over it offers
  the root back (`root-remove --undo`), and the engine drops it only when the folder is unchanged:
  the write was refused or failed. An undo the refusal's abort cut short is retried at the turn's
  end.
- **Continuation.** After a main-loop turn that ended with an answer, the mod asks `next-action`;
  on `continue` it submits the engine's prompt (after compacting, when `auto_compact` is due).

## The engine

| Module | What |
| --- | --- |
| `cli.py`, `commands.py` | argv in, one JSON answer out; the command table |
| `state.py` | the run document, its transitions, `next_action` and its continuation token |
| `workspace.py`, `gitsnap.py` | roots, nested-repository discovery, late baselines; snapshots through a throwaway `GIT_INDEX_FILE` (git roots) or a shadow git dir (plain folders) |
| `review.py` | `review prepare` / `record` / `run-external` / `abandon`, dispositions, repair, failure budgets, stalls |
| `bundle.py` | the bundle: diff chunks per root with alias-prefixed paths, `range.txt`, `prior-rounds.txt`, `verification.txt`, the composed prompt (`reviewer-phase` for round 1, `reviewer-phase-late` from round 2, `reviewer-final`) |
| `contract.py` | the output contract: `FINDING`, `SUPERSEDES` and `RESOLVED` lines, the verdict recomputed from them, and the accounting a later round's approval owes the phase's earlier rounds |
| `guidance.py` | each root's review skill and `review_guide` |
| `runner.py`, `harness/` | the bounded runner and the opencode harness |
| `oscillation.py` | stall detection across rounds |
| `report.py`, `configcmd.py`, `config.py` | reports, `/arl-config`, configuration layers |
| `stats.py` | `/arl-stats`: each round's model, usage and findings; display only |

## A round, step by step

1. `review prepare --session <id>` (verification and dispositions on stdin) refuses when the
   run waits on a decision only the user ends (a stall, the reviewer's call for a person), has no
   roots, or a root's baseline is unknown; a wait for the failure budget does not refuse.
   Otherwise it scans for new nested repositories, snapshots every root, and issues a **ticket**:
   review id, run id, phase, phase-list revision, and every root's snapshot tree. Dispositions
   naming findings of the previous round are checked and stored on the run, not on the ticket, so
   a review that fails keeps them for the next. On a stage's first round they have nothing to
   answer: they are checked for shape, then ignored, and the round's answer to Claude says so.
2. The bundle is the diff from each root's `approved_tree` (or `start_tree`, for the final review)
   to that snapshot.
3. The mod reads the prompt file, spawns the reviewer on the round's model (`late_model` from
   round 2 of a phase: the weak tier, `sonnet`, by default on the subagent, `model` when set to
   `""`), writes
   its answer to the answer file, and runs `review record <id> --answer <file>` (or
   `--failed transient|operational`).
4. `record` runs under the state lock and lands the result only if the ticket is still the pending
   one and every root still has the tree it was reviewed at. A stale ticket (the run was stopped,
   replanned, restarted, or a newer review began) is refused and reported, never applied.
5. The verdict is recomputed from the findings. An approval moves every root's `approved_tree`; a
   contract failure gets one repair round on the same ticket; failures count toward the budgets.
   From round 2 of a phase, an approval must leave no earlier blocking finding open.
   `contract.replay` replays the stage's rounds in order: each closes what it names exactly (a
   `RESOLVED` id, or a `SUPERSEDES` whose round and exact `file=` value match one finding of that
   round), then opens its own blocking findings. The same replay writes "Still open" and each
   finding's closure into `prior-rounds.txt`; "Still open" is never cut, and a list past
   `max_findings_bytes` makes the run wait for the user. An approval that leaves one open is a
   contract failure whose repair is told the ids (`repair-reason.txt`). Only that repair may
   approve, and only keeping every line of the block it re-emits; no accounting repair may add a
   `SUPERSEDES`. An ordinary repair, of an answer the engine could not read, may copy a
   `RESOLVED` line only from that answer, never write one of its own. `RESOLVED` lines are
   stored in the round's `round_history` entry and never change what blocks.
   A block with no `VERDICT` is `CHANGES_REQUIRED` when a finding blocks and a contract failure
   otherwise. A cited line past the end of its file adds a note to what Claude reads, nothing more.
6. What the round cost goes with it: a recorded round's `round_history` entry, or a failed one's
   `failure_history` entry, keeps its usage (summed over a repair), the harness and the model
   that answered. The mod counts a subagent's model requests from its `turn.step`s; an external
   harness reports its own. `/arl-stats` reads these and nothing decides on them.

## Two trees per root

`start_tree` is the snapshot taken when the root joined the run, or the baseline chosen for a late
root, and never changes. `approved_tree` moves on every approval. A phase review diffs
`approved_tree` to now; the final review diffs `start_tree` to now. When or how Claude commits
does not matter.

## Testing

- `tests/unit/`: pytest over the engine, against scratch repositories, with the reviewer replaced
  by stand-ins. No model is called.
- `tests/mod/`: the mod under `claude plugin test`. The flows run on a hand-made `$`
  (`tests/mod/world.ts`), because the test kit returns no agent id from `$.agent.spawn` and has no
  `process` or `fs`.
- `tests/selftest.sh`: the launcher.
- `tests/bench/`: the reviewer benchmark, which measures what a review costs and how many seeded
  bugs it finds. It calls real models, so it is run by hand (`make bench-baseline`), never by
  `make test` or CI.
- `tests/STEP0.md`: the Claude Code behaviour the design rests on, measured in live sessions.
