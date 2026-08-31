# Core

Security-shaped Claude Code plugin: external adversarial review gates commits. Primary project rules and layout live in root `AGENTS.md`. Gate is Python (`scripts/arl/`, `scripts/arl-bootstrap.py`), invoked only via `python3 -I` behind the guarded shim `scripts/arl.sh`; the Bash implementation under `scripts/lib/` was removed in Phase 8, so there is no reference implementation or rollback path left. State lives only in XDG state; failures must block/escalate, hook stdout is strict JSON protocol, snapshot must not touch real index/worktree, user alone controls exits.
