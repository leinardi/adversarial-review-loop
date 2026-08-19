#!/usr/bin/env bash
# Stand-in reviewer used by the selftest, so the loop logic can be exercised
# without spending a model call. Behaviour is chosen with OCRL_FAKE_MODE.
#
# Invoked as: fake-reviewer.sh <bundle_dir> <prompt_file>

set -uo pipefail

bundle=${1:-}
mode=${OCRL_FAKE_MODE:-approve}

case "$mode" in
    approve)
        printf 'Read the whole diff. Nothing blocking.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    approve-with-nit)
        printf 'One taste-level remark, nothing that must change.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Could be named better\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    changes)
        printf 'The error path is wrong.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    approve-with-critical)
        # A reviewer that contradicts itself: the gate must recompute and block.
        printf 'Looks fine overall.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVE\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    critical-nonactionable)
        printf 'A worry I cannot pin down.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=no file=- | General unease about the design\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    malformed)
        printf 'I reviewed it and it seems fine to me.\n'
        ;;
    no-verdict)
        printf 'Review body.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Something\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    empty) ;;
    nonzero)
        printf 'boom\n' >&2
        exit 3
        ;;
    slow)
        sleep 30
        ;;
    many)
        printf 'Very many findings.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        for i in $(seq 1 "${OCRL_FAKE_COUNT:-6}"); do
            printf 'FINDING severity=medium actionable=yes file=a.txt:%s | Finding number %s\n' "$i" "$i"
        done
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    big-prose)
        # Prose far past max_reason_bytes; every FINDING line must still arrive.
        for i in $(seq 1 2000); do
            printf 'Prose line %s: padding that exists only to overflow the prose budget.\n' "$i"
        done
        printf '\n<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Must survive truncation\n'
        printf 'FINDING severity=critical actionable=yes file=b.txt:2 | Must also survive truncation\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    echo-bundle)
        printf 'Bundle contents:\n'
        ls -1 "$bundle"
        printf '\n<<<OCRL-FINDINGS>>>\nVERDICT APPROVED\n<<<OCRL-END>>>\n'
        ;;
    *)
        printf 'unknown OCRL_FAKE_MODE: %s\n' "$mode" >&2
        exit 2
        ;;
esac
