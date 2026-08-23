# Documentation

The top-level [`README.md`](../README.md) is the quickstart: install, commands, a quick
tour of how the gate behaves. The pages here go deeper, for anyone who needs more than the
quickstart gives:

| Page | For | Covers |
| --- | --- | --- |
| [how-it-works.md](how-it-works.md) | anyone, no engineering background needed | What problem this solves and why, in plain language |
| [architecture.md](architecture.md) | engineers working on or integrating with the plugin | Components, data flow, the state machine, on-disk layout |
| [configuration.md](configuration.md) | anyone running the loop day to day | Every setting, precedence, examples |
| [edge-cases.md](edge-cases.md) | anyone debugging unexpected behaviour | What happens when things go sideways, and why |
| [security.md](security.md) | anyone assessing whether this is safe to trust | The threat model, what is and is not enforced, and why |

[`AGENTS.md`](../AGENTS.md) in the repository root is the canonical authority for anyone
*changing* this project — the five non-negotiable rules, the invariants a change must not
break, and the non-obvious hazards that silently reopen a closed hole if reverted. These
docs describe the system as it behaves; `AGENTS.md` is the contract a change to it must
honour. Where the two disagree, `AGENTS.md` and the code win.
