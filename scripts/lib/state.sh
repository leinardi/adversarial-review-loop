# shellcheck shell=bash
# Activation state: the session pointer, state.json, and the frozen artefacts.
#
# Layout ($XDG_STATE_HOME/opencode-review-loop):
#   sessions/<session_id>              -> worktree path of the armed activation
#   worktrees/<sha256(worktree)>/<session_id>/
#       state.json
#       plan.frozen.md
#       phases.frozen                  (one phase description per line)
#       reports/NNN-*.md
#       bundles/NNN/
#
# Nothing is ever written inside the repository under review.

OCRL_ACT_DIR=''
OCRL_STATE_FILE=''
OCRL_STATE='{}'

# --------------------------------------------------------------------------
# Session pointer
# --------------------------------------------------------------------------

ocrl_pointer_write() {
    local session=$1 worktree=$2 dir
    dir=$(ocrl_sessions_dir)
    mkdir -p "$dir"
    printf '%s\n' "$worktree" >"$dir/$session"
}

ocrl_pointer_read() {
    local session=$1 f
    f="$(ocrl_sessions_dir)/$session"
    [ -f "$f" ] || return 1
    head -n 1 "$f"
}

ocrl_pointer_clear() {
    rm -f "$(ocrl_sessions_dir)/$1"
}

# --------------------------------------------------------------------------
# state.json
# --------------------------------------------------------------------------

ocrl_state_bind() {
    local worktree=$1 session=$2
    OCRL_ACT_DIR=$(ocrl_activation_dir "$worktree" "$session")
    OCRL_STATE_FILE="$OCRL_ACT_DIR/state.json"
}

ocrl_state_exists() { [ -f "$OCRL_STATE_FILE" ]; }

ocrl_state_load() {
    if [ -f "$OCRL_STATE_FILE" ] && jq -e 'type=="object"' "$OCRL_STATE_FILE" >/dev/null 2>&1; then
        OCRL_STATE=$(jq -c . "$OCRL_STATE_FILE")
        return 0
    fi
    OCRL_STATE='{}'
    return 1
}

ocrl_state_save() {
    local tmp
    mkdir -p "$OCRL_ACT_DIR"
    tmp="$OCRL_STATE_FILE.tmp.$$"
    printf '%s\n' "$OCRL_STATE" | jq . >"$tmp"
    mv -f "$tmp" "$OCRL_STATE_FILE"
}

ocrl_get() {
    jq -r --arg k "$1" '.[$k] // "" | if type=="string" then . else tostring end' <<<"$OCRL_STATE"
}

ocrl_get_array() {
    jq -r --arg k "$1" '(.[$k] // []) | .[]' <<<"$OCRL_STATE"
}

# ocrl_set k v [k v ...] -- string values.
ocrl_set() {
    while [ "$#" -ge 2 ]; do
        OCRL_STATE=$(jq -c --arg k "$1" --arg v "$2" '.[$k]=$v' <<<"$OCRL_STATE")
        shift 2
    done
}

# ocrl_setj k <json> -- raw JSON values (numbers, booleans, arrays).
ocrl_setj() {
    while [ "$#" -ge 2 ]; do
        OCRL_STATE=$(jq -c --arg k "$1" --argjson v "$2" '.[$k]=$v' <<<"$OCRL_STATE")
        shift 2
    done
}

ocrl_state_new() {
    OCRL_STATE=$(jq -nc '{
        version: 1,
        status: "ARMED",
        reason: "",
        session_id: "",
        worktree: "",
        plan_path: "",
        baseline_tree: "",
        activation_commit: "",
        armed_at: 0,
        allow_dirty: false,
        phases: [],
        phase: 1,
        last_approved_tree: "",
        approved_trees: [],
        pending_approved_tree: "",
        pending_head: "",
        pending_command: "",
        bad_commit: "",
        bad_commit_parent: "",
        failures: 0,
        stop_blocks: 0,
        stop_marker: "",
        defers: 0,
        defer_pending: false,
        final_done_tree: "",
        report_seq: 0
    }')
}

# --------------------------------------------------------------------------
# Derived status
# --------------------------------------------------------------------------

# Effective status, taking the TTL into account. Never silently disarms:
# an expired activation becomes STALE, and STALE still blocks.
ocrl_effective_status() {
    local st armed_at ttl now
    st=$(ocrl_get status)
    case "$st" in
        COMPLETE | ARM_FAILED | NEEDS_HUMAN)
            printf '%s' "$st"
            return
            ;;
    esac
    armed_at=$(ocrl_get armed_at)
    ttl=$(ocrl_cfg ttl_hours)
    now=$(ocrl_now)
    if [ -n "$armed_at" ] && [ "$armed_at" -gt 0 ] 2>/dev/null &&
        [ "$ttl" -gt 0 ] 2>/dev/null &&
        [ $((now - armed_at)) -gt $((ttl * 3600)) ]; then
        printf 'STALE'
        return
    fi
    printf '%s' "$st"
}

ocrl_phase_count() {
    jq -r '(.phases // []) | length' <<<"$OCRL_STATE"
}

ocrl_phase_desc() {
    local n=$1
    jq -r --argjson n "$n" '(.phases // [])[$n-1] // ""' <<<"$OCRL_STATE"
}

ocrl_tree_approved() {
    local tree=$1
    jq -e --arg t "$tree" '((.approved_trees // []) | index($t)) != null' <<<"$OCRL_STATE" >/dev/null
}

ocrl_mark_tree_approved() {
    OCRL_STATE=$(jq -c --arg t "$1" '.approved_trees = ((.approved_trees // []) + [$t] | unique)' <<<"$OCRL_STATE")
}

# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------

ocrl_needs_human() {
    ocrl_set status NEEDS_HUMAN reason "$1"
    ocrl_state_save
}

ocrl_arm_failed() {
    ocrl_set status ARM_FAILED reason "$1"
    ocrl_state_save
}
