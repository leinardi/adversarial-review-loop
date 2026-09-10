# Design notes

The argument behind each line of the invariant index in [`AGENTS.md`](../../AGENTS.md). An
index line states what must stay true; the note here says why, what was measured, and which
plausible-looking change reverts it. `AGENTS.md`'s "Load before changing" table maps a
touched path to the notes that are mandatory reading before changing it, and the
`adversarial-review` skill carries the same table for reviews.

| Note | Covers |
| --- | --- |
| [rule-0-intent.md](rule-0-intent.md) | Why hooks register at plugin load, and the three things that deny when a hook call carries no session pointer |
| [end-state-record.md](end-state-record.md) | What Rule 4 does and does not guarantee, and how the `ended_*` record bounds both reports to the activation's own lifetime |
| [deny-list-and-parser.md](deny-list-and-parser.md) | Why vendoring bashlex did not move the boundary, what `unresolved_expansion` guarantees, and the two layers behind the parser |
| [argument-channel.md](argument-channel.md) | Why every skill that takes an argument passes it through a quoted here-document |
| [resume-and-retirement.md](resume-and-retirement.md) | One live activation per worktree: the retire-first order, what a retirement may never span, and the inverted carry-forward rule |
| [state-fields.md](state-fields.md) | Why `state.json` is not a trust boundary, what a migration may not synthesize, and the invariant each added field carries |
| [config-overlay.md](config-overlay.md) | Where an activation's own overrides sit in the precedence chain, and why `harness` is pinned to what was probed |
| [config-keys-rationale.md](config-keys-rationale.md) | How a new key is added, and why `final_review`, `review_guide`, `late_block_severity` and `max_session_rounds` are admissible |
| [verify-cmd.md](verify-cmd.md) | The hashing order that keeps a bundle honest, and the process-group reap that bounds what `verify_cmd` leaves behind |
| [interpreter-and-watchdog.md](interpreter-and-watchdog.md) | How the gate is launched, what the outer watchdog covers, which atomic writer may touch which root, and what the hot path may not grow |
| [adding-a-harness.md](adding-a-harness.md) | What a new reviewer-CLI module must settle, and what it may never decide |
| [environment-hazards.md](environment-hazards.md) | The things outside the gate that change what it sees |
