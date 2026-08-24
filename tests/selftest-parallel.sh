#!/usr/bin/env bash
# Run tests/selftest.sh as N sharded processes and aggregate the result.
#
# The selftest spends most of its wall clock waiting on git and on one gate process per
# assertion, so the serial run left every core but one idle. Sections share no state --
# `new_case` builds a fresh repository under the process's own $ROOT with its own
# OCRL_STATE_DIR -- so each shard is the same script running a disjoint subset.
#
# Output is buffered per shard and printed in shard order once every shard has finished, so
# the transcript stays readable rather than interleaved. Failures are therefore reported at
# the end of the run, not the instant they happen; `OCRL_SELFTEST_JOBS=1` restores the
# straight-through serial run for debugging one.
#
# usage: tests/selftest-parallel.sh [name-filter]

set -uo pipefail

TESTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SELFTEST="$TESTS_DIR/selftest.sh"

JOBS=${OCRL_SELFTEST_JOBS:-}
if [ -z "$JOBS" ]; then
	JOBS=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
fi
case $JOBS in
[0-9]*) ;;
*) JOBS=1 ;;
esac

# One shard is the serial script, without the buffering that would hide its progress.
if [ "$JOBS" -le 1 ]; then
	exec "$SELFTEST" "$@"
fi

LOGS=$(mktemp -d "${TMPDIR:-/tmp}/ocrl-selftest-logs.XXXXXX")
trap 'rm -rf "$LOGS"' EXIT

pids=()
for ((shard = 0; shard < JOBS; shard++)); do
	OCRL_SELFTEST_SHARD="$shard/$JOBS" "$SELFTEST" "$@" >"$LOGS/$shard.log" 2>&1 &
	pids+=("$!")
done

status=0
for pid in "${pids[@]}"; do
	wait "$pid" || status=1
done

pass=0
fail=0
for ((shard = 0; shard < JOBS; shard++)); do
	log=$LOGS/$shard.log
	# Everything but each shard's own summary line, which is replaced by the total below.
	grep -av 'passed, .* failed' "$log"
	counts=$(sed -e 's/\x1b\[[0-9;]*m//g' "$log" | sed -n 's/^\([0-9]\{1,\}\) passed, \([0-9]\{1,\}\) failed$/\1 \2/p' | tail -1)
	if [ -z "$counts" ]; then
		printf '\033[31mshard %s produced no summary; its output is above\033[0m\n' "$shard"
		status=1
		continue
	fi
	pass=$((pass + ${counts% *}))
	fail=$((fail + ${counts#* }))
done

printf '\n\033[1m%s passed, %s failed\033[0m (%s shards)\n' "$pass" "$fail" "$JOBS"
[ "$fail" -eq 0 ] && [ "$status" -eq 0 ]
