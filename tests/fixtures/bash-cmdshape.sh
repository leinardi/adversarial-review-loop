#!/usr/bin/env bash
# Drive the shell command-shape library directly, so the Python port can be tested against
# the real implementation rather than against a reading of it.
#
# Phase 3 is a translation with no behaviour change, and "no behaviour change" means the
# verdict *and* the explanation the model is shown. Both are compared.
#
# usage: bash-cmdshape.sh <op> <command>
#   validate <cmd>        exit 0, or exit 1 with $OCRL_CMD_ERROR on stdout
#   reset-target <cmd>    exit 0 with the target on stdout, or exit 1 with the error
#   tokenize <cmd>        exit 0 with one NUL-terminated token per record, or exit 1
#   is-escape <cmd>       exit 0 / 1, no output
#   mentions-commit <cmd> exit 0 / 1, no output
#   mentions-reset <cmd>  exit 0 / 1, no output

set -uo pipefail

HERE=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)
LIB="$HERE/../../scripts/lib"

# shellcheck source=../../scripts/lib/cmdshape.sh
. "$LIB/cmdshape.sh"

op=${1:-}
cmd=${2-}

case "$op" in
    validate)
        if ocrl_cmd_validate_commit "$cmd"; then
            exit 0
        fi
        printf '%s' "$OCRL_CMD_ERROR"
        exit 1
        ;;
    reset-target)
        # Redirected to a file rather than captured with $(...): command substitution runs
        # the function in a subshell, where the OCRL_CMD_ERROR it sets dies with it. The
        # shell gate itself never reads that variable after this call, so the loss is
        # invisible in production -- but here the explanation is the thing being compared.
        out=$(mktemp "${TMPDIR:-/tmp}/ocrl-reset.XXXXXX") || exit 2
        trap 'rm -f "$out"' EXIT
        if ocrl_cmd_reset_target "$cmd" >"$out"; then
            cat "$out"
            exit 0
        fi
        printf '%s' "$OCRL_CMD_ERROR"
        exit 1
        ;;
    tokenize)
        if ! ocrl_cmd_tokenize "$cmd"; then
            printf '%s' "$OCRL_CMD_ERROR"
            exit 1
        fi
        # NUL-terminated: a token may legitimately contain a space, and the point of the
        # comparison is that the split agrees exactly.
        for t in ${OCRL_TOKENS[@]+"${OCRL_TOKENS[@]}"}; do
            printf '%s\0' "$t"
        done
        exit 0
        ;;
    is-escape)
        ocrl_cmd_is_escape "$cmd"
        ;;
    mentions-commit)
        ocrl_cmd_mentions_commit "$cmd"
        ;;
    mentions-reset)
        ocrl_cmd_mentions_reset "$cmd"
        ;;
    *)
        printf 'unknown op: %s\n' "$op" >&2
        exit 2
        ;;
esac
