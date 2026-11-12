---
name: adversarial-review
description: >
  Adversarial code review of changes to adversarial-review-loop: working tree,
  staged diff, branch, commit range, or PR. Hunts for false approvals, wrong
  or incomplete review evidence, reviewer-contract parsing bugs, broken state
  handling, stale references left by the v4 rewrite and tests that are not
  isolated, then reports ranked findings. Use when the user asks to review
  changes, a diff, a PR, a branch or a commit, check work before committing,
  assess merge readiness, or poke holes in an implementation.
---

# Adversarial Review - adversarial-review-loop

**Interim, v4 rewrite in progress.** The project is being rebuilt from v3's
enforcement gate into a plan-and-go review loop (see `AGENTS.md`). v4 is not a
security boundary, so do not report that the agent under review could bypass
the loop. Report what makes a review wrong: evidence the reviewer never sees, a
failure read as an approval, a verdict parsed loosely, state that loses or
misattributes a round, and code or docs that still describe v3 behaviour that
no longer exists.

Find the exact input, repository state, reviewer response or failure point
that produces a false approval, an incomplete review, lost work or a misleading
result. Do not praise or restyle. A review with no findings is credible only
after real attempts to break the changed behaviour.

## 1. Establish the diff

Read the actual diff, never a description of it. With no scope given, review
the uncommitted work; if the tree is clean, review the branch against `main`.

| Scope | Command |
| --- | --- |
| uncommitted work | `git status --short`, then `git diff HEAD`; read every untracked file too |
| staged changes | `git diff --staged` |
| a branch or PR | `git diff main...HEAD`, or `gh pr diff <n>` |
| a commit range | `git diff <base>..<head>` |

Read the plan or phase description the change implements. Code that works but
implements a different contract is a finding.

## 2. Load project authority

Read `AGENTS.md`. Then read the changed modules with enough context to know
their callers. Runtime behaviour wins over docs, but drift in a contract the
code, the prompts or the tests rely on is a finding.

## 3. Checks

- **No failure is an approval.** A missing or malformed input, a diff or
  snapshot failure, a timeout, a non-zero reviewer exit, empty output, broken
  markers, an unknown verdict or a ceiling hit must block or escalate.
- **The evidence is complete.** The bundle covers every change since the last
  approved snapshot: staged, unstaged, deleted, renamed and untracked files,
  in every root. Binary, oversized, symlink and submodule changes are shown or
  explicitly escalated.
- **Output is parsed strictly.** Exactly one marker block, the documented
  severities, `actionable=yes|no`, one-line findings and known verdicts. The
  recomputed verdict is at least as strict as the reviewer's own.
- **State stays coherent.** Rounds, counters, reports and history are not lost
  or attributed to the wrong phase, round or root.
- **Nothing is written into the reviewed repositories' tracked content**, and
  the engine's stdout carries only its answer.
- **Deletions leave nothing dangling.** No import, test, Make target, CI job,
  pre-commit hook or doc reference may point at removed code; a check that now
  passes because its file list is empty is a false pass.
- **Tests are isolated and meaningful.** Scratch repositories, isolated
  `HOME`/XDG paths and `ARL_REVIEWER_CMD`; no real model call. A behaviour
  change needs a test that fails on the old code.
- **Commit type matches whether it ships** (`AGENTS.md` § Commits).

## 4. Verify

Run `make test` and `make check` (after `git add -N` on new files) when you
can, plus focused `pytest` runs while investigating. Reproduce or trace each
candidate finding end to end; drop it if you cannot confirm it. If a gate
cannot run, say so and mark it unverified.

## 5. Report

Rank findings worst first:

```text
<path>:<line> - <severity: critical | high | medium | low>: <one-line defect>
  Failure: <concrete input/state -> wrong result>
  Fix: <specific corrective change>
```

Then open questions, then a one-line verdict: **block**, **approve with
nits**, or **approve**. List the gates you ran and the ones you did not.

When `prompts/reviewer-phase.md` or `prompts/reviewer-final.md` is the active
instruction, its `<<<ARL-FINDINGS>>>` contract wins: emit exactly one machine
block with every finding and a consistent verdict.
