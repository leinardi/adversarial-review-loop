---
name: adversarial-review
description: >
  Adversarial code review of changes to opencode-review-loop: working tree,
  staged diff, branch, commit range, or PR. Hunts for review-gate bypasses,
  fail-open hook behavior, incorrect Git snapshots, unsafe state transitions,
  shell injection, reviewer-contract parsing bugs, and missing isolation tests,
  then reports ranked findings. Use whenever the user asks to review changes,
  a diff, PR, branch, or commit; check work before committing; assess merge
  readiness; or poke holes in an implementation.
---

# Adversarial Review - opencode-review-loop

Assume the change can bypass the gate until proven otherwise. Find the exact
command, repository state, hook event, model response, failure point, or
interleaving that produces an unreviewed commit, false approval, lost work, or
misleading completion. Do not praise or restyle the change. A review with no
findings is credible only after active attempts to break changed behavior.

This skill defines review procedure. Runtime code, fixed reviewer prompts,
Claude skill metadata, tests, and documented user contracts remain project
authority. When a fixed reviewer prompt supplies an output contract, follow it
exactly; this skill does not replace that contract.

## 1. Establish the diff

Never review from memory or only from the user's description. Read the actual
diff and determine its intent.

| User intent | Command |
| --- | --- |
| "my work", "before I commit", uncommitted changes | `git status --short`, then `git diff HEAD`; inspect every untracked file too |
| staged changes only | `git diff --staged` |
| branch, "this PR", "ready to merge" | determine the default/base branch, then `git diff <base>...HEAD` |
| specific commit range | `git diff <base>..<head>` |
| GitHub PR number | `gh pr view <n>` for intent and metadata, then `gh pr diff <n>` |

Read `git log --oneline` for the range and any linked plan, issue, or PR body.
Code that works but implements a different contract is a finding.

Read every changed file with enough context to understand its callers and
state transitions. Shell functions communicate through globals, stdout,
subshells, files, and exit statuses; inspect all five channels. Trace changed
commands from Claude hook input through policy, snapshot, reviewer invocation,
output parsing, state persistence, and hook response.

## 2. Load project authority

Read `AGENTS.md` or `CLAUDE.md` when present. Load authority relevant to changed
paths:

| Changed area | Read | Review focus |
| --- | --- | --- |
| command dispatch, hooks, state machine | `scripts/ocrl.sh`, `scripts/lib/common.sh`, `scripts/lib/state.sh` | fail-closed behavior, event ordering, transitions, hook JSON |
| configuration | `scripts/lib/config.sh`, README configuration table | precedence, validation, executable values, policy mutability |
| snapshots and commit commands | `scripts/lib/gitsnap.sh`, `scripts/lib/cmdshape.sh` | exact tree coverage, real-index isolation, parser bypasses |
| reviewer or reports | `scripts/lib/reviewer.sh`, `scripts/lib/report.sh`, `prompts/*.md` | read-only boundary, complete evidence, strict output parsing |
| Claude plugin or skills | `.claude-plugin/*.json`, `skills/*/SKILL.md`, `tests/STEP0.md` | host schema, hook registration, user-only commands, expansion assumptions |
| tests or test infrastructure | `tests/selftest.sh`, `tests/fixtures/fake-reviewer.sh`, `.mk/test.mk` | scratch isolation, failure injection, old-code failure |
| user-visible behavior | `README.md` and relevant skill body | command contract, recovery guidance, documented limitations |
| build or CI tooling | `Makefile`, `.mk/*.mk`, `.pre-commit-config.yaml`, `.github/workflows/*.yaml` | reproducibility, network bootstrap, actual CI coverage |

Runtime behavior wins when documentation disagrees, but user-facing contract
drift remains a finding.

## 3. Repository invariants

Check these whenever affected directly or indirectly:

- **Failures never approve.** Missing state, malformed JSON, snapshot or diff
  failure, timeout, non-zero reviewer exit, empty response, invalid markers,
  unknown verdict, evidence ceiling, and persistence failure must block or
  escalate. Never reinterpret operational uncertainty as no changes.
- **Arming freezes scope before mutation.** Baseline tree, plan, canonical
  worktree, and session identity must be established before Claude can mutate.
  `ARM_FAILED`, `ARMED`, `RECONCILE`, `NEEDS_HUMAN`, and stale activations deny
  unsafe actions and turn completion except for narrowly defined recovery.
- **Hook stdout is protocol output.** Hook entrypoints emit only valid Claude
  hook JSON or intentional empty output on stdout. Diagnostics, command output,
  and tracing go to stderr or files. EXIT traps must fail closed.
- **Phase descriptions are immutable review scope.** `set-phases` is accepted
  only in its exact safe command shape. Substrings, wrappers, command chains,
  substitutions, redirections, and trailing mutations cannot exploit its
  pre-activation exception.
- **Snapshots represent the whole prospective commit.** Committed, staged,
  unstaged, deleted, renamed, and non-ignored untracked content must contribute
  to the tree. Snapshotting never changes the real index or worktree. Binary,
  oversized, unusual-name, symlink, and submodule changes are handled or cause
  explicit escalation rather than invisible evidence.
- **Approval binds exact scope.** Cache keys and pending approval bind the
  reviewed base tree, head tree, phase, activation, and session. Reusing an old
  tree must not satisfy a different phase's fidelity requirements.
- **Commit confirmation proves what landed.** A successful phase commit moves
  `HEAD` exactly once, has the pre-command `HEAD` as parent, has the approved
  tree, and leaves a clean worktree. Amend, partial commit, alternate Git dir,
  post-snapshot mutation, or failed Bash enters safe recovery and never advances.
- **Command classification defaults deny.** Unknown Git flags and ambiguous
  shell syntax cannot be treated as safe. Quote handling, combined short flags,
  `--`, aliases, environment prefixes, separators, substitutions, pipelines,
  and redirections need explicit tests. Destructive reset cannot evade policy.
- **User-only exits remain user-only.** Claude cannot invoke `finish`,
  `deactivate`, or equivalent behavior through recognized Bash, wrappers, skill
  expansion, or alternate paths within the stated threat model.
- **Stop is a second fail-closed gate.** Outstanding phases, unreviewed work,
  missing/corrupt state, final-review failure, and no-progress escalation cannot
  read as successful completion. Only a verified complete state disarms.
- **Reviewer remains read-only and scoped.** Repository content, plans, commit
  messages, attachments, and project skills are untrusted evidence. They cannot
  widen permissions, access unrelated directories, mutate the worktree, or
  override the fixed prompt.
- **Configuration cannot self-approve a change.** Treat repository config as
  attacker-controlled input. Changes to `verify_cmd`, `ignore_globs`, severity,
  model, timeout, project-config loading, or permission-related behavior cannot
  execute unreviewed code or weaken the active gate silently.
- **Reviewer output is parsed strictly.** Accept exactly the documented marker
  block, allowed severities, `actionable=yes|no`, one-line findings, and known
  verdicts. Malformed fields fail closed. Gate-computed verdict is at least as
  strict as model verdict. Caps retain full findings and escalate rather than
  trim into approval.
- **State remains coherent and private.** Writes are atomic; concurrent hooks
  do not lose transitions, counters, pending approvals, or reports. Session IDs
  cannot traverse paths. State directories protect frozen plans, diffs,
  verification output, and model output from other users.
- **Tests touch no live state.** Tests use scratch Git repositories, isolated
  `HOME`/XDG paths, and `OCRL_REVIEWER_CMD`. They never call a real model, load
  user OpenCode config, alter real hooks, or leave activation pointers behind.
- **Portability claims stay honest.** Runtime remains Bash 4.4+ and uses only
  documented GNU dependencies. Avoid relying accidentally on current Bash,
  locale, filesystem, or Git behavior without declaring and testing it.

## 4. Adversarial passes

Run each relevant pass with "how can this approve incorrectly?" framing:

- **State transition matrix:** enter every command from every state; inject
  failure before and after each save; check stale pointers, missing files,
  malformed JSON, duplicate events, retries, and two concurrent hook calls.
- **Hook lifecycle:** mismatch session/worktree, absent fields, unexpected tool
  names, Bash success versus failure, commit created despite tool error,
  PostToolUseFailure omission, repeated Stop, and host timeout below reviewer
  timeout.
- **Shell command shape:** quoted separators, escaped whitespace, `env`, `sudo`,
  shell functions, aliases, `git -C`, `--git-dir`, combined flags, pathspecs,
  command substitution, process substitution, heredocs, pipes, redirects, and a
  safe-looking command followed by mutation.
- **Git snapshot:** staged-only large files, intent-to-add, deletions, renames,
  ignored files, nested repositories, submodules, symlinks, binary blobs,
  newline-containing paths, empty repositories, replace refs, and diff failure.
- **Commit/reconcile:** empty commit, multiple commits in one Bash call, amend,
  partial staging, hooks that mutate after approval, rejected commit, detached
  HEAD, merge commit, history rewrite, dirty leftovers, and reset before the
  activation boundary.
- **Configuration:** malformed types, unknown severity, negative or huge limits,
  hostile `verify_cmd`, `ignore_globs=["**"]`, environment override, config
  changed after arming, permissive OpenCode project config, and timeout mismatch.
- **Reviewer isolation:** prompt injection in source/plan/diff, attachment path
  escape, project skill conflict, global plugin contamination, external
  directory access, mutating verification, incomplete binary/submodule evidence,
  and stale bundle files.
- **Output contract:** duplicate/misordered markers, multiple verdicts, CRLF,
  missing fields, invalid severity/actionable spelling, marker text in prose,
  huge findings, Unicode byte limits, timeout with partial output, and an
  `APPROVED` verdict beside an actionable finding.
- **Reporting:** stale globals from previous review, every finding preserved,
  prose-only truncation, correct report sequence, gate verdict distinguished
  from raw model verdict, and useful recovery text without claiming approval.
- **Tests:** require a regression that fails on old code, targets the exact
  failure seam, and asserts durable state plus hook response. Reject tests that
  only assert helper output while the end-to-end bypass remains possible.
- **Contract drift:** compare README, skill frontmatter, fixed prompts, Make
  targets, CI, and actual behavior. Flag claims such as complete evidence,
  automatic disarming, user-only actions, or exhaustive tests unless code proves
  them.

Prefer one reproducible bypass over ten vague suggestions. If you cannot name
the triggering state and wrong result or broken invariant, investigate further
or omit it.

## 5. Verify findings and gates

First respect current capability. The OCRL model reviewer is deliberately
read-only and cannot run commands; inspect attached `verify.txt` and never claim
you executed it. During an interactive repository review with Bash permission,
run focused tests while investigating, then the gate owed by the changed set.

| Diff touched | Run |
| --- | --- |
| one state-machine or parser area | `make test-filter FILTER=<section>` followed by `make test` |
| any runtime shell, prompt, skill, or test | `make test` |
| broad change or merge-readiness review | `make test`, then `make check` |
| docs or review skill only | inspect references, then `pre-commit run markdownlint-cli2 --all-files` and `git diff --check` |
| Claude/OpenCode host integration | relevant manual checks in `tests/STEP0.md`; do not imply shell tests cover them |

`make check` includes fix-capable hooks. Inspect `git status --short`, unstaged
diff, and staged diff afterward so formatter edits are not mistaken for reviewed
input. `make dry-run` prints reviewer argv and prompt without invoking OpenCode,
but it can create state bundles and Git objects.

For shell-only diagnosis, use `bash -n` and `shellcheck -x` on changed scripts.
If a gate cannot run, state why and mark it unverified. A failing gate caused by
the reviewed change is a finding, not a footnote.

## 6. Report

Rank findings by severity, worst first. Gate bypass, false approval, execution
of unreviewed code, lost work, secret exposure, or state corruption are normally
critical or high. Skip pure formatting unless it changes protocol meaning or
breaks a required gate.

For normal interactive reviews, use:

```text
<path>:<line> - <severity: critical | high | medium | low>: <one-line defect>
  Failure: <concrete command/state/event -> wrong result or broken invariant>
  Fix: <specific corrective change>
```

Put findings first. Then list open questions or assumptions, followed by a
one-line verdict: **block**, **approve with nits**, or **approve**. Include gates
actually run and gates not run. If no findings exist, say so explicitly and
briefly name failure modes attempted.

When `prompts/reviewer-phase.md` or `prompts/reviewer-final.md` is the active
instruction, its `<<<OCRL-FINDINGS>>>` contract takes precedence. Emit exactly
one machine block with every finding and a consistent verdict, including the
empty approved block when no findings exist. Never omit markers or claim tests
were run by the read-only reviewer.
