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

# adversarial-review-loop selftest: the shim, and only the shim.
#
# tests/unit/ drives the gate's behaviour end to end through the same bootstrap
# this script does, so this suite no longer re-tests any of it. What it keeps is
# the layer pytest structurally cannot reach, because pytest runs *inside* the
# Python this layer exists to launch and bound:
#
#   - the interpreter probe: what happens when there is no usable python3;
#   - the shim contract: a partial answer, a hung interpreter, and the timeout
#     environment variables that must not be able to loosen either;
#   - the watchdog layers: timeout, then gtimeout, then the perl fallback, plus
#     the perl layer's own isolation and its CLOCK_MONOTONIC requirement;
#   - the hook input channel: a payload arriving on a socket rather than a pipe;
#   - the hot path's process budget, measured with strace;
#   - one bootstrap smoke test (arm -> set-phases -> first edit) proving the shim
#     and the gate compose at all.
#
# Runs against scratch repositories under $TMPDIR, with the reviewer replaced by
# tests/fixtures/fake-reviewer.sh. No model is called and nothing outside the
# scratch directories is touched.
#
# usage: tests/selftest.sh [name-filter]

set -uo pipefail

TESTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_ROOT=$(dirname -- "$TESTS_DIR")
ARL="$PLUGIN_ROOT/scripts/arl.sh"
FAKE="$TESTS_DIR/fixtures/fake-reviewer.sh"
FILTER=${1:-}

export CLAUDE_PLUGIN_ROOT=$PLUGIN_ROOT
export ARL_REVIEWER_CMD=$FAKE

# jq is a test-only dependency -- the gate itself parses JSON in-process (see the note at the
# top of scripts/arl.sh) -- but this suite reads every hook response with it. Say so once, here,
# rather than as dozens of empty comparisons that look like real failures.
if ! command -v jq >/dev/null 2>&1; then
    printf 'tests/selftest.sh needs jq (a test-only dependency; the gate itself does not use it).\n' >&2
    printf 'Install it: "brew install jq" on macOS, "apt install jq" on Debian/Ubuntu.\n' >&2
    exit 1
fi

PASS=0
FAIL=0
CURRENT=''
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/arl-selftest.XXXXXX")
# Resolved to its physical path, because the state directory is addressed by the *hash* of the
# worktree path: macOS puts TMPDIR behind a symlink (/tmp -> /private/tmp), so git and the gate
# would report the resolved path while these helpers hashed the symlinked one, and every lookup
# would land in a directory that does not exist.
ROOT=$(cd "$ROOT" && pwd -P)
trap 'rm -rf "$ROOT"' EXIT

# Run only every Nth section, offset by I: ARL_SELFTEST_SHARD=I/N. Sections share nothing
# -- each `new_case` builds its own repository under its own $ROOT and its own
# ARL_STATE_DIR -- so splitting them across processes is a scheduling decision and not a
# semantic one. tests/selftest-parallel.sh is what sets this; running the script by hand
# without it executes everything, in order, exactly as before.
SHARD=${ARL_SELFTEST_SHARD:-}
SHARD_INDEX=${SHARD%%/*}
SHARD_TOTAL=${SHARD##*/}
SECTION_N=0

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

start() {
    CURRENT=$1
    SECTION_N=$((SECTION_N + 1))
    # Counted before the filter so a shard's membership never depends on the filter.
    if [ -n "$SHARD" ] && [ $(((SECTION_N - 1) % SHARD_TOTAL)) -ne "$SHARD_INDEX" ]; then
        return 1
    fi
    if [ -n "$FILTER" ] && [[ $CURRENT != *"$FILTER"* ]]; then
        return 1
    fi
    printf '\n\033[1m== %s\033[0m\n' "$CURRENT"
    return 0
}

ok() {
    PASS=$((PASS + 1))
    printf '  \033[32mok\033[0m   %s\n' "$1"
}

bad() {
    FAIL=$((FAIL + 1))
    printf '  \033[31mFAIL\033[0m %s\n' "$1"
    [ -n "${2:-}" ] && printf '       got: %s\n' "$2"
    [ -n "${3:-}" ] && printf '       want: %s\n' "$3"
}

assert_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$2" "$3"; fi
}

assert_contains() {
    if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "${2:0:400}" "contains: $3"; fi
}

# --------------------------------------------------------------------------
# Scratch repositories
# --------------------------------------------------------------------------

CASE_N=0
new_case() {
    CASE_N=$((CASE_N + 1))
    CASE_DIR="$ROOT/case-$CASE_N"
    REPO="$CASE_DIR/repo"
    export ARL_STATE_DIR="$CASE_DIR/state"
    mkdir -p "$REPO"
    git -C "$REPO" init -q -b main
    git -C "$REPO" config user.email selftest@example.invalid
    git -C "$REPO" config user.name 'arl selftest'
    git -C "$REPO" config commit.gpgsign false
    printf 'seed\n' >"$REPO/seed.txt"
    git -C "$REPO" add -A
    git -C "$REPO" commit -qm 'seed'
    PLAN="$CASE_DIR/plan.md"
    printf '# Plan\n\nDo the thing, then do the other thing.\n' >"$PLAN"
    SESSION="sess-$CASE_N"
}

arl() { (cd "$REPO" && "$ARL" "$@"); }

# pre <tool> [command] -- runs the PreToolUse dispatcher, prints the decision.
# The helpers run inside command substitution, so the last hook payload is
# kept in a file rather than a variable: a subshell assignment would be lost.
last_out() { cat "$ROOT/last.json" 2>/dev/null; }

pre() { pre_at "$REPO" "$@"; }

pre_reason() {
    last_out | jq -r '.hookSpecificOutput.permissionDecisionReason // ""'
}

# pre_at <cwd> <tool> [command]
pre_at() {
    local at=$1 tool=$2 cmd=${3:-} out
    out=$(jq -nc --arg s "$SESSION" --arg c "$at" --arg t "$tool" --arg cmd "$cmd" \
        '{session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:$t,tool_input:{command:$cmd}}' |
        (cd "$at" && "$ARL" pretool))
    printf '%s' "$out" >"$ROOT/last.json"
    if [ -z "$out" ]; then
        printf 'pass'
    else
        printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"'
    fi
}

stop_gate() {
    local out
    out=$(jq -nc --arg s "$SESSION" --arg c "$REPO" \
        '{session_id:$s,cwd:$c,hook_event_name:"Stop",stop_hook_active:false}' |
        (cd "$REPO" && "$ARL" gate-stop))
    printf '%s' "$out" >"$ROOT/last.json"
    printf '%s' "$out"
}

stop_decision() {
    local out
    out=$(stop_gate)
    if [ -z "$out" ]; then printf 'ok'; else printf '%s' "$out" | jq -r '.decision // "ok"'; fi
}

state_file() {
    find "$ARL_STATE_DIR/worktrees" -name state.json | head -n 1
}

# Mirrors arl_get, including its avoidance of `//` -- in jq `false // ""` is
# "", which would blank every boolean field being asserted on.
sget() {
    jq -r --arg k "$1" '
        if has($k) and .[$k] != null
        then (.[$k] | if type == "string" then . else tostring end)
        else "" end' "$(state_file)"
}

# BSD `wc` (macOS) right-aligns its count in a field of blanks -- "       0" rather than "0" --
# which every assert_eq against a bare number would read as a mismatch. The count is what these
# callers want, not its spelling.
count_lines() {
    wc -l | tr -d '[:space:]'
}

arm_ok() {
    arl arm --session "$SESSION" --plan "$PLAN" >/dev/null 2>&1
}

phases_ok() {
    arl set-phases --phase 'Phase one: the thing' --phase 'Phase two: the other thing' >/dev/null 2>&1
}

# --------------------------------------------------------------------------
# The gate, launched through the shim
# --------------------------------------------------------------------------
#
# Two smoke cases, not a behaviour suite. The hook input channel and one
# arm -> set-phases -> first edit walk are here because they are the only
# assertions in this file that need the gate to actually work; everything the
# gate then decides is pytest's, driven through the same bootstrap.

if start 'hook input: the payload arrives on a socket, not a pipe'; then
    # Claude Code gives hooks their stdin as a socket. `$(</dev/stdin)` reads a
    # pipe fine and fails with ENXIO on a socket, which emptied every field and
    # made the gate read its own armed session as unarmed -- while still
    # denying, so it surfaced as a mystery rather than a breach.
    new_case
    arm_ok && phases_ok
    if command -v python3 >/dev/null 2>&1; then
        jq -nc --arg s "$SESSION" --arg c "$REPO" \
            '{session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Edit",tool_input:{file_path:"a.txt"}}' \
            >"$CASE_DIR/payload.json"
        sock_out=$(cd "$REPO" && python3 "$TESTS_DIR/fixtures/socket-stdin.py" \
            "$CASE_DIR/payload.json" "$ARL" pretool 2>/dev/null)
        # Phases are frozen, so an Edit is allowed: reaching that verdict at all
        # proves the payload was read.
        if [ -z "$sock_out" ]; then
            ok 'a socket payload is read (Edit passes in ACTIVE state)'
        else
            bad 'socket payload not read' "$sock_out" 'empty output (pass-through)'
        fi

        # And the fields really did parse: an unshaped commit must be refused
        # by name, which is only possible if tool_name and command arrived.
        jq -nc --arg s "$SESSION" --arg c "$REPO" \
            '{session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"git commit --amend -m x"}}' \
            >"$CASE_DIR/payload2.json"
        sock_out=$(cd "$REPO" && python3 "$TESTS_DIR/fixtures/socket-stdin.py" \
            "$CASE_DIR/payload2.json" "$ARL" pretool 2>/dev/null)
        assert_eq 'a socket payload parses tool_name and command' \
            "$(printf '%s' "$sock_out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        assert_contains 'and reaches the real classifier' \
            "$(printf '%s' "$sock_out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'amend'
    else
        ok 'socket-stdin check skipped (python3 unavailable)'
    fi
fi

if start 'bootstrap: arm -> set-phases -> first edit, with no deadlock'; then
    new_case
    arm_ok
    assert_eq 'armed' "$(sget status)" 'ARMED'
    assert_eq 'Edit denied before the phases are frozen' "$(pre Edit)" 'deny'
    assert_contains 'the denial carries the exact command' "$(pre_reason)" 'set-phases --phase'
    assert_eq 'Read allowed before the phases are frozen' "$(pre Read)" 'pass'
    assert_eq 'Grep allowed' "$(pre Grep)" 'pass'
    assert_eq 'an arbitrary Bash call is denied' "$(pre Bash 'ls')" 'deny'
    assert_eq 'the set-phases command itself is allowed' \
        "$(pre Bash "$ARL set-phases --phase 'a'")" 'allow'
    assert_eq 'ending the turn here blocks' "$(stop_decision)" 'block'

    phases_ok
    assert_eq 'active after set-phases' "$(sget status)" 'ACTIVE'
    assert_eq 'on phase 1' "$(sget phase)" '1'
    assert_eq 'Edit is allowed once the phases are frozen' "$(pre Edit)" 'pass'
    assert_eq 'ordinary Bash is allowed too' "$(pre Bash 'make test')" 'pass'

    out=$(arl set-phases --phase 'x' 2>&1)
    assert_contains 'the phase list cannot be re-frozen' "$out" 'already frozen'
fi

# --------------------------------------------------------------------------
# The shim: the interpreter, its contract, and the watchdog
# --------------------------------------------------------------------------

if start 'hot path: a read-only tool answers without loading config or state'; then
    new_case
    arm_ok && phases_ok
    if command -v strace >/dev/null 2>&1 &&
        strace -f -e trace=execve -o /dev/null true >/dev/null 2>&1; then
        trace_procs() {
            local tool=$1 out
            out="$CASE_DIR/trace-$tool.txt"
            jq -nc --arg s "$SESSION" --arg c "$REPO" --arg t "$tool" \
                '{session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:$t,tool_input:{}}' |
                (cd "$REPO" && strace -f -e trace=execve -o "$out" "$ARL" pretool >/dev/null 2>&1)
            # Only successful execs count; the shebang's PATH probe fails cheaply.
            grep -c 'execve.*= 0' "$out" 2>/dev/null || printf '0'
        }
        read_procs=$(trace_procs Read)
        edit_procs=$(trace_procs Edit)

        # The dispatcher runs on every tool call, so this is a real budget, not
        # a style preference. See docs/design/interpreter-and-watchdog.md.
        #
        # Under the shell implementation, a mutating tool legitimately forked
        # more processes than a read-only one: config and state were loaded
        # through jq. Under Python, loading them is in-process file I/O with
        # no forking either way, so the hoist's saving is no longer visible as
        # a process-count difference -- both tools share the same two-process
        # floor (`timeout` + `python3`). What the budget still proves is that
        # neither path shells out per field the way jq did.
        if [ "$read_procs" -le 5 ]; then
            ok "a read-only tool costs $read_procs processes (budget 5)"
        else
            bad 'read-only tool process budget' "$read_procs processes" 'at most 5'
        fi
        if [ "$edit_procs" -le 5 ]; then
            ok "a mutating tool also stays within budget ($edit_procs processes)"
        else
            bad 'mutating tool process budget' "$edit_procs processes" 'at most 5'
        fi
    else
        ok 'process-budget guard skipped (strace unavailable or not permitted)'
    fi
fi

# --------------------------------------------------------------------------
# Interpreter probe
# --------------------------------------------------------------------------

# hook_payload <event> [cmd] -- the raw JSON stdin for one hook invocation,
# shared by every case in this section so each one only has to say which
# event and, for Bash-shaped ones, which command.
hook_payload() {
    case "$1" in
        PreToolUse)
            jq -nc --arg s "$SESSION" --arg c "$REPO" \
                '{session_id:$s,cwd:$c,hook_event_name:"PreToolUse",tool_name:"Edit",tool_input:{file_path:"a.txt"}}'
            ;;
        PostToolUse)
            jq -nc --arg s "$SESSION" --arg c "$REPO" --arg cmd "${2:-git add -A && git commit -m x}" \
                '{session_id:$s,cwd:$c,hook_event_name:"PostToolUse",tool_name:"Bash",tool_input:{command:$cmd},tool_response:{exit_code:0}}'
            ;;
        PostToolUseFailure)
            jq -nc --arg s "$SESSION" --arg c "$REPO" --arg cmd "${2:-git add -A && git commit -m x}" \
                '{session_id:$s,cwd:$c,hook_event_name:"PostToolUseFailure",tool_name:"Bash",tool_input:{command:$cmd}}'
            ;;
        Stop)
            jq -nc --arg s "$SESSION" --arg c "$REPO" \
                '{session_id:$s,cwd:$c,hook_event_name:"Stop",stop_hook_active:false}'
            ;;
    esac
    # Always 0, even when the write above died of EPIPE. Every fail-closed path this
    # section exists to test -- a missing python3, no watchdog, an interpreter that exits
    # without reading -- has the shim answer and exit *without draining stdin*, so this
    # producer is racing a reader that may already be gone. Losing that race is not a
    # finding about anything: it is jq reporting that nobody was listening. But the callers
    # read `$?` of the whole pipeline under `set -o pipefail`, so jq's status would be
    # handed to an `assert_eq` whose subject is the shim's -- which is exactly the two
    # "the shim itself exits 0 / got: 2" failures CI run 34153953264 produced and no local
    # run reproduced. A genuine jq failure is still caught, one assertion later: the payload
    # is then empty, and the hook it feeds says so.
    return 0
}

if start 'interpreter probe: a missing python3 fails closed, not open, on every hook'; then
    new_case
    arm_ok && phases_ok

    # A curated PATH holding every binary the shim and git need, but no
    # python3. Stripping PATH down to nothing would also hide bash itself,
    # since the shim's own `#!/usr/bin/env bash` shebang resolves through
    # this same PATH.
    nopy="$CASE_DIR/no-python-path"
    mkdir -p "$nopy"
    # A watchdog is included for the same reason git is: without one the shim would deny for
    # that reason instead, and this case is about the interpreter.
    for b in bash sh cat printf timeout gtimeout perl env grep sed cut git mktemp true false ls find head tr; do
        p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$nopy/$b"
    done

    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$nopy" "$ARL" pretool))
    rc=$?
    assert_eq 'pretool: the shim itself exits 0, not 127' "$rc" '0'
    assert_eq 'pretool denies rather than failing open' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
    assert_contains 'and names the missing interpreter' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'python3'

    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$nopy" "$ARL" confirm-commit))
    rc=$?
    assert_eq 'confirm-commit: the shim itself exits 0' "$rc" '0'
    assert_contains 'and reports the failure rather than staying silent' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'python3'

    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$nopy" "$ARL" posttool-failure))
    rc=$?
    assert_eq 'posttool-failure: the shim itself exits 0' "$rc" '0'
    assert_eq 'and stays silent, matching its ordinary behaviour' "$out" ''

    out=$(hook_payload Stop | (cd "$REPO" && PATH="$nopy" "$ARL" gate-stop))
    rc=$?
    assert_eq 'gate-stop: the shim itself exits 0' "$rc" '0'
    assert_eq 'and blocks the turn rather than letting it end' \
        "$(printf '%s' "$out" | jq -r '.decision // "ok"')" 'block'
fi

if start 'shim contract: partial output + non-zero exit is discarded on every hook'; then
    new_case
    arm_ok && phases_ok

    # Simulates the interpreter dying mid-write: a fragment of a plausible
    # PreToolUse response reaches stdout, then a non-zero exit. The shim must
    # discard this outright -- forwarding it would concatenate a fail-closed
    # fallback onto real bytes and produce unparseable JSON, which for
    # PreToolUse is not a denial.
    fakepy="$CASE_DIR/fake-python"
    mkdir -p "$fakepy"
    cat >"$fakepy/python3" <<'PYEOF'
#!/usr/bin/env bash
printf '{"hookSpecificOutput":{"hookEventName":"PreTo'
exit 1
PYEOF
    chmod +x "$fakepy/python3"

    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$fakepy:$PATH" "$ARL" pretool))
    assert_eq 'pretool: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | count_lines)" '1'
    assert_eq 'and it denies' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'

    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$fakepy:$PATH" "$ARL" confirm-commit))
    assert_eq 'confirm-commit: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | count_lines)" '1'
    assert_contains 'and it reports the failure' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'could not run'

    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$fakepy:$PATH" "$ARL" posttool-failure))
    assert_eq 'posttool-failure: exactly zero bytes, not the fragment' "$out" ''

    out=$(hook_payload Stop | (cd "$REPO" && PATH="$fakepy:$PATH" "$ARL" gate-stop))
    assert_eq 'gate-stop: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | count_lines)" '1'
    assert_eq 'and it blocks' \
        "$(printf '%s' "$out" | jq -r '.decision // "ok"')" 'block'
fi

if start 'shim contract: a hung interpreter still denies, before the host timeout'; then
    new_case
    arm_ok && phases_ok

    hangpy="$CASE_DIR/hang-python"
    mkdir -p "$hangpy"
    cat >"$hangpy/python3" <<'PYEOF'
#!/usr/bin/env bash
sleep 300
PYEOF
    chmod +x "$hangpy/python3"

    # Each event's timeout is overridden to 1s so this proves the mechanism
    # -- `timeout` returning 124 and the shim treating that as any other
    # failure -- without waiting out the real, minutes-long default.
    t0=$(date +%s)
    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$hangpy:$PATH" ARL_SHIM_TIMEOUT_PRETOOL=1 "$ARL" pretool))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'pretool: a hung parser still denies' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
    assert_contains 'and names the timeout' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'timed out'
    if [ "$elapsed" -le 15 ]; then ok "pretool returned in ${elapsed}s, not after a real hook timeout"; else
        bad 'pretool returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$hangpy:$PATH" ARL_SHIM_TIMEOUT_CONFIRM_COMMIT=1 "$ARL" confirm-commit))
    elapsed=$(($(date +%s) - t0))
    assert_contains 'confirm-commit: reports the timeout rather than hanging' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'timed out'
    if [ "$elapsed" -le 15 ]; then ok "confirm-commit returned in ${elapsed}s"; else
        bad 'confirm-commit returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$hangpy:$PATH" ARL_SHIM_TIMEOUT_POSTTOOL_FAILURE=1 "$ARL" posttool-failure))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'posttool-failure: stays silent rather than hanging' "$out" ''
    if [ "$elapsed" -le 15 ]; then ok "posttool-failure returned in ${elapsed}s"; else
        bad 'posttool-failure returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload Stop | (cd "$REPO" && PATH="$hangpy:$PATH" ARL_SHIM_TIMEOUT_GATE_STOP=1 "$ARL" gate-stop))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'gate-stop: a hung parser still blocks' \
        "$(printf '%s' "$out" | jq -r '.decision // "ok"')" 'block'
    if [ "$elapsed" -le 15 ]; then ok "gate-stop returned in ${elapsed}s"; else
        bad 'gate-stop returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi
fi

if start 'shim contract: ARL_SHIM_TIMEOUT_* cannot loosen or disable the timeout'; then
    new_case
    arm_ok && phases_ok

    # A spy, not a stub: it records the duration the shim actually asked
    # `timeout` for, then runs the real command so the hook still completes.
    # `timeout 0` means "no limit" to both GNU and uutils coreutils, so an
    # override of 0 would be exactly as dangerous as removing the wrapper
    # entirely -- this is the regression the clamp in arl_bounded_timeout
    # exists to prevent.
    faketimeout="$CASE_DIR/fake-timeout-bin"
    mkdir -p "$faketimeout"
    cat >"$faketimeout/timeout" <<'PYEOF'
#!/usr/bin/env bash
printf '%s' "$1" >"$ARL_TEST_TIMEOUT_LOG"
shift
exec "$@"
PYEOF
    chmod +x "$faketimeout/timeout"

    logged() {
        local value=$1
        rm -f "$CASE_DIR/log.txt"
        hook_payload PreToolUse |
            (cd "$REPO" && PATH="$faketimeout:$PATH" ARL_TEST_TIMEOUT_LOG="$CASE_DIR/log.txt" ARL_SHIM_TIMEOUT_PRETOOL="$value" "$ARL" pretool) >/dev/null
        cat "$CASE_DIR/log.txt" 2>/dev/null
    }

    assert_eq '0 does not disable the timeout' "$(logged 0)" '1150'
    assert_eq 'a negative value is rejected' "$(logged -5)" '1150'
    assert_eq 'garbage is rejected' "$(logged nope)" '1150'
    assert_eq 'a value above the ceiling is clamped down, never up' "$(logged 999999)" '1150'
    assert_eq 'a value at the ceiling passes through' "$(logged 1150)" '1150'
    assert_eq 'a value below the ceiling passes through -- what the hang tests above rely on' "$(logged 5)" '5'

    # A digit string too large for bash's integer type makes `[ -gt ]` error
    # out rather than compare, and that error must not be read as "not
    # bigger than the ceiling" -- length is checked first, precisely to
    # avoid ever handing a value like this to `-gt` at all.
    assert_eq 'an oversized digit string is clamped, not forwarded unclamped' \
        "$(logged 999999999999999999999999999999999999)" '1150'

    # `timeout 00 …` and `timeout 0000 …` mean "no limit" exactly like
    # `timeout 0 …` does, to both GNU and uutils coreutils -- a bare
    # string-equality check against "0" would miss both.
    assert_eq '00 does not disable the timeout' "$(logged 00)" '1150'
    assert_eq '0000 does not disable the timeout' "$(logged 0000)" '1150'
    assert_eq 'a legitimate value with a leading zero still passes through' "$(logged 0005)" '5'
fi

if start 'watchdog: the shim resolves timeout, then gtimeout, then perl'; then
    new_case
    arm_ok && phases_ok

    # Resolution order is asserted with no ARL_WATCHDOG anywhere: setting it would bypass the
    # very `command -v` ordering under test, so a regression that reversed the order or dropped
    # a layer would still pass. The layers are selected by what is on PATH instead.
    wdbase="$CASE_DIR/wd-base"
    mkdir -p "$wdbase"
    for b in bash sh cat printf env grep sed cut git mktemp true false ls find head tr date python3; do
        p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$wdbase/$b"
    done

    # Spies, not stubs: each records that it was the one chosen, then runs the real command so
    # the hook still completes and produces a real decision.
    wdspy="$CASE_DIR/wd-spies"
    mkdir -p "$wdspy"
    for name in timeout gtimeout; do
        cat >"$wdspy/$name" <<EOF
#!/usr/bin/env bash
printf '%s' '$name' >"\$ARL_TEST_WATCHDOG_LOG"
shift
exec "\$@"
EOF
        chmod +x "$wdspy/$name"
    done
    # The perl layer is invoked as: perl -e <script> <seconds> <argv...>
    cat >"$wdspy/perl" <<'EOF'
#!/usr/bin/env bash
printf 'perl' >"$ARL_TEST_WATCHDOG_LOG"
shift 3
exec "$@"
EOF
    chmod +x "$wdspy/perl"

    picked() {
        local dir name
        dir=$(mktemp -d "$CASE_DIR/wd.XXXXXX")
        ln -sf "$wdbase"/* "$dir/"
        for name in "$@"; do ln -sf "$wdspy/$name" "$dir/$name"; done
        rm -f "$CASE_DIR/wd.txt"
        hook_payload PreToolUse |
            (cd "$REPO" && PATH="$dir" ARL_TEST_WATCHDOG_LOG="$CASE_DIR/wd.txt" "$ARL" pretool) >/dev/null
        cat "$CASE_DIR/wd.txt" 2>/dev/null
    }

    assert_eq 'timeout wins when every layer is available' "$(picked timeout gtimeout perl)" 'timeout'
    assert_eq 'gtimeout is next, for a Homebrew coreutils install' "$(picked gtimeout perl)" 'gtimeout'
    assert_eq 'perl is the last layer, which is what macOS lands on' "$(picked perl)" 'perl'

    # The regression this whole change exists for: on a stock macOS there is no timeout(1) at
    # all, and the bare call to it exited 127, so every hook fail-closed and the session was
    # unusable until the plugin was uninstalled.
    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$wdbase" "$ARL" pretool))
    rc=$?
    assert_eq 'with no watchdog at all the shim still exits 0' "$rc" '0'
    assert_eq 'and denies rather than failing open' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
    assert_contains 'and names the missing dependency instead of an opaque 127' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'perl'
fi

if start 'watchdog: ARL_WATCHDOG selects a layer but can never disable one'; then
    new_case
    arm_ok && phases_ok

    wdbase="$CASE_DIR/wd-base"
    mkdir -p "$wdbase"
    for b in bash sh cat printf env grep sed cut git mktemp true false ls find head tr date python3; do
        p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$wdbase/$b"
    done
    wdspy="$CASE_DIR/wd-spies"
    mkdir -p "$wdspy"
    for name in timeout gtimeout; do
        cat >"$wdspy/$name" <<EOF
#!/usr/bin/env bash
printf '%s' '$name' >"\$ARL_TEST_WATCHDOG_LOG"
shift
exec "\$@"
EOF
        chmod +x "$wdspy/$name"
    done
    cat >"$wdspy/perl" <<'EOF'
#!/usr/bin/env bash
printf 'perl' >"$ARL_TEST_WATCHDOG_LOG"
shift 3
exec "$@"
EOF
    chmod +x "$wdspy/perl"

    dir=$(mktemp -d "$CASE_DIR/wd.XXXXXX")
    ln -sf "$wdbase"/* "$dir/"
    for name in timeout gtimeout perl; do ln -sf "$wdspy/$name" "$dir/$name"; done

    chose() {
        rm -f "$CASE_DIR/wd.txt"
        hook_payload PreToolUse |
            (cd "$REPO" && PATH="$dir" ARL_TEST_WATCHDOG_LOG="$CASE_DIR/wd.txt" ARL_WATCHDOG="$1" "$ARL" pretool) >/dev/null
        cat "$CASE_DIR/wd.txt" 2>/dev/null
    }

    assert_eq 'an explicit layer is honoured' "$(chose perl)" 'perl'
    assert_eq 'and so is the middle one' "$(chose gtimeout)" 'gtimeout'
    # Like ARL_SHIM_TIMEOUT_*, this is a test seam that can narrow but never remove: every
    # value that does not name an available layer falls back to auto-detection, and there is
    # deliberately no spelling of "run without a watchdog".
    assert_eq 'an unknown value falls back to auto-detection' "$(chose none)" 'timeout'
    assert_eq 'so does the empty value' "$(chose '')" 'timeout'
    assert_eq 'and so does a layer that is not installed' "$(chose nosuchwatchdog)" 'timeout'
fi

if start 'watchdog: the perl layer enforces the deadline with no added grace'; then
    new_case
    arm_ok && phases_ok

    hangpy="$CASE_DIR/hang-python"
    mkdir -p "$hangpy"
    cat >"$hangpy/python3" <<'PYEOF'
#!/usr/bin/env bash
sleep 300
PYEOF
    chmod +x "$hangpy/python3"

    if ! command -v perl >/dev/null 2>&1; then
        ok 'perl watchdog checks skipped (perl unavailable)'
    else
        t0=$(date +%s)
        out=$(hook_payload PreToolUse |
            (cd "$REPO" && PATH="$hangpy:$PATH" ARL_WATCHDOG=perl ARL_SHIM_TIMEOUT_PRETOOL=1 "$ARL" pretool))
        elapsed=$(($(date +%s) - t0))
        assert_eq 'pretool: a hung interpreter still denies under the perl layer' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        assert_contains 'and names the timeout' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'timed out'

        # The margin regression. Each shim ceiling sits just under the timeout Claude Code
        # itself enforces -- intent 8s under 10s, reorient 25s under 30s -- so a watchdog that
        # added a SIGTERM grace period before its SIGKILL would push those two past the point
        # where the host tears the hook down with nothing, and the fallback would never be
        # read. Returning within a second or two of the deadline is what keeps that headroom.
        if [ "$elapsed" -le 3 ]; then
            ok "perl layer returned in ${elapsed}s, at its deadline rather than after a grace period"
        else
            bad 'perl layer returned at its deadline' "${elapsed}s" '<=3s'
        fi

        # The gate is killed by process group, so a descendant that outlived it -- the case a
        # direct-pid kill leaves behind, still holding the response pipe -- cannot stall the
        # shim past its deadline either.
        cat >"$hangpy/python3" <<'PYEOF'
#!/usr/bin/env bash
sleep 300 &
sleep 300
PYEOF
        chmod +x "$hangpy/python3"
        t0=$(date +%s)
        out=$(hook_payload PreToolUse |
            (cd "$REPO" && PATH="$hangpy:$PATH" ARL_WATCHDOG=perl ARL_SHIM_TIMEOUT_PRETOOL=1 "$ARL" pretool))
        elapsed=$(($(date +%s) - t0))
        assert_eq 'a hung gate with a live descendant still denies' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        if [ "$elapsed" -le 5 ]; then
            ok "descendant reaped with the group in ${elapsed}s, not left holding the response"
        else
            bad 'descendant reaped with the process group' "${elapsed}s" '<=5s'
        fi
    fi
fi

if start 'watchdog: the perl layer loads no module the repository can supply'; then
    new_case
    arm_ok && phases_ok

    if ! command -v perl >/dev/null 2>&1; then
        ok 'perl isolation checks skipped (perl unavailable)'
    else
        # The supervisor runs with the repository under review as its cwd, so it needs the same
        # isolation `python3 -I` gives the gate. PERL5LIB and PERLLIB prepend to @INC and so
        # shadow even a core module; PERL5OPT injects `-M` directly. A hostile POSIX.pm here
        # returns WNOHANG as 0, which would turn the poll into a blocking wait and disarm the
        # deadline entirely -- the gate would hang until the host killed it with no response.
        hostile="$CASE_DIR/hostile-perl"
        mkdir -p "$hostile"
        cat >"$hostile/POSIX.pm" <<'PMEOF'
package POSIX;
sub import { }
sub WNOHANG { 0 }
1;
PMEOF
        mkdir -p "$hostile/Time"
        cat >"$hostile/Time/HiRes.pm" <<'PMEOF'
package Time::HiRes;
sub import { }
sub time { 0 }
sub sleep { CORE::sleep(1) }
1;
PMEOF

        # A hostile module dropped in the repository itself, for the perls that still carry
        # `.` in @INC (before 5.26).
        cp "$hostile/POSIX.pm" "$REPO/POSIX.pm"

        hangpy="$CASE_DIR/hang-python"
        mkdir -p "$hangpy"
        cat >"$hangpy/python3" <<'PYEOF'
#!/usr/bin/env bash
sleep 300
PYEOF
        chmod +x "$hangpy/python3"

        for vector in PERL5LIB PERLLIB; do
            t0=$(date +%s)
            out=$(hook_payload PreToolUse | (
                cd "$REPO" && PATH="$hangpy:$PATH" ARL_WATCHDOG=perl ARL_SHIM_TIMEOUT_PRETOOL=1 \
                    env "$vector=$hostile" "$ARL" pretool
            ))
            elapsed=$(($(date +%s) - t0))
            assert_eq "$vector cannot replace the supervisor's modules" \
                "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
            if [ "$elapsed" -le 3 ]; then
                ok "$vector: the deadline still fired in ${elapsed}s"
            else
                bad "$vector: the deadline still fires" "${elapsed}s" '<=3s'
            fi
        done

        t0=$(date +%s)
        out=$(hook_payload PreToolUse | (
            cd "$REPO" && PATH="$hangpy:$PATH" ARL_WATCHDOG=perl ARL_SHIM_TIMEOUT_PRETOOL=1 \
                env "PERL5LIB=$hostile" "PERL5OPT=-MPOSIX" "$ARL" pretool
        ))
        elapsed=$(($(date +%s) - t0))
        assert_eq 'PERL5OPT cannot inject a module either' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        if [ "$elapsed" -le 3 ]; then
            ok "PERL5OPT: the deadline still fired in ${elapsed}s"
        else
            bad 'PERL5OPT: the deadline still fires' "${elapsed}s" '<=3s'
        fi

        assert_contains 'the interpreter environment is scrubbed before perl starts' \
            "$(cat "$PLUGIN_ROOT/scripts/arl.sh")" "PERL5LIB='' PERL5OPT='' PERLLIB=''"
    fi
fi

if start 'watchdog: a perl without CLOCK_MONOTONIC is refused, not run on the wall clock'; then
    new_case
    arm_ok && phases_ok

    if ! command -v perl >/dev/null 2>&1; then
        ok 'monotonic-clock checks skipped (perl unavailable)'
    else
        # A deadline on the wall clock is not a deadline: a backwards NTP step stretches it past
        # the host's own hook timeout, and Claude Code then tears the hook down before the
        # fail-closed response is written -- the one failure mode that produces *no* answer at
        # all rather than a denial. Falling back to wall time would leave that open while
        # looking like it works, so the supervisor refuses instead.
        #
        # A stand-in perl whose Time::HiRes has everything except CLOCK_MONOTONIC, which is what
        # a platform lacking it looks like from here.
        nomono="$CASE_DIR/perl-no-monotonic"
        mkdir -p "$nomono"
        # The real interpreter, with only that one constant made to fail -- the shim calls it as
        # `perl -e <script> <seconds> <argv...>`, so the wrapper injects a prelude ahead of the
        # supervisor's own -e and passes the rest through untouched.
        cat >"$nomono/perl" <<PERLEOF
#!/usr/bin/env bash
script=\$2
shift 2
exec $(command -v perl) -e 'require Time::HiRes; *Time::HiRes::CLOCK_MONOTONIC = sub { die "unsupported" };' -e "\$script" -- "\$@"
PERLEOF
        chmod +x "$nomono/perl"

        out=$(hook_payload PreToolUse | (
            cd "$REPO" && PATH="$nomono:$PATH" ARL_WATCHDOG=perl "$ARL" pretool
        ))
        assert_eq 'the hook denies rather than running unbounded on the wall clock' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        assert_contains 'and names the missing clock rather than a bare exit status' \
            "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'CLOCK_MONOTONIC'

        # And the same capability is a precondition for arming, so this is one refusal while the
        # user is watching rather than a denial per tool call. `timeout`/`gtimeout` are kept off
        # this PATH so perl really is the layer that would be chosen.
        armdir="$CASE_DIR/arm-no-monotonic"
        mkdir -p "$armdir"
        for b in bash sh cat printf env python3 grep sed cut git mktemp true false ls find head tr date; do
            p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$armdir/$b"
        done
        ln -sf "$nomono/perl" "$armdir/perl"

        arm_out=$( (cd "$REPO" && PATH="$armdir" "$ARL" arm --session "no-mono" --plan "$PLAN") 2>&1)
        arm_rc=$?
        assert_eq 'arming is refused' "$arm_rc" '1'
        assert_contains 'and says which capability is missing' "$arm_out" 'CLOCK_MONOTONIC'
    fi
fi

# --------------------------------------------------------------------------

printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
