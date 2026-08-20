#!/usr/bin/env bash
# Drive the shell reviewer/report libraries directly, so the Python port can be tested
# against the real implementation rather than against a reading of it.
#
# Phase 4 is a translation: the bundle the reviewer is shown, the argv it is invoked with,
# the verdict recomputed from its answer and the text Claude is handed must all be the same
# before and after. Each op below exposes one of those.
#
# usage: bash-reviewer.sh <op> [args]
#   permission <bundle_dir>
#   argv <repo> <bundle_dir> <title>
#   bundle <worktree> <session> <repo> <base> <head> <scope> <phase> <dir>
#   parse <repo> <out_file>                      -> NUL-separated verdict, error,
#                                                   blocking findings, all findings, prose
#   reason <repo> <out_file> <headline>
#   store <worktree> <session> <repo> <out_file> <seq> <scope> <phase> <base> <head>
#
# OCRL_FAKE_WARNINGS is honoured by `bundle`, so the snapshot-warnings section can be
# exercised without a real submodule.

set -uo pipefail

HERE=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)
LIB="$HERE/../../scripts/lib"
export OCRL_PLUGIN_ROOT="$HERE/../.."

# shellcheck source=../../scripts/lib/common.sh
. "$LIB/common.sh"
# shellcheck source=../../scripts/lib/config.sh
. "$LIB/config.sh"
# shellcheck source=../../scripts/lib/state.sh
. "$LIB/state.sh"
# shellcheck source=../../scripts/lib/gitsnap.sh
. "$LIB/gitsnap.sh"
# shellcheck source=../../scripts/lib/reviewer.sh
. "$LIB/reviewer.sh"
# shellcheck source=../../scripts/lib/report.sh
. "$LIB/report.sh"

op=${1:-}
shift || true

case "$op" in
    permission)
        ocrl_review_permission "$1"
        ;;
    argv)
        ocrl_config_load "$1" >/dev/null
        ocrl_review_argv "$1" "$2" "$3"
        ;;
    bundle)
        # Sourcing gitsnap.sh blanks OCRL_SNAP_WARNINGS, so the warnings section is driven
        # through a variable of our own instead of the exported one.
        OCRL_SNAP_WARNINGS=${OCRL_FAKE_WARNINGS:-}
        ocrl_config_load "$3" >/dev/null
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_bundle_build "$3" "$4" "$5" "$6" "$7" "$8"
        exit $?
        ;;
    parse)
        ocrl_config_load "$1" >/dev/null
        ocrl_review_parse "$2"
        printf '%s\0%s\0%s\0%s\0%s\0' \
            "$OCRL_REVIEW_VERDICT" "$OCRL_REVIEW_ERROR" \
            "$OCRL_REVIEW_FINDINGS" "$OCRL_REVIEW_ALL" "$OCRL_REVIEW_PROSE"
        ;;
    reason)
        ocrl_config_load "$1" >/dev/null
        ocrl_review_parse "$2"
        ocrl_report_reason "$3"
        ;;
    store)
        ocrl_config_load "$3" >/dev/null
        ocrl_state_bind "$1" "$2"
        ocrl_state_load || exit 1
        ocrl_review_parse "$4"
        # shellcheck disable=SC2034  # read by ocrl_report_store in report.sh
        OCRL_REVIEW_RAW=$4
        ocrl_report_store "$5" "$6" "$7" "$8" "$9" "$4"
        printf '%s' "$OCRL_REVIEW_REPORT"
        ;;
    *)
        printf 'unknown op: %s\n' "$op" >&2
        exit 2
        ;;
esac
