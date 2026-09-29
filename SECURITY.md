# Security policy

## Supported versions

Only the latest release gets security fixes. A fix ships as a new version: release tags are immutable, so an existing version is
never re-tagged. Update the plugin to the latest release before reporting.

## Reporting a vulnerability

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/leinardi/adversarial-review-loop/security/advisories/new), not in a public
issue, a pull request or a discussion. Include the plugin version, the Claude Code version, the reviewer harness (`claude` or
`opencode`), your operating system, and the steps or the command that trigger the problem.

This is a project maintained in spare time, so reports are handled on a best-effort basis. You will get an answer in the advisory,
and the fix, once released, is credited there unless you prefer otherwise.

## Scope

The failure that matters here is an unreviewed commit that looks reviewed. In scope: any way to get a commit past an armed gate
without the review it claims, a hook that fails open, a state transition that launders an unreviewed tree, and command injection
through the reviewer invocation or the repository's configuration.

Out of scope: what [`docs/security.md`](docs/security.md) documents as deliberately not enforced, such as a deliberately hostile
agent working around the honest-agent bar. Such findings are still welcome as issues if you think the boundary should move, but they
are treated as design discussions, not vulnerabilities.
