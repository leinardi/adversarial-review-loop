#!/usr/bin/env bash
# Drive the shell's argument splitter directly, so the Python port is tested against the
# real implementation rather than against a reading of it.
#
# `ocrl_split_args` lives in scripts/ocrl.sh rather than in a library, so this sources the
# entrypoint with `help` -- which prints usage and returns without exiting -- and then calls
# the function. Sourcing the entrypoint is the only way to reach it while ocrl.sh is still
# the live gate.
#
# usage: bash-args.sh <raw argument string>
#   stdout: <plan>\0<flag>\0   (NUL-separated: a plan path may contain spaces)

set -uo pipefail

HERE=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)

# shellcheck source=../../scripts/ocrl.sh
. "$HERE/../../scripts/ocrl.sh" help >/dev/null

ocrl_split_args "${1-}"
printf '%s\0%s\0' "$OCRL_ARG_PLAN" "$OCRL_ARG_FLAG"
