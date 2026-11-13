# Architecture

## The shape of it

```text
hooks/mod.ts (Claude Code mod: orchestrator, UI)    scripts/arl/ (engine: Python CLI, JSON in and out)
  tools     set_phases, add_root, review, clarify     run state in <launch>/.arl/state.json, one lock
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
  a file is written to, unless the session's rules deny the write, and takes it back
  (`root-remove --undo`) when the write is refused or fails and the folder is unchanged.
- **Continuation.** After a main-loop turn that ended with an answer, the mod asks `next-action`;
  on `continue` it submits the engine's prompt (after compacting, when `auto_compact` is due).

## The engine

| Module | What |
| --- | --- |
| `cli.py`, `commands.py` | argv in, one JSON answer out; the command table |
| `state.py` | the run document, its transitions, `next_action` and its continuation token |
| `workspace.py`, `gitsnap.py` | roots, nested-repository discovery, late baselines; snapshots through a throwaway `GIT_INDEX_FILE` (git roots) or a shadow git dir (plain folders) |
| `review.py` | `review prepare` / `record` / `run-external` / `abandon`, repair, failure budgets, stalls, `clarify` |
| `bundle.py` | the bundle: diff chunks per root with alias-prefixed paths, `range.txt`, `prior-rounds.txt`, `verification.txt`, the composed prompt |
| `contract.py` | the output contract: `FINDING` and `SUPERSEDES` lines, the verdict recomputed from them |
| `guidance.py` | each root's review skill and `review_guide` |
| `runner.py`, `harness/` | the bounded runner and the opencode harness |
| `oscillation.py` | stall detection across rounds |
| `report.py`, `configcmd.py`, `config.py` | reports, `/arl-config`, configuration layers |

## A round, step by step

1. `review prepare --session <id>` (verification on stdin) refuses when the run waits on a
   decision only the user ends (a stall, the reviewer's call for a person), has no roots, or a
   root's baseline is unknown; a wait for the failure budget does not refuse. Otherwise it scans
   for new nested repositories, snapshots every root, and issues a **ticket**: review id, run id,
   phase, phase-list revision, and every root's snapshot tree.
2. The bundle is the diff from each root's `approved_tree` (or `start_tree`, for the final review)
   to that snapshot.
3. The mod reads the prompt file, spawns the reviewer, writes its answer to the answer file, and
   runs `review record <id> --answer <file>` (or `--failed transient|operational`).
4. `record` runs under the state lock and lands the result only if the ticket is still the pending
   one and every root still has the tree it was reviewed at. A stale ticket (the run was stopped,
   replanned, restarted, or a newer review began) is refused and reported, never applied.
5. The verdict is recomputed from the findings. An approval moves every root's `approved_tree`; a
   contract failure gets one repair round on the same ticket; failures count toward the budgets.

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
- `tests/STEP0.md`: the Claude Code behaviour the design rests on, measured in live sessions.
