# Core

Security-shaped Claude Code plugin: external OpenCode review gates commits. Primary project rules and layout live in root `AGENTS.md`. Python additive port under `scripts/ocrl/`; legacy live Bash under `scripts/ocrl.sh` + `scripts/lib/` until Phase 6. State lives only in XDG state; failures must block/escalate, hook stdout is strict JSON protocol, snapshot must not touch real index/worktree, user alone controls exits.
