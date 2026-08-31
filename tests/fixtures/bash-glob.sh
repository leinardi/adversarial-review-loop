#!/usr/bin/env bash
# The one line of scripts/lib/gitsnap.sh that decides whether a review is skipped:
#
#     [[ $p == $g ]]        (arl_all_paths_ignored)
#
# Exposed on its own so the Python matcher can be compared against real bash rather than
# against a reading of the manual. Exit 0 when the path matches the glob, 1 when it does
# not. Nothing is read from the environment, and nothing is written.
#
# usage: bash-glob.sh <path> <glob>
#        bash-glob.sh --batch  < NUL-separated <path> <glob> pairs on stdin
#
# The batch form answers many pairs from one bash, printing `1` or `0` per pair, one per
# line, in the order they arrived. It exists only because forking a shell per pair made the
# differential tests the second-slowest file in the suite; the deciding expression is the
# same `[[ $path == $glob ]]` in both forms, and neither is allowed to drift from the other.
# NUL is the separator because every other byte -- newline, backslash, the empty string --
# is a case these tests deliberately feed in, and no path or glob can contain a NUL.

set -uo pipefail

if [[ ${1-} == --batch ]]; then
	while IFS= read -r -d '' path && IFS= read -r -d '' glob; do
		# shellcheck disable=SC2053  # unquoted right-hand side: glob matching is the point
		if [[ $path == $glob ]]; then
			printf '1\n'
		else
			printf '0\n'
		fi
	done
	exit 0
fi

path=${1-}
glob=${2-}

# shellcheck disable=SC2053  # unquoted right-hand side: glob matching is the point
[[ $path == $glob ]]
