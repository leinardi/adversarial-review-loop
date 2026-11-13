# Core

v4 rewrite in progress on the `v4` branch: the plugin moves from an enforcement gate to a
plan-and-go review loop with no security guarantees. Project rules and layout live in root
`AGENTS.md`. The engine is Python (`scripts/arl/`, `scripts/arl-bootstrap.py`), run only as
`python3 -I` through `scripts/arl.sh`; the orchestrator and UI become a Claude Code mod under
`hooks/`. A failure is never an approval, the reviewer sees the whole phase, and snapshots never
touch the real index or worktree.
