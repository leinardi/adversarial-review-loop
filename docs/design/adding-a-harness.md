# Adding a harness

The long form of the harness-seam invariants in [`AGENTS.md`](../../AGENTS.md): what a new reviewer CLI module must settle, and what it may never decide.

A new reviewer CLI is a new module under `scripts/arl/harness/` plus one line in
`harness._registry`. Nothing else in the gate learns its name — that is the property the seam
exists to keep, and the tests below are what stop it eroding.

**A harness composes a command; it never decides anything.** It answers with a `Command`
(argv, env *overrides*, optional stdin, optional cwd) and a `SessionStrategy`, and
`reviewer.py` runs it. Nothing in `harness/` may read a verdict, touch `state.json`, or turn a
failure into an approval. The two places that look like exceptions are not: `transcript()`
*refuses* a run whose own report says a tool was denied or a turn failed — refusing is always
allowed — and `probe_models()` answers `None` for a CLI with no model-list subcommand, which
makes the callers check binary presence only rather than inventing a list.

What a new module has to settle, in the order the existing two settled it:

1. **How the prompt and attachments are delivered.** Both current harnesses inline every
   attachment: OpenCode through `-f`, Claude Code by concatenating them into the payload it
   writes to stdin. That is not a style choice — it is what keeps `context/` from existing at
   a path the reviewer can re-open, which is the whole evidence-boundary argument in
   `docs/security.md`. A path-based channel is measurably *worse* on tokens as well (the
   benchmark is in the harness split plan), so a third harness that wants one owes both
   arguments. If it inlines, it must verify each attachment's digest as it reads it
   (`Attachment` carries one for exactly this) and raise `PayloadError` on a mismatch — the
   gate's launch-time re-check ends at a pathname and cannot cover bytes this process reads.
2. **How its sessions come into existence.** `DiscoveredSessions` (list-and-match a unique
   title afterwards) and `AssignedSessions` (mint a uuid up front) are the two shapes so far.
   `capture_timeout_sec` must be honest: the gate sizes its claim leases and its
   repair-budget reserve from it, and `_MAX_LEASE_SEC` is the ceiling across *all* registered
   harnesses, so an inflated value there loosens a check for everyone.
3. **What isolation its CLI actually gives, measured rather than read off the flag help.**
   `tests/STEP0.md` records what each probe found; three of the four things the Claude Code
   harness relies on were not what the help implied. Split the flags the way that module does:
   what makes the reviewer read-only is unconditional, and only the *ambient instruction*
   isolation is what `pure` selects.

Then: `tests/unit/test_harness.py` is parametrised over the registry and will pick the new
module up for free (protocol, binary-first argv, no session on a cold or clarify call,
`is_session_id` strictness, the lease ceiling); add a `test_harness_<name>.py` for its own argv
and output shapes, and a `--harness <name>` case in `tests/unit/test_commands_dryrun.py`. `make
dry-run` prints whatever `review_command` composed, generically, so there is no rendering to
add — if the new harness's argv looks right there and its stdin carries every attachment
between its fences, the seam is wired.
