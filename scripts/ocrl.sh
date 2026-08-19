#!/usr/bin/env bash
# opencode-review-loop -- single entrypoint for the arming, gating and review
# machinery. Every hook registration and every slash command routes through
# here.
#
# Subcommands:
#   arm | set-phases | pretool | confirm-commit | posttool-failure | gate-stop
#   status | report | defer | finish | deactivate | dry-run | selftest

set -uo pipefail

OCRL_SELF=${BASH_SOURCE[0]}
# Parameter expansion rather than dirname(1): this file is re-executed on every
# tool call, so two forks here are two forks per hook.
OCRL_SCRIPT_DIR=$(cd -- "${OCRL_SELF%/*}" && pwd)
OCRL_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-${OCRL_SCRIPT_DIR%/*}}

# shellcheck source=lib/common.sh
. "$OCRL_SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/config.sh
. "$OCRL_SCRIPT_DIR/lib/config.sh"
# shellcheck source=lib/state.sh
. "$OCRL_SCRIPT_DIR/lib/state.sh"
# shellcheck source=lib/gitsnap.sh
. "$OCRL_SCRIPT_DIR/lib/gitsnap.sh"
# shellcheck source=lib/cmdshape.sh
. "$OCRL_SCRIPT_DIR/lib/cmdshape.sh"
# shellcheck source=lib/reviewer.sh
. "$OCRL_SCRIPT_DIR/lib/reviewer.sh"
# shellcheck source=lib/report.sh
. "$OCRL_SCRIPT_DIR/lib/report.sh"

# Tools that cannot change the repository. Everything else is treated as a
# mutation, so an unknown or new tool fails closed rather than open.
OCRL_READONLY_TOOLS=" Read Grep Glob LS NotebookRead WebFetch WebSearch TodoWrite \
AskUserQuestion Skill SlashCommand ToolSearch ExitPlanMode EnterPlanMode TaskOutput \
ListMcpResourcesTool ReadMcpResourceTool ReadMcpResourceDirTool "

ocrl_tool_is_readonly() {
    [[ $OCRL_READONLY_TOOLS == *" $1 "* ]]
}

# --------------------------------------------------------------------------
# Activation lookup
# --------------------------------------------------------------------------

ocrl_latest_pointer_path() {
    printf '%s' "$(ocrl_state_root)/worktrees/$(ocrl_sha256 "$1")/latest"
}

# Resolve the activation for a command run from the shell (no hook payload).
# Prefers an explicit --session, then the worktree's most recent activation.
ocrl_resolve_local_activation() {
    local session=${1:-} repo latest
    repo=$(ocrl_repo_root "$PWD")
    [ -n "$repo" ] || repo=$PWD
    if [ -z "$session" ]; then
        latest=$(ocrl_latest_pointer_path "$repo")
        [ -f "$latest" ] && session=$(head -n 1 "$latest")
    fi
    [ -n "$session" ] || return 1
    ocrl_config_load "$repo" >/dev/null
    ocrl_state_bind "$repo" "$session"
    ocrl_state_load || return 1
    OCRL_REPO=$repo
    OCRL_SESSION=$session
    return 0
}

# --------------------------------------------------------------------------
# arm
# --------------------------------------------------------------------------

ocrl_arm_die() {
    local reason=$1
    ocrl_state_exists || ocrl_state_new
    ocrl_set status ARM_FAILED reason "$reason" \
        session_id "$OCRL_SESSION" worktree "$OCRL_REPO"
    ocrl_setj armed_at "$(ocrl_now)"
    ocrl_state_save
    ocrl_pointer_write "$OCRL_SESSION" "$OCRL_REPO"
    mkdir -p "$(dirname -- "$(ocrl_latest_pointer_path "$OCRL_REPO")")"
    printf '%s\n' "$OCRL_SESSION" >"$(ocrl_latest_pointer_path "$OCRL_REPO")"
    cat <<EOF
**opencode-review-loop: ARMING FAILED — the review loop is NOT active.**

Reason: $reason

Every file mutation and every commit in this worktree is denied until this is
resolved. Do not attempt to implement the plan.

Ways out:
  - fix the cause and run \`/opencode-review-loop:implement <plan.md>\` again
  - abandon the mode with \`/opencode-review-loop:stop\`
EOF
    exit 1
}

cmd_arm() {
    local session='' plan='' dirty_flag='' arg
    while [ "$#" -gt 0 ]; do
        arg=$1
        case "$arg" in
            --session)
                session=${2:-}
                shift 2
                ;;
            --plan)
                plan=${2:-}
                shift 2
                ;;
            --allow-dirty)
                dirty_flag='--allow-dirty'
                shift
                ;;
            '')
                shift # an omitted positional arrives as an empty string
                ;;
            *)
                dirty_flag="$arg"
                shift
                ;;
        esac
    done

    OCRL_REPO=$(ocrl_repo_root "$PWD")
    [ -n "$OCRL_REPO" ] || OCRL_REPO=$PWD
    OCRL_SESSION=$session
    ocrl_config_load "$OCRL_REPO" >/dev/null

    if [ -z "$session" ]; then
        printf '%s\n' "**opencode-review-loop: ARMING FAILED** — no session id was supplied, so no state could be recorded. The review loop is NOT active; do not implement anything."
        exit 1
    fi
    ocrl_state_bind "$OCRL_REPO" "$session"
    ocrl_state_load || ocrl_state_new

    if [ -n "$dirty_flag" ] && [ "$dirty_flag" != '--allow-dirty' ]; then
        ocrl_arm_die "the second argument was \"$dirty_flag\"; the only accepted value is --allow-dirty"
    fi

    # -- plan path -------------------------------------------------------
    [ -n "$plan" ] || ocrl_arm_die 'no plan path was supplied (usage: /opencode-review-loop:implement <path-to-plan.md>)'
    if [[ ! $plan =~ ^[A-Za-z0-9._/@:+,~-][A-Za-z0-9._/@:+,~\ -]*$ ]]; then
        ocrl_arm_die "the plan path contains characters that are not safe to pass through shell expansion: \"$plan\""
    fi
    if [ "${plan:0:1}" = '~' ] && [ "${plan:1:1}" = '/' ]; then
        plan="$HOME/${plan:2}"
    fi
    [ -f "$plan" ] || ocrl_arm_die "the plan path does not resolve to an existing regular file: \"$plan\""
    [ -r "$plan" ] || ocrl_arm_die "the plan file is not readable: \"$plan\""

    # -- repository ------------------------------------------------------
    git -C "$OCRL_REPO" rev-parse --git-dir >/dev/null 2>&1 ||
        ocrl_arm_die "the working directory ($PWD) is not inside a git repository; the commit is the phase boundary, so a repository is required"

    # -- dirty check -----------------------------------------------------
    local allow_dirty='false'
    [ "$dirty_flag" = '--allow-dirty' ] && allow_dirty='true'
    [ "$(ocrl_cfg allow_dirty)" = 'true' ] && allow_dirty='true'

    if [ "$allow_dirty" != 'true' ] && ! ocrl_worktree_clean "$OCRL_REPO"; then
        ocrl_arm_die "the worktree is dirty. Either commit or stash the existing changes, or re-run with --allow-dirty to fold them into phase 1's review:
$(ocrl_dirty_summary "$OCRL_REPO")"
    fi

    # -- reviewer reachability ------------------------------------------
    local model probe
    model=$(ocrl_cfg model)
    if [ -z "${OCRL_REVIEWER_CMD:-}" ]; then
        # shellcheck disable=SC2016  # backticks are markdown, not substitution
        ocrl_have opencode || ocrl_arm_die 'the `opencode` binary is not on PATH, so the review gate cannot run'
        probe=$(timeout 60 opencode models 2>/dev/null || true)
        if [ -z "$probe" ]; then
            # shellcheck disable=SC2016  # backticks are markdown, not substitution
            ocrl_arm_die 'could not list OpenCode models (`opencode models` returned nothing); the reviewer is unreachable'
        fi
        if ! printf '%s\n' "$probe" | grep -Fxq "$model"; then
            ocrl_arm_die "the configured model \"$model\" is not among the models OpenCode reports. Set \`model\` in .opencode-review-loop.json or OCRL_MODEL to one that is."
        fi
    fi

    # -- freeze ----------------------------------------------------------
    local head_commit baseline
    head_commit=$(ocrl_head_commit "$OCRL_REPO")
    if [ -n "$head_commit" ]; then
        baseline=$(ocrl_head_tree "$OCRL_REPO")
    else
        baseline=$(git -C "$OCRL_REPO" hash-object -t tree /dev/null)
    fi

    mkdir -p "$OCRL_ACT_DIR"
    cp -- "$plan" "$OCRL_ACT_DIR/plan.frozen.md"

    ocrl_state_new
    ocrl_set status ARMED reason '' session_id "$session" worktree "$OCRL_REPO" \
        plan_path "$plan" baseline_tree "$baseline" activation_commit "$head_commit" \
        last_approved_tree "$baseline"
    ocrl_setj armed_at "$(ocrl_now)" allow_dirty "$allow_dirty"
    ocrl_mark_tree_approved "$baseline"
    ocrl_state_save

    ocrl_pointer_write "$session" "$OCRL_REPO"
    mkdir -p "$(dirname -- "$(ocrl_latest_pointer_path "$OCRL_REPO")")"
    printf '%s\n' "$session" >"$(ocrl_latest_pointer_path "$OCRL_REPO")"

    cat <<EOF
**opencode-review-loop is ARMED for this worktree.**

- repository: $OCRL_REPO
- plan: $plan (frozen copy: $OCRL_ACT_DIR/plan.frozen.md)
- baseline tree: $baseline
- activation commit: ${head_commit:-<empty repository>}
- reviewer: $model$([ -n "$(ocrl_cfg variant)" ] && printf ' (variant %s)' "$(ocrl_cfg variant)")
- pre-existing uncommitted work folded into phase 1: $allow_dirty
- block_severity: $(ocrl_cfg block_severity)

**Phases are not set yet, so every file mutation is currently denied.**

Do this first, and nothing else:

1. Read the frozen plan.
2. Split it into the smallest sensible sequence of phases, each of which ends in
   one commit.
3. Run exactly this, one \`--phase\` per phase, in order:

       $OCRL_PLUGIN_ROOT/scripts/ocrl.sh set-phases --phase "…" --phase "…"

After that the loop is: implement phase N -> \`git add -A && git commit -m "…"\`.
The commit is intercepted, the working tree is reviewed by OpenCode, and the
commit only proceeds when the review passes. Findings come back as a denial with
the full list; fix them and commit again.

Constraints while the mode is active:
- each phase must commit all of its work and leave a clean worktree
- \`git commit --amend\`, partial commits, and compound commands that build or
  mutate files before committing are denied
- run builds and tests as their own Bash calls, then commit separately
EOF
}

# --------------------------------------------------------------------------
# set-phases
# --------------------------------------------------------------------------

cmd_set_phases() {
    local session='' arg
    local -a phases=()
    while [ "$#" -gt 0 ]; do
        arg=$1
        case "$arg" in
            --session)
                session=${2:-}
                shift 2
                ;;
            --phase)
                phases+=("${2:-}")
                shift 2
                ;;
            *) shift ;;
        esac
    done

    if ! ocrl_resolve_local_activation "$session"; then
        printf 'opencode-review-loop: no activation found for this worktree. Run /opencode-review-loop:implement <plan.md> first.\n' >&2
        exit 1
    fi

    local st
    st=$(ocrl_effective_status)
    case "$st" in
        ARMED) ;;
        ACTIVE | RECONCILE)
            printf 'opencode-review-loop: the phase list is already frozen (%s phases, currently on phase %s). It cannot be changed for this activation.\n' \
                "$(ocrl_phase_count)" "$(ocrl_get phase)" >&2
            exit 1
            ;;
        *)
            printf 'opencode-review-loop: cannot set phases while the activation is %s (%s).\n' "$st" "$(ocrl_get reason)" >&2
            exit 1
            ;;
    esac

    if [ "${#phases[@]}" -eq 0 ]; then
        printf 'opencode-review-loop: at least one --phase "…" is required.\n' >&2
        exit 1
    fi
    if [ "${#phases[@]}" -gt 30 ]; then
        printf 'opencode-review-loop: %s phases is more than the 30 this gate accepts; group them.\n' "${#phases[@]}" >&2
        exit 1
    fi
    local p
    for p in "${phases[@]}"; do
        if [ -z "${p// /}" ]; then
            printf 'opencode-review-loop: empty phase description.\n' >&2
            exit 1
        fi
    done

    printf '%s\n' "${phases[@]}" >"$OCRL_ACT_DIR/phases.frozen"
    local json
    json=$(printf '%s\n' "${phases[@]}" | jq -Rsc 'split("\n") | map(select(length>0))')
    ocrl_setj phases "$json" phase 1
    ocrl_set status ACTIVE reason ''
    ocrl_state_save

    printf 'opencode-review-loop: %s phases frozen. Now on phase 1 of %s:\n\n' "${#phases[@]}" "${#phases[@]}"
    jq -r '(.phases // []) | to_entries[] | "  \(.key+1). \(.value)"' <<<"$OCRL_STATE"
    printf '\nImplement phase 1, then commit it. The commit is the review gate.\n'
}

# --------------------------------------------------------------------------
# pretool
# --------------------------------------------------------------------------

ocrl_deny_preamble() {
    printf 'opencode-review-loop is enforcing an external review gate in this worktree.\n\n'
}

# Commit gate. Runs the review and decides on the Bash call that would commit.
ocrl_gate_commit() {
    local repo=$1 cmd=$2
    local tree phase base oversized head

    if ! ocrl_cmd_validate_commit "$cmd"; then
        ocrl_deny "$(
            ocrl_deny_preamble
            cat <<EOF
This commit command was not accepted: $OCRL_CMD_ERROR

Only these shapes are accepted, because nothing in them can change working-tree
content after the gate has snapshotted it:

  git commit -m "…"
  git add -A && git commit -m "…"
  git add -A && git status && git commit -m "…"

Run builds, tests, formatters and \`git rm\` as their own separate Bash calls
first; the next snapshot picks their result up. Then commit with one of the
shapes above.
EOF
        )"
    fi

    # A pending approval from an earlier attempt is stale by definition here.
    if [ -n "$(ocrl_get pending_approved_tree)" ]; then
        ocrl_set pending_approved_tree '' pending_head '' pending_command ''
        ocrl_state_save
    fi

    oversized=$(ocrl_snap_oversized "$repo" "$(ocrl_cfg max_file_bytes)")
    if [ -n "$oversized" ]; then
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'These files are above max_file_bytes (%s) and would be committed without ever being snapshotted for review:\n\n%s\n\nGitignore them, commit them deliberately outside this mode, or raise max_file_bytes. Nothing is silently dropped from the snapshot.\n' \
                "$(ocrl_cfg max_file_bytes)" "$oversized"
        )"
    fi

    tree=$(ocrl_snap_tree "$repo")
    if [ -z "$tree" ]; then
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'The working state could not be snapshotted: %s\n\nThe commit is denied because there is nothing to review against.\n' "$OCRL_SNAP_ERROR"
        )"
    fi

    phase=$(ocrl_get phase)
    base=$(ocrl_get last_approved_tree)
    head=$(ocrl_head_commit "$repo")

    ocrl_gate_approve() {
        ocrl_mark_tree_approved "$tree"
        ocrl_set pending_approved_tree "$tree" pending_head "$head" pending_command "$cmd"
        ocrl_setj failures 0
        ocrl_state_save
        ocrl_allow "$1"
    }

    if [ "$tree" = "$base" ]; then
        ocrl_gate_approve "opencode-review-loop: the working state is byte-identical to the last approved tree, so no review was needed. Commit allowed."
    fi
    if ocrl_tree_approved "$tree"; then
        ocrl_gate_approve "opencode-review-loop: this exact tree ($tree) was already approved by a previous review. Commit allowed."
    fi
    if [ -z "$(ocrl_git "$repo" diff --name-only "$base" "$tree" 2>/dev/null)" ]; then
        ocrl_gate_approve "opencode-review-loop: no content difference from the last approved tree. Commit allowed."
    fi
    if ocrl_all_paths_ignored "$repo" "$base" "$tree"; then
        ocrl_gate_approve "opencode-review-loop: every changed path matches ignore_globs, so the review was skipped. Commit allowed."
    fi

    ocrl_review_execute "$repo" "$base" "$tree" phase "$phase"

    case "$OCRL_REVIEW_VERDICT" in
        APPROVED)
            ocrl_gate_approve "$(
                printf 'opencode-review-loop: OpenCode approved phase %s.\n' "$phase"
                [ -n "$OCRL_REVIEW_ALL" ] && printf '\nNon-blocking findings (recorded, not required):\n%s' "$OCRL_REVIEW_ALL"
                printf '\nFull report: %s\n' "$OCRL_REVIEW_REPORT"
            )"
            ;;
        CHANGES_REQUIRED)
            ocrl_state_save
            ocrl_deny "$(ocrl_report_reason "opencode-review-loop: OpenCode requires changes before phase $phase can be committed.")"
            ;;
        NEEDS_HUMAN)
            ocrl_needs_human "$OCRL_REVIEW_ERROR"
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'This escalated to NEEDS_HUMAN and is not an approval.\n\n%s\n\nFull report: %s\n\nStop here and tell the user. The gate keeps denying until they run /opencode-review-loop:stop.\n' \
                    "$OCRL_REVIEW_ERROR" "${OCRL_REVIEW_REPORT:-<none>}"
            )"
            ;;
        *)
            local failures max_failures
            failures=$(($(ocrl_get failures) + 1))
            max_failures=$(ocrl_cfg max_failures)
            ocrl_setj failures "$failures"
            ocrl_state_save
            if [ "$failures" -gt "$max_failures" ]; then
                ocrl_needs_human "the reviewer failed $failures times in a row: $OCRL_REVIEW_ERROR"
                ocrl_deny "$(
                    ocrl_deny_preamble
                    printf 'The reviewer failed %s times in a row (limit %s), so this escalated to NEEDS_HUMAN. A failed review is never an approval.\n\nLast failure: %s\n\nStop here and tell the user.\n' \
                        "$failures" "$max_failures" "$OCRL_REVIEW_ERROR"
                )"
            fi
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'The review could not be completed (%s of %s consecutive failures), so the commit is denied. A failed review is never an approval.\n\n%s\n\nRaw output: %s\n\nCommit again to retry.\n' \
                    "$failures" "$max_failures" "$OCRL_REVIEW_ERROR" "${OCRL_REVIEW_RAW:-<none>}"
            )"
            ;;
    esac
}

# Bounded `git reset --soft` during reconcile.
ocrl_gate_reset() {
    local repo=$1 cmd=$2 target resolved parent activation
    target=$(ocrl_cmd_reset_target "$cmd") || ocrl_deny "$(
        ocrl_deny_preamble
        printf 'The recovery reset was rejected: %s\n\nDuring reconcile the only permitted command is:\n\n  git reset --soft %s\n' \
            "$OCRL_CMD_ERROR" "$(ocrl_get bad_commit_parent)"
    )"

    resolved=$(ocrl_git "$repo" rev-parse --verify --quiet "$target^{commit}" 2>/dev/null || true)
    parent=$(ocrl_get bad_commit_parent)
    activation=$(ocrl_get activation_commit)

    if [ -z "$resolved" ]; then
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'The reset target "%s" does not resolve to a commit. The only permitted target is %s.\n' "$target" "$parent"
        )"
    fi
    if [ "$resolved" != "$parent" ]; then
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'The reset target resolves to %s, but the only permitted target during this reconcile is the diverging commit'"'"'s own parent, %s.\n' \
                "$resolved" "$parent"
        )"
    fi
    if [ -n "$activation" ] && [ "$resolved" != "$activation" ]; then
        if ocrl_git "$repo" merge-base --is-ancestor "$resolved" "$activation" 2>/dev/null; then
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'The reset target %s is strictly before the activation commit %s. The loop may not rewind history that predates it.\n' \
                    "$resolved" "$activation"
            )"
        fi
    fi
    ocrl_allow "opencode-review-loop: bounded recovery reset to $resolved permitted. Rebuild the intended complete tree, then commit again."
}

# The dispatcher only ever runs because the implement skill was invoked in this
# session -- that is what registers the hooks. So reaching a hook with no
# session pointer means `ocrl arm` never executed at all: a denied sandbox, a
# missing interpreter, an unreadable script, a ${CLAUDE_PLUGIN_ROOT} that did
# not resolve. cmd_arm persists ARM_FAILED for every failure it can observe,
# but it cannot persist a failure to start.
#
# Record it here instead, so the very next call takes the ordinary ARM_FAILED
# path with all of its counting and messaging. Enforcement was requested and is
# not running; passing would be the one outcome the design exists to prevent.
ocrl_record_unstarted_arm() {
    local session=$1 cwd=$2 repo
    repo=$(ocrl_repo_root "$cwd")
    [ -n "$repo" ] || repo=$cwd
    ocrl_config_load "$repo" >/dev/null
    ocrl_state_bind "$repo" "$session"
    ocrl_state_load || ocrl_state_new
    ocrl_set status ARM_FAILED worktree "$repo" session_id "$session" \
        reason 'arming never executed: the hooks registered, but ocrl.sh arm did not run at prompt-expansion time. Nothing was frozen and nothing has been reviewed.'
    ocrl_setj armed_at "$(ocrl_now)"
    ocrl_state_save
    ocrl_pointer_write "$session" "$repo"
    mkdir -p "${OCRL_ACT_DIR%/*}"
    printf '%s\n' "$session" >"$(ocrl_latest_pointer_path "$repo")"
}

cmd_pretool() {
    ocrl_arm_failclosed pretool
    ocrl_read_hook_input

    local session cwd tool cmd repo worktree st
    session=$OCRL_SESSION_ID
    cwd=$OCRL_CWD
    tool=$OCRL_TOOL
    cmd=$OCRL_CMD
    [ -n "$cwd" ] || cwd=$PWD

    # No pointer for this session -> arming never executed. Fail closed.
    if ! worktree=$(ocrl_pointer_read "$session") || [ -z "$worktree" ]; then
        ocrl_tool_is_readonly "$tool" && ocrl_pass
        ocrl_record_unstarted_arm "$session" "$cwd"
        ocrl_deny "$(
            ocrl_deny_preamble
            cat <<'EOF'
Arming never ran, so nothing was frozen, nothing is being reviewed, and this
mutation is denied.

The hooks registered, which means /opencode-review-loop:implement was invoked,
but the arm command itself never executed. The usual causes are a sandbox that
refused to run it, an unreadable or non-executable scripts/ocrl.sh, or a
missing interpreter.

Tell the user. They can look at the error the slash command printed, fix the
cause and run /opencode-review-loop:implement <plan.md> again, or leave the
mode with /opencode-review-loop:stop.

Do not implement the plan: enforcement was requested and is not running.
EOF
        )"
    fi

    # The overwhelmingly common case is cwd already being the armed worktree
    # root, and that needs no git process to establish.
    if [ "$cwd" = "$worktree" ]; then
        repo=$worktree
    else
        repo=$(ocrl_repo_root "$cwd")
        [ -n "$repo" ] || repo=$cwd
    fi
    # Work outside the armed worktree in the same session is untouched.
    [ "$repo" = "$worktree" ] || ocrl_pass

    # A read-only tool is permitted in *every* state -- each branch below ends
    # up calling ocrl_pass for one. Answering here avoids loading the config and
    # the state to reach a foregone conclusion, and Read/Grep/Glob are the bulk
    # of all tool traffic. If a future state ever needs to deny a read-only
    # tool, this hoist has to come out first.
    ocrl_tool_is_readonly "$tool" && ocrl_pass

    ocrl_config_load "$repo" >/dev/null
    ocrl_state_bind "$worktree" "$session"

    if ! ocrl_state_load; then
        # The dispatcher ran, so the skill was invoked, but no state exists.
        # That is the fail-open case, and it fails closed instead.
        ocrl_tool_is_readonly "$tool" && ocrl_pass
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'The activation state for this session is missing, so the gate cannot tell what has been reviewed. Every mutation and commit is denied.\n\nRun /opencode-review-loop:implement <plan.md> again, or /opencode-review-loop:stop to leave the mode.\n'
        )"
    fi

    st=$(ocrl_effective_status)

    case "$st" in
        COMPLETE | DISARMED) ocrl_pass ;;
        ARM_FAILED)
            ocrl_tool_is_readonly "$tool" && ocrl_pass
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'Arming failed, so the review loop is NOT active and nothing may be changed.\n\nReason: %s\n\nRun /opencode-review-loop:implement <plan.md> again, or /opencode-review-loop:stop to leave the mode.\n' \
                    "$(ocrl_get reason)"
            )"
            ;;
        NEEDS_HUMAN)
            ocrl_tool_is_readonly "$tool" && ocrl_pass
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'The loop escalated to NEEDS_HUMAN and every mutation is denied until the user intervenes.\n\nReason: %s\n\nStop and tell the user. They can leave the mode with /opencode-review-loop:stop.\n' \
                    "$(ocrl_get reason)"
            )"
            ;;
        STALE)
            ocrl_tool_is_readonly "$tool" && ocrl_pass
            ocrl_deny "$(
                ocrl_deny_preamble
                printf 'This activation is older than ttl_hours (%s) and its baseline can no longer be trusted. It blocks rather than silently disarming.\n\nRe-arm with /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop.\n' \
                    "$(ocrl_cfg ttl_hours)"
            )"
            ;;
    esac

    # The escapes are user-only; Claude's route is Bash, and it is denied.
    if [ "$tool" = 'Bash' ] && ocrl_cmd_is_escape "$cmd"; then
        ocrl_deny "$(
            ocrl_deny_preamble
            printf 'ocrl finish and ocrl deactivate are user-only escapes. You may not end the review loop yourself.\n\nIf you believe the loop should end, say so and let the user run /opencode-review-loop:finish or /opencode-review-loop:stop.\n'
        )"
    fi

    if [ "$st" = 'ARMED' ]; then
        ocrl_tool_is_readonly "$tool" && ocrl_pass
        if [ "$tool" = 'Bash' ] && printf '%s' "$cmd" | grep -q 'ocrl\(\.sh\)\?[[:space:]]\+set-phases'; then
            ocrl_allow 'opencode-review-loop: set-phases is the one command allowed before the phase list is frozen.'
        fi
        ocrl_deny "$(
            ocrl_deny_preamble
            cat <<EOF
The phase list has not been frozen yet, so nothing may be changed.

Read the frozen plan ($OCRL_ACT_DIR/plan.frozen.md), then run exactly:

    $OCRL_PLUGIN_ROOT/scripts/ocrl.sh set-phases --phase "…" --phase "…"

one --phase per phase, in order. Reading the repository is allowed; changing it
is not, until that command has run.
EOF
        )"
    fi

    if [ "$tool" != 'Bash' ]; then
        ocrl_pass
    fi

    if [ "$st" = 'RECONCILE' ] && ocrl_cmd_mentions_reset "$cmd"; then
        ocrl_gate_reset "$repo" "$cmd"
    fi

    if ocrl_cmd_mentions_commit "$cmd"; then
        ocrl_gate_commit "$repo" "$cmd"
    fi

    if ocrl_cmd_mentions_reset "$cmd"; then
        local target resolved head
        if target=$(ocrl_cmd_reset_target "$cmd" 2>/dev/null); then
            resolved=$(ocrl_git "$repo" rev-parse --verify --quiet "$target^{commit}" 2>/dev/null || true)
            head=$(ocrl_head_commit "$repo")
            if [ -n "$resolved" ] && [ "$resolved" != "$head" ]; then
                ocrl_deny "$(
                    ocrl_deny_preamble
                    printf 'Moving HEAD off a reviewed commit is only permitted during a reconcile. Every commit in this history is a tree that was actually reviewed, and rewinding one breaks that guarantee.\n'
                )"
            fi
        fi
    fi

    ocrl_pass
}

# --------------------------------------------------------------------------
# confirm-commit (PostToolUse) / posttool-failure
# --------------------------------------------------------------------------

ocrl_enter_reconcile() {
    local detail=$1 bad=$2 parent=$3
    ocrl_set status RECONCILE reason "$detail" bad_commit "$bad" bad_commit_parent "$parent" \
        pending_approved_tree '' pending_head '' pending_command ''
    ocrl_state_save
    ocrl_emit_posttool_context "$(
        cat <<EOF
**opencode-review-loop: the commit that landed is not the tree that was reviewed.**

$detail

The phase has NOT advanced. Recover explicitly, in this order:

1. \`git reset --soft $parent\`   (permitted only during this reconcile)
2. rebuild the intended complete tree for this phase
3. commit again with \`git add -A && git commit -m "…"\` — it goes through the
   normal review gate

Do not commit forward on top of the diverging commit: the per-commit invariant
is recorded as broken and the final cumulative review will still cover it.
EOF
    )"
}

cmd_confirm_commit() {
    ocrl_read_hook_input

    local session cwd tool cmd repo worktree pending pending_head pending_cmd
    session=$OCRL_SESSION_ID
    cwd=$OCRL_CWD
    tool=$OCRL_TOOL
    cmd=$OCRL_CMD
    [ -n "$cwd" ] || cwd=$PWD
    [ "$tool" = 'Bash' ] || exit 0

    worktree=$(ocrl_pointer_read "$session") || exit 0
    if [ "$cwd" = "$worktree" ]; then
        repo=$worktree
    else
        repo=$(ocrl_repo_root "$cwd")
    fi
    [ "$repo" = "$worktree" ] || exit 0

    ocrl_config_load "$repo" >/dev/null
    ocrl_state_bind "$worktree" "$session"
    ocrl_state_load || exit 0

    pending=$(ocrl_get pending_approved_tree)
    [ -n "$pending" ] || exit 0
    pending_head=$(ocrl_get pending_head)
    pending_cmd=$(ocrl_get pending_command)
    # Only the very command the gate approved may consume the approval.
    [ "$cmd" = "$pending_cmd" ] || exit 0

    local head parent head_tree
    head=$(ocrl_head_commit "$repo")
    parent=$(ocrl_git "$repo" rev-parse --verify --quiet 'HEAD^' 2>/dev/null || true)
    head_tree=$(ocrl_head_tree "$repo")

    if [ -z "$head" ] || [ "$head" = "$pending_head" ]; then
        ocrl_enter_reconcile \
            "The command reported success but HEAD did not move, so no commit was created for the approved tree $pending." \
            "${head:-<none>}" "${pending_head:-$(ocrl_get activation_commit)}"
    fi
    if [ "$parent" != "$pending_head" ]; then
        ocrl_enter_reconcile \
            "The new commit $head has parent ${parent:-<none>}, but HEAD was ${pending_head:-<none>} when the review was approved. That is an amend or a rewrite, not a commit on top of the reviewed state." \
            "$head" "${parent:-$(ocrl_get activation_commit)}"
    fi
    if [ "$head_tree" != "$pending" ]; then
        ocrl_enter_reconcile \
            "The commit $head has tree $head_tree, but the approved tree was $pending. Something other than the reviewed snapshot was committed (a partial stage, a pathspec, or a change made between the gate and the commit)." \
            "$head" "$parent"
    fi
    if ! ocrl_worktree_clean "$repo"; then
        ocrl_enter_reconcile \
            "The commit $head matches the approved tree, but the worktree is not clean afterwards: content appeared between the gate and now. Every phase must commit all of its work." \
            "$head" "$parent"
    fi

    # -- the commit is exactly what was reviewed --------------------------
    local phase total next
    phase=$(ocrl_get phase)
    total=$(ocrl_phase_count)
    next=$((phase + 1))

    ocrl_set last_approved_tree "$pending" pending_approved_tree '' pending_head '' \
        pending_command '' status ACTIVE reason ''
    ocrl_setj phase "$next" failures 0 stop_blocks 0
    ocrl_state_save

    if [ "$next" -gt "$total" ]; then
        ocrl_emit_posttool_context "$(
            cat <<EOF
**opencode-review-loop: phase $phase of $total committed and verified.**

The commit's tree is exactly the tree OpenCode approved, and the worktree is clean.

All $total phases are now committed. End your turn — the Stop gate runs the final
cumulative review over the whole activation (baseline to HEAD) and will come back
with findings if the phases do not hold together.
EOF
        )"
    fi

    ocrl_emit_posttool_context "$(
        cat <<EOF
**opencode-review-loop: phase $phase of $total committed and verified.**

The commit's tree is exactly the tree OpenCode approved, and the worktree is clean.

Continue straight into phase $next of $total without ending your turn:

    $(ocrl_phase_desc "$next")

Implement it, then commit it the same way.
EOF
    )"
}

cmd_posttool_failure() {
    ocrl_read_hook_input
    local session cwd repo worktree
    session=$OCRL_SESSION_ID
    cwd=$OCRL_CWD
    [ -n "$cwd" ] || cwd=$PWD
    worktree=$(ocrl_pointer_read "$session") || exit 0
    if [ "$cwd" = "$worktree" ]; then
        repo=$worktree
    else
        repo=$(ocrl_repo_root "$cwd")
    fi
    [ "$repo" = "$worktree" ] || exit 0

    ocrl_config_load "$repo" >/dev/null
    ocrl_state_bind "$worktree" "$session"
    ocrl_state_load || exit 0
    [ -n "$(ocrl_get pending_approved_tree)" ] || exit 0

    ocrl_set pending_approved_tree '' pending_head '' pending_command ''
    ocrl_state_save
    exit 0
}

# --------------------------------------------------------------------------
# Stop gate
# --------------------------------------------------------------------------

# Blocks, but first accounts for whether any progress happened since the last
# block. Only genuine stalls count toward max_stop_blocks.
ocrl_stop_block_counted() {
    local reason=$1 marker blocks max
    marker="$(ocrl_get last_approved_tree):$(ocrl_get phase):$(ocrl_get status)"
    if [ "$marker" = "$(ocrl_get stop_marker)" ]; then
        blocks=$(($(ocrl_get stop_blocks) + 1))
    else
        blocks=1
    fi
    max=$(ocrl_cfg max_stop_blocks)
    ocrl_setj stop_blocks "$blocks"
    ocrl_set stop_marker "$marker"
    ocrl_state_save

    if [ "$blocks" -gt "$max" ]; then
        ocrl_needs_human "the Stop gate blocked $blocks times with no progress in between: $reason"
        ocrl_emit_stop_ok "$(
            printf 'opencode-review-loop: STALLED — %s no-progress Stop blocks in a row (limit %s), so the loop escalated to NEEDS_HUMAN. This is NOT an approval and the work was NOT reviewed to completion.\n\nLast reason: %s\n\nEvery mutation stays denied until you run /opencode-review-loop:stop.\n' \
                "$blocks" "$max" "$reason"
        )"
    fi
    ocrl_emit_stop_block "$reason"
}

cmd_gate_stop() {
    ocrl_arm_failclosed stop
    ocrl_read_hook_input

    local session cwd repo worktree st
    session=$OCRL_SESSION_ID
    cwd=$OCRL_CWD
    [ -n "$cwd" ] || cwd=$PWD

    if ! worktree=$(ocrl_pointer_read "$session") || [ -z "$worktree" ]; then
        # Same reasoning as the dispatcher: the Stop hook is only registered
        # because implement was invoked, so no pointer means arming never ran.
        ocrl_record_unstarted_arm "$session" "$cwd"
        ocrl_emit_stop_block 'opencode-review-loop: arming never ran, so nothing was frozen and nothing has been reviewed. The hooks registered but ocrl.sh arm did not execute at prompt-expansion time. Tell the user what the slash command reported; they can fix the cause and re-run /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop. Do not implement the plan.'
    fi
    repo=$(ocrl_repo_root "$cwd")
    [ -n "$repo" ] || repo=$worktree

    ocrl_config_load "$worktree" >/dev/null
    ocrl_state_bind "$worktree" "$session"
    ocrl_state_load || ocrl_emit_stop_ok

    st=$(ocrl_effective_status)
    case "$st" in
        COMPLETE | DISARMED) ocrl_emit_stop_ok ;;
        NEEDS_HUMAN)
            ocrl_emit_stop_ok "opencode-review-loop: still in NEEDS_HUMAN ($(ocrl_get reason)). The work was not reviewed to completion. Run /opencode-review-loop:stop to leave the mode."
            ;;
        ARM_FAILED)
            ocrl_stop_block_counted "$(
                printf 'opencode-review-loop: arming failed, so nothing has been reviewed and nothing may be implemented.\n\nReason: %s\n\nTell the user. They can re-run /opencode-review-loop:implement <plan.md> or /opencode-review-loop:stop. Do not attempt to implement the plan.\n' \
                    "$(ocrl_get reason)"
            )"
            ;;
        STALE)
            ocrl_stop_block_counted "$(
                printf 'opencode-review-loop: this activation is past ttl_hours (%s) and blocks rather than silently disarming. Tell the user to re-arm with /opencode-review-loop:implement <plan.md>, or leave the mode with /opencode-review-loop:stop.\n' \
                    "$(ocrl_cfg ttl_hours)"
            )"
            ;;
        ARMED)
            ocrl_stop_block_counted "$(
                cat <<EOF
opencode-review-loop: the phase list has not been frozen, so no work can start and no review has run.

Read the frozen plan ($OCRL_ACT_DIR/plan.frozen.md) and run exactly:

    $OCRL_PLUGIN_ROOT/scripts/ocrl.sh set-phases --phase "…" --phase "…"
EOF
            )"
            ;;
        RECONCILE)
            # shellcheck disable=SC2016  # backticks are markdown, not substitution
            ocrl_stop_block_counted "$(
                printf 'opencode-review-loop: a commit diverged from the reviewed tree and the reconcile is unfinished.\n\n%s\n\nRecover with `git reset --soft %s`, rebuild the phase, and commit again.\n' \
                    "$(ocrl_get reason)" "$(ocrl_get bad_commit_parent)"
            )"
            ;;
    esac

    # A deliberate pause to ask the user something: allowed once, and logged.
    if [ "$(ocrl_get defer_pending)" = 'true' ]; then
        ocrl_setj defer_pending false
        ocrl_state_save
        ocrl_emit_stop_ok 'opencode-review-loop: turn end deferred once at your request. The gate is still armed.'
    fi

    local tree phase total base
    tree=$(ocrl_snap_tree "$worktree")
    if [ -z "$tree" ]; then
        ocrl_stop_block_counted "opencode-review-loop: the working state could not be snapshotted ($OCRL_SNAP_ERROR), so the turn cannot be approved."
    fi
    phase=$(ocrl_get phase)
    total=$(ocrl_phase_count)
    base=$(ocrl_get last_approved_tree)

    # Unreviewed work sweep: anything not yet approved gets reviewed now.
    if [ "$tree" != "$base" ] && ! ocrl_tree_approved "$tree"; then
        ocrl_review_execute "$worktree" "$base" "$tree" phase "$phase"
        case "$OCRL_REVIEW_VERDICT" in
            APPROVED) ocrl_mark_tree_approved "$tree" && ocrl_state_save ;;
            CHANGES_REQUIRED)
                ocrl_stop_block_counted "$(ocrl_report_reason "opencode-review-loop: the turn is ending with uncommitted work that OpenCode requires changes to.")"
                ;;
            NEEDS_HUMAN)
                ocrl_needs_human "$OCRL_REVIEW_ERROR"
                ocrl_emit_stop_ok "opencode-review-loop: escalated to NEEDS_HUMAN — $OCRL_REVIEW_ERROR. This is NOT an approval."
                ;;
            *)
                ocrl_stop_block_counted "opencode-review-loop: the review of the uncommitted work failed ($OCRL_REVIEW_ERROR), so the turn cannot end as reviewed. A failed review is never an approval."
                ;;
        esac
    fi

    if [ "$phase" -le "$total" ] && [ "$(ocrl_get finish_requested)" != 'true' ]; then
        ocrl_stop_block_counted "$(
            printf 'opencode-review-loop: phases %s..%s are still outstanding, so the final cumulative review has not run.\n\nNext up, phase %s of %s:\n\n    %s\n\nImplement it and commit it. If the remaining phases should be abandoned, say so and let the user run /opencode-review-loop:finish.\n' \
                "$phase" "$total" "$phase" "$total" "$(ocrl_phase_desc "$phase")"
        )"
    fi

    if ! ocrl_worktree_clean "$worktree"; then
        # shellcheck disable=SC2016  # backticks are markdown, not substitution
        ocrl_stop_block_counted "$(
            printf 'opencode-review-loop: the worktree is not clean, so the last of the work is not in any reviewed commit. Commit it (`git add -A && git commit -m "…"`) before the final review runs.\n\n%s\n' \
                "$(ocrl_dirty_summary "$worktree")"
        )"
    fi

    if [ "$(ocrl_get final_done_tree)" = "$tree" ]; then
        ocrl_emit_stop_ok
    fi

    ocrl_review_execute "$worktree" "$(ocrl_get baseline_tree)" "$tree" final "$total"
    case "$OCRL_REVIEW_VERDICT" in
        APPROVED)
            ocrl_set final_done_tree "$tree" status COMPLETE reason 'final cumulative review approved'
            ocrl_state_save
            ocrl_emit_stop_ok "$(
                printf 'opencode-review-loop: COMPLETE. The final cumulative review of the whole activation (baseline %s -> %s) passed, across %s phases. The mode has disarmed itself; further commits are ungated.\n\nFull report: %s\n' \
                    "$(ocrl_get baseline_tree)" "$tree" "$total" "$OCRL_REVIEW_REPORT"
            )"
            ;;
        CHANGES_REQUIRED)
            ocrl_stop_block_counted "$(ocrl_report_reason 'opencode-review-loop: the final cumulative review found problems across the whole activation.')"
            ;;
        NEEDS_HUMAN)
            ocrl_needs_human "$OCRL_REVIEW_ERROR"
            ocrl_emit_stop_ok "opencode-review-loop: the final review escalated to NEEDS_HUMAN — $OCRL_REVIEW_ERROR. This is NOT an approval."
            ;;
        *)
            ocrl_stop_block_counted "opencode-review-loop: the final cumulative review failed ($OCRL_REVIEW_ERROR). A failed review is never an approval; end your turn again to retry."
            ;;
    esac
}

# --------------------------------------------------------------------------
# defer / status / report / finish / deactivate
# --------------------------------------------------------------------------

cmd_defer() {
    local reason='' arg
    while [ "$#" -gt 0 ]; do
        arg=$1
        case "$arg" in
            --reason)
                reason=${2:-}
                shift 2
                ;;
            *) shift ;;
        esac
    done
    ocrl_resolve_local_activation '' || {
        printf 'opencode-review-loop: nothing armed in this worktree.\n' >&2
        exit 1
    }
    local defers max
    defers=$(($(ocrl_get defers) + 1))
    max=$(ocrl_cfg max_defers)
    if [ "$defers" -gt "$max" ]; then
        printf 'opencode-review-loop: %s defers already used (limit %s). The turn cannot be deferred again; finish the phase or ask the user to run /opencode-review-loop:stop.\n' \
            "$((defers - 1))" "$max" >&2
        exit 1
    fi
    ocrl_setj defers "$defers" defer_pending true
    ocrl_set reason "deferred: $reason"
    ocrl_state_save
    printf 'opencode-review-loop: turn end deferred (%s of %s). Reason recorded: %s\n' "$defers" "$max" "$reason"
}

cmd_status() {
    if ! ocrl_resolve_local_activation ''; then
        printf 'opencode-review-loop: not armed in this worktree.\n'
        exit 0
    fi
    local st
    st=$(ocrl_effective_status)
    cat <<EOF
opencode-review-loop status
---------------------------
worktree:            $(ocrl_get worktree)
session:             $(ocrl_get session_id)
status:              $st$([ "$st" != "$(ocrl_get status)" ] && printf ' (stored: %s)' "$(ocrl_get status)")
reason:              $(ocrl_get reason)
plan:                $(ocrl_get plan_path)
frozen plan:         $OCRL_ACT_DIR/plan.frozen.md
baseline tree:       $(ocrl_get baseline_tree)
activation commit:   $(ocrl_get activation_commit)
last approved tree:  $(ocrl_get last_approved_tree)
pending approval:    $(ocrl_get pending_approved_tree)
phase:               $(ocrl_get phase) of $(ocrl_phase_count)
consecutive failures:$(ocrl_get failures) / $(ocrl_cfg max_failures)
no-progress blocks:  $(ocrl_get stop_blocks) / $(ocrl_cfg max_stop_blocks)
defers used:         $(ocrl_get defers) / $(ocrl_cfg max_defers)
model:               $(ocrl_cfg model) $(ocrl_cfg variant)
block_severity:      $(ocrl_cfg block_severity)
state directory:     $OCRL_ACT_DIR

phases:
$(jq -r '(.phases // []) | to_entries[] | "  \(.key+1). \(.value)"' <<<"$OCRL_STATE")

reports:
$(ocrl_report_list | sed 's/^/  /')
EOF
}

cmd_report() {
    if ! ocrl_resolve_local_activation ''; then
        printf 'opencode-review-loop: not armed in this worktree.\n'
        exit 0
    fi
    ocrl_report_print "${1:-}"
}

cmd_finish() {
    if ! ocrl_resolve_local_activation ''; then
        printf 'opencode-review-loop: not armed in this worktree.\n'
        exit 0
    fi
    local tree base
    ocrl_set finish_requested 'true'
    ocrl_state_save

    if ! ocrl_worktree_clean "$OCRL_REPO"; then
        printf 'opencode-review-loop: the worktree is not clean. Commit the outstanding work first — the final review runs over committed history plus the working state, and every phase must land in a reviewed commit.\n\n%s\n' \
            "$(ocrl_dirty_summary "$OCRL_REPO")"
        exit 1
    fi

    tree=$(ocrl_snap_tree "$OCRL_REPO")
    base=$(ocrl_get baseline_tree)
    printf 'opencode-review-loop: running the final cumulative review (%s -> %s). This can take a few minutes.\n\n' "$base" "$tree"

    ocrl_review_execute "$OCRL_REPO" "$base" "$tree" final "$(ocrl_phase_count)"
    case "$OCRL_REVIEW_VERDICT" in
        APPROVED)
            ocrl_set final_done_tree "$tree" status COMPLETE reason 'final cumulative review approved (user-invoked finish)'
            ocrl_state_save
            printf 'opencode-review-loop: COMPLETE. The final review passed. The mode has disarmed itself; further commits are ungated.\n\nFull report: %s\n' "$OCRL_REVIEW_REPORT"
            ;;
        *)
            ocrl_state_save
            printf '%s\n' "$(ocrl_report_reason 'opencode-review-loop: the final cumulative review did not pass. The mode stays armed.')"
            exit 1
            ;;
    esac
}

cmd_deactivate() {
    if ! ocrl_resolve_local_activation ''; then
        printf 'opencode-review-loop: not armed in this worktree, so there is nothing to stop.\n'
        exit 0
    fi
    ocrl_set status DISARMED reason 'stopped by the user'
    ocrl_state_save
    # The pointer deliberately stays: the hooks are still registered for this
    # session, and removing it would make every later tool call look like an
    # arm that never ran. DISARMED is what makes the gates pass through.
    cat <<EOF
opencode-review-loop: STOPPED for this worktree.

Commits and file changes are no longer gated. Nothing was reverted — the repository
is exactly as you left it, at phase $(ocrl_get phase) of $(ocrl_phase_count).

State and reports are kept at:
  $OCRL_ACT_DIR

Re-arm at any time with /opencode-review-loop:implement <plan.md>.
EOF
}

# --------------------------------------------------------------------------
# dry-run / selftest
# --------------------------------------------------------------------------

cmd_dry_run() {
    local repo tree base dir
    repo=$(ocrl_repo_root "$PWD")
    [ -n "$repo" ] || {
        printf 'not a git repository\n' >&2
        exit 1
    }
    if ! ocrl_resolve_local_activation ''; then
        ocrl_config_load "$repo" >/dev/null
        ocrl_state_bind "$repo" 'dry-run'
        ocrl_state_new
        ocrl_set worktree "$repo" activation_commit "$(ocrl_head_commit "$repo")" \
            baseline_tree "$(ocrl_head_tree "$repo")"
        ocrl_state_save
    fi
    base=$(ocrl_get last_approved_tree)
    [ -n "$base" ] || base=$(ocrl_head_tree "$repo")
    tree=$(ocrl_snap_tree "$repo")
    dir="$OCRL_ACT_DIR/bundles/dry-run"
    ocrl_bundle_build "$repo" "$base" "$tree" phase "$(ocrl_get phase)" "$dir" || {
        printf 'bundle build failed: %s\n' "$OCRL_REVIEW_ERROR" >&2
        exit 1
    }
    printf '# OPENCODE_PERMISSION\n%s\n\n# argv (one element per line)\n' "$(ocrl_review_permission "$dir")"
    printf 'opencode\nrun\n<the prompt printed below, as a single argument>\n'
    ocrl_review_argv "$repo" "$dir" "review-loop dry run"
    printf '\n# the prompt argument\n'
    cat "$OCRL_PLUGIN_ROOT/prompts/reviewer-phase.md"
    printf '\n# bundle: %s\n' "$dir"
    ls -la "$dir"
}

cmd_selftest() {
    exec "$OCRL_PLUGIN_ROOT/tests/selftest.sh" "$@"
}

# --------------------------------------------------------------------------

usage() {
    cat <<'EOF'
usage: ocrl.sh <subcommand> [args]

  arm --session <id> --plan <path> [--allow-dirty]
  set-phases --phase "…" [--phase "…" …]
  pretool | confirm-commit | posttool-failure | gate-stop   (hook entrypoints)
  status | report [n] | defer --reason "…" | finish | deactivate
  dry-run | selftest
EOF
}

main() {
    local sub=${1:-}
    [ "$#" -gt 0 ] && shift
    case "$sub" in
        arm) cmd_arm "$@" ;;
        set-phases) cmd_set_phases "$@" ;;
        pretool) cmd_pretool "$@" ;;
        confirm-commit) cmd_confirm_commit "$@" ;;
        posttool-failure) cmd_posttool_failure "$@" ;;
        gate-stop) cmd_gate_stop "$@" ;;
        defer) cmd_defer "$@" ;;
        status) cmd_status "$@" ;;
        report) cmd_report "$@" ;;
        finish) cmd_finish "$@" ;;
        deactivate) cmd_deactivate "$@" ;;
        dry-run) cmd_dry_run "$@" ;;
        selftest) cmd_selftest "$@" ;;
        -h | --help | help | '') usage ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
