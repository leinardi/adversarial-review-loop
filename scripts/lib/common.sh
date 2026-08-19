# shellcheck shell=bash
# Shared helpers: paths, logging, JSON hook I/O, fail-closed decision emitters.

# shellcheck disable=SC2034  # consumed by the other library files
OCRL_VERSION="0.1.0"

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ocrl_state_root() {
    printf '%s' "${OCRL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/opencode-review-loop}"
}

ocrl_sessions_dir() {
    printf '%s' "$(ocrl_state_root)/sessions"
}

ocrl_sha256() {
    local out
    out=$(printf '%s' "$1" | sha256sum)
    printf '%s' "${out%% *}"
}

# Activation directory for a (worktree, session) pair.
ocrl_activation_dir() {
    local worktree=$1 session=$2
    printf '%s' "$(ocrl_state_root)/worktrees/$(ocrl_sha256 "$worktree")/$session"
}

ocrl_have() { command -v "$1" >/dev/null 2>&1; }

ocrl_log() { printf 'ocrl: %s\n' "$*" >&2; }

# Resolve the git worktree root of a directory. Empty on failure.
ocrl_repo_root() {
    local dir=$1
    [ -d "$dir" ] || return 0
    git -C "$dir" rev-parse --show-toplevel 2>/dev/null || true
}

# --------------------------------------------------------------------------
# Hook input
# --------------------------------------------------------------------------

OCRL_HOOK_INPUT='{}'

# The fields every hook entrypoint needs, pulled out in one pass. This runs on
# every single tool call, so it deliberately spawns one process, not five: the
# old field-at-a-time helper cost a jq per lookup.
OCRL_SESSION_ID=''
OCRL_CWD=''
OCRL_TOOL=''
OCRL_CMD=''

ocrl_read_hook_input() {
    local raw
    # A builtin redirect, not $(cat) -- one fewer process on the hot path.
    raw=$(</dev/stdin)
    [ -n "$raw" ] || raw='{}'
    OCRL_HOOK_INPUT=$raw
    ocrl_hook_parse
}

# Populates OCRL_SESSION_ID / OCRL_CWD / OCRL_TOOL / OCRL_CMD from the payload.
# NUL-delimited because a Bash command may legitimately contain newlines.
ocrl_hook_parse() {
    local -a v=()
    mapfile -d '' -t v < <(
        jq -j '
            def s: if . == null then "" elif type == "string" then . else tostring end;
            (.session_id | s), "\u0000",
            (.cwd | s), "\u0000",
            (.tool_name | s), "\u0000",
            (.tool_input.command | s), "\u0000"
        ' <<<"$OCRL_HOOK_INPUT" 2>/dev/null
    )
    # A payload that will not parse yields no fields, and every caller then
    # treats the event as "not ours" or denies -- never as a silent allow.
    OCRL_SESSION_ID=${v[0]-}
    OCRL_CWD=${v[1]-}
    OCRL_TOOL=${v[2]-}
    OCRL_CMD=${v[3]-}
}

# --------------------------------------------------------------------------
# Fail-closed decision emitters
#
# Every hook entrypoint arms a trap. If the script dies before emitting a
# decision, the trap emits the configured fallback. For PreToolUse the
# fallback is deny: an internal error must never open the gate.
# --------------------------------------------------------------------------

OCRL_DECIDED=0
OCRL_FAILCLOSED=none # none | pretool | stop

ocrl_arm_failclosed() {
    OCRL_FAILCLOSED=$1
    trap ocrl_failclosed_exit EXIT
}

ocrl_failclosed_exit() {
    local rc=$?
    trap - EXIT
    [ "$OCRL_DECIDED" = "1" ] && exit 0
    case "$OCRL_FAILCLOSED" in
        pretool)
            ocrl_emit_pretool deny \
                "opencode-review-loop: internal gate error (exit $rc). Denying to fail closed. Run /opencode-review-loop:status, then /opencode-review-loop:stop if the mode is wedged."
            ;;
        stop)
            # A crashing Stop hook must not masquerade as approval.
            ocrl_emit_stop_block \
                "opencode-review-loop: internal error in the Stop gate (exit $rc). The final review did not run. Run /opencode-review-loop:status."
            ;;
        *) ;;
    esac
    exit 0
}

ocrl_emit_pretool() {
    local decision=$1 reason=$2
    OCRL_DECIDED=1
    jq -nc --arg d "$decision" --arg r "$reason" \
        '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:$d,permissionDecisionReason:$r}}'
    exit 0
}

ocrl_allow() { ocrl_emit_pretool allow "${1:-opencode-review-loop: allowed}"; }
ocrl_deny() { ocrl_emit_pretool deny "$1"; }

# Silent pass-through: no opinion, leave the normal permission flow alone.
ocrl_pass() {
    OCRL_DECIDED=1
    exit 0
}

ocrl_emit_posttool_context() {
    OCRL_DECIDED=1
    jq -nc --arg c "$1" \
        '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$c}}'
    exit 0
}

ocrl_emit_stop_block() {
    OCRL_DECIDED=1
    jq -nc --arg r "$1" '{decision:"block",reason:$r}'
    exit 0
}

ocrl_emit_stop_ok() {
    local msg=${1:-}
    OCRL_DECIDED=1
    if [ -n "$msg" ]; then
        jq -nc --arg m "$msg" '{systemMessage:$m}'
    fi
    exit 0
}

# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

# Truncate stdin to N bytes, appending a marker when it had to cut.
ocrl_truncate_bytes() {
    local limit=$1 text
    text=$(cat)
    if [ "${#text}" -le "$limit" ]; then
        printf '%s' "$text"
    else
        printf '%s\n\n[... truncated at %s bytes; the full report is on disk, print it with /opencode-review-loop:report ...]' \
            "${text:0:$limit}" "$limit"
    fi
}

# A bash builtin (4.2+), not date(1): this runs on the hot path too.
ocrl_now() { printf '%(%s)T' -1; }
