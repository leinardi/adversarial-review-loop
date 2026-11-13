# Security policy

## Supported versions

Only the latest release gets security fixes. A fix ships as a new version: release tags are
immutable, so an existing version is never re-tagged. Update the plugin to the latest release before
reporting.

## Reporting a vulnerability

Report it privately through GitHub's [private vulnerability
reporting](https://github.com/leinardi/adversarial-review-loop/security/advisories/new), not in a
public issue, a pull request or a discussion. Include the plugin version, the Claude Code version,
the reviewer harness (`subagent` or `opencode`), your operating system, and the steps that trigger
the problem.

This is a project maintained in spare time, so reports are handled on a best-effort basis. You will
get an answer in the advisory, and the fix, once released, is credited there unless you prefer
otherwise.

## Scope

Since v4 the plugin enforces nothing: it is not a security boundary, and the agent being reviewed
can bypass the loop by design (see [docs/philosophy.md](docs/philosophy.md)). In scope:

- a failure reported as an approval: a reviewer that crashed, timed out, said nothing or broke the
  output contract, read as `APPROVED`;
- a review whose bundle leaves out part of the phase's change without saying so;
- a result recorded on a state it was not prepared for (a stale or foreign review ticket);
- command injection through the reviewer invocation, the configuration or a reviewed file's name;
- the reviewer subagent being allowed to write or run something its permission hook should deny;
- the plugin writing into a reviewed repository's tracked content.

Out of scope: the agent choosing not to use the loop (committing before a review, ignoring a
finding, writing outside every root through Bash, editing the launch folder's config). Such reports
are welcome as issues if you think the design should change, but they are design discussions, not
vulnerabilities.
