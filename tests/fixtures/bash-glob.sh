#!/usr/bin/env bash
# The one line of scripts/lib/gitsnap.sh that decides whether a review is skipped:
#
#     [[ $p == $g ]]        (ocrl_all_paths_ignored)
#
# Exposed on its own so the Python matcher can be compared against real bash rather than
# against a reading of the manual. Exit 0 when the path matches the glob, 1 when it does
# not. Nothing is read from the environment, and nothing is written.
#
# usage: bash-glob.sh <path> <glob>

set -uo pipefail

path=${1-}
glob=${2-}

# shellcheck disable=SC2053  # unquoted right-hand side: glob matching is the point
[[ $path == $glob ]]
