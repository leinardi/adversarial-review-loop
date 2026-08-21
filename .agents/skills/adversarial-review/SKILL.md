---
name: adversarial-review
description: >
  Adversarial code review of opencode-review-loop changes: working tree,
  staged diff, branch, commit range, or PR. Hunt review-gate bypass,
  fail-open hook, wrong Git snapshot, unsafe state transition,
  shell injection, reviewer-contract parse bug, missing isolation test.
  Report ranked findings. Use when user ask review changes, diff, PR,
  branch, commit; check work before commit; assess merge readiness;
  poke holes in implementation.
---

# Adversarial Review - opencode-review-loop

Assume change bypass gate until proven otherwise. Find exact command, repo
state, hook event, model response, failure point, or interleaving that make
unreviewed commit, false approval, lost work, or misleading completion. No
praise, no restyle. Zero findings credible only after real attempts to break
changed behavior.

This skill = review procedure. Runtime code, fixed reviewer prompts, Claude
skill metadata, tests, documented user contracts = project authority. Fixed
reviewer prompt supply output contract → follow exact; this skill no replace
that contract.

## 1. Establish the diff

Never review from memory or user description alone. Read real diff, find intent.

| User intent | Command |
| --- | --- |
| "my work", "before I commit", uncommitted changes | `git status --short`, then `git diff HEAD`; inspect every untracked file too |
| staged changes only | `git diff --staged` |
| branch, "this PR", "ready to merge" | determine the default/base branch, then `git diff <base>...HEAD` |
| specific commit range | `git diff <base>..<head>` |
| GitHub PR number | `gh pr view <n>` for intent and metadata, then `gh pr diff <n>` |

Read `git log --oneline` for range plus any linked plan, issue, PR body. Code
that work but implement different contract = finding.

Read every changed file with enough context to know callers and state
transitions. Gate is Python; inspect module boundaries, exception flow
(`hookio`'s `Decided`/`OutputFailure` control-flow exceptions), return values.
Guard shim (`scripts/ocrl.sh`) still Bash, talk through globals, stdout, exit
status, captured subshell output — inspect all there. Trace changed commands
from Claude hook input → policy → snapshot → reviewer invocation → output
parsing → state persistence → hook response.

## 2. Load project authority

Read `AGENTS.md` or `CLAUDE.md` when present. Load authority for changed paths:

| Changed area | Read | Review focus |
| --- | --- | --- |
| command dispatch, hooks, state machine | `scripts/ocrl.sh`, `scripts/ocrl-bootstrap.py`, `scripts/ocrl/hookio.py`, `scripts/ocrl/state.py`, `scripts/ocrl/commands/` | fail-closed behavior, event ordering, transitions, hook JSON, interpreter-invocation hardening |
| configuration | `scripts/ocrl/config.py`, README configuration table | precedence, validation, executable values, policy mutability |
| snapshots and commit commands | `scripts/ocrl/gitsnap.py`, `scripts/ocrl/cmdshape.py`, `scripts/ocrl/_vendor/bashlex/` | exact tree coverage, real-index isolation, deny-list/parser agreement, parser bypasses |
| reviewer or reports | `scripts/ocrl/reviewer.py`, `scripts/ocrl/report.py`, `prompts/*.md` | read-only boundary, complete evidence, strict output parsing |
| Claude plugin or skills | `.claude-plugin/*.json`, `skills/*/SKILL.md`, `tests/STEP0.md` | host schema, hook registration, user-only commands, expansion assumptions |
| tests or test infrastructure | `tests/selftest.sh`, `tests/unit/`, `tests/fixtures/fake-reviewer.sh`, `.mk/test.mk` | scratch isolation, failure injection, old-code failure |
| user-visible behavior | `README.md` and relevant skill body | command contract, recovery guidance, documented limitations |
| build or CI tooling | `Makefile`, `.mk/*.mk`, `.pre-commit-config.yaml`, `.github/workflows/*.yaml` | reproducibility, network bootstrap, actual CI coverage |

Runtime behavior win when docs disagree, but user-facing contract drift still
finding.

## 3. Repository invariants

Check whenever affected direct or indirect:

- **Failures never approve.** Missing state, malformed JSON, snapshot or diff
  failure, timeout, non-zero reviewer exit, empty response, invalid markers,
  unknown verdict, evidence ceiling, persistence failure → must block or
  escalate. Never read operational uncertainty as no changes.
- **Arming freezes scope before mutation.** Baseline tree, plan, canonical
  worktree, session identity must exist before Claude can mutate. `ARM_FAILED`,
  `ARMED`, `RECONCILE`, `NEEDS_HUMAN`, stale activations deny unsafe actions and
  turn completion except narrow recovery.
- **Hook stdout is protocol output.** Hook entrypoints emit only valid Claude
  hook JSON or intentional empty output on stdout. Diagnostics, command output,
  tracing → stderr or files. EXIT traps fail closed.
- **Phase descriptions are immutable review scope.** `set-phases` accepted only
  in exact safe command shape. Substrings, wrappers, command chains,
  substitutions, redirections, trailing mutations cannot exploit its
  pre-activation exception.
- **Snapshots represent the whole prospective commit.** Committed, staged,
  unstaged, deleted, renamed, non-ignored untracked content all feed tree.
  Snapshot never touch real index or worktree. Binary, oversized, unusual-name,
  symlink, submodule changes handled or explicit escalation — never invisible
  evidence.
- **Approval binds exact scope.** Cache keys and pending approval bind reviewed
  base tree, head tree, phase, activation, session. Old tree must not satisfy
  another phase's fidelity requirement.
- **Commit confirmation proves what landed.** Successful phase commit move
  `HEAD` exactly once, parent = pre-command `HEAD`, tree = approved tree,
  worktree clean. Amend, partial commit, alternate Git dir, post-snapshot
  mutation, failed Bash → safe recovery, never advance.
- **Command classification defaults deny.** Unknown Git flags and ambiguous
  shell syntax never safe. Quote handling, combined short flags, `--`, aliases,
  environment prefixes, separators, substitutions, pipelines, redirections need
  explicit tests. Destructive reset cannot evade policy.
- **User-only exits remain user-only.** Claude cannot invoke `finish`,
  `deactivate`, or equivalent through recognized Bash, wrappers, skill
  expansion, or alternate paths inside stated threat model.
- **Stop is a second fail-closed gate.** Outstanding phases, unreviewed work,
  missing/corrupt state, final-review failure, no-progress escalation cannot
  read as successful completion. Only verified complete state disarms.
- **Reviewer remains read-only and scoped.** Repo content, plans, commit
  messages, attachments, project skills = untrusted evidence. Cannot widen
  permissions, reach unrelated directories, mutate worktree, or override fixed
  prompt.
- **Configuration cannot self-approve a change.** Repo config = attacker-
  controlled input. Changes to `verify_cmd`, `ignore_globs`, severity, model,
  timeout, project-config loading, or permission behavior cannot run unreviewed
  code or weaken active gate silently.
- **Reviewer output is parsed strictly.** Accept exactly documented marker
  block, allowed severities, `actionable=yes|no`, one-line findings, known
  verdicts. Malformed fields fail closed. Gate verdict at least as strict as
  model verdict. Caps keep full findings and escalate, never trim into approval.
- **State remains coherent and private.** Writes atomic; concurrent hooks lose
  no transitions, counters, pending approvals, reports. Session IDs cannot
  traverse paths. State dirs protect frozen plans, diffs, verification output,
  model output from other users.
- **Tests touch no live state.** Tests use scratch Git repos, isolated
  `HOME`/XDG paths, `OCRL_REVIEWER_CMD`. Never call real model, load user
  OpenCode config, alter real hooks, or leave activation pointers behind.
- **Portability claims stay honest.** Gate need Python 3.12+, stdlib plus
  vendored bashlex; guard shim need Bash 4.4+ and `timeout` binary (GNU or
  uutils). No accidental reliance on current Python, locale, filesystem, or Git
  behavior without declaring and testing it.
- **Interpreter invocation stays hardened.** Gate invoked only as
  `python3 -I <absolute bootstrap path>`, never `-m`, never relative path,
  never through `uv run`. `sys.pycache_prefix` must resolve outside plugin
  repo, reviewed repo, and `cwd` before any import, else bytecode writing
  must disable itself, not fall back beside source.
- **The shim never forwards a partial response.** `scripts/ocrl.sh` capture
  stdout, forward only on exit `0`; any other exit (crash, missing
  interpreter, `timeout`'s `124`) discard everything captured and emit that
  event's own fallback. Fallback shape per-event, never reused:
  `permissionDecision: deny` for `pretool`, `{"decision":"block"}` for
  `gate-stop`, `additionalContext` for `confirm-commit`, exactly zero bytes
  for `posttool-failure`. Hung bashlex parse must still deny before host hook
  timeout, via both in-process `signal.setitimer` deadline and shim's outer
  `timeout`.

## 4. Adversarial passes

Run each relevant pass with "how can this approve wrong?" framing:

- **State transition matrix:** enter every command from every state; inject
  failure before and after each save; check stale pointers, missing files,
  malformed JSON, duplicate events, retries, two concurrent hook calls.
- **Hook lifecycle:** mismatched session/worktree, absent fields, unexpected
  tool names, Bash success vs failure, commit created despite tool error,
  PostToolUseFailure omission, repeated Stop, host timeout below reviewer
  timeout.
- **Shell command shape:** quoted separators, escaped whitespace, `env`, `sudo`,
  shell functions, aliases, `git -C`, `--git-dir`, combined flags, pathspecs,
  command substitution, process substitution, heredocs, pipes, redirects, and
  safe-looking command followed by mutation.
- **Git snapshot:** staged-only large files, intent-to-add, deletions, renames,
  ignored files, nested repos, submodules, symlinks, binary blobs,
  newline-containing paths, empty repos, replace refs, diff failure.
- **Commit/reconcile:** empty commit, multiple commits in one Bash call, amend,
  partial staging, hooks that mutate after approval, rejected commit, detached
  HEAD, merge commit, history rewrite, dirty leftovers, reset before activation
  boundary.
- **Configuration:** malformed types, unknown severity, negative or huge limits,
  hostile `verify_cmd`, `ignore_globs=["**"]`, environment override, config
  changed after arming, permissive OpenCode project config, timeout mismatch.
- **Reviewer isolation:** prompt injection in source/plan/diff, attachment path
  escape, project skill conflict, global plugin contamination, external
  directory access, mutating verification, incomplete binary/submodule evidence,
  stale bundle files.
- **Output contract:** duplicate/misordered markers, multiple verdicts, CRLF,
  missing fields, invalid severity/actionable spelling, marker text in prose,
  huge findings, Unicode byte limits, timeout with partial output, `APPROVED`
  verdict beside actionable finding.
- **Interpreter invocation and shim contract:** `-m` or relative bootstrap
  path back, `uv run` reintroducing cwd-as-`sys.path[0]` exploit one level
  up, `sys.pycache_prefix` inside plugin repo or reviewed repo, shim
  forwarding output after non-zero exit or `timeout`'s `124`, wrong or
  reused fallback shape across entrypoints, hung parse returning after host
  hook timeout instead of before.
- **Reporting:** stale globals from previous review, every finding preserved,
  prose-only truncation, correct report sequence, gate verdict distinct from raw
  model verdict, useful recovery text without claiming approval.
- **Tests:** need regression that fail on old code, hit exact failure seam,
  assert durable state plus hook response. Reject tests that only assert helper
  output while end-to-end bypass still possible.
- **Contract drift:** compare README, skill frontmatter, fixed prompts, Make
  targets, CI, real behavior. Flag claims like complete evidence, automatic
  disarming, user-only actions, or exhaustive tests unless code prove them.

One reproducible bypass beat ten vague suggestions. Cannot name triggering
state and wrong result or broken invariant → dig more or drop it.

## 5. Verify findings and gates

Respect current capability first. OCRL model reviewer deliberately read-only,
cannot run commands; inspect attached `verify.txt`, never claim you ran it.
During interactive repo review with Bash permission, run focused tests while
investigating, then gate owed by changed set.

| Diff touched | Run |
| --- | --- |
| one state-machine or parser area | `make test-filter FILTER=<section>` followed by `make test` |
| any runtime shell, prompt, skill, or test | `make test` |
| broad change or merge-readiness review | `make test`, then `make check` |
| docs or review skill only | inspect references, then `pre-commit run markdownlint-cli2 --all-files` and `git diff --check` |
| Claude/OpenCode host integration | relevant manual checks in `tests/STEP0.md`; do not imply shell tests cover them |

`make check` include fix-capable hooks. Inspect `git status --short`, unstaged
diff, staged diff after, so formatter edits not mistaken for reviewed input.
`make dry-run` print reviewer argv and prompt without invoking OpenCode, but can
create state bundles and Git objects.

Shell-only diagnosis: `bash -n` and `shellcheck -x` on `scripts/ocrl.sh` and
`tests/*.sh`. Python-only diagnosis: `ruff check` and `mypy` on changed modules
direct. Gate cannot run → state why, mark unverified. Failing gate caused by
reviewed change = finding, not footnote.

## 6. Report

Rank findings by severity, worst first. Gate bypass, false approval, execution
of unreviewed code, lost work, secret exposure, state corruption = normally
critical or high. Skip pure formatting unless it change protocol meaning or
break required gate.

Normal interactive reviews use:

```text
<path>:<line> - <severity: critical | high | medium | low>: <one-line defect>
  Failure: <concrete command/state/event -> wrong result or broken invariant>
  Fix: <specific corrective change>
```

Findings first. Then open questions or assumptions, then one-line verdict:
**block**, **approve with nits**, or **approve**. List gates run and gates not
run. No findings → say so explicit, name failure modes attempted.

When `prompts/reviewer-phase.md` or `prompts/reviewer-final.md` is active
instruction, its `<<<OCRL-FINDINGS>>>` contract win. Emit exactly one machine
block with every finding and consistent verdict, including empty approved block
when no findings. Never omit markers or claim tests were run by read-only
reviewer.
