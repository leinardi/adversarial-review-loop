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

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

start() {
    CURRENT=$1
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

sget() {
    jq -r --arg k "$1" '.[$k] // "" | if type=="string" then . else tostring end' "$(state_file)"
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
# Snapshot layer
# --------------------------------------------------------------------------

if start 'snapshot: clean, dirty, untracked, ignored, no-op'; then
    new_case
    # shellcheck source=../scripts/lib/common.sh
    . "$PLUGIN_ROOT/scripts/lib/common.sh"
    # shellcheck source=../scripts/lib/config.sh
    . "$PLUGIN_ROOT/scripts/lib/config.sh"
    # shellcheck source=../scripts/lib/gitsnap.sh
    . "$PLUGIN_ROOT/scripts/lib/gitsnap.sh"
    ocrl_config_load "$REPO" >/dev/null

    t_clean=$(ocrl_snap_tree "$REPO")
    assert_eq 'clean worktree snapshots to the HEAD tree' "$t_clean" "$(ocrl_head_tree "$REPO")"

    printf 'x\n' >"$REPO/untracked.txt"
    t_untracked=$(ocrl_snap_tree "$REPO")
    if [ "$t_untracked" != "$t_clean" ]; then ok 'untracked content changes the snapshot'; else bad 'untracked content changes the snapshot'; fi

    printf 'untracked.txt\n' >"$REPO/.gitignore"
    git -C "$REPO" add .gitignore && git -C "$REPO" commit -qm ignore
    t_ignored=$(ocrl_snap_tree "$REPO")
    assert_eq 'ignored content is excluded from the snapshot' "$t_ignored" "$(ocrl_head_tree "$REPO")"

    if ocrl_worktree_clean "$REPO"; then ok 'a worktree holding only ignored files counts as clean'; else bad 'a worktree holding only ignored files counts as clean'; fi

    printf 'modified\n' >>"$REPO/seed.txt"
    if ocrl_worktree_clean "$REPO"; then bad 'a modified tracked file counts as dirty'; else ok 'a modified tracked file counts as dirty'; fi

    git -C "$REPO" checkout -q -- seed.txt
    over=$(ocrl_snap_oversized "$REPO" 1000000)
    assert_eq 'nothing oversized in a small repo' "$over" ''
    head -c 2000 /dev/zero | tr '\0' 'a' >"$REPO/big.bin"
    over=$(ocrl_snap_oversized "$REPO" 1000)
    assert_contains 'an oversized stageable file is reported' "$over" 'big.bin'
    rm -f "$REPO/big.bin"
fi

# --------------------------------------------------------------------------
# Command shape
# --------------------------------------------------------------------------

if start 'command shape: allowlist table'; then
    new_case
    # shellcheck source=../scripts/lib/cmdshape.sh
    . "$PLUGIN_ROOT/scripts/lib/cmdshape.sh"

    shape_ok() {
        if ocrl_cmd_validate_commit "$1"; then ok "accepted: $1"; else bad "accepted: $1" "$OCRL_CMD_ERROR"; fi
    }
    shape_no() {
        if ocrl_cmd_validate_commit "$1"; then bad "denied: $1" 'accepted'; else ok "denied: $1 ($OCRL_CMD_ERROR)"; fi
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
    shape_no 'git -C /other commit -m x'
    shape_no 'git commit -F msg.txt'
    shape_no 'git add -p && git commit -m x'
    shape_no 'sed -i s/a/b/ f && git commit -m x'
    shape_no 'git add -A & git commit -m x'
    shape_no 'git status'
    shape_no 'git commit -m'

    if ocrl_cmd_is_escape 'ocrl.sh finish'; then ok 'finish is recognised as an escape'; else bad 'finish is recognised as an escape'; fi
    if ocrl_cmd_is_escape '/x/y/ocrl.sh deactivate'; then ok 'deactivate is recognised as an escape'; else bad 'deactivate is recognised as an escape'; fi
    if ocrl_cmd_is_escape 'ocrl.sh status'; then bad 'status is not an escape'; else ok 'status is not an escape'; fi

    if reset_target=$(ocrl_cmd_reset_target 'git reset --soft HEAD^'); then
        assert_eq 'reset target parsed' "$reset_target" 'HEAD^'
    else
        bad 'reset target parsed' "$OCRL_CMD_ERROR"
    fi
    if ocrl_cmd_reset_target 'git reset --hard HEAD^' >/dev/null 2>&1; then
        bad 'hard reset rejected'
    else ok 'hard reset rejected'; fi
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
        "$(pre Bash "$OCRL scripts/ocrl.sh set-phases --phase 'a'")" 'allow'
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
    d=$(with_env OCRL_FAKE_MODE=approve stop_decision)
    assert_eq 'the turn ends' "$d" 'ok'
    assert_eq 'the activation is complete' "$(sget status)" 'COMPLETE'
    assert_eq 'and further commits are ungated' "$(pre Bash 'git commit --amend -m whatever')" 'pass'
fi

if start 'stop: a failing final review blocks rather than completing'; then
    new_case
    arm_ok && ocrl set-phases --phase 'only phase' >/dev/null 2>&1
    printf 'x\n' >"$REPO/a.txt"
    with_env OCRL_FAKE_MODE=approve pre Bash 'git add -A && git commit -m x' >/dev/null
    commit_now 'phase 1'
    confirm 'git add -A && git commit -m x' >/dev/null
    out=$(with_env OCRL_FAKE_MODE=changes stop_gate)
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
        if [ "$read_procs" -le 5 ]; then
            ok "a read-only tool costs $read_procs processes (budget 5)"
        else
            bad 'read-only tool process budget' "$read_procs processes" 'at most 5'
        fi
        if [ "$edit_procs" -gt "$read_procs" ]; then
            ok "a mutating tool legitimately costs more ($edit_procs vs $read_procs)"
        else
            bad 'the read-only hoist is doing nothing' \
                "Read=$read_procs Edit=$edit_procs" 'Read strictly cheaper than Edit'
        fi
    else
        ok 'process-budget guard skipped (strace unavailable or not permitted)'
    fi
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
