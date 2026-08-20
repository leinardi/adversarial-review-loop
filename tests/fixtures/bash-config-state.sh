#!/usr/bin/env bash
# Drive the shell config/state libraries directly, so the Python port can be tested
# against the real implementation rather than against a reading of it.
#
# Phase 6 must be revertible mid-session, which means a session created by one
# implementation has to be loadable and advanceable by the other. These operations are the
# fixtures for that.
#
# usage: bash-config-state.sh <op> [args]
#   config-load <repo>                             merged config JSON on stdout
#   state-new <worktree> <session>                 write a populated state.json
#   state-get <worktree> <session> <key>           ocrl_get for one key
#   state-array <worktree> <session> <key>         ocrl_get_array, one item per line
#   state-advance <worktree> <session>             load, mutate, save (the round trip)
#   effective-status <worktree> <session> <repo>   ocrl_effective_status
#   phase-count <worktree> <session>

set -uo pipefail

HERE=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)
LIB="$HERE/../../scripts/lib"

# shellcheck source=../../scripts/lib/common.sh
. "$LIB/common.sh"
# shellcheck source=../../scripts/lib/config.sh
. "$LIB/config.sh"
# shellcheck source=../../scripts/lib/state.sh
. "$LIB/state.sh"

op=${1:-}
shift || true

case "$op" in
    config-load)
        ocrl_config_load "${1:-}"
        ;;
    state-new)
        ocrl_state_bind "$1" "$2"
        ocrl_state_new
        ocrl_set status ACTIVE reason 'bash wrote this' session_id "$2" worktree "$1" \
            plan_path '/tmp/plan.md' baseline_tree 'base0' last_approved_tree 'tree1' \
            pending_command 'git commit -m "héllo"'
        ocrl_setj armed_at 1700000000 allow_dirty true defer_pending false \
            phases '["first phase","second phase"]' phase 2 failures 1 report_seq 3
        ocrl_mark_tree_approved 'tree1'
        ocrl_mark_tree_approved 'tree0'
        ocrl_state_save
        ;;
    state-get)
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_get "$3"
        ;;
    state-array)
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_get_array "$3"
        ;;
    state-advance)
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_set status RECONCILE reason 'bash advanced it'
        ocrl_setj phase 3 stop_blocks 2
        ocrl_mark_tree_approved 'tree2'
        ocrl_state_save
        ;;
    effective-status)
        ocrl_config_load "${3:-}" >/dev/null
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_effective_status
        ;;
    phase-count)
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_phase_count
        ;;
    *)
        printf 'unknown op: %s\n' "$op" >&2
        exit 2
        ;;
esac
