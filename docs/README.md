# Documentation

The top-level [`README.md`](../README.md) is the landing page: what this is, how to install
it, the commands, and the handful of settings most people touch. Everything deeper lives
here:

| Page | For | Covers |
| --- | --- | --- |
| [how-it-works.md](how-it-works.md) | anyone, no engineering background needed | What problem this solves and why, in plain language |
| [faq.md](faq.md) | anyone using it day to day | The questions that come up first, answered short |
| [configuration.md](configuration.md) | anyone running the loop day to day | Every setting, precedence, cost, examples |
| [architecture.md](architecture.md) | engineers working on or integrating with the plugin | Components, data flow, the state machine, what blocks, on-disk layout |
| [edge-cases.md](edge-cases.md) | anyone debugging unexpected behaviour | What happens when things go sideways, and why |
| [security.md](security.md) | anyone assessing whether this is safe to trust | The threat model, what is and is not enforced, and why |
| [design/](design/) | anyone changing the plugin | The argument behind each invariant in `AGENTS.md` — one note per topic |

[`AGENTS.md`](../AGENTS.md) in the repository root is the canonical authority for anyone
*changing* this project — the five non-negotiable rules and a one-line index of every
invariant a change must not break. Each index line names a note under
[`design/`](design/) carrying the argument for it: the measurement, the bypass that
motivated it, and what silently reopens if it is reverted. These docs describe the system
as it behaves; `AGENTS.md` is the contract a change to it must honour. Where the two
disagree, `AGENTS.md` and the code win.
