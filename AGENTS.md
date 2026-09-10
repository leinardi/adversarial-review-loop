# AGENTS.md

Project authority for `adversarial-review-loop`. Read this before changing anything here.

This file owns what the project *is*, the rules a change must not break, and a one-line index of
every invariant behind them. The **arguments** for those invariants — the measurements, the
bypasses that motivated them, the reasoning that reverts to something plausible if forgotten —
live in [`docs/design/`](docs/design/). The index below names the note for each claim, and
"Load before changing" says which ones are mandatory reading for which paths.

For **how to review** a change to this repo, use the `adversarial-review` skill in
`.agents/skills/` — it owns the review procedure and the full review checklist.

## What this is

A Claude Code plugin that turns an external adversarial review into an **enforcement gate** on `git commit`. Claude implements a plan phase by phase; each phase's commit is intercepted by a `PreToolUse` hook, the whole working state is snapshotted into a git tree, the reviewer reviews the delta, and the commit proceeds only if the review passes.

It is a security-shaped component. The failure that matters is not a crash — it is an **unreviewed commit that looks reviewed**.

## The five rules

Everything else is detail. These are not negotiable, and a change that weakens one is a defect even if every test passes.

0. **A gate that cannot prove it is running denies.** Hooks register at plugin load (`hooks/hooks.json`), never on skill invocation, so the dispatcher runs in every process the plugin is enabled in — a resumed session included. A hook call with no session pointer is a session that never *completed* `implement`/`resume`, and three things still deny there, in this order: an unanswered arming marker recorded by `commands/intent.py`; a repository that cannot be resolved (`paths.repo_root_or_raise` — git answering "not a repository" passes, git being unrunnable, a vanished `cwd` or a timeout denies); and a worktree whose `latest` activation is still live while this session is unbound. Everything else passes silently and writes nothing. **Absence of state is never an opt-out**; absence of *this session's* state in an armed worktree, or after an arming prompt, is a denial. → [`rule-0-intent.md`](docs/design/rule-0-intent.md)
1. **Nothing converts a failure into an approval.** Missing state, malformed JSON, a snapshot failure, a timeout, a non-zero reviewer exit, empty output, absent markers, an unknown verdict, an evidence ceiling — every one of them blocks or escalates. Operational uncertainty is never "no findings".
2. **Hook stdout is protocol.** Hook entrypoints (`pretool`, `confirm-commit`, `posttool-failure`, `gate-stop`) emit valid Claude hook JSON or nothing. Diagnostics go to stderr only, through `arl.util.log`. A stray `print` in a module that runs under a hook corrupts the response — `report.store` is deliberately silent on stdout for this reason. An uncaught exception at the top level still emits that event's fallback JSON before exiting 0. → [`interpreter-and-watchdog.md`](docs/design/interpreter-and-watchdog.md)
3. **Nothing is written inside the repository under review, with one explicit exception.** All state, frozen plans, bundles and reports live under `$XDG_STATE_HOME/adversarial-review-loop/`. The snapshot uses a throwaway `GIT_INDEX_FILE` and never touches the real index or worktree. `sys.pycache_prefix` keeps `__pycache__` out of both the plugin repo and the reviewed one. The exception is `config <key> <value> --repo`, which writes the repository's own `.adversarial-review-loop.json` — user-only, explicit, documented, and never reachable from a hook or from Claude. No code path that runs on a tool call ever writes inside the reviewed repository.
4. **The user owns the exits.** `implement`, `finish`, `stop`, `resume`, `config`, `accept` and `pause` are `disable-model-invocation: true`, and Claude's own route to them — Bash — is denied in `commands/pretool.py`. `pause` is in that set because reachable by Claude it would be an unbounded, strictly better `defer`; `accept` because it grants rather than only ends, which is why it binds to one exact tree hash. **Rule 4 is not enforceable against a deliberately adversarial model** — a wrapper script and a hand-edited `state.json` both defeat it — so what is actually guaranteed is *reporting*, bounded to the activation's own lifetime by the `ended_*` record. → [`end-state-record.md`](docs/design/end-state-record.md)

## Load before changing

Read the note before changing the paths in its row — the index below states each invariant, but not the evidence that a plausible-looking change would destroy. The `adversarial-review` skill carries the same table as mandatory reading before ranking findings.

| Touched | Must load |
| --- | --- |
| `commands/hooks.py`, `commands/intent.py`, `commands/pretool.py` (`_no_pointer`, Rule 0 paths) | [`rule-0-intent.md`](docs/design/rule-0-intent.md) |
| `state.py` `pointer_write`, `paths.py` `repo_root_or_raise`, `hooks.py` `_answered_by_live_activation`, `pending_intent`, `resolve_repo` | [`rule-0-intent.md`](docs/design/rule-0-intent.md) |
| `commands/resume.py`, `commands/session.py` (`deactivate`, `_finish_the_retirement`) | [`resume-and-retirement.md`](docs/design/resume-and-retirement.md), [`end-state-record.md`](docs/design/end-state-record.md) |
| `state.py` `_migrate`, `STATE_VERSION`, `commands/pretool.py` `_upgrade_stale_document` | [`state-fields.md`](docs/design/state-fields.md), [`end-state-record.md`](docs/design/end-state-record.md) |
| `reviewer.py` (`_claim_*`, `_reserve_round`, `_publish`, `session_ref`, `_repair_*`), `oscillation.py` | [`state-fields.md`](docs/design/state-fields.md) |
| `cmdshape.py`, `_vendor/bashlex` | [`deny-list-and-parser.md`](docs/design/deny-list-and-parser.md) |
| `skills/*/SKILL.md`, `util.stdin_argument`, `commands/arm.py` `split_args`, the `--args-stdin`/`--reason-stdin` parsers in `arm`/`resume`/`pausecmd`/`configcmd`/`accept` | [`argument-channel.md`](docs/design/argument-channel.md) |
| `config.py`, `commands/configcmd.py`, `commands/arm.py` `_check_reviewer` | [`config-overlay.md`](docs/design/config-overlay.md), [`config-keys-rationale.md`](docs/design/config-keys-rationale.md) |
| `scripts/arl.sh`, `arl-bootstrap.py`, `cli.py`, `atomic.py`, `hookio.py` | [`interpreter-and-watchdog.md`](docs/design/interpreter-and-watchdog.md) |
| `reviewer.build_bundle`, `_hashed_rows`, `_confirm_*` | [`verify-cmd.md`](docs/design/verify-cmd.md) |
| `harness/` | [`adding-a-harness.md`](docs/design/adding-a-harness.md) |
| `gitsnap.py`, `commands/stop.py` `_guard_exclude`, `commands/posttool.py` | [`environment-hazards.md`](docs/design/environment-hazards.md), [`end-state-record.md`](docs/design/end-state-record.md) |
| `commands/pretool.py` `_review_failed`, `_check_retry_backoff`; `commands/posttool.py` `_advance` | [`state-fields.md`](docs/design/state-fields.md) |
| `commands/resume.py` `_refuse_if_the_overlay_moved`, `commands/arm.py` overlay writes | [`config-overlay.md`](docs/design/config-overlay.md) |
| `commands/arm.py` `exclude_digest` capture, `gitsnap.exclude_digest` | [`resume-and-retirement.md`](docs/design/resume-and-retirement.md) |

## Invariant index

Each line is a claim the code must keep true. `→ name` names its long form, [`docs/design/name.md`](docs/design/), which says why and what breaks if it is reverted. Where a line and a note disagree, the note and the code win.

### Sessions and enforcement

- Neither `skills/implement/SKILL.md` nor `skills/resume/SKILL.md` carries a `hooks:` block, and neither may grow one — every hook lives in `hooks/hooks.json`. → `resume-and-retirement`
- `hooks.pending_intent` is checked *ahead of* the session pointer, and is scoped to the worktree the marker names. → `rule-0-intent`
- The arming marker is read three-state: absent is "no intent"; unreadable, unscopable or token-less denies **everywhere** and is consumed by nothing but `deactivate --session`. → `rule-0-intent`
- **The pointer is published carrying the marker's token *before* the marker is unlinked**, so what answers a marker is the token and not the file. Unlinking first opens a window with no marker, no pointer and no `latest`, in which a saved `ACTIVE` activation goes ungated; a crash between the two leaves litter instead. → `rule-0-intent`
- **The marker's write order is not guaranteed** — measured, it can land after the expansion it announces has already run — so a marker whose worktree has a live gating activation bound to the same session is also answered (`hooks._answered_by_live_activation`), never recorded as a failure. The gate is on in both possible histories. → `rule-0-intent`
- `deactivate` leaves the session pointer in place and relies on `DISARMED` rather than deleting it. → `rule-0-intent`
- `deactivate` never rewrites a document that already ended; `RESUMED` is not a no-op but proof a resume died mid-publication, and `_finish_the_retirement` converges on the value retirement already decided. → `resume-and-retirement`

### Command shape

- **The deny-list and the parser are one design.** `_deny_shell_grammar` runs first and refuses everything outside words, two quoting forms, backslash escape and `&&`; relaxing it without re-reading `_words` is the specific change that breaks this component. → `deny-list-and-parser`
- `_deny_shell_grammar` must decide where the quotes are exactly as bash does, in both directions; inside single quotes there is no escape, in bash or here — do not add one. → `deny-list-and-parser`
- `unresolved_expansion` guarantees one thing only: textual detection may not go blind on a command **name**. It is not the boundary, and its exec-wrapper list is a speed bump. → `deny-list-and-parser`
- Its textual scan **may not skip text bash executes**; the only thing it skips is a heredoc body, and every rule in it was a real bypass found by differential testing against bash. Touch it, re-run that differential. → `deny-list-and-parser`
- Detection stays looser than validation, and **every detector knows the dashed spelling** — `git-commit`, `git-reset` and `git-update-ref` all exist in `git --exec-path` and do what their subcommand does. Over-detection is the safe direction. → `environment-hazards`
- A command bashlex cannot parse, or hangs on, **denies** — `CommandShapeTimeout` and the `signal.setitimer` deadline exist for exactly that. → `interpreter-and-watchdog`
- A refusal names its cause: `cmdshape.set_phases_refusal` changes no verdict and is gated on a textual pre-check so an impostor `arl` still gets the ordinary message. → `deny-list-and-parser`
- Skill arguments reach the shell through a here-document with a **quoted** delimiter inside a fenced block, never `--args "$ARGUMENTS"`; the argv spellings stay for older cached skill bodies, and `disable-model-invocation: true` is still the containment. → `argument-channel`

### Commit confirmation

- `confirm-commit` runs after **every** commit-shaped call and makes two different guarantees that must not be run together: `_verify` (the strong, phase-advancing check) and `_guard_unreviewed_head` (one question — is `HEAD^{tree}` in `approved_trees`?). → `deny-list-and-parser`
- Detection is exactly "the recorded tree is absent from `approved_trees`" — not proof of review, not proof of commit identity. State it at that width. → `end-state-record`
- `_ENDED` (`DISARMED`, `COMPLETE`, `RESUMED`) is **disjoint from `_RECONCILABLE`**, makes no git call, and reports from the recorded end state; `NEEDS_HUMAN` and `STALE` deliberately stay on the current-HEAD path. → `deny-list-and-parser`
- Only a *recorded* tree absent from `approved_trees` may carry the categorical headline; malformed, unreadable and unborn captures go through `ENDED_UNCERTAIN_REPORT`. → `end-state-record`
- `confirm-commit` reports an **unborn HEAD** when `activation_commit` is non-empty — the one HEAD move the tree comparison cannot make. → `environment-hazards`
- The final cumulative review is **opt-in** (`final_review`, off by default). Never write "the cumulative review will catch it" without saying which configuration you mean. → `deny-list-and-parser`

### Resume and retirement

- **Retire the predecessor before the successor exists, never the reverse.** Both sides deny if the process dies in between; there is **no automatic rollback** and the recovery is `implement`. → `resume-and-retirement`
- **A retirement may never span an in-flight approval** — `pretool` and retirement take the same lock, because a `status="ACTIVE"` write from any path resurrects a `RESUMED` activation. → `resume-and-retirement`
- **A retired activation's directory is never mutated**, with one bounded exception: a `reviewer.execute` already in flight may strand `bundles/`/`raw/` litter. Everything it still controls is withheld by the fingerprint guard. → `resume-and-retirement`
- **The inverted carry-forward rule**: the successor copies the whole predecessor document and resets a *named* set of fields — never an enumerated keep-list, which silently drops the next field added. → `resume-and-retirement`
- `latest` has **five publishers and no shared lock**, so `_finish_the_retirement` publishes only while `latest` still names what it resolved from — a compare-and-swap without an atomic swap. → `resume-and-retirement`
- Every resume bumps `activation_generation`, same-session included, and so does every `accept`; both `hooks.Activation` and `completion.Fingerprint` carry it. → `state-fields`

### State

- **`state.json` is not a trust boundary.** Every field read out of it is untrusted: validate a filename with `paths.is_safe_component`, resolve with `os.path.realpath` on both sides, verify the recorded `sha256`, and terminate every git argv built from a state-supplied object id with `--`. → `state-fields`
- The **`ended_*` record is write-once and folded into the same `state.update` as its terminal status write** — one code path, so evidence can never land without the transition or the transition without the evidence. Retirement captures only when the predecessor's stored status was still live. → `resume-and-retirement`
- **Absent and present-empty are different, and collapsing them is how a check turns itself off.** `ended_capture` absent means "written before the field existed" (silence); present-and-empty means an edited document (report). The same rule governs `exclude_digest`. → `state-fields`
- The `.git/info/exclude` digest comparison is **tri-state**, not boolean: changed, baseline-not-written-by-an-arm, and could-not-read are three different claims and only the first is evidence about the worktree. Only an *absent* baseline passes. → `resume-and-retirement`
- `_migrate` runs its arms in order and each bumps `version` to its own target; a version this build does not recognise refuses outright. The 4→5 arm resolves `ended_capture`'s ambiguity from the **stored status**, never by inventing evidence. → `state-fields`
- `_migrate` only runs inside `transaction()`, which is why `pretool._upgrade_stale_document` exists — and why it is **deliberately not hoisted above `hooks.tool_is_readonly`**. → `state-fields`
- **`round_history` is evidence, not a counter** — carried forward, never reset by `resume`; **only `reviewer.execute` appends**, only for a parsed `APPROVED`/`CHANGES_REQUIRED`, only under the fingerprint guard. → `state-fields`
- `active_review` is a **dict keyed by label**, and it is a **lease**: every step under it must be separately bounded, the window is the **max** of `execute`'s two stretches rather than their sum, and `_publish` refuses to record anything once the slot no longer holds this run's `claim_id`. → `state-fields`
- A **busy slot is denied and paced but never counted** against either budget (`Review.contended`): nothing was invoked, and the claim of a hook killed mid-review outlives it for the rest of the lease, so counting would let one interrupted turn escalate to `NEEDS_HUMAN` on a wall clock. The denial names the remaining lease and the resume that clears it. → `state-fields`
- **A claim's window is recorded on the claim, not recomputed by whoever reads it**, and `_MAX_LEASE_SEC` is derived from the formula it bounds rather than chosen. → `state-fields`
- `review_attempts` records the newest `report_seq` any *attempt* reserved, whatever became of it; an approval must still be the newest attempt. → `state-fields`
- `report_seq` and `clarify_seq` are **carried forward**, never reset — a reset one overwrites an inherited report or question and destroys its bundle. → `state-fields`
- `replan_pending` fences every mutation except the one exact `set-phases`, and is cleared by whichever `set-phases` runs next, the ordinary first freeze included. → `state-fields`
- Both stall signals ask only about findings a round still stands behind, and they are **complementary** — a change must keep them so, or one can be dodged by leaning on the other. → `state-fields`
- `_classify_op_failure` is an **allow-list**: only a timeout or a bounded, anchored rate-limit phrase is `"transient"`. `126`/`127` keep the ordinary budget, because retrying faster cannot fix a missing binary. → `state-fields`
- **A `"contract"` failure gets exactly one repair call, and only a `CHANGES_REQUIRED` carrying a blocking finding is accepted.** No approval may originate from a repair, and a `SUPERSEDES` line from one fails the contract. → `state-fields`
- The repair is the **only** call that can follow the primary invocation under the lease, which is what lets `_invoking_budget` be `timeout_sec + REPAIR_TIMEOUT_SEC`; `settle_margin` is derived from the steps it covers, not chosen. → `state-fields`
- The busy-slot classification creates an ordering hazard closed by **three complementary fixes, none of which is redundant**: `_review_failed`'s transient branch is fingerprint-guarded inside the same transaction that would bump the counter; `_check_retry_backoff` still runs **only after** every free shortcut, never before them, because a revert to `last_approved_tree` under an unrelated backoff moves nothing the fingerprint can see; and `_advance` resets `transient_failures`/`retry_not_before` as it already resets `failures`. → `state-fields`

### Reviewer and evidence

- **Evidence is hashed before `verify_cmd` runs and re-checked after**, and only `verify.txt` is hashed afterwards; `_downgrade_bundle_round` is the one place a new trusted digest is minted. → `verify-cmd`
- `verify_cmd`'s process group is killed after a normal exit too, and every staged attachment is re-checked immediately before launch — that narrows the window, it does not close it. → `verify-cmd`
- **A harness composes a command; it never decides anything.** Nothing in `harness/` may read a verdict, touch `state.json`, or turn a failure into an approval. → `adding-a-harness`
- Both harnesses **inline** every attachment, which is what keeps `context/` from existing at a path the reviewer can re-open; an inlining harness verifies each digest as it reads. → `adding-a-harness`
- `capture_timeout_sec` must be honest — the gate sizes its leases and its repair reserve from it, and `_MAX_LEASE_SEC` is the ceiling across *all* registered harnesses. → `adding-a-harness`
- The session-continuity pointer is **advisory and never authorizes anything**; it rides `activation_generation`, the revisions and the `harness` field, and a mismatch costs exactly one fresh review. → `state-fields`

### Config

- Precedence is defaults < user config < repo config < activation overrides < environment, and **`harness` is pinned in the overlay to what `_check_reviewer` actually probed**, never to the `--harness` value. Only keys already in `config.DEFAULTS` are accepted from the overlay. → `config-overlay`
- Each resume probes the reviewer against the overlay it read *before* taking the lock, so `resume._refuse_if_the_overlay_moved` **compare-and-swaps on the stored overlay** inside both the same-session transaction and `_retire` — a second concurrent resume is refused and retried, never merged. → `config-overlay`
- Repository config is attacker-controlled: a config change must not be able to execute unreviewed code or silently weaken the active gate. `ignore_globs: ["**"]` is already a complete per-commit bypass — close that class there first, not at whichever new key raised the question. → `config-keys-rationale`
- `review_guide` is the first key whose *value* becomes instruction to the reviewer. It is bounded structurally — framing, splice position, per-composition nonce, path allowlist, arm-time refusal, and a verdict recomputed from the `FINDING` lines — not stylistically. → `config-keys-rationale`
- `late_block_severity` narrows *what blocks*: **every doubt disables the scope, none narrows it**, and deferred means "did not block this approval" — the next review of the same phase finds the path in `prior_files` and blocks. Anything replacing a `Review` after `parse` must carry the deferred lines with it. → `config-keys-rationale`
- `max_session_rounds` only ever removes context, and `round` is read through `_pointer_round` in **both** readers because it is arithmetic on both sides of the cap. → `config-keys-rationale`

### The invocation path

- **Never `python3 -m arl`, never a relative path.** `-m` puts the reviewed repository's `cwd` at `sys.path[0]`. The only sanctioned invocation is `python3 -I "$PLUGIN_ROOT/scripts/arl-bootstrap.py"`. → `interpreter-and-watchdog`
- **`-I` is load-bearing, not a style choice** — it implies `-P`, `-E` and `-s`, which is why the bootstrap re-derives `sys.path` from its own absolute `__file__`. → `interpreter-and-watchdog`
- **`uv run` must never appear on the hook path** — it executes code from the reviewed repository and its `.python-version` redirects the interpreter. → `interpreter-and-watchdog`
- `sys.pycache_prefix` is set at runtime after checking it overlaps neither repo nor `cwd`; a failed check sets `sys.dont_write_bytecode`, never a fall-back to writing beside the source. → `interpreter-and-watchdog`
- **The shim never `exec`s a hook subcommand.** It captures stdout, forwards only on exit exactly `0`, and on any other exit discards everything and emits that event's own fallback. The discriminator is exit status, never empty stdout. → `interpreter-and-watchdog`
- **The fallback shape is per-event, not one shape reused everywhere**: `deny` for `pretool`, `{"decision":"block"}` for `gate-stop`, `additionalContext` for `confirm-commit`, exactly zero bytes for `posttool-failure`. Python mirrors it through `hookio`. → `interpreter-and-watchdog`
- The outer watchdog is `timeout`, else `gtimeout`, else the perl supervisor; it uses no signals, is isolated like `python3 -I`, measures against `CLOCK_MONOTONIC`, kills at the deadline with no grace, and kills the **process group**. `arm`/`resume` refuse up front when none exists. → `interpreter-and-watchdog`
- **Every state write is same-directory `os.replace` under an `fcntl.flock`**, never a direct write. → `interpreter-and-watchdog`
- **`atomic.write_private_atomic` is the only writer used under `paths.state_root()`** and must never be pointed anywhere else — its chmod walk would chmod the target directory itself. `atomic.write_atomic` is the one for `config --repo`, and it preserves an existing file's mode. → `interpreter-and-watchdog`
- **Read-only tools answer before config or state is loaded.** If a future state ever needs to deny a read-only tool, remove that hoist first and put the per-branch checks back with it. → `interpreter-and-watchdog`
- **One process per job on the hot path** — four processes regardless of branch. Re-measure with `strace -f -e trace=execve` before and after any change that might add one. → `interpreter-and-watchdog`

### Repository hygiene

- The snapshot's throwaway index is a **copy of the repository's real index**, mtime included, mode deliberately not. Seeding from `HEAD` loses entries `git add -A` would not recreate. → `environment-hazards`
- `.git/info/exclude` is the one ignore file the gate can neither see past nor review, so `arm` baselines its digest and the Stop gate re-checks after **every** reviewer call. The fix is not to look through the file. → `resume-and-retirement`
- That digest is **captured before the cleanliness check and re-verified immediately before the document is written**, so the "this worktree is clean" verdict and the recorded baseline describe one and the same set of ignore rules. `resume` leaves the field out of both reset tables, so a resume cannot launder an edit made under the predecessor into the successor's starting truth. → `resume-and-retirement`
- A diverging **root** commit has no parent, so its recovery is a bounded `git update-ref -d HEAD`; every other `git update-ref` is denied. Do not describe the reset as the only recovery. → `environment-hazards`
- An activation armed on an **unborn HEAD** can never take the no-review completion path, and must not escalate on that — all three remedies the escalation named are themselves refused. → `environment-hazards`

## Layout

| Path | What lives there |
| --- | --- |
| `scripts/arl.sh` | the guarded shim registered as every hook's command; probes the interpreter, runs it, fails closed |
| `scripts/arl-bootstrap.py` | trusted absolute entrypoint; establishes `sys.path` and `sys.pycache_prefix` before `arl` is imported |
| `scripts/arl/cli.py` | subcommand dispatch, reached only through the bootstrap |
| `scripts/arl/paths.py` | state-directory layout and repository resolution |
| `scripts/arl/atomic.py` | durable, private writes for everything the gate persists — same-directory `os.replace`, `0700`/`0600` permissions, `flock` |
| `scripts/arl/hookio.py` | hook input parsing and the fail-closed decision emitters (**Rule 2** lives here) |
| `scripts/arl/config.py` | config precedence: `ARL_*` env → repo json → user json → defaults |
| `scripts/arl/state.py` | session pointer, `state.json`, effective status incl. TTL |
| `scripts/arl/gitsnap.py` | temp-index snapshot, oversized guard, submodule detection |
| `scripts/arl/cmdshape.py` | deny-list plus bashlex AST walk deciding whether a commit command may run |
| `scripts/arl/globmatch.py` | `[[ $path == $glob ]]` semantics, reimplemented rather than shelled out to |
| `scripts/arl/reviewer.py` | bundle building, staging, running the reviewer command, contract parsing — everything that decides an outcome, and nothing that names a CLI |
| `scripts/arl/harness/` | the reviewer-CLI seam: the `Harness`/`SessionStrategy` protocols, the registry in `__init__.py`, one module per implementation (`opencode.py`, `claudecode.py`) |
| `scripts/arl/reviewer_probe.py` | the `opencode models` reachability probe, reached through `opencode.probe_models`; a harness that cannot enumerate its models answers `None` |
| `scripts/arl/planrev.py` | plan-revision bookkeeping: backfilling revision 0, path/hash verification, the active revision |
| `scripts/arl/report.py` | report storage and the text Claude actually sees |
| `scripts/arl/commands/` | one module per subcommand group — `arm`, `resume`, `phases`, `session`, `configcmd`, `completion`, `dryrun`, `accept`, `clarify`, `pausecmd`, and the four hook entrypoints in `pretool.py`, `posttool.py`, `stop.py`, `hooks.py` |
| `scripts/arl/_vendor/bashlex/` | vendored parser; lint-excluded, `_vendor/README.md` records the upstream version and commit |
| `scripts/arl/guide.py` | the repo-supplied review guide: resolution, the arm-time refusals, freezing, re-verification, and composing it into a prompt |
| `prompts/*.md` | the reviewer prompts — Claude writes none of this. The phase and final ones carry one `<!-- ARL:PROJECT-GUIDANCE -->` line, which `guide.compose` replaces with the frozen guide (or strips); nothing else is composed |
| `skills/*/SKILL.md` | the nine slash commands; none registers a hook — `hooks/hooks.json` does, at plugin load |
| `docs/design/` | the argument behind every line of the invariant index above — one note per topic |
| `tests/selftest.sh` | the **shim** suite, and only that: interpreter probe, shim contract, watchdog layers, socket stdin, the hot path's process budget, one bootstrap smoke walk. Everything the gate *decides* is `tests/unit/`. Bash, because it runs outside the Python whose launch it tests |
| `tests/unit/` | pytest unit tests for the Python modules. Shared fixtures and helpers are in `conftest.py`; the reviewer suite is `test_reviewer_<subsystem>.py` over the helpers in `reviewer_common.py`. A helper used by one other file is imported from the module that owns it (`test_commands_arm`, `test_commands_pretool`), which mypy allows only for a name that module actually defines |
| `tests/STEP0.md` | runbook for the assumptions only a live session can settle |
| `tests/step0-fixture.sh` | builds the throwaway repo that runbook needs |

## Working on it

```console
make dev-deps                # install the pinned dev dependencies (once per checkout)
make test                    # full suite; no model is called
make test-unit               # the pytest half only
make test-accept             # tests/selftest.sh only (the shim)
make test-filter FILTER=watchdog # one selftest section
make check                   # pre-commit: shellcheck, markdownlint, yamllint, actionlint, ruff, mypy
make sync-pins               # propagate requirements-dev.txt into .pre-commit-config.yaml
make dry-run                 # print the exact reviewer command and prompt without invoking it
ARL_HARNESS=opencode make dry-run   # the same, for the other harness
```

`make test` needs pytest, which is pinned in `requirements-dev.txt` and installed by `make dev-deps`;
CI installs from the same file, so a local run and a CI run see the same version. That is a
*development* dependency only — the plugin's own runtime needs nothing beyond `python3`, the
standard library, and the vendored bashlex under `scripts/arl/_vendor/`, and must keep working
straight from a checkout with no install step.

pytest is pinned in two places — `requirements-dev.txt` and the `additional_dependencies` of the mypy hooks in `.pre-commit-config.yaml`, which is what gives mypy pytest's `py.typed`. Dependabot bumps the first and never the second, so `tests/unit/test_pins.py` fails when they drift. `make sync-pins` propagates `requirements-dev.txt` into the hook config; run it on a Dependabot bump and commit the result alongside.

`make test` must pass before any commit. A change to the gate needs a test that **fails on the old code** — a test that only asserts a helper's return value while the end-to-end bypass survives is not a regression test.

`make check` runs fix-capable hooks (markdownlint, prettier, end-of-file-fixer). Check `git status --short` afterwards so a formatter's edits are not mistaken for reviewed input.

**`make check` only sees files git already knows about.** pre-commit enumerates `git ls-files`, so a brand-new file that has never been added is skipped in silence — ruff, mypy and shellcheck report clean on code they never read, and the first real run is the commit hook, after the change has already been reviewed. Run `git add -N` on every new file (intent-to-add is enough; it needs no staged content) before `make check`, and treat a green run that skipped a new module as no run at all.

### Python conventions

- Python 3.12, standard library only at runtime. `requirements-dev.txt` (pytest and friends) is a *development* dependency; the gate itself must keep working straight from a checkout, with no install step.
- `ruff` (`.ruff.toml`, `line-length = 150`, `target-version = "py312"`) and `mypy` (`mypy.ini`, `strict = True`) must both be clean. Both exclude `scripts/arl/_vendor` — it is kept diffable against upstream bashlex, not reformatted or typed to this repo's standard.
- `mypy --strict`: every function is typed, including its return type. `disallow_untyped_defs` and `check_untyped_defs` are both on, so an untyped helper is a lint failure, not a gap that slips through on an existing function.
- One module per concern, not one process sourcing globals — `hookio.py`, `config.py`, `state.py`, `cmdshape.py`, `gitsnap.py`, `reviewer.py`, `report.py` each own one layer, and `commands/` holds one module per subcommand group. Cross-module state is passed explicitly (a `Config`, a `State`, a `Hook`), never a module-level global mutated by a caller.
- Hook entrypoints unwind through `hookio`'s `Decided` / `OutputFailure` control-flow exceptions (deliberately `BaseException` subclasses, not `Exception` — see the class docstrings in `hookio.py`), not through a shell-style `exit 0` scattered across branches.
- `__all__` is declared on every module that has one, and is the module's actual public surface — an import from outside that list is a sign the boundary is wrong, not that the list needs extending.

### Adding config

New keys go in `config.DEFAULTS`, in `config.from_env`'s key list with the right type branch (a severity label also goes in `config.SEVERITY_KEYS`, which is what `configcmd` validates against), and in the README table. Treat repository config as attacker-controlled. → [`config-keys-rationale.md`](docs/design/config-keys-rationale.md)

### Adding a harness

A new reviewer CLI is a new module under `scripts/arl/harness/` plus one line in `harness._registry`. Nothing else in the gate learns its name. `tests/unit/test_harness.py` is parametrised over the registry and picks it up for free; add a `test_harness_<name>.py` and a `--harness <name>` case in `tests/unit/test_commands_dryrun.py`. → [`adding-a-harness.md`](docs/design/adding-a-harness.md)

### Commits

Conventional Commits with a mandatory scope (`conventional-pre-commit --force-scope`), e.g. `fix(cmdshape): reject git commit --only`.

### The install cache, and what it means for iterating

A local marketplace install **copies** the plugin to `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, but sets `${CLAUDE_PLUGIN_ROOT}` to the `installLocation` — this repository. The two are served from different places:

| What changed | Takes effect |
| --- | --- |
| anything under `scripts/` | **immediately** — hooks run `${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh`, which is the working tree |
| `skills/*/SKILL.md` body or frontmatter | only after the cache is refreshed |
| `prompts/*.md` | immediately, for the same reason as `scripts/` |

`/plugin marketplace update` refreshes the marketplace record but **not** the cached copy when the version is unchanged. To pick up a skill-body change, bump `version` in `.claude-plugin/plugin.json`, then reinstall and restart. A stale body is easy to misdiagnose because the script it invokes is current — the giveaway is an error quoting a command line you no longer have in the repo.

## Host integration

Some behaviour cannot be tested from a shell — skill-hook registration, `` !`…` `` expansion inside a skill body, `${CLAUDE_SESSION_ID}` equality with the hooks' `session_id`, the Stop-hook block cap. These live in `tests/STEP0.md` with an expected result and a fallback each. **Do not claim shell tests cover them**, and do not change the arming path or the skill frontmatter without re-running the relevant STEP0 item.

Verified against Claude Code 2.1.235 and `opencode 1.18.18`.
