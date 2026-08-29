#!/usr/bin/env bash
# opencode-review-loop -- guarded shim over the Python implementation.
#
# scripts/ocrl.sh is the one path SKILL.md registers for every hook, so it
# must keep existing under that exact name even though the gate itself now
# lives in scripts/ocrl/ and runs under Python. This file's only job is:
#
#   probe the interpreter, run it, and fail closed if either step fails.
#
# For PreToolUse, Claude Code treats exit 2 as blocking and any *other*
# non-zero exit as a non-blocking error -- the tool call proceeds. A naive
# `exec python3 ...` fails open on a missing interpreter (exit 127, no
# output) and on an uncaught exception (exit 1, no output). So the four hook
# entrypoints below never `exec`: they run the interpreter, capture what it
# wrote to stdout, and only forward it verbatim when the process exited 0.
# Any other outcome discards whatever was captured and emits that event's
# own fail-closed response here, with printf -- jq is not a runtime
# dependency of the Python port.
#
# The discriminator is exit status, never empty stdout: a silent pass-through
# is the commonest hot-path outcome and legitimately empty. Capture goes to a
# shell variable, not a temp file -- hook responses are small, and a temp
# file would be one more thing to place outside the repository under review.
#
# Each hook also runs under `timeout`, at a value below the timeout Claude
# Code itself enforces (see skills/implement/SKILL.md). If Python hangs,
# `timeout` kills it and returns 124 -- non-zero, so it lands in the same
# discard-and-fallback path as any other failure, and the fallback still
# reaches stdout before the host's own timeout would have torn the hook down
# with nothing.
#
# Non-hook subcommands (arm, status, report, ...) are not part of this
# contract: they stream directly, and a missing interpreter or a crash there
# is an ordinary shell/CLI failure. Rule 0 already covers an `arm` that never
# ran -- the next hook call finds no session pointer and records ARM_FAILED
# itself.

set -uo pipefail

OCRL_SELF=${BASH_SOURCE[0]}
# Parameter expansion rather than dirname(1): this file is re-executed on
# every tool call, so a fork here is a fork per hook.
OCRL_SCRIPT_DIR=$(cd -- "${OCRL_SELF%/*}" && pwd)
OCRL_BOOTSTRAP="$OCRL_SCRIPT_DIR/ocrl-bootstrap.py"

# --------------------------------------------------------------------------
# Guarded hook run
# --------------------------------------------------------------------------

# Emits the fail-closed response for one hook event. $1 is the event's
# subcommand name, $2 a short, safe (no quotes, no untrusted input) detail
# string. Always exits 0: the JSON on stdout *is* the decision, not the exit
# code.
ocrl_hook_fallback() {
    local event=$1 detail=$2
    case "$event" in
        pretool)
            printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"opencode-review-loop: the review gate process could not run (%s). Denying to fail closed. Run /opencode-review-loop:status, then /opencode-review-loop:stop if the mode is wedged."}}' \
                "$detail"
            ;;
        gate-stop)
            printf '{"decision":"block","reason":"opencode-review-loop: the review gate process could not run (%s). The final review did not run. Run /opencode-review-loop:status."}' \
                "$detail"
            ;;
        confirm-commit)
            printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"opencode-review-loop: the review gate process could not run (%s). The commit was NOT confirmed against the approved tree, so this is not a verification. Run /opencode-review-loop:status."}}' \
                "$detail"
            ;;
        posttool-failure)
            # Silent, matching the entrypoint's own behaviour: its only job is
            # clearing a pending approval, and a crash here leaves that pending
            # tree stale rather than granting anything.
            ;;
    esac
    return 0
}

# ocrl_hook_run <event> <timeout-seconds> <argv...>
ocrl_hook_run() {
    local event=$1 timeout_s=$2
    shift 2
    local out rc

    if ! command -v python3 >/dev/null 2>&1; then
        ocrl_hook_fallback "$event" 'python3 is not on PATH'
        return 0
    fi

    # Command substitution captures stdout only; stderr is inherited
    # untouched, exactly the split this contract needs.
    #
    # OCRL_HOOK_DEADLINE_SEC is the same number `timeout` is given, handed to
    # the gate so it can tell how much of the hook's whole budget is left
    # before deciding to start further work (`reviewer.remaining_budget`).
    # Passed here rather than re-derived inside, so a shrunk test timeout
    # shrinks both together and the two can never disagree; the Python side
    # still clamps it to the same ceiling, since an environment variable is
    # not a trust boundary.
    out=$(OCRL_HOOK_DEADLINE_SEC="$timeout_s" timeout "$timeout_s" python3 -I "$OCRL_BOOTSTRAP" "$@")
    rc=$?

    if [ "$rc" -eq 0 ]; then
        printf '%s' "$out"
        return 0
    fi
    if [ "$rc" -eq 124 ]; then
        ocrl_hook_fallback "$event" "timed out after ${timeout_s}s"
    else
        ocrl_hook_fallback "$event" "exited with status $rc"
    fi
    return 0
}

# ocrl_bounded_timeout <ceiling> <raw> -- the ceiling is the production value
# and a hard maximum, never a default to be loosened. A test may shrink a
# timeout, via the OCRL_SHIM_TIMEOUT_* below, to prove the hang path resolves
# without waiting out the real, minutes-long default. Nothing may raise it or
# disable it: `timeout 0` means "no limit" to both GNU and uutils coreutils,
# so an override of `0` is exactly as dangerous as no timeout at all, and is
# rejected the same as any other value that is not a positive integer at
# most the ceiling.
ocrl_bounded_timeout() {
    local ceiling=$1 raw=${2:-}
    case "$raw" in
        '' | *[!0-9]*)
            printf '%s' "$ceiling"
            return
            ;;
    esac
    # Reject absurd lengths before anything else touches this string: `[
    # -gt ]` errors out ("integer expected", exit 2) on a value too large
    # for bash's integer type, `if` treats that non-zero exit as false the
    # same as a real "no", and a later branch would then silently forward
    # the oversized, unclamped value to `timeout`. Ten digits is generous
    # headroom over every ceiling here (at most four) and still nowhere
    # near where integer comparison stops being safe.
    if [ "${#raw}" -gt 10 ]; then
        printf '%s' "$ceiling"
        return
    fi
    # Strip leading zeros so length reflects magnitude, not spelling: "0",
    # "00" and "0000" all mean zero to `timeout` -- GNU and uutils coreutils
    # both read `timeout 0 …` (and every other-zeros spelling of it) as "no
    # limit" -- and a bare `= 0` check catches only the one canonical form.
    while [ "${#raw}" -gt 1 ] && [ "${raw:0:1}" = 0 ]; do
        raw=${raw#0}
    done
    if [ "$raw" = 0 ]; then
        printf '%s' "$ceiling"
        return
    fi
    # A digit string longer than the ceiling's own is unconditionally bigger
    # than it, checked by length before `-gt` ever sees it -- both operands
    # are now at most ten digits, safely inside bash's integer range.
    if [ "${#raw}" -gt "${#ceiling}" ] || [ "$raw" -gt "$ceiling" ]; then
        printf '%s' "$ceiling"
    else
        printf '%s' "$raw"
    fi
}

# --------------------------------------------------------------------------

# Timeouts are below the values registered in skills/implement/SKILL.md, so
# the fallback above has time to reach stdout before Claude Code's own
# timeout would tear the hook down with nothing.
case "${1:-}" in
    pretool) ocrl_hook_run pretool "$(ocrl_bounded_timeout 1150 "${OCRL_SHIM_TIMEOUT_PRETOOL:-}")" "$@" ;;
    confirm-commit) ocrl_hook_run confirm-commit "$(ocrl_bounded_timeout 50 "${OCRL_SHIM_TIMEOUT_CONFIRM_COMMIT:-}")" "$@" ;;
    posttool-failure) ocrl_hook_run posttool-failure "$(ocrl_bounded_timeout 20 "${OCRL_SHIM_TIMEOUT_POSTTOOL_FAILURE:-}")" "$@" ;;
    gate-stop) ocrl_hook_run gate-stop "$(ocrl_bounded_timeout 1750 "${OCRL_SHIM_TIMEOUT_GATE_STOP:-}")" "$@" ;;
    *)
        if ! command -v python3 >/dev/null 2>&1; then
            printf 'opencode-review-loop: python3 is not on PATH.\n' >&2
            exit 127
        fi
        exec python3 -I "$OCRL_BOOTSTRAP" "$@"
        ;;
esac
