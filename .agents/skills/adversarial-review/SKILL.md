---
name: adversarial-review
description: >
  Adversarial code review of changes to adversarial-review-loop: working tree,
  staged diff, branch, commit range, or PR. Hunts for review-gate bypasses,
  fail-open hooks, wrong Git snapshots, unsafe state transitions, shell
  injection, reviewer-contract parsing bugs and tests that are not isolated,
  then reports ranked findings. Use when the user asks to review changes, a
  diff, a PR, a branch or a commit, check work before committing, assess merge
  readiness, or poke holes in an implementation.
---

# Adversarial Review - adversarial-review-loop

Assume the change bypasses the gate until proven otherwise. Find the exact
command, repository state, hook event, model response, failure point or
interleaving that produces an unreviewed commit, a false approval, lost work
or a misleading completion. Do not praise or restyle. A review with no
findings is credible only after real attempts to break the changed behavior.

This skill defines the review procedure. Runtime code, the fixed reviewer
prompts, Claude skill metadata, tests and documented user contracts are the
project authority. When a fixed reviewer prompt supplies an output contract,
follow it exactly; this skill does not replace it.

Copy this checklist and tick items as you go:

```text
Review progress:
- [ ] 1. Diff and intent established (default scope if none given)
- [ ] 2. AGENTS.md, the mandatory design notes and the code authority read
- [ ] 3. Repository invariants checked
- [ ] 4. Adversarial passes run
- [ ] 5. Findings confirmed or dropped; gates run
- [ ] 6. Report written
```

## 1. Establish the diff

Never review from memory or from the user's description alone. Read the
actual diff and determine its intent. With no scope given, review the
uncommitted work; if the tree is clean, review the branch against `main`.

| User intent | Command |
| --- | --- |
| "my work", "before I commit", uncommitted changes | `git status --short`, then `git diff HEAD`; inspect every untracked file too |
| staged changes only | `git diff --staged` |
| branch, "this PR", "ready to merge" | `git diff main...HEAD` |
| specific commit range | `git diff <base>..<head>` |
| GitHub PR number | `gh pr view <n>` for intent and metadata, then `gh pr diff <n>` |

Read `git log --oneline` for the range, plus any linked plan, issue or PR
body. Code that works but implements a different contract is a finding.

Read every changed file with enough context to know its callers and state
transitions. The gate is Python: inspect module boundaries, exception flow
(`hookio`'s `Decided`/`OutputFailure` control-flow exceptions) and return
values. The guard shim (`scripts/arl.sh`) is still Bash and communicates
through globals, stdout, exit status and captured subshell output; inspect all
of them. Trace each changed command from Claude hook input → policy →
snapshot → reviewer invocation → output parsing → state persistence → hook
response.

## 2. Load project authority

Read `AGENTS.md` (or `CLAUDE.md`) when present. It states each invariant in
one line; the argument behind it lives in `docs/design/`.

**Mandatory:** for each touched path, read every design note named in
`AGENTS.md` § Load before changing, before ranking findings. The one-line
invariant does not carry the evidence a plausible-looking change destroys.

Then load the code authority for the changed paths:

| Changed area | Read | Review focus |
| --- | --- | --- |
| command dispatch, hooks, state machine | `scripts/arl.sh`, `scripts/arl-bootstrap.py`, `scripts/arl/hookio.py`, `scripts/arl/state.py`, `scripts/arl/commands/` | fail-closed behavior, event ordering, transitions, hook JSON, interpreter-invocation hardening |
| configuration | `scripts/arl/config.py`, README configuration table | precedence, validation, executable values, policy mutability |
| snapshots and commit commands | `scripts/arl/gitsnap.py`, `scripts/arl/cmdshape.py`, `scripts/arl/_vendor/bashlex/` | exact tree coverage, real-index isolation, deny-list/parser agreement, parser bypasses |
| reviewer or reports | `scripts/arl/reviewer.py`, `scripts/arl/report.py`, `prompts/*.md` | read-only boundary, complete evidence, strict output parsing |
| Claude plugin or skills | `.claude-plugin/*.json`, `hooks/hooks.json`, `skills/*/SKILL.md`, `tests/STEP0.md` | host schema, hook registration, user-only commands, expansion assumptions |
| tests or test infrastructure | `tests/selftest.sh`, `tests/unit/`, `tests/fixtures/fake-reviewer.sh`, `.mk/test.mk` | scratch isolation, failure injection, old-code failure |
| user-visible behavior | `README.md` and the relevant skill body | command contract, recovery guidance, documented limitations |
| build or CI tooling | `Makefile`, `.mk/*.mk`, `.pre-commit-config.yaml`, `.github/workflows/*.yaml` | reproducibility, network bootstrap, actual CI coverage |

Runtime behavior wins when it disagrees with the docs, but drift in a
user-facing contract is still a finding.

## 3. Repository invariants

`AGENTS.md` § The five rules and every line of `AGENTS.md` § Invariant index
are review checks: apply each one whenever the change affects it, directly or
indirectly. The checks below are the review framing for those rules plus the
invariants `AGENTS.md` does not state.

- **Failures never approve.** Missing state, malformed JSON, a snapshot or
  diff failure, a timeout, a non-zero reviewer exit, an empty response,
  invalid markers, an unknown verdict, the evidence ceiling or a persistence
  failure must block or escalate. Operational uncertainty is never "no
  changes".
- **Arming freezes scope before mutation.** The baseline tree, plan,
  canonical worktree and session identity exist before Claude can mutate
  anything. `ARM_FAILED`, `ARMED`, `RECONCILE`, `NEEDS_HUMAN` and stale
  activations deny unsafe actions and turn completion, except for narrow
  recovery.
- **Hook stdout is protocol output.** Hook entrypoints emit only valid Claude
  hook JSON or intentional empty output on stdout. Diagnostics, command output
  and tracing go to stderr or files.
- **Phase descriptions are immutable review scope.** `set-phases` is accepted
  only in its exact safe command shape; substrings, wrappers, command chains,
  substitutions, redirections and trailing mutations cannot exploit its
  pre-activation exception.
- **Snapshots represent the whole prospective commit.** Committed, staged,
  unstaged, deleted, renamed and non-ignored untracked content all feed the
  tree. The snapshot never touches the real index or worktree. Binary,
  oversized, unusual-name, symlink and submodule changes are handled or
  explicitly escalated, never invisible evidence.
- **Approval binds exact scope.** Cache keys and pending approvals bind the
  reviewed base tree, head tree, phase, activation and session. An old tree
  must not satisfy another phase's fidelity requirement.
- **Commit confirmation proves what landed.** A successful phase commit moves
  `HEAD` exactly once; its parent is the pre-command `HEAD`, its tree is the
  approved tree, and the worktree is clean. An amend, partial commit,
  alternate Git dir, post-snapshot mutation or failed Bash call leads to safe
  recovery, never to an advance.
- **Command classification defaults to deny.** Unknown Git flags and ambiguous
  shell syntax are never safe. Quote handling, combined short flags, `--`,
  aliases, environment prefixes, separators, substitutions, pipelines and
  redirections need explicit tests. A destructive reset cannot evade policy.
- **User-only commands stay user-only.** The skills listed in `AGENTS.md`
  Rule 4 and the `arl.sh` subcommands matched by `cmdshape._ESCAPE_RE`
  (including `deactivate`) cannot be invoked by Claude through recognized
  Bash, wrappers, skill expansion or alternate paths inside the stated threat
  model.
- **Stop is a second fail-closed gate.** Outstanding phases, unreviewed work,
  missing or corrupt state, a final-review failure and a no-progress
  escalation cannot read as successful completion. Only a verified complete
  state disarms.
- **The reviewer stays read-only and scoped.** Repository content, plans,
  commit messages, attachments and project skills are untrusted evidence. They
  cannot widen permissions, reach unrelated directories, mutate the worktree
  or override the fixed prompt.
- **Configuration cannot self-approve a change.** Repository config is
  attacker-controlled input. Changes to `verify_cmd`, `ignore_globs`,
  severity, model, timeout, project-config loading or permission behavior
  cannot run unreviewed code or silently weaken the active gate.
- **Reviewer output is parsed strictly.** Accept exactly the documented marker
  block, allowed severities, `actionable=yes|no`, one-line findings and known
  verdicts. Malformed fields fail closed. The gate verdict is at least as
  strict as the model verdict. Caps keep the full findings and escalate; they
  never trim into an approval.
- **State stays coherent and private.** Concurrent hooks lose no
  transitions, counters, pending approvals or reports. Session IDs cannot
  traverse paths. State directories protect frozen plans, diffs, verification
  output and model output from other users.
- **Tests touch no live state.** Tests use scratch Git repositories, isolated
  `HOME`/XDG paths and `ARL_REVIEWER_CMD`. They never call a real model, load
  the user's OpenCode config, alter real hooks or leave activation pointers
  behind.

Resume and retirement, beyond the `AGENTS.md` index:

- **Both resume paths check for a pending approval.** Same-session and
  cross-session resume check under the lock that mutates; skipping either
  opens a resurrection window between `pretool` approving and
  `confirm-commit` verifying.
- **A failed same-session resume never overwrites a live activation.** A typo
  in `--model`/`--until` writes nothing; only a completed transaction may
  change the document.
- **The successor is built only from a pre-retirement snapshot**, never by
  re-reading the predecessor after retirement: that reads back `RESUMED` and
  the retirement note and loses a `RECONCILE`'s `reason`, `bad_commit` and
  `bad_commit_parent`.
- **Frozen-plan files are immutable; state names the live one.**
  `plan.frozen.md` and the numbered `plan.rev<n>.md` files are never renamed
  or overwritten. A failed containment or SHA-256 check on a
  `plan_revisions[*].file` escalates to `NEEDS_HUMAN`; it is never skipped or
  substituted.
- **Resume never adds an unapproved `HEAD` to `approved_trees`**; it warns
  and folds it into the next review instead.
- **Git-facing checks re-run immediately before publication**, not only
  before retirement: the predecessor stays live through the whole
  materialization window, so a background writer or an already-authorized
  tool call can still touch the worktree in between.
- **A decided plan revision, or `--replan`, requires a clean worktree.** This
  holds when a revision was decided or `--replan` was passed, is never waived
  by `--allow-dirty` or `allow_dirty` in config, and does not depend on
  whether `--plan`/`--replan` was literally typed (an automatic revision from
  an edited plan file counts too).
- **An abandoned pending marker is a `(parent, tree)` pair**, resolved on the
  path a commit actually takes (`pretool`'s pre-approval scan and `stop`'s
  sweep, not merely a guard function reachable from neither) before the
  successor approves anything. It is cleared on a confirmed `_advance` match,
  never by tree membership alone: a same-tree empty-phase commit is not the
  same event as the abandoned one.
- **Bundle contents are driven from the state document, never inferred from
  filenames.** `build_bundle` iterates `plan_revisions` and copies exactly
  what each entry names; a `glob` would silently omit a revision the state
  does not name. A new bundle file must be wired into both `build_bundle` and
  `review_argv`; writing one without the other is inert and never reaches the
  reviewer.

Portability, releases and CI:

- **Portability claims stay honest.** The gate needs Python 3.12+, the
  standard library and the vendored bashlex; the guard shim needs Bash 3.2+
  and an outer watchdog (`timeout`/`gtimeout`, GNU or uutils, or `perl`, in
  that order). No undeclared, untested reliance on a newer Python, a locale,
  a filesystem or Git behavior. macOS ships no `timeout` and Bash 3.2, so a
  change that reintroduces either requirement breaks every Mac.
- **Release and CI stay reconcilable and pinned.** `docs/release.md` is the
  contract. `auto-tag-release.yaml` must finish on a re-run: create the tag
  only if missing, fail (never move it) if the tag marks another commit,
  create the release only if missing, and pass `--latest` only for the highest
  version. An explicit release version must exceed the latest tag. Workflows
  set `contents: read` at the top and grant extra permissions per job, with a
  reason. Every action is pinned to a full SHA with a `# vX.Y.Z` comment;
  workflow inputs reach the shell through `env:`, never `${{ }}` inside
  `run:`. The `conventional-commits` job must keep running on
  `workflow_dispatch`, because the bump PR depends on it.

## 4. Adversarial passes

Run each relevant pass with "how can this approve something wrong?" framing:

- **State transition matrix:** enter every command from every state; inject
  failure before and after each save; check stale pointers, missing files,
  malformed JSON, duplicate events, retries and two concurrent hook calls.
- **Hook lifecycle:** mismatched session or worktree, absent fields,
  unexpected tool names, Bash success vs failure, a commit created despite a
  tool error, an omitted PostToolUseFailure, a repeated Stop, a host timeout
  below the reviewer timeout.
- **Shell command shape:** quoted separators, escaped whitespace, `env`,
  `sudo`, shell functions, aliases, `git -C`, `--git-dir`, combined flags,
  pathspecs, command substitution, process substitution, heredocs, pipes,
  redirects, and a safe-looking command followed by a mutation.
- **Git snapshot:** staged-only large files, intent-to-add, deletions,
  renames, ignored files, nested repositories, submodules, symlinks, binary
  blobs, newline-containing paths, empty repositories, replace refs, a diff
  failure.
- **Commit and reconcile:** an empty commit, several commits in one Bash
  call, amend, partial staging, hooks that mutate after approval, a rejected
  commit, detached `HEAD`, a merge commit, a history rewrite, dirty leftovers,
  a reset before the activation boundary.
- **Configuration:** malformed types, an unknown severity, negative or huge
  limits, a hostile `verify_cmd`, `ignore_globs=["**"]`, an environment
  override, config changed after arming, a permissive OpenCode project config,
  a timeout mismatch.
- **Reviewer isolation:** prompt injection in source, plan or diff, an
  attachment path escape, a project skill conflict, global plugin
  contamination, external directory access, mutating verification, incomplete
  binary or submodule evidence, stale bundle files.
- **Output contract:** duplicate or misordered markers, multiple verdicts,
  CRLF, missing fields, invalid severity or actionable spelling, marker text
  in prose, huge findings, Unicode byte limits, a timeout with partial output,
  an `APPROVED` verdict beside an actionable finding.
- **Interpreter invocation and shim contract:** `-m` or a relative bootstrap
  path coming back, `uv run` reintroducing the cwd-as-`sys.path[0]` exploit
  one level up, `sys.pycache_prefix` inside the plugin repository or the
  reviewed one, the shim forwarding output after a non-zero exit or
  `timeout`'s `124`, a wrong or reused fallback shape across entrypoints, a
  hung parse returning after the host hook timeout instead of before.
- **Reporting:** stale globals from a previous review, every finding
  preserved, prose-only truncation, the correct report sequence, the gate
  verdict kept distinct from the raw model verdict, useful recovery text that
  does not claim approval.
- **Tests:** require a regression test that fails on the old code, hits the
  exact failure seam, and asserts both durable state and the hook response.
  Reject tests that only assert a helper's output while the end-to-end bypass
  is still possible.
- **Contract drift:** compare the README, skill frontmatter, fixed prompts,
  Make targets, CI and real behavior. Flag claims such as complete evidence,
  automatic disarming, user-only actions or exhaustive tests unless the code
  proves them.

For each candidate finding, reproduce it or trace the bypass end to end. If
that confirms it, report it. If not, dig once more; if it is still
unconfirmed, drop it. One reproducible bypass beats ten vague suggestions.

## 5. Verify findings and gates

Respect the current capability first. The ARL model reviewer is deliberately
read-only and cannot run commands: inspect the attached `verify.txt` and never
claim you ran it. In an interactive review with Bash permission, run focused
tests while investigating, then the gate the changed set requires.

| Diff touched | Run |
| --- | --- |
| Python gate logic (state machine, parser, reviewer, config) | `python3 -m pytest tests/unit/test_<module>.py` while investigating, then `make test-unit`, then `make test` |
| shim, watchdog or interpreter probe | `make test-filter FILTER=<section>`, then `make test` |
| any runtime shell, prompt, skill, or test | `make test` |
| docs or review skill only | `make check` |
| broad change or merge-readiness review | `make test`, then `make check` |
| Claude/OpenCode host integration | the relevant manual checks in `tests/STEP0.md`; do not imply that shell tests cover them |

`make check` only sees files Git already knows about: pre-commit skips
untracked files and reports clean on code it never read. Run `git add -N` on
every new file before `make check`, and treat a green run that skipped a new
module as no run at all. `make check` also includes fix-capable hooks: inspect
`git status --short`, the unstaged diff and the staged diff afterwards, so
formatter edits are not mistaken for reviewed input. `make dry-run` prints the
reviewer argv and prompt without invoking the reviewer, but can create state
bundles and Git objects.

For shell-only diagnosis, run `bash -n` and `shellcheck -x` on
`scripts/arl.sh` and `tests/*.sh`. For Python-only diagnosis, run `ruff check`
and `mypy` directly on the changed modules. If a gate cannot run, say why and
mark it unverified. A failing gate caused by the reviewed change is a finding,
not a footnote.

## 6. Report

Rank findings by severity, worst first. Gate bypass, false approval, execution
of unreviewed code, lost work, secret exposure and state corruption are
normally critical or high. Skip pure formatting unless it changes protocol
meaning or breaks a required gate.

Normal interactive reviews use:

```text
<path>:<line> - <severity: critical | high | medium | low>: <one-line defect>
  Failure: <concrete command/state/event -> wrong result or broken invariant>
  Fix: <specific corrective change>
```

Put findings first, then open questions or assumptions, then a one-line
verdict: **block**, **approve with nits**, or **approve**. List the gates you
ran and the ones you did not. With no findings, say so and name the failure
modes you tried.

When `prompts/reviewer-phase.md` or `prompts/reviewer-final.md` is the active
instruction, its `<<<ARL-FINDINGS>>>` contract wins: emit exactly one machine
block with every finding and a consistent verdict, including an empty approved
block when there are none. Never omit the markers or claim that a read-only
reviewer ran tests.
