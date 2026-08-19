---
name: report
description: Print a stored OpenCode review report in full, including every finding and the raw reviewer output.
argument-hint: "[report-number]"
user-invocable: true
---

# Stored review report

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh report "$1"`

The report above is printed in full — nothing is truncated here, unlike the summary attached to a denial.

Point the user at what matters in it: the blocking findings first, then anything the reviewer raised that did not block. If they ask you to act on it, the normal review gate still applies to any fix.
