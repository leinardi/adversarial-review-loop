---
name: report
description: Print a stored review report in full, including every finding and the raw reviewer output.
argument-hint: "[report-number]"
user-invocable: true
allowed-tools: "Bash(${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh report:*)"
---

# Stored review report

```!
${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh report --args-stdin <<'ARL-ARGUMENTS-EOF'
$ARGUMENTS
ARL-ARGUMENTS-EOF
```

The report above is printed in full — nothing is truncated here, unlike the summary attached to a denial.

Point the user at what matters in it: the blocking findings first, then anything the reviewer raised that did not block. If they ask you to act on it, the normal review gate still applies to any fix.
