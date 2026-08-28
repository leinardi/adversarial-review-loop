#!/usr/bin/env bash
# opencode-review-loop selftest.
#
# Runs the whole gate against scratch repositories under $TMPDIR, with the
# reviewer replaced by tests/fixtures/fake-reviewer.sh. No model is called and
# nothing outside the scratch directories is touched.
#
# usage: tests/selftest.sh [name-filter]

set -uo pipefail

TESTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_ROOT=$(dirname -- "$TESTS_DIR")
OCRL="$PLUGIN_ROOT/scripts/ocrl.sh"
FAKE="$TESTS_DIR/fixtures/fake-reviewer.sh"
FILTER=${1:-}

export CLAUDE_PLUGIN_ROOT=$PLUGIN_ROOT
export OCRL_REVIEWER_CMD=$FAKE

PASS=0
FAIL=0
CURRENT=''
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ocrl-selftest.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT

# Run only every Nth section, offset by I: OCRL_SELFTEST_SHARD=I/N. Sections share nothing
# -- each `new_case` builds its own repository under its own $ROOT and its own
# OCRL_STATE_DIR -- so splitting them across processes is a scheduling decision and not a
# semantic one. tests/selftest-parallel.sh is what sets this; running the script by hand
# without it executes everything, in order, exactly as before.
SHARD=${OCRL_SELFTEST_SHARD:-}
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
    export OCRL_STATE_DIR="$CASE_DIR/state"
    mkdir -p "$REPO"
    git -C "$REPO" init -q -b main
    git -C "$REPO" config user.email selftest@example.invalid
    git -C "$REPO" config user.name 'ocrl selftest'
    git -C "$REPO" config commit.gpgsign false
    printf 'seed\n' >"$REPO/seed.txt"
    git -C "$REPO" add -A
    git -C "$REPO" commit -qm 'seed'
    PLAN="$CASE_DIR/plan.md"
    printf '# Plan\n\nDo the thing, then do the other thing.\n' >"$PLAN"
    SESSION="sess-$CASE_N"
}

ocrl() { (cd "$REPO" && "$OCRL" "$@"); }

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
        (cd "$at" && "$OCRL" pretool))
    printf '%s' "$out" >"$ROOT/last.json"
    if [ -z "$out" ]; then
        printf 'pass'
    else
        printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"'
    fi
}

# with_env VAR=VAL ... -- <command>: exports for one call only. Plain
# `VAR=x func` would not reach the child process the helper spawns.
with_env() {
    local -a names=() saved=()
    local kv rc i name
    while [ "$#" -gt 0 ] && [[ $1 == *=* ]]; do
        kv=$1
        shift
        name=${kv%%=*}
        names+=("$name")
        # Remember whether it was set, and to what, so the restore is exact:
        # blindly unsetting would strip an export the whole suite relies on.
        if [ -n "${!name+set}" ]; then saved+=("set:${!name}"); else saved+=('unset:'); fi
        export "${kv?}"
    done
    "$@"
    rc=$?
    for i in "${!names[@]}"; do
        name=${names[i]}
        if [ "${saved[i]%%:*}" = 'set' ]; then
            export "$name=${saved[i]#set:}"
        else
            unset "$name"
        fi
    done
    return $rc
}

confirm() {
    local cmd=$1 out
    out=$(jq -nc --arg s "$SESSION" --arg c "$REPO" --arg cmd "$cmd" \
        '{session_id:$s,cwd:$c,hook_event_name:"PostToolUse",tool_name:"Bash",tool_input:{command:$cmd},tool_response:{exit_code:0}}' |
        (cd "$REPO" && "$OCRL" confirm-commit))
    printf '%s' "$out" >"$ROOT/last.json"
    printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""'
}

posttool_fail() {
    jq -nc --arg s "$SESSION" --arg c "$REPO" --arg cmd "$1" \
        '{session_id:$s,cwd:$c,hook_event_name:"PostToolUseFailure",tool_name:"Bash",tool_input:{command:$cmd}}' |
        (cd "$REPO" && "$OCRL" posttool-failure)
}

stop_gate() {
    local out
    out=$(jq -nc --arg s "$SESSION" --arg c "$REPO" \
        '{session_id:$s,cwd:$c,hook_event_name:"Stop",stop_hook_active:false}' |
        (cd "$REPO" && "$OCRL" gate-stop))
    printf '%s' "$out" >"$ROOT/last.json"
    printf '%s' "$out"
}

stop_decision() {
    local out
    out=$(stop_gate)
    if [ -z "$out" ]; then printf 'ok'; else printf '%s' "$out" | jq -r '.decision // "ok"'; fi
}

state_file() {
    find "$OCRL_STATE_DIR/worktrees" -name state.json | head -n 1
}

# Mirrors ocrl_get, including its avoidance of `//` -- in jq `false // ""` is
# "", which would blank every boolean field being asserted on.
sget() {
    jq -r --arg k "$1" '
        if has($k) and .[$k] != null
        then (.[$k] | if type == "string" then . else tostring end)
        else "" end' "$(state_file)"
}

# state_file_for <session> / sget_for <session> <key> -- same as state_file/sget, but for a
# specific session's activation directory rather than "whichever state.json find happens to
# return first". Needed once a case has more than one session live under the same worktree
# (a resumed predecessor plus its successor), where the plain helpers above are ambiguous.
# paths.sha256_hex documents itself as matching `printf '%s' … | sha256sum`, so this reproduces
# the same hash the Python side computes for the worktree directory.
state_file_for() {
    printf '%s/worktrees/%s/%s/state.json' "$OCRL_STATE_DIR" "$(printf '%s' "$REPO" | sha256sum | cut -d' ' -f1)" "$1"
}

sget_for() {
    jq -r --arg k "$2" '
        if has($k) and .[$k] != null
        then (.[$k] | if type == "string" then . else tostring end)
        else "" end' "$(state_file_for "$1")"
}

arm_ok() {
    ocrl arm --session "$SESSION" --plan "$PLAN" >/dev/null 2>&1
}

phases_ok() {
    ocrl set-phases --phase 'Phase one: the thing' --phase 'Phase two: the other thing' >/dev/null 2>&1
}

commit_now() {
    git -C "$REPO" add -A && git -C "$REPO" commit -qm "$1"
}

# --------------------------------------------------------------------------
# Command shape
# --------------------------------------------------------------------------
#
# The snapshot layer is retired from this file: pytest has owned it,
# assertion-for-assertion, since Phase 3, and the black-box arm/commit
# sections above already exercise snapshotting through the CLI on every run.
#
# The allowlist table below used to call scripts/lib/cmdshape.sh directly.
# It is now driven through an armed `scripts/ocrl.sh pretool`, because
# unit-level correctness (already proven in pytest) says nothing about
# whether `pretool` actually calls the shape validator, on the right
# argument, and turns its verdict into the right hook decision. This is the
# one phase where that wiring could be wrong, so it is proven here rather
# than only once the parser changed. Swapping the hand-rolled tokenizer for
# bashlex reused this suite with every assertion unchanged, which is what
# proves the vendored parser was actually wired into the gate and not merely
# correct in isolation.
#
# Only commands containing "commit" reach the shape validator at all --
# `pretool` decides that with a cheap substring pre-filter before ever
# tokenizing. A command like `git status` never reaches it and is not part
# of this corpus; that routing is exercised directly below instead.

if start 'command shape: allowlist table, through an armed pretool'; then
    new_case
    arm_ok && phases_ok

    # Every accepted shape here changes nothing in the worktree, so each one
    # hits the "byte-identical to the last approved tree" cache hit and is
    # allowed without the reviewer ever running -- proving the shape passed,
    # not that there was something to approve.
    shape_ok() {
        local d
        d=$(pre Bash "$1")
        if [ "$d" = 'allow' ]; then ok "accepted: $1"; else bad "accepted: $1" "$d ($(pre_reason))"; fi
    }
    # Every rejected shape must be denied for having been read as an unsafe
    # commit command -- never merely "pass" (which would mean it was never
    # classified as a commit at all) and never "allow".
    shape_no() {
        local d
        d=$(pre Bash "$1")
        if [ "$d" = 'deny' ]; then ok "denied: $1"; else bad "denied: $1" "$d"; fi
    }

    shape_ok 'git commit -m x'
    shape_ok 'git commit -m "feat(x): a message with spaces"'
    shape_ok 'git add -A && git commit -m x'
    shape_ok 'git add -A && git status --porcelain && git commit -m x'
    shape_ok 'git commit -am "both"'
    shape_ok 'git add -u && git commit --message="long form"'
    shape_ok 'git add src lib && git commit -m x'

    shape_no 'make build && git commit -m x'
    shape_no 'git rm f && git commit -m x'
    shape_no 'git diff --output=/tmp/x && git commit -m x'
    shape_no 'git commit --amend'
    shape_no 'git commit --amend -m x'
    # shellcheck disable=SC2016  # the literal text is the point: it must be denied
    shape_no 'git commit -m "$(printf hi)"'
    # shellcheck disable=SC2016  # ditto
    shape_no 'git commit -m `hostname`'
    shape_no 'git commit -m x; rm -rf /'
    shape_no 'git commit -m x > out.txt'
    shape_no 'git commit -m x | tee log'
    shape_no 'git commit --only src -m x'
    shape_no 'git commit --include src -m x'
    shape_no 'git commit src/main.go -m x'
    shape_no 'git commit -F msg.txt'
    shape_no 'git add -p && git commit -m x'
    shape_no 'sed -i s/a/b/ f && git commit -m x'
    shape_no 'git add -A & git commit -m x'
    shape_no 'git commit -m'

    # "git status" mentions neither expansion nor commit, so it never reaches
    # the shape validator at all -- it is ordinary Bash, passed through.
    assert_eq 'git status never reaches the shape validator' "$(pre Bash 'git status')" 'pass'

    # KNOWN GAP, carried over unchanged from the shell: the cheap "does this
    # command mention commit" pre-filter only understands a flag as a single
    # space-free token, so a flag taking a separate value -- `-C /other` --
    # breaks the pattern before it reaches "commit", and the command is never
    # routed to the validator at all. `validate_commit` itself still rejects
    # `-C` when called directly (see tests/unit/test_cmdshape.py), but that
    # code path is unreachable in production for exactly this shape. This gap
    # predates the Python port -- it was already true of the plugin's retired
    # Bash gate, carried over unchanged rather than fixed, because fixing the
    # detector was out of scope for a same-behaviour translation. Recorded
    # here so the gap is asserted, not silently lost when this section
    # changed shape.
    assert_eq 'git -C /other commit -m x is a live detection gap, not denied' \
        "$(pre Bash 'git -C /other commit -m x')" 'pass'

    # is_escape and reset_target are exercised as armed-CLI tests too, just
    # not here: see "escapes: Claude may not finish or deactivate" and
    # "reconcile: a bad phase-1 commit is recoverable ..." below.
fi

# --------------------------------------------------------------------------
# Arming
# --------------------------------------------------------------------------

if start 'arm: refuses a dirty worktree, folds it in with --allow-dirty'; then
    new_case
    printf 'dirt\n' >"$REPO/dirt.txt"
    out=$(ocrl arm --session "$SESSION" --plan "$PLAN" 2>&1)
    rc=$?
    assert_eq 'arming a dirty worktree exits non-zero' "$rc" '1'
    assert_contains 'the failure names the dirt' "$out" 'worktree is dirty'
    assert_eq 'the failure is persisted as ARM_FAILED' "$(sget status)" 'ARM_FAILED'

    new_case
    printf 'dirt\n' >"$REPO/dirt.txt"
    out=$(ocrl arm --session "$SESSION" --plan "$PLAN" --allow-dirty 2>&1)
    assert_eq '--allow-dirty arms cleanly' "$(sget status)" 'ARMED'
    assert_eq 'the baseline is the HEAD tree, so the dirt lands in phase 1' \
        "$(sget baseline_tree)" "$(git -C "$REPO" rev-parse 'HEAD^{tree}')"
fi

if start 'arm: rejects a bad second argument and a missing plan'; then
    new_case
    ocrl arm --session "$SESSION" --plan "$PLAN" '--wat' >/dev/null 2>&1
    assert_eq 'a malformed second argument fails closed' "$(sget status)" 'ARM_FAILED'
    assert_contains 'and says why' "$(sget reason)" '--allow-dirty'

    new_case
    ocrl arm --session "$SESSION" --plan "$CASE_DIR/nope.md" >/dev/null 2>&1
    assert_eq 'a non-existent plan fails closed' "$(sget status)" 'ARM_FAILED'

    new_case
    ocrl arm --session "$SESSION" --plan "$CASE_DIR/pl\`an.md" >/dev/null 2>&1
    assert_eq 'a plan path with a backtick fails closed' "$(sget status)" 'ARM_FAILED'
    assert_contains 'and names the character class' "$(sget reason)" 'not safe'

    new_case
    printf 'x\n' >"$CASE_DIR/a plan.md"
    ocrl arm --session "$SESSION" --plan "$CASE_DIR/a plan.md" >/dev/null 2>&1
    assert_eq 'a plan path with a space is accepted' "$(sget status)" 'ARMED'

    new_case
    with_env OCRL_MODEL='provider/does-not-exist' OCRL_REVIEWER_CMD='' \
        ocrl arm --session "$SESSION" --plan "$PLAN" >/dev/null 2>&1
    assert_eq 'an unreachable model fails closed' "$(sget status)" 'ARM_FAILED'
fi

if start 'arm: --args parsing, as the slash command actually delivers it'; then
    # Claude Code substitutes $ARGUMENTS as one string, so the split happens in
    # the script. $1 is unusable: its positional substitution is 0-based, so a
    # single-argument invocation leaves $1 literal and the shell empties it.
    new_case
    ocrl arm --session "$SESSION" --args "$PLAN" >/dev/null 2>&1
    assert_eq 'a bare plan path arms' "$(sget status)" 'ARMED'
    assert_eq 'and allow_dirty stays off' "$(sget allow_dirty)" 'false'

    new_case
    ocrl arm --session "$SESSION" --args "$PLAN --allow-dirty" >/dev/null 2>&1
    assert_eq 'a trailing --allow-dirty arms' "$(sget status)" 'ARMED'
    assert_eq 'and turns allow_dirty on' "$(sget allow_dirty)" 'true'

    new_case
    printf 'x\n' >"$CASE_DIR/a plan with spaces.md"
    ocrl arm --session "$SESSION" --args "$CASE_DIR/a plan with spaces.md" >/dev/null 2>&1
    assert_eq 'a path containing spaces survives' "$(sget status)" 'ARMED'
    assert_contains 'and is the plan that was frozen' "$(sget plan_path)" 'a plan with spaces.md'

    new_case
    printf 'x\n' >"$CASE_DIR/spaced plan.md"
    ocrl arm --session "$SESSION" --args "$CASE_DIR/spaced plan.md --allow-dirty" >/dev/null 2>&1
    assert_eq 'spaces plus the flag together' "$(sget status)" 'ARMED'
    assert_eq 'flag applied' "$(sget allow_dirty)" 'true'
    assert_contains 'path intact' "$(sget plan_path)" 'spaced plan.md'

    new_case
    ocrl arm --session "$SESSION" --args "" >/dev/null 2>&1
    assert_eq 'an empty argument string fails closed' "$(sget status)" 'ARM_FAILED'
    assert_contains 'naming the missing plan' "$(sget reason)" 'no plan path'

    new_case
    ocrl arm --session "$SESSION" --args "$PLAN --wat" >/dev/null 2>&1
    assert_eq 'an unknown trailing flag fails closed' "$(sget status)" 'ARM_FAILED'
    assert_contains 'naming the offending flag' "$(sget reason)" '--wat'
fi

# --------------------------------------------------------------------------
# Fail-closed gating
# --------------------------------------------------------------------------

if start 'fail-closed: every arm failure denies every mutation'; then
    for mode in dirty badarg noplan badmodel; do
        new_case
        case "$mode" in
            dirty)
                printf 'dirt\n' >"$REPO/dirt.txt"
                ocrl arm --session "$SESSION" --plan "$PLAN" >/dev/null 2>&1
                ;;
            badarg) ocrl arm --session "$SESSION" --plan "$PLAN" '--nope' >/dev/null 2>&1 ;;
            noplan) ocrl arm --session "$SESSION" --plan "$CASE_DIR/missing.md" >/dev/null 2>&1 ;;
            badmodel) with_env OCRL_MODEL='provider/nope' OCRL_REVIEWER_CMD='' ocrl arm --session "$SESSION" --plan "$PLAN" >/dev/null 2>&1 ;;
        esac
        assert_eq "[$mode] Edit is denied" "$(pre Edit)" 'deny'
        assert_eq "[$mode] Write is denied" "$(pre Write)" 'deny'
        assert_eq "[$mode] NotebookEdit is denied" "$(pre NotebookEdit)" 'deny'
        assert_eq "[$mode] Bash is denied" "$(pre Bash 'echo hi')" 'deny'
        assert_eq "[$mode] an MCP mutation tool is denied" "$(pre mcp__serena__replace_content)" 'deny'
        assert_eq "[$mode] a commit is denied" "$(pre Bash 'git add -A && git commit -m x')" 'deny'
        assert_eq "[$mode] Read is still allowed" "$(pre Read)" 'pass'
        assert_eq "[$mode] the turn cannot end quietly" "$(stop_decision)" 'block'
    done
fi

if start 'fail-closed: missing state denies mutations'; then
    new_case
    arm_ok
    rm -f "$(state_file)"
    assert_eq 'Edit denied with no state' "$(pre Edit)" 'deny'
    assert_contains 'and says how to recover' "$(pre_reason)" '/opencode-review-loop:implement'
    assert_eq 'Read still allowed' "$(pre Read)" 'pass'
fi

if start 'fail-closed: an arm that never executed still denies'; then
    # The hooks register when the skill is invoked, so a dispatcher running
    # with no session pointer means `ocrl arm` never started -- a denied
    # sandbox, an unreadable script, an unresolved plugin root. cmd_arm cannot
    # persist a failure to start, so the dispatcher has to.
    new_case
    # Deliberately no arm_ok here: this is the "expansion never ran" case.
    assert_eq 'no state exists yet' "$(find "$OCRL_STATE_DIR" -name state.json 2>/dev/null | wc -l)" '0'

    assert_eq 'Edit is denied rather than silently passing' "$(pre Edit)" 'deny'
    assert_contains 'and says arming never ran' "$(pre_reason)" 'Arming never ran'
    assert_contains 'and tells Claude not to implement' "$(pre_reason)" 'Do not implement the plan'

    assert_eq 'the dispatcher recorded the failure itself' "$(sget status)" 'ARM_FAILED'
    assert_contains 'with a reason naming the cause' "$(sget reason)" 'arming never executed'

    # Now that it is recorded, the ordinary ARM_FAILED path takes over.
    assert_eq 'Write stays denied' "$(pre Write)" 'deny'
    assert_eq 'Bash stays denied' "$(pre Bash 'git add -A && git commit -m x')" 'deny'
    assert_eq 'an MCP mutation stays denied' "$(pre mcp__serena__replace_content)" 'deny'
    assert_eq 'Read is still allowed' "$(pre Read)" 'pass'
    assert_eq 'the turn cannot end quietly' "$(stop_decision)" 'block'
fi

if start 'fail-closed: a turn ending before any tool call still blocks'; then
    new_case
    out=$(stop_gate)
    assert_contains 'the Stop gate blocks' "$out" 'arming never ran'
    assert_eq 'and records it' "$(sget status)" 'ARM_FAILED'
fi

if start 'stop: disarming survives the unstarted-arm guard'; then
    # Regression: deactivate used to delete the session pointer, which the new
    # guard would then read as "arming never ran" and deny on every call.
    new_case
    arm_ok && phases_ok
    ocrl deactivate >/dev/null 2>&1
    assert_eq 'status is DISARMED' "$(sget status)" 'DISARMED'
    assert_eq 'Edit passes after stopping' "$(pre Edit)" 'pass'
    assert_eq 'Bash passes after stopping' "$(pre Bash 'git add -A && git commit -m x')" 'pass'
    assert_eq 'the turn ends cleanly' "$(stop_decision)" 'ok'
fi

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
            "$CASE_DIR/payload.json" "$OCRL" pretool 2>/dev/null)
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
            "$CASE_DIR/payload2.json" "$OCRL" pretool 2>/dev/null)
        assert_eq 'a socket payload parses tool_name and command' \
            "$(printf '%s' "$sock_out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
        assert_contains 'and reaches the real classifier' \
            "$(printf '%s' "$sock_out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'amend'
    else
        ok 'socket-stdin check skipped (python3 unavailable)'
    fi
fi

if start 'hook input: a payload with no session id denies rather than guessing' ; then
    new_case
    arm_ok && phases_ok
    out=$(jq -nc --arg c "$REPO" \
        '{cwd:$c,hook_event_name:"PreToolUse",tool_name:"Edit",tool_input:{}}' |
        (cd "$REPO" && "$OCRL" pretool))
    assert_eq 'denied' "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision')" 'deny'
    assert_contains 'and names it an integration fault' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason')" 'no session id'
    # It must not leave junk state keyed by an empty session.
    assert_eq 'no state was written under an empty session key' \
        "$(find "$OCRL_STATE_DIR/worktrees" -maxdepth 2 -name state.json 2>/dev/null | wc -l)" '0'
fi

# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------

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
        "$(pre Bash "$OCRL set-phases --phase 'a'")" 'allow'
    assert_eq 'ending the turn here blocks' "$(stop_decision)" 'block'

    phases_ok
    assert_eq 'active after set-phases' "$(sget status)" 'ACTIVE'
    assert_eq 'on phase 1' "$(sget phase)" '1'
    assert_eq 'Edit is allowed once the phases are frozen' "$(pre Edit)" 'pass'
    assert_eq 'ordinary Bash is allowed too' "$(pre Bash 'make test')" 'pass'

    out=$(ocrl set-phases --phase 'x' 2>&1)
    assert_contains 'the phase list cannot be re-frozen' "$out" 'already frozen'
fi

# --------------------------------------------------------------------------
# Commit gate
# --------------------------------------------------------------------------

if start 'commit gate: approve, commit, advance'; then
    new_case
    arm_ok && phases_ok
    printf 'phase one\n' >"$REPO/a.txt"

    d=$(with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m "phase 1"')
    assert_eq 'an approved review allows the commit' "$d" 'allow'
    assert_eq 'the approval is pending, and the phase has not advanced' "$(sget phase)" '1'
    if [ -n "$(sget pending_approved_tree)" ]; then ok 'a pending tree is recorded'; else bad 'a pending tree is recorded'; fi

    commit_now 'phase 1'
    ctx=$(confirm 'git add -A && git commit -m "phase 1"')
    assert_contains 'the confirmation reports the phase committed' "$ctx" 'phase 1 of 2 committed'
    assert_contains 'and pushes straight into the next phase' "$ctx" 'without ending your turn'
    assert_eq 'the phase advanced' "$(sget phase)" '2'
    assert_eq 'the pending approval was consumed' "$(sget pending_approved_tree)" ''
    assert_eq 'the approved tree became the new base' "$(sget last_approved_tree)" "$(git -C "$REPO" rev-parse 'HEAD^{tree}')"
fi

if start 'commit gate: changes required blocks, and every finding comes back'; then
    new_case
    arm_ok && phases_ok
    printf 'phase one\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x')
    assert_eq 'a review that requires changes denies the commit' "$d" 'deny'
    assert_contains 'the finding is returned inline' "$(pre_reason)" 'Returns success on a failed lookup'
    assert_eq 'the phase did not advance' "$(sget phase)" '1'
fi

if start 'commit gate: an APPROVE alongside an actionable critical still blocks'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=approve-with-critical pre Bash 'git add -A && git commit -m x')
    assert_eq 'the gate recomputes the verdict and blocks' "$d" 'deny'
    assert_contains 'the critical finding is quoted' "$(pre_reason)" 'Nil deref'
fi

if start 'commit gate: a non-actionable critical does not block'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=critical-nonactionable pre Bash 'git add -A && git commit -m x')
    assert_eq 'actionable=no never blocks, at any severity' "$d" 'allow'
fi

if start 'commit gate: prose truncates but FINDING lines survive'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    d=$(with_env OCRL_MAX_REASON_BYTES=2000 OCRL_FAKE_MODE=big-prose pre Bash 'git add -A && git commit -m x')
    r=$(pre_reason)
    assert_eq 'the commit is denied' "$d" 'deny'
    assert_contains 'the first finding survived' "$r" 'Must survive truncation'
    assert_contains 'the second finding survived' "$r" 'Must also survive truncation'
    assert_contains 'the prose was truncated' "$r" 'truncated at 2000 bytes'
fi

if start 'commit gate: operational failures never approve'; then
    for mode in malformed no-verdict empty nonzero; do
        new_case
        arm_ok && phases_ok
        printf 'x\n' >"$REPO/a.txt"
        d=$(with_env OCRL_FAKE_MODE=$mode pre Bash 'git add -A && git commit -m x')
        assert_eq "[$mode] denied" "$d" 'deny'
        assert_contains "[$mode] and says a failed review is not an approval" "$(pre_reason)" 'never an approval'
    done

    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    d=$(with_env OCRL_TIMEOUT_SEC=1 OCRL_FAKE_MODE=slow pre Bash 'git add -A && git commit -m x')
    assert_eq '[timeout] denied' "$d" 'deny'
    assert_contains '[timeout] and names the timeout' "$(pre_reason)" 'timed out after 1s'
fi

if start 'commit gate: a block the contract does not allow never approves'; then
    # Each of these carries the reviewer's own APPROVED, and each used to get it: the
    # findings were dropped as unrecognised and the advisory verdict then stood.
    for mode in bad-actionable bad-severity mangled-finding stray-end two-blocks two-verdicts chatty-block \
        inline-start-marker suffixed-end-marker nul-byte; do
        new_case
        arm_ok && phases_ok
        printf 'x\n' >"$REPO/a.txt"
        d=$(with_env OCRL_FAKE_MODE=$mode pre Bash 'git add -A && git commit -m x')
        assert_eq "[$mode] denied" "$d" 'deny'
        assert_contains "[$mode] and says a failed review is not an approval" "$(pre_reason)" 'never an approval'
    done
fi

if start 'commit gate: repeated failures escalate to needs-human'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    export OCRL_MAX_FAILURES=2
    with_env OCRL_FAKE_MODE=malformed pre Bash 'git add -A && git commit -m x' >/dev/null
    with_env OCRL_FAKE_MODE=malformed pre Bash 'git add -A && git commit -m x' >/dev/null
    d=$(with_env OCRL_FAKE_MODE=malformed pre Bash 'git add -A && git commit -m x')
    assert_eq 'the third consecutive failure denies' "$d" 'deny'
    assert_eq 'and escalates' "$(sget status)" 'NEEDS_HUMAN'
    assert_eq 'after which every mutation stays denied' "$(pre Edit)" 'deny'
    unset OCRL_MAX_FAILURES
fi

if start 'transient failures: a rate-limited exit paces retries and denies without invoking the reviewer'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    export OCRL_MAX_FAILURES=1
    d=$(with_env OCRL_FAKE_MODE=rate-limited pre Bash 'git add -A && git commit -m x')
    assert_eq 'the commit is denied' "$d" 'deny'
    assert_contains 'and the message names it as transient' "$(pre_reason)" 'transient'
    assert_eq 'the ordinary failure budget is untouched' "$(sget failures)" '0'
    assert_eq 'the transient counter moved instead' "$(sget transient_failures)" '1'
    before=$(sget report_seq)

    # A reviewer command that does not exist proves the very next attempt never reaches it:
    # the backoff denies before another provider call, not merely before another approval.
    d=$(with_env OCRL_REVIEWER_CMD=/nonexistent/reviewer-must-not-run pre Bash 'git add -A && git commit -m x')
    assert_eq 'the backed-off retry still denies' "$d" 'deny'
    assert_contains 'and names the remaining wait' "$(pre_reason)" 'Retry in'
    assert_eq 'no report sequence was reserved for it' "$(sget report_seq)" "$before"
    unset OCRL_MAX_FAILURES
fi

if start 'transient failures: exhausting the budget escalates, and a later approval clears both counters'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    export OCRL_MAX_TRANSIENT_FAILURES=1
    with_env OCRL_FAKE_MODE=rate-limited pre Bash 'git add -A && git commit -m x' >/dev/null

    # Clears the backoff so this attempt reaches the reviewer rather than being denied by
    # the wait itself -- this case is about the budget being exhausted, not about pacing.
    f=$(state_file)
    jq '.retry_not_before = 1' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    d=$(with_env OCRL_FAKE_MODE=rate-limited pre Bash 'git add -A && git commit -m x')
    assert_eq 'the second transient failure denies' "$d" 'deny'
    assert_eq 'and escalates once the transient budget is exhausted' "$(sget status)" 'NEEDS_HUMAN'
    unset OCRL_MAX_TRANSIENT_FAILURES

    jq '.status = "ACTIVE" | .retry_not_before = 1' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    printf 'y\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x')
    assert_eq 'a later approval succeeds' "$d" 'allow'
    assert_eq 'and clears the transient counter' "$(sget transient_failures)" '0'
    assert_eq 'and any pending backoff' "$(sget retry_not_before)" '0'
fi

if start 'commit gate: the findings cap escalates instead of trimming'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    d=$(with_env OCRL_MAX_FINDINGS=5 OCRL_FAKE_COUNT=6 OCRL_FAKE_MODE=many pre Bash 'git add -A && git commit -m x')
    assert_eq 'denied' "$d" 'deny'
    assert_eq 'escalated to needs-human' "$(sget status)" 'NEEDS_HUMAN'
    assert_contains 'and says the list was not trimmed' "$(pre_reason)" 'not an approval'
    if find "$OCRL_STATE_DIR" -name '*.md' -path '*reports*' | grep -q .; then
        ok 'the full report is retained on disk'
    else bad 'the full report is retained on disk'; fi
fi

if start 'commit gate: an unchanged tree is a cache hit with no review'; then
    new_case
    arm_ok && phases_ok
    d=$(with_env OCRL_FAKE_MODE=changes pre Bash 'git commit -m "empty" --allow-empty')
    assert_eq 'no delta means no review, so a blocking reviewer is never consulted' "$d" 'allow'
    assert_contains 'and it says so' "$(pre_reason)" 'no review was needed'
fi

if start 'commit gate: an oversized stageable file blocks'; then
    new_case
    arm_ok && phases_ok
    head -c 4000 /dev/zero | tr '\0' 'a' >"$REPO/big.bin"
    d=$(with_env OCRL_MAX_FILE_BYTES=1000 pre Bash 'git add -A && git commit -m x')
    assert_eq 'the commit is denied rather than the file silently dropped' "$d" 'deny'
    assert_contains 'and names the file' "$(pre_reason)" 'big.bin'
fi

if start 'commit gate: ignore_globs skip the review'; then
    new_case
    arm_ok && phases_ok
    printf 'notes\n' >"$REPO/NOTES.md"
    d=$(with_env OCRL_IGNORE_GLOBS='NOTES.md' OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x')
    assert_eq 'a change confined to ignore_globs is allowed without a review' "$d" 'allow'
fi

if start 'prior rounds: round 2 is shown round 1 findings, and no bundle file holds them'; then
    new_case
    arm_ok && phases_ok

    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null

    printf 'phase one, revised\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=echo-context pre Bash 'git add -A && git commit -m x')
    assert_eq 'round 2 still denies' "$d" 'deny'

    act=$(dirname "$(state_file)")
    pr="$act/context/002-prior-rounds.txt"
    if [ -f "$pr" ]; then ok 'round 2 wrote context/002-prior-rounds.txt beside bundles/'; else bad 'round 2 wrote context/002-prior-rounds.txt beside bundles/'; fi
    assert_eq 'round 1 wrote no such file' "$(ls "$act/context/001-prior-rounds.txt" 2>/dev/null)" ''
    assert_contains 'the attachment carries round 1 findings' "$(cat "$pr" 2>/dev/null)" 'Returns success on a failed lookup'
    assert_contains 'round 2 reviewer received it inline' "$(pre_reason)" 'Returns success on a failed lookup'

    # The invariant: bundles/ holds gate-generated evidence only -- no model output, ever.
    if grep -rqF 'Returns success on a failed lookup' "$act"/bundles/ 2>/dev/null; then
        bad 'no file under bundles/ contains a stored finding line'
    else
        ok 'no file under bundles/ contains a stored finding line'
    fi
fi

if start 'incremental diff: round 2 attaches it, round 1 does not'; then
    new_case
    arm_ok && phases_ok

    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null

    printf 'phase one, revised\n' >"$REPO/a.txt"
    d=$(with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x')
    assert_eq 'round 2 still denies' "$d" 'deny'

    act=$(dirname "$(state_file)")
    assert_eq 'round 1 wrote no incremental.diff' "$(ls "$act/bundles/001/incremental.diff" 2>/dev/null)" ''
    if [ -f "$act/bundles/002/incremental.diff" ]; then
        ok 'round 2 wrote bundles/002/incremental.diff'
    else
        bad 'round 2 wrote bundles/002/incremental.diff'
    fi
    assert_contains 'range.txt discloses the changed paths' "$(cat "$act/bundles/002/range.txt" 2>/dev/null)" 'Changed since round 1'
    assert_contains 'and names the changed file' "$(cat "$act/bundles/002/incremental.diff" 2>/dev/null)" 'a.txt'
fi

if start 'stall detection: two rounds of the same finding escalate without invoking the reviewer'; then
    new_case
    arm_ok && phases_ok

    printf 'r1\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null

    printf 'r2\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null

    before=$(sget report_seq)
    assert_eq 'two ordinary rounds ran, each reaching the reviewer' "$before" '2'

    # A reviewer command that does not exist is what proves the third attempt never calls it:
    # the escalation is a static decision over round_history, not merely another denial.
    printf 'r3\n' >"$REPO/a.txt"
    d=$(with_env OCRL_REVIEWER_CMD=/nonexistent/reviewer-must-not-run pre Bash 'git add -A && git commit -m x')
    assert_eq 'the third attempt still denies' "$d" 'deny'
    assert_contains 'as an escalation to NEEDS_HUMAN' "$(pre_reason)" 'NEEDS_HUMAN'
    assert_contains 'naming the persisting anchor' "$(pre_reason)" 'a.txt'
    assert_eq 'status escalated' "$(sget status)" 'NEEDS_HUMAN'
    assert_eq 'no report sequence was reserved for the stalled round' "$(sget report_seq)" "$before"
fi

if start 'stall detection: four rounds of distinct findings never escalate'; then
    new_case
    arm_ok && phases_ok

    # Deliberately the design phase 5 does not cap: a genuinely fresh finding every round
    # keeps the reviewer running with no round limit at all.
    for f in a b c d; do
        printf '%s\n' "$f" >"$REPO/$f.txt"
        d=$(with_env OCRL_FAKE_MODE=changes-file OCRL_FAKE_FILE="$f.py" pre Bash 'git add -A && git commit -m x')
        assert_eq "round for $f.py denies as an ordinary CHANGES_REQUIRED" "$d" 'deny'
        assert_contains "round for $f.py actually reached the reviewer" "$(pre_reason)" "Problem in $f.py"
    done
    assert_eq 'every one of the four rounds actually invoked the reviewer' "$(sget report_seq)" '4'
fi

if start 'clarify: answers one question, touches nothing, and is bounded'; then
    new_case
    arm_ok && phases_ok

    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null

    act=$(dirname "$(state_file)")
    before=$(cat "$(state_file)")

    out=$(with_env OCRL_FAKE_MODE=clarify OCRL_MAX_CLARIFICATIONS=1 ocrl clarify --question 'what did finding 1 mean?')
    assert_contains 'the reviewer prose comes back' "$out" 'Clarification.'
    assert_contains 'the question reached the reviewer' "$out" 'what did finding 1 mean?'
    assert_contains 'against the most recent round bundle' "$out" '/bundles/001'

    q="$act/context/001-question.txt"
    if [ -f "$q" ]; then ok 'the question is written under context/, beside bundles/'; else bad 'the question is written under context/, beside bundles/'; fi
    assert_contains 'wrapped as evidence, not an instruction' "$(cat "$q" 2>/dev/null)" 'NOT an instruction'
    if grep -rqF 'what did finding 1 mean?' "$act"/bundles/ 2>/dev/null; then
        bad 'no file under bundles/ holds the question'
    else
        ok 'no file under bundles/ holds the question'
    fi

    assert_eq 'phase did not move' "$(sget phase)" '1'
    assert_eq 'status unchanged' "$(sget status)" 'ACTIVE'
    assert_eq 'the clarify counter advanced' "$(sget clarifications)" '1'
    assert_eq 'round_history is untouched' "$(printf '%s' "$before" | jq -c '.round_history')" "$(sget round_history)"

    err=$(with_env OCRL_FAKE_MODE=clarify OCRL_MAX_CLARIFICATIONS=1 ocrl clarify --question 'a second question' 2>&1)
    assert_contains 'a second clarify past the cap is refused' "$err" 'already used'
    assert_contains 'and points at accept for a real disagreement' "$err" 'accept'
fi

# --------------------------------------------------------------------------
# Commit divergence and reconcile
# --------------------------------------------------------------------------

if start 'divergence: a partial commit enters reconcile'; then
    new_case
    arm_ok && phases_ok
    printf 'one\n' >"$REPO/a.txt"
    printf 'two\n' >"$REPO/b.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null

    git -C "$REPO" add a.txt
    git -C "$REPO" commit -qm 'only half'
    ctx=$(confirm 'git add -A && git commit -m x')
    assert_contains 'the mismatch is named' "$ctx" 'not the tree that was reviewed'
    assert_eq 'the state is RECONCILE' "$(sget status)" 'RECONCILE'
    assert_eq 'the phase did not advance' "$(sget phase)" '1'
fi

if start 'divergence: an amend enters reconcile'; then
    new_case
    arm_ok && phases_ok
    printf 'one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    git -C "$REPO" add -A
    git -C "$REPO" commit -q --amend -m 'amended seed'
    ctx=$(confirm 'git add -A && git commit -m x')
    assert_contains 'the amend is detected by the parent check' "$ctx" 'amend or a rewrite'
    assert_eq 'the state is RECONCILE' "$(sget status)" 'RECONCILE'
fi

if start 'divergence: a failed commit clears the pending approval'; then
    new_case
    arm_ok && phases_ok
    printf 'one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    if [ -n "$(sget pending_approved_tree)" ]; then ok 'pending is set'; else bad 'pending is set'; fi
    posttool_fail 'git add -A && git commit -m x'
    assert_eq 'PostToolUseFailure clears it' "$(sget pending_approved_tree)" ''
    assert_eq 'and the phase did not advance' "$(sget phase)" '1'
fi

if start 'reconcile: a bad phase-1 commit is recoverable, bounded by the activation'; then
    new_case
    arm_ok && phases_ok
    activation=$(git -C "$REPO" rev-parse HEAD)
    before=$(git -C "$REPO" rev-parse 'HEAD~0')
    printf 'one\n' >"$REPO/a.txt"
    printf 'two\n' >"$REPO/b.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    git -C "$REPO" add a.txt
    git -C "$REPO" commit -qm 'only half'
    confirm 'git add -A && git commit -m x' >/dev/null
    assert_eq 'reconcile entered' "$(sget status)" 'RECONCILE'
    assert_eq 'the recorded parent is the activation commit' "$(sget bad_commit_parent)" "$activation"

    d=$(pre Bash "git reset --soft $activation")
    assert_eq 'resetting to the activation commit is permitted' "$d" 'allow'
    d=$(pre Bash 'git reset --soft HEAD^')
    assert_eq 'HEAD^ resolves to the same commit and is permitted' "$d" 'allow'
    d=$(pre Bash 'git reset --hard HEAD^')
    assert_eq 'a hard reset is refused' "$d" 'deny'

    # A target strictly before the activation commit must be refused.
    git -C "$REPO" reset --soft "$activation" >/dev/null 2>&1
    git -C "$REPO" commit -qm 'rebuilt'
    older=$(git -C "$REPO" rev-parse "$activation^" 2>/dev/null || true)
    if [ -n "$older" ]; then
        d=$(pre Bash "git reset --soft $older")
        assert_eq 'a target before the activation commit is refused' "$d" 'deny'
    else
        ok 'a target before the activation commit is refused (no such commit in this fixture)'
    fi
    : "$before"
fi

if start 'reset: moving HEAD off a reviewed commit is denied outside reconcile'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    d=$(pre Bash 'git reset --soft HEAD^')
    assert_eq 'denied' "$d" 'deny'
    assert_contains 'and explains why' "$(pre_reason)" 'only permitted during a reconcile'
fi

# --------------------------------------------------------------------------
# Escapes
# --------------------------------------------------------------------------

if start 'escapes: Claude may not finish or deactivate'; then
    new_case
    arm_ok && phases_ok
    assert_eq 'ocrl finish via Bash is denied' "$(pre Bash "$OCRL finish")" 'deny'
    assert_eq 'ocrl deactivate via Bash is denied' "$(pre Bash "$OCRL deactivate")" 'deny'
    assert_contains 'and says who may run it' "$(pre_reason)" 'user-only'
    out=$(ocrl deactivate 2>&1)
    assert_contains 'the skill route works' "$out" 'STOPPED'
    assert_eq 'after which nothing is gated' "$(pre Edit)" 'pass'
fi

# --------------------------------------------------------------------------
# Session and worktree scoping
# --------------------------------------------------------------------------

if start 'scoping: another repository in the same session is untouched'; then
    new_case
    arm_ok
    other="$CASE_DIR/other"
    mkdir -p "$other"
    git -C "$other" init -q -b main
    assert_eq 'a mutation in another repo is not gated' "$(pre_at "$other" Edit)" 'pass'
    assert_eq 'while the armed repo still gates' "$(pre Edit)" 'deny'
fi

if start 'scoping: a session with no pointer fails closed, it does not opt out'; then
    # This asserted "pass" until the guard landed, which encoded the fail-open
    # hole: the dispatcher only runs in a session where implement was invoked,
    # so an unrecognised session id means arming never completed, not that the
    # session is uninvolved. Worktree scoping is the test above; this is not it.
    new_case
    arm_ok
    SESSION='some-other-session'
    assert_eq 'an unrecognised session denies rather than passing' "$(pre Edit)" 'deny'
    assert_contains 'and explains why' "$(pre_reason)" 'Arming never ran'
fi

# --------------------------------------------------------------------------
# Stop gate
# --------------------------------------------------------------------------

if start 'stop: outstanding phases block, and the final review does not run'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    assert_eq 'now on phase 2 of 2' "$(sget phase)" '2'

    out=$(with_env OCRL_FAKE_MODE=approve stop_gate)
    assert_contains 'the turn is blocked' "$out" 'still outstanding'
    assert_eq 'and the final review did not complete' "$(sget final_done_tree)" ''
    assert_eq 'so the activation is not complete' "$(sget status)" 'ACTIVE'
fi

if start 'stop: the final cumulative review completes the activation'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    d=$(with_env OCRL_FAKE_MODE=approve OCRL_FINAL_REVIEW=true stop_decision)
    assert_eq 'the turn ends' "$d" 'ok'
    assert_eq 'the activation is complete' "$(sget status)" 'COMPLETE'
    assert_eq 'and further commits are ungated' "$(pre Bash 'git commit --amend -m whatever')" 'pass'
fi

if start 'stop: with final_review at its default off, COMPLETE never calls the reviewer'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    d=$(with_env OCRL_REVIEWER_CMD='/nonexistent/reviewer' stop_decision)
    assert_eq 'the turn ends' "$d" 'ok'
    assert_eq 'the activation is complete' "$(sget status)" 'COMPLETE'
    assert_eq 'final_done_tree is set' "$(sget final_done_tree)" "$(git -C "$REPO" rev-parse 'HEAD^{tree}')"
    assert_eq 'and further commits are ungated' "$(pre Bash 'git commit --amend -m whatever')" 'pass'
fi

if start 'finish: the review runs, and can still refuse, with final_review off'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    out=$(with_env OCRL_FAKE_MODE=changes OCRL_FINAL_REVIEW=false ocrl finish 2>&1 || true)
    assert_contains 'the reviewer ran and refused' "$out" 'did not pass'
    assert_eq 'so the activation is not complete' "$(sget status)" 'ACTIVE'
    assert_eq 'and final_done_tree is untouched' "$(sget final_done_tree)" ''
    out=$(with_env OCRL_FAKE_MODE=approve OCRL_FINAL_REVIEW=false ocrl finish 2>&1)
    assert_contains 'an approving one completes it' "$out" 'COMPLETE'
    assert_eq 'through the review, not around it' "$(sget reason)" 'final cumulative review approved (user-invoked finish)'
fi

if start 'stop: a failing final review blocks rather than completing'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    out=$(with_env OCRL_FAKE_MODE=changes OCRL_FINAL_REVIEW=true stop_gate)
    assert_contains 'blocked with the findings' "$out" 'Returns success on a failed lookup'
    assert_eq 'not complete' "$(sget status)" 'ACTIVE'
fi

if start 'stop: uncommitted work is reviewed, then the commit is demanded'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    out=$(with_env OCRL_FAKE_MODE=changes stop_gate)
    assert_contains 'unreviewed work is swept and blocks on findings' "$out" 'uncommitted work'
fi

if start 'stop: the no-progress counter escalates, and progress resets it'; then
    new_case
    arm_ok
    export OCRL_MAX_STOP_BLOCKS=2
    assert_eq 'block 1' "$(stop_decision)" 'block'
    assert_eq 'block 2' "$(stop_decision)" 'block'
    out=$(stop_gate)
    assert_contains 'the third no-progress block escalates' "$out" 'STALLED'
    assert_eq 'to needs-human, not to approval' "$(sget status)" 'NEEDS_HUMAN'
    unset OCRL_MAX_STOP_BLOCKS

    new_case
    arm_ok
    export OCRL_MAX_STOP_BLOCKS=2
    assert_eq 'block 1' "$(stop_decision)" 'block'
    phases_ok # progress
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    assert_eq 'block after progress' "$(stop_decision)" 'block'
    assert_eq 'the counter restarted rather than accumulating' "$(sget stop_blocks)" '1'
    assert_eq 'and the loop is still ACTIVE' "$(sget status)" 'ACTIVE'
    unset OCRL_MAX_STOP_BLOCKS
fi

if start 'stop: a pre-active state blocks without ever calling the reviewer'; then
    new_case
    ocrl arm --session "$SESSION" --plan "$CASE_DIR/missing.md" >/dev/null 2>&1
    out=$(with_env OCRL_REVIEWER_CMD='/nonexistent/reviewer' stop_gate)
    assert_contains 'ARM_FAILED blocks with the reason' "$out" 'arming failed'

    new_case
    arm_ok
    out=$(with_env OCRL_REVIEWER_CMD='/nonexistent/reviewer' stop_gate)
    assert_contains 'phases-unset blocks with the command' "$out" 'set-phases --phase'
fi

if start 'stop: defer is honoured once and bounded'; then
    new_case
    arm_ok && phases_ok
    export OCRL_MAX_DEFERS=1
    ocrl defer --reason 'need to ask the user something' >/dev/null 2>&1
    assert_eq 'the deferred turn ends' "$(stop_decision)" 'ok'
    out=$(ocrl defer --reason 'again' 2>&1)
    assert_contains 'the second defer is refused' "$out" 'limit'
    unset OCRL_MAX_DEFERS
fi

# --------------------------------------------------------------------------
# Resume and pause
# --------------------------------------------------------------------------

if start 'resume: continues in a new session, baseline and approvals survive'; then
    new_case
    arm_ok && phases_ok
    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m "phase 1"' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m "phase 1"' >/dev/null
    assert_eq 'phase advanced to 2' "$(sget phase)" '2'

    baseline_before=$(sget baseline_tree)
    approved_before=$(jq -c '.approved_trees' "$(state_file)")
    OLD_SESSION=$SESSION
    NEW_SESSION="sess-$CASE_N-resumed"

    out=$(cd "$REPO" && "$OCRL" resume --session "$NEW_SESSION" 2>&1)
    assert_contains 'the resume banner confirms' "$out" 'RESUMED for this worktree'

    assert_eq 'the baseline tree is unchanged' "$(sget_for "$NEW_SESSION" baseline_tree)" "$baseline_before"
    assert_eq 'approved_trees is unchanged' "$(jq -c '.approved_trees' "$(state_file_for "$NEW_SESSION")")" "$approved_before"
    assert_eq 'phase is still 2' "$(sget_for "$NEW_SESSION" phase)" '2'
    assert_eq 'the pending approval is empty' "$(sget_for "$NEW_SESSION" pending_approved_tree)" ''

    # Both pointers name the new session: the `latest` pointer (resolve_local_activation's
    # fallback, exercised through a shell command with no --session) and the per-session
    # pointer (exercised by driving pretool with the new session id, which only resolves a
    # worktree at all through pointer_read(session)).
    s=$(ocrl status)
    assert_contains 'the latest pointer names the new session' "$s" "session:             $NEW_SESSION"
    SESSION=$NEW_SESSION
    printf 'phase two\n' >"$REPO/b.txt"
    assert_eq 'the session pointer resolves and the phase-2 commit is still gated' \
        "$(with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x')" 'deny'

    # report_seq continues rather than restarting: the copied 001 report is still on disk
    # under the successor, and the phase-2 review just above (denied on findings) wrote 002.
    reports=$(find "$OCRL_STATE_DIR/worktrees" -path "*/$NEW_SESSION/reports/*.md" -exec basename {} \; | sort)
    assert_contains 'report 001 was carried forward' "$reports" '001-'
    assert_contains 'report 002 continues the sequence' "$reports" '002-'
fi

if start 'resume: retires the predecessor, which denies rather than gates live'; then
    new_case
    arm_ok && phases_ok
    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m "phase 1"' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m "phase 1"' >/dev/null

    OLD_SESSION=$SESSION
    NEW_SESSION="sess-$CASE_N-resumed"
    ocrl resume --session "$NEW_SESSION" >/dev/null 2>&1

    # This is the two-writable-authorities regression: with the old session id back in the
    # payload, pretool must read the retired document and deny -- never fall through to
    # treating it as a second, still-live ACTIVE activation.
    SESSION=$OLD_SESSION
    d=$(pre Edit)
    assert_eq 'the retired predecessor denies a mutation' "$d" 'deny'
    assert_contains 'named as RESUMED, not a generic gate' "$(pre_reason)" 'retired by a resume'
    assert_contains 'and names the successor' "$(pre_reason)" "$NEW_SESSION"

    assert_eq 'the predecessor is RESUMED on disk' "$(sget_for "$OLD_SESSION" status)" 'RESUMED'
    assert_eq 'and points at the successor' "$(sget_for "$OLD_SESSION" resumed_into)" "$NEW_SESSION"
fi

if start 'pause: --until stops the loop early with no final review, resume completes it'; then
    new_case
    ocrl arm --session "$SESSION" --plan "$PLAN" --until 1 >/dev/null 2>&1
    assert_eq 'armed with a pause target' "$(sget status)" 'ARMED'
    phases_ok
    assert_contains 'the pause target is reported' "$(ocrl status)" 'pause target:        1 of 2'

    printf 'phase one\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m "phase 1"' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m "phase 1"' >/dev/null
    assert_eq 'phase advanced to 2' "$(sget phase)" '2'

    # A broken reviewer command proves the final review is never invoked here: the tree is
    # unchanged since the last approval, so neither the unreviewed-work sweep nor a final
    # review has anything to call it for, and the turn ends paused instead.
    out=$(with_env OCRL_REVIEWER_CMD='/nonexistent/reviewer' stop_gate)
    assert_contains 'the turn ends paused' "$out" 'paused'
    assert_contains 'not an approval of the whole plan' "$out" 'NOT an approval'
    assert_eq 'no final review ran' "$(sget final_done_tree)" ''
    assert_eq 'the activation did not complete' "$(sget status)" 'ACTIVE'

    NEW_SESSION="sess-$CASE_N-resumed"
    out=$(ocrl resume --session "$NEW_SESSION" --until 2 2>&1)
    assert_contains 'resumed with the new pause target' "$out" 'pause target: 2 of 2'
    SESSION=$NEW_SESSION

    printf 'phase two\n' >"$REPO/b.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m "phase 2"' >/dev/null
    commit_now 'phase 2'
    confirm 'git add -A && git commit -m "phase 2"' >/dev/null
    d=$(with_env OCRL_FAKE_MODE=approve stop_decision)
    assert_eq 'the turn ends' "$d" 'ok'
    assert_eq 'the final review ran and the activation completed' "$(sget_for "$NEW_SESSION" status)" 'COMPLETE'
fi

# --------------------------------------------------------------------------
# TTL
# --------------------------------------------------------------------------

if start 'ttl: an expired activation blocks and never silently disarms'; then
    new_case
    arm_ok && phases_ok
    f=$(state_file)
    jq '.armed_at = 1' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    assert_eq 'a mutation is denied' "$(pre Edit)" 'deny'
    assert_contains 'and asks for a re-arm' "$(pre_reason)" 'Re-arm with'
    with_env OCRL_REVIEWER_CMD='/nonexistent/reviewer' assert_eq 'the turn cannot end' "$(stop_decision)" 'block'
fi

# --------------------------------------------------------------------------
# Hot path
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
                (cd "$REPO" && strace -f -e trace=execve -o "$out" "$OCRL" pretool >/dev/null 2>&1)
            # Only successful execs count; the shebang's PATH probe fails cheaply.
            grep -c 'execve.*= 0' "$out" 2>/dev/null || printf '0'
        }
        read_procs=$(trace_procs Read)
        edit_procs=$(trace_procs Edit)

        # The dispatcher runs on every tool call, so this is a real budget, not
        # a style preference. See "Hot-path rules" in AGENTS.md.
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
    for b in bash sh cat printf timeout env grep sed cut git mktemp true false ls find head tr; do
        p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$nopy/$b"
    done

    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$nopy" "$OCRL" pretool))
    rc=$?
    assert_eq 'pretool: the shim itself exits 0, not 127' "$rc" '0'
    assert_eq 'pretool denies rather than failing open' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
    assert_contains 'and names the missing interpreter' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'python3'

    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$nopy" "$OCRL" confirm-commit))
    rc=$?
    assert_eq 'confirm-commit: the shim itself exits 0' "$rc" '0'
    assert_contains 'and reports the failure rather than staying silent' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'python3'

    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$nopy" "$OCRL" posttool-failure))
    rc=$?
    assert_eq 'posttool-failure: the shim itself exits 0' "$rc" '0'
    assert_eq 'and stays silent, matching its ordinary behaviour' "$out" ''

    out=$(hook_payload Stop | (cd "$REPO" && PATH="$nopy" "$OCRL" gate-stop))
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

    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$fakepy:$PATH" "$OCRL" pretool))
    assert_eq 'pretool: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | wc -l)" '1'
    assert_eq 'and it denies' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'

    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$fakepy:$PATH" "$OCRL" confirm-commit))
    assert_eq 'confirm-commit: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | wc -l)" '1'
    assert_contains 'and it reports the failure' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'could not run'

    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$fakepy:$PATH" "$OCRL" posttool-failure))
    assert_eq 'posttool-failure: exactly zero bytes, not the fragment' "$out" ''

    out=$(hook_payload Stop | (cd "$REPO" && PATH="$fakepy:$PATH" "$OCRL" gate-stop))
    assert_eq 'gate-stop: exactly one valid JSON object, not the fragment' \
        "$(printf '%s' "$out" | jq -c . 2>/dev/null | wc -l)" '1'
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
    out=$(hook_payload PreToolUse | (cd "$REPO" && PATH="$hangpy:$PATH" OCRL_SHIM_TIMEOUT_PRETOOL=1 "$OCRL" pretool))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'pretool: a hung parser still denies' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "pass"')" 'deny'
    assert_contains 'and names the timeout' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""')" 'timed out'
    if [ "$elapsed" -le 15 ]; then ok "pretool returned in ${elapsed}s, not after a real hook timeout"; else
        bad 'pretool returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload PostToolUse | (cd "$REPO" && PATH="$hangpy:$PATH" OCRL_SHIM_TIMEOUT_CONFIRM_COMMIT=1 "$OCRL" confirm-commit))
    elapsed=$(($(date +%s) - t0))
    assert_contains 'confirm-commit: reports the timeout rather than hanging' \
        "$(printf '%s' "$out" | jq -r '.hookSpecificOutput.additionalContext // ""')" 'timed out'
    if [ "$elapsed" -le 15 ]; then ok "confirm-commit returned in ${elapsed}s"; else
        bad 'confirm-commit returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload PostToolUseFailure | (cd "$REPO" && PATH="$hangpy:$PATH" OCRL_SHIM_TIMEOUT_POSTTOOL_FAILURE=1 "$OCRL" posttool-failure))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'posttool-failure: stays silent rather than hanging' "$out" ''
    if [ "$elapsed" -le 15 ]; then ok "posttool-failure returned in ${elapsed}s"; else
        bad 'posttool-failure returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi

    t0=$(date +%s)
    out=$(hook_payload Stop | (cd "$REPO" && PATH="$hangpy:$PATH" OCRL_SHIM_TIMEOUT_GATE_STOP=1 "$OCRL" gate-stop))
    elapsed=$(($(date +%s) - t0))
    assert_eq 'gate-stop: a hung parser still blocks' \
        "$(printf '%s' "$out" | jq -r '.decision // "ok"')" 'block'
    if [ "$elapsed" -le 15 ]; then ok "gate-stop returned in ${elapsed}s"; else
        bad 'gate-stop returned before a real hook timeout' "${elapsed}s" '<=15s'
    fi
fi

if start 'shim contract: OCRL_SHIM_TIMEOUT_* cannot loosen or disable the timeout'; then
    new_case
    arm_ok && phases_ok

    # A spy, not a stub: it records the duration the shim actually asked
    # `timeout` for, then runs the real command so the hook still completes.
    # `timeout 0` means "no limit" to both GNU and uutils coreutils, so an
    # override of 0 would be exactly as dangerous as removing the wrapper
    # entirely -- this is the regression the clamp in ocrl_bounded_timeout
    # exists to prevent.
    faketimeout="$CASE_DIR/fake-timeout-bin"
    mkdir -p "$faketimeout"
    cat >"$faketimeout/timeout" <<'PYEOF'
#!/usr/bin/env bash
printf '%s' "$1" >"$OCRL_TEST_TIMEOUT_LOG"
shift
exec "$@"
PYEOF
    chmod +x "$faketimeout/timeout"

    logged() {
        local value=$1
        rm -f "$CASE_DIR/log.txt"
        hook_payload PreToolUse |
            (cd "$REPO" && PATH="$faketimeout:$PATH" OCRL_TEST_TIMEOUT_LOG="$CASE_DIR/log.txt" OCRL_SHIM_TIMEOUT_PRETOOL="$value" "$OCRL" pretool) >/dev/null
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

# --------------------------------------------------------------------------
# Reporting surfaces
# --------------------------------------------------------------------------

if start 'status and report render'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=changes pre Bash 'git add -A && git commit -m x' >/dev/null
    s=$(ocrl status)
    assert_contains 'status names the phase' "$s" 'phase:               1 of 2'
    assert_contains 'status lists the reports' "$s" 'changes_required'
    r=$(ocrl report)
    assert_contains 'the stored report holds the raw output' "$r" 'Raw reviewer output'
    assert_contains 'and the blocking findings' "$r" 'Returns success on a failed lookup'
fi

if start 'dry-run prints the exact invocation without calling the reviewer'; then
    new_case
    arm_ok && phases_ok
    printf 'x\n' >"$REPO/a.txt"
    out=$(ocrl dry-run 2>&1)
    assert_contains 'the model flag is shown' "$out" '-m'
    assert_contains 'the bundle is attached' "$out" 'range.txt'
    assert_contains 'the permission object is shown' "$out" 'external_directory'
    assert_contains 'the prompt is shown' "$out" 'adversarial code reviewer'
fi

# --------------------------------------------------------------------------

printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
