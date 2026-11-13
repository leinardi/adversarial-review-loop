# FAQ

## Getting started

### Do I need a plan file?

Yes: `/arl-implement` takes one. Any Markdown works; write the phases in order, and name the
repositories it touches by path, so existing git repositories become roots at the start. A plain
folder joins with `--root`, the `roots` setting, or when Claude writes into it.

### Do I need OpenCode?

No. The default reviewer is a Claude Code subagent. Set `harness` to `opencode` to have the engine
run OpenCode instead (2.2.7 or newer). Each review runs a private OpenCode server
(`--standalone`) as the `build` agent, with read-only permissions placed after the user's and the
repository's own. The repository's OpenCode plugins still load unless `disable_project_config` is
set.

### Does it write anything into my repository?

Not into its tracked content. The run lives in `<launch folder>/.arl/`, which a git root ignores
through `.git/info/exclude`. Snapshots use a throwaway git index. The only exception is one you ask
for: `/arl-config --repo` writes `.adversarial-review-loop.json` into the launch folder.

### Does it work with Claude Code's sandbox?

Yes, with no settings exception: the mod and the engine run outside the sandbox.

### Does it work headless (`claude -p`)?

Yes. `claude -p "/arl-implement plan.md"` runs the plan. With nobody to ask, a decision the loop
needs from you becomes an instruction to Claude to stop and say what happened, and `auto_compact`
does nothing (Claude Code does not compact headless sessions).

## During a run

### Claude says a folder's baseline is unknown. What does that mean?

The folder joined the run after it already had changes, so nobody knows which of them are this
run's. You are asked once: whether everything in it is new, or its current content counts as
already reviewed, and for a git repository also the changes since a commit the engine proposes, when
it finds one (where the repository stood when the run started). No review runs until you choose.

### The loop keeps raising the same findings

After `stall_rounds` rounds of the same blocking findings, the loop asks you. Accept the phase as it
stands, keep going (the stalled rounds no longer count), or stop. Claude can also ask the reviewer
about a finding it believes is wrong with `clarify`; the reply may retract it for the next round.

### A file is too large to review

The loop asks you rather than review a cut diff. Remove the file, or ignore it in its root's
`.gitignore`, then keep going.

### A folder became a root by mistake

`/arl-remove-root <alias>`. Nothing in it is reviewed from then on, later reviews are told it was
removed, and its alias is never given to another root. The run's last root cannot be removed, and
neither can a repository inside another root (give it a baseline instead).

### Can I approve a phase myself?

`/arl-accept` approves the current phase as it stands, with no review. The approval is recorded as
yours, and the final review, if any, still sees the whole run.

### What does the final review cover?

Every change of the run: each root from where it joined the run to now, roots added mid-run
included. Turn it on for every run with `final_review`, or for one run with `/arl-finish`.

## Picking up where you left off

### I closed Claude mid-run. Now what?

The run is in `.arl/`. Start Claude in the same folder; the band shows the run, and a review that
was pending is reported. Run `/arl-resume`: it abandons a review nothing is running any more and
prompts Claude to go on. Approvals and baselines are kept.

### A second session in the same folder says another session's review is pending

The plugin cannot tell a live session from a dead one, so a starting session only reports it. If the
other session is gone, `/arl-resume` abandons the review and continues here.

### I want to start over

`/arl-implement --new plan.md` archives the current run under `.arl/runs/<id>/` and starts afresh.
A stopped or complete run is archived without `--new`.

## Cost

### What does a review cost?

One reviewer run per round, plus at most one repair when an answer breaks the contract. Each report's
`- cost:` line gives the tokens and the time; `/arl-report` shows them. To spend less: a cheaper
`model` or a lower `effort`, `ignore_globs` for paths that need no review, and smaller phases.
