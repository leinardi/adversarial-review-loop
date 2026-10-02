# The display module: it shows, it never decides

The long form of the display-module lines in [`AGENTS.md`](../../AGENTS.md): what `hooks/register.js` is, why it may not grow a say in any verdict, and what `status --json` guarantees it.

## What it is

Since Claude Code 2.1.287 a plugin's `hooks/hooks.json` may name a JavaScript hooks module beside its command hooks. This plugin ships one, `hooks/register.js`, and it is a display only:

- **A band above the prompt** with this session's activation: `ARL ACTIVE · phase 2/5 · round 3 · failures 0/2`, or `all 5 phases committed` once only the turn end is left. Another session's live activation in this worktree draws as "armed by another session; resume binds this one". An unarmed worktree draws nothing, and any state the module cannot establish draws `ARL: state unknown (<cause>)`. The band is shared, so the line is drawn above whatever `next(e)` returns (the engine's band and other plugins' content) and never replaces it. While a survey holds the band, the line yields.
- **A transcript line when an alert appears**, through `$.ui.log`. The line reaches the user and is not part of the transcript the model reads (`tests/STEP0.md` item 25). `$.ui.toast` is not used: in the same measurement it was never seen.

Its one source is `arl.sh status --json --session <id>`, run through `$.process.run` at `session.start`, `turn.start`, `turn.complete`, and after a `Bash` call while the last answer was not `unarmed`. `/clear` mints a new session id and fires no `session.start` (item 22), so every refresh reads the id afresh.

## Why it may never decide

**A module that throws, times out or answers the wrong shape is skipped by the engine, and the call proceeds.** Three crashes of the worker that every installed module shares unload all of them for the session, and modules can be off entirely while command hooks keep running (`allowManagedModsOnly`, `--bare`, Anthropic's remote switch). Measured: a module that does not parse, and one whose every hook throws, both leave the command hooks gating (item 21). That is exactly what makes a module unfit to be the gate under Rule 1, and safe as a display: whatever happens to it, nothing the gate decides changes.

So the module's contract is narrow, and `tests/unit/test_mod_contract.py` pins it from the source:

- It hooks exactly `session.start`, `turn.start`, `turn.complete`, `tool.call` (matched to `Bash`) and `ui.render` (matched to `AbovePrompt`). Every one passes `next(e)` the event unchanged. The first four return its result unchanged, and `ui.render` returns it either alone or beneath the line. No hook answers an event.
- It never hooks `tool.check`, any `classic.*` event, `command.*` or `prompt.*`, and registers no `.catch`. Each of those can approve, block, rewrite or suppress something the gate depends on (see [`end-state-record.md`](end-state-record.md#mods-three-more-routes-none-of-them-a-tool-call)).
- Its only process is the shim, by absolute path, with `status --json --session`. It touches no `$.fs`, `$.store`, `$.state`, `$.env`, `$.http`, `$.model`, `$.agent` or `$.tool`, and imports nothing.
- It draws only enums and integers it has checked against fixed sets, and the fixed strings that go with them. `state.json` is not a trust boundary, and this is drawn in a terminal. An alert it has no line for, or an `alerts` that is not a list, makes the whole answer `unparseable`. Dropping it would leave a quiet `ACTIVE` while a newer `status` was raising something, so a new alert key is added to `ALERTS` explicitly.

No command is registered. A text reply from `command.run` is read by Claude, so it would not be token-free, and the band already carries the state.

## What `status --json` guarantees it

`commands.session._status_document`, reached only by `status --json --session <id>`:

- **Bound to the session it is given.** It never falls back to the worktree's `latest`, which is what showed a retired predecessor its successor's state. `--json` without `--session` is refused.
- **Reads only.** It deliberately does not call `hooks.pending_intent`, which unlinks an answered marker and publishes a pointer for a late one. A display refreshing every turn must not be what moves gate state. An unanswered arming marker is therefore not shown; the gate's own denial reports it.
- **Unknown is never unarmed.** A missing, unreadable, malformed or future-version document, an unrecognised status, and a repository git cannot resolve are each `binding: unknown` with a cause. The module adds its own two, `status_failed` and `unparseable`, for a call that did not answer and an answer it could not read.
- **Every alert is computed in Python.** `_ended_alert` mirrors `stop._ended`, and `test_status_json_alerts_exactly_when_the_stop_gate_reports` holds the two together. The module never re-derives one.

## Stated width

This is a display for an honest agent. A hostile module earlier in the hook chain can rewrite what this one sees, or suppress it; that is not defended, for the same reason the three routes in `end-state-record.md` are not.

## The files the engine writes

Loading a module from a folder the user owns writes `.claude-plugin/types/` into it (self-ignored, through its own `.gitignore` of `*`) and, when none exists, a root `tsconfig.json` containing `{"extends": "./.claude-plugin/types/tsconfig.json"}` (item 24). The root file is committed, so a dogfooding session does not find an untracked file and refuse to end its phase clean. The committed one also turns on `allowJs`/`checkJs`, since the generated config checks `.ts` only; measured, the engine leaves an existing root file alone.

Those generated types are what the `typecheck-mod` pre-commit hook checks `register.js` (typed through JSDoc, so it stays plain JavaScript importing nothing) and `tests/mod/` against. They are written at the loading Claude Code's version and never committed, so the hook fails, saying how to generate them, wherever they are absent. CI never has them and does not run it.
