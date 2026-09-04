#!/usr/bin/env bash
#
# This file is part of adversarial-review-loop.
#
# Copyright (c) 2026 Roberto Leinardi
#
# adversarial-review-loop is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# adversarial-review-loop is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.
#

# adversarial-review-loop -- guarded shim over the Python implementation.
#
# scripts/arl.sh is the one path SKILL.md registers for every hook, so it
# must keep existing under that exact name even though the gate itself now
# lives in scripts/arl/ and runs under Python. This file's only job is:
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
# Each hook also runs under an outer watchdog, at a value below the timeout
# Claude Code itself enforces (see skills/implement/SKILL.md). If Python
# hangs, the watchdog kills it and returns 124 -- non-zero, so it lands in
# the same discard-and-fallback path as any other failure, and the fallback
# still reaches stdout before the host's own timeout would have torn the hook
# down with nothing.
#
# The watchdog is `timeout` where it exists, `gtimeout` where coreutils is
# installed under Homebrew's prefixed name, and otherwise a small perl
# supervisor -- macOS ships no timeout(1) at all, and without a fallback every
# hook there died with 127 and denied every tool call. All three kill the
# child's *process group*, so a descendant that outlives the gate is reaped
# too; see arl_watchdog_run for why the perl one uses no signals. If none of
# the three exists the hook fails closed by name, and `arm`/`resume` refuse up
# front (arm._check_reviewer) rather than arming into a gate that cannot run.
#
# Non-hook subcommands (arm, status, report, ...) are not part of this
# contract: they stream directly, and a missing interpreter or a crash there
# is an ordinary shell/CLI failure. Rule 0 already covers an `arm` that never
# ran -- the `intent` hook recorded that it was asked for, and the next hook
# call finds that marker with no session pointer and records ARM_FAILED itself.

set -uo pipefail

ARL_SELF=${BASH_SOURCE[0]}
# Parameter expansion rather than dirname(1): this file is re-executed on
# every tool call, so a fork here is a fork per hook.
ARL_SCRIPT_DIR=$(cd -- "${ARL_SELF%/*}" && pwd)
ARL_BOOTSTRAP="$ARL_SCRIPT_DIR/arl-bootstrap.py"
# Set by arl_watchdog_pick before any use; declared here so `set -u` has a
# definition even on the paths that never reach the picker.
ARL_WATCHDOG_IMPL=

# --------------------------------------------------------------------------
# Guarded hook run
# --------------------------------------------------------------------------

# Emits the fail-closed response for one hook event. $1 is the event's
# subcommand name, $2 a short, safe (no quotes, no untrusted input) detail
# string. Always exits 0: the JSON on stdout *is* the decision, not the exit
# code.
arl_hook_fallback() {
    local event=$1 detail=$2
    case "$event" in
        pretool)
            printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"adversarial-review-loop: the review gate process could not run (%s). Denying to fail closed. Run /adversarial-review-loop:status, then /adversarial-review-loop:stop if the mode is wedged."}}' \
                "$detail"
            ;;
        gate-stop)
            printf '{"decision":"block","reason":"adversarial-review-loop: the review gate process could not run (%s). The final review did not run. Run /adversarial-review-loop:status."}' \
                "$detail"
            ;;
        confirm-commit)
            printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"adversarial-review-loop: the review gate process could not run (%s). The commit was NOT confirmed against the approved tree, so this is not a verification. Run /adversarial-review-loop:status."}}' \
                "$detail"
            ;;
        posttool-failure)
            # Silent, matching the entrypoint's own behaviour: its only job is
            # clearing a pending approval, and a crash here leaves that pending
            # tree stale rather than granting anything.
            ;;
        intent)
            # Silent. UserPromptSubmit could block the prompt, but the shim cannot
            # tell an arming prompt from any other without parsing the payload, and
            # blocking every prompt because the interpreter is missing would wedge
            # the whole session. The loss is bounded: if the interpreter cannot run
            # here it cannot run for pretool either, whose fallback denies everything.
            ;;
        reorient)
            # Silent, and it must be. SessionStart reads a hook's plain stdout as
            # *context for Claude*, so there is no JSON decision to fall back to --
            # anything printed here would be injected into the session as though the
            # gate had said it. Failing to re-orient costs Claude a re-read of the
            # frozen plan; the gate itself is unaffected, because this hook grants
            # nothing and blocks nothing.
            ;;
    esac
    return 0
}

# Sets ARL_WATCHDOG_IMPL to the watchdog to run under, or returns non-zero if
# none of them exists. ARL_WATCHDOG selects one explicitly for tests; like
# ARL_SHIM_TIMEOUT_* it can never *disable* the watchdog -- an unset, unknown
# or unavailable value falls through to the same auto-detection, and there is
# no value meaning "none".
#
# The result is returned in a variable rather than printed: this runs on every
# tool call, and a command substitution would fork a subshell per hook for an
# answer built entirely from builtins.
arl_watchdog_pick() {
    local candidate
    case "${ARL_WATCHDOG:-}" in
        timeout | gtimeout | perl)
            if command -v "$ARL_WATCHDOG" >/dev/null 2>&1; then
                ARL_WATCHDOG_IMPL=$ARL_WATCHDOG
                return 0
            fi
            ;;
    esac
    for candidate in timeout gtimeout perl; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ARL_WATCHDOG_IMPL=$candidate
            return 0
        fi
    done
    return 1
}

# arl_watchdog_run <impl> <seconds> <argv...> -- run argv under a deadline,
# exiting 124 when it expires and 127 when the child cannot be started, which
# is what `timeout` reports and what arl_hook_run branches on.
arl_watchdog_run() {
    local impl=$1 secs=$2
    shift 2
    case "$impl" in
        timeout | gtimeout)
            ARL_HOOK_DEADLINE_SEC="$secs" "$impl" "$secs" "$@"
            ;;
        perl)
            # No signals anywhere in here, deliberately, and both halves of
            # that matter:
            #
            # A SIGALRM handler that kills races the reap -- the child can exit
            # just as the alarm arrives, leaving the handler to signal a pid it
            # no longer owns, which after recycling is an unrelated process.
            # reviewer._kill_group keeps its child unreaped through its grace
            # period for exactly this reason. The textbook fix, a handler that
            # only sets a flag, does not work in perl at all: safe signals
            # restart the interrupted waitpid, so the flag is set and the loop
            # never regains control (measured: an 8s deadline never fired).
            #
            # Polling waitpid(WNOHANG) against a wall clock needs neither. A
            # zero return *is* the proof the child is still ours to signal, so
            # the kill only ever happens on an iteration that just saw it
            # running, and the reap only after that.
            #
            # SIGKILL straight away, with no SIGTERM grace: the shim's
            # ceilings sit close under the host's own hook timeouts (intent 8s
            # under 10s, reorient 25s under 30s), so a grace period would push
            # those two past the point where Claude Code tears the hook down
            # with nothing. Nothing is lost by it -- python3 installs no
            # SIGTERM handler and dies just as abruptly under `timeout`.
            #
            # The environment scrubbing and the BEGIN block are the perl
            # equivalents of `python3 -I`, and they are needed for the same
            # reason: this runs with the repository under review as its cwd.
            # PERL5LIB and PERLLIB *prepend* to @INC, so either one shadows a
            # core module -- confirmed by experiment, a PERL5LIB-supplied
            # POSIX.pm loads in place of the real one -- and PERL5OPT can
            # inject `-M` outright. Perl before 5.26 also carries `.` in @INC,
            # which the repository itself controls. Clearing the variables
            # covers the first, and the BEGIN block runs before the `use`
            # statements it protects, which is what makes it effective rather
            # than decorative.
            PERL5LIB='' PERL5OPT='' PERLLIB='' ARL_HOOK_DEADLINE_SEC="$secs" perl -e '
                BEGIN { @INC = grep { !ref($_) && m{^/} } @INC }
                use POSIX qw(:sys_wait_h);
                use Time::HiRes qw(sleep);
                # A monotonic clock, or nothing: the deadline must survive the
                # wall clock moving, because an NTP step backwards stretches it
                # past the host'"'"'s own hook timeout and Claude Code then tears
                # the hook down before the fail-closed fallback can be written.
                # Falling back to wall time would leave that hole open while
                # looking like it works, so a platform without CLOCK_MONOTONIC
                # is refused here (125, the status `timeout` itself uses for "the
                # watchdog could not do its job") and named by the shim. arm and
                # resume probe the same capability up front.
                eval { Time::HiRes::clock_gettime(Time::HiRes::CLOCK_MONOTONIC()); 1 } or exit 125;
                my $now = sub { Time::HiRes::clock_gettime(Time::HiRes::CLOCK_MONOTONIC()) };
                my $secs = shift;
                my $pid = fork();
                defined $pid or exit 127;
                if ($pid == 0) { setpgrp(0, 0); exec { $ARGV[0] } @ARGV; exit 127; }
                # Both sides set the group: the deadline can arrive between the
                # fork and the child getting there itself, and killing a group
                # that does not exist yet would report success while the child
                # ran on. Redundant once the child has exec-ed (EACCES), which
                # is harmless.
                setpgrp($pid, $pid);
                my $deadline = $now->() + $secs;
                my $nap = 0.001;
                while (1) {
                    my $reaped = waitpid($pid, WNOHANG);
                    if ($reaped == $pid) {
                        my $status = $?;
                        exit($status & 127 ? 128 + ($status & 127) : $status >> 8);
                    }
                    exit 127 if $reaped < 0;
                    if ($now->() >= $deadline) {
                        kill("KILL", -$pid) or kill("KILL", $pid);
                        waitpid($pid, 0);
                        exit 124;
                    }
                    # Short naps first so a fast hook is not held up waiting to
                    # notice it finished, widening to a cap that costs nothing
                    # over a long review.
                    sleep($nap);
                    $nap = $nap * 2 if $nap < 0.02;
                }
            ' "$secs" "$@"
            ;;
    esac
}

# arl_hook_run <event> <timeout-seconds> <argv...>
arl_hook_run() {
    local event=$1 timeout_s=$2
    shift 2
    local out rc

    if ! command -v python3 >/dev/null 2>&1; then
        arl_hook_fallback "$event" 'python3 is not on PATH'
        return 0
    fi

    if ! arl_watchdog_pick; then
        # Naming the missing dependency, rather than letting it surface as the
        # 127 a bare `timeout` produced on every macOS install.
        arl_hook_fallback "$event" 'no timeout(1), gtimeout(1) or perl on PATH'
        return 0
    fi

    # Command substitution captures stdout only; stderr is inherited
    # untouched, exactly the split this contract needs.
    #
    # ARL_HOOK_DEADLINE_SEC is the same number `timeout` is given, handed to
    # the gate so it can tell how much of the hook's whole budget is left
    # before deciding to start further work (`reviewer.remaining_budget`).
    # Passed here rather than re-derived inside, so a shrunk test timeout
    # shrinks both together and the two can never disagree; the Python side
    # still clamps it to the same ceiling, since an environment variable is
    # not a trust boundary.
    out=$(arl_watchdog_run "$ARL_WATCHDOG_IMPL" "$timeout_s" python3 -I "$ARL_BOOTSTRAP" "$@")
    rc=$?

    if [ "$rc" -eq 0 ]; then
        printf '%s' "$out"
        return 0
    fi
    if [ "$rc" -eq 124 ]; then
        arl_hook_fallback "$event" "timed out after ${timeout_s}s"
    elif [ "$rc" -eq 125 ] && [ "$ARL_WATCHDOG_IMPL" = perl ]; then
        # The perl supervisor refuses to run on a wall clock; say which
        # capability is missing rather than leaving a bare status.
        arl_hook_fallback "$event" 'the perl watchdog has no CLOCK_MONOTONIC; install coreutils'
    else
        arl_hook_fallback "$event" "exited with status $rc"
    fi
    return 0
}

# arl_bounded_timeout <ceiling> <raw> -- the ceiling is the production value
# and a hard maximum, never a default to be loosened. A test may shrink a
# timeout, via the ARL_SHIM_TIMEOUT_* below, to prove the hang path resolves
# without waiting out the real, minutes-long default. Nothing may raise it or
# disable it: `timeout 0` means "no limit" to both GNU and uutils coreutils,
# so an override of `0` is exactly as dangerous as no timeout at all, and is
# rejected the same as any other value that is not a positive integer at
# most the ceiling.
arl_bounded_timeout() {
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
    pretool) arl_hook_run pretool "$(arl_bounded_timeout 1150 "${ARL_SHIM_TIMEOUT_PRETOOL:-}")" "$@" ;;
    confirm-commit) arl_hook_run confirm-commit "$(arl_bounded_timeout 50 "${ARL_SHIM_TIMEOUT_CONFIRM_COMMIT:-}")" "$@" ;;
    posttool-failure) arl_hook_run posttool-failure "$(arl_bounded_timeout 20 "${ARL_SHIM_TIMEOUT_POSTTOOL_FAILURE:-}")" "$@" ;;
    gate-stop) arl_hook_run gate-stop "$(arl_bounded_timeout 1750 "${ARL_SHIM_TIMEOUT_GATE_STOP:-}")" "$@" ;;
    reorient) arl_hook_run reorient "$(arl_bounded_timeout 25 "${ARL_SHIM_TIMEOUT_REORIENT:-}")" "$@" ;;
    intent) arl_hook_run intent "$(arl_bounded_timeout 8 "${ARL_SHIM_TIMEOUT_INTENT:-}")" "$@" ;;
    *)
        if ! command -v python3 >/dev/null 2>&1; then
            printf 'adversarial-review-loop: python3 is not on PATH.\n' >&2
            exit 127
        fi
        exec python3 -I "$ARL_BOOTSTRAP" "$@"
        ;;
esac
