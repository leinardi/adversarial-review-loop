# Philosophy

## v3 was a gate; v4 is a loop

v3 treated the implementing agent as a suspect. Its review was a `PreToolUse` gate on `git commit`,
and most of its code defended that gate against the agent it reviewed: parsing command shapes,
binding sessions, retiring stale activations, sealing manifests, checking for tampering. Each
defence was a real hole closed, and each cost friction: denied commands, arming rules, states to
reconcile, prompts the person had to answer.

With current models and current Claude Code, that trade no longer pays. An agent that follows the
loop gains nothing from a gate, and an agent determined to get around it can, by means no plugin
sees. v4 drops every security guarantee and keeps what makes the reviews good.

## What v4 must get right

- **The reviewer sees the whole phase.** The evidence is the diff from the last approved snapshot to
  the current one, for every root, untracked files included. A path left out of the bundle is a
  change nobody reviewed, so the engine refuses to build an incomplete one rather than trim it.
- **No failure is an approval.** A bundle that cannot be built, a reviewer that times out, says
  nothing, breaks the output contract or gives an unknown verdict is a failure or a question for
  you, never an approval.
- **The verdict is the engine's.** The reviewer's own conclusion is advisory; an actionable finding
  at or above `block_severity` blocks whatever it said.
- **Every round is a fresh reviewer.** No reviewer carries a session from one round to the next;
  the earlier rounds reach it as a record (`prior-rounds.txt`), so it judges the current change, not
  its own earlier opinion.
- **The reviewer does not redo the implementer's work.** It never re-runs the checks; Claude ran
  them and reports their outcome as evidence.

## What v4 gives up, knowingly

- Claude can commit before a review, skip one, or ignore its findings. The loop only prompts.
- A change made through Bash outside every root is not seen.
- The launch folder's config can be edited to weaken the review. The edit is in the diff, but it
  takes effect on the review that shows it, and a weakened `ignore_globs` skips later reviews
  altogether (see [configuration.md](configuration.md)).

None of these turns a failure into an approval or hides a reviewed change from the reviewer. They
are ways for an agent to not use the loop, and the answer to an agent that will not use the loop is
not a better fence.

## Why a mod and an engine

The loop's orchestration lives in a Claude Code mod (`hooks/mod.ts`): it registers the tools Claude
calls, the reviewer subagent, the commands and the band and pane, and it runs at the points where a
session can be continued. Everything about the run's state, the snapshots, the bundle and the
verdict lives in a Python engine (`scripts/arl/`) that answers one JSON object per command, so the
part that decides is plain, typed and tested without a model.
