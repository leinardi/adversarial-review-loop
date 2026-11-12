#!/usr/bin/env bash
#
# This file is part of adversarial-review-loop.
#
# Copyright (c) 2026 Roberto Leinardi
#
# adversarial-review-loop is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# adversarial-review-loop is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.
#

# Smoke test for scripts/arl.sh: it finds a usable Python, runs the engine isolated, and writes
# no bytecode into the directory it runs from. Everything the engine decides is covered by
# tests/unit/ instead.

set -euo pipefail

root=$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd)
shim="$root/scripts/arl.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

failures=0
check() {
    local name=$1
    shift
    if "$@"; then
        printf 'ok   %s\n' "$name"
    else
        printf 'FAIL %s\n' "$name"
        failures=$((failures + 1))
    fi
}

usage_runs() {
    (cd "$work" && "$shim" >"$work/out" 2>"$work/err") && head -n 1 "$work/out" | cmp -s - <(printf 'usage: arl.sh [--dir LAUNCH_DIR] <subcommand> [args]\n')
}

unknown_fails() {
    local rc=0
    (cd "$work" && "$shim" no-such-subcommand >/dev/null 2>&1) || rc=$?
    [ "$rc" -eq 2 ]
}

no_python_fails() {
    local rc=0 bin="$work/bin"
    mkdir -p "$bin"
    ln -sf "$(command -v bash)" "$bin/bash"
    ln -sf "$(command -v dirname)" "$bin/dirname" 2>/dev/null || true
    (cd "$work" && PATH="$bin" "$bin/bash" "$shim" >/dev/null 2>"$work/err") || rc=$?
    [ "$rc" -eq 127 ] && head -n 1 "$work/err" | cmp -s - <(printf 'arl: no Python 3.12 or newer found on PATH (tried python3, python3.14, python3.13, python3.12)\n')
}

no_bytecode_in_cwd() {
    (cd "$work" && XDG_CACHE_HOME="$work/cache" "$shim" >/dev/null 2>&1)
    [ -z "$(find "$work" -path "$work/cache" -prune -o -name '__pycache__' -print)" ]
}

check "arl.sh with no arguments prints the usage" usage_runs
check "an unknown subcommand exits 2" unknown_fails
check "no Python 3.12 on PATH exits 127" no_python_fails
check "no __pycache__ is written into the working directory" no_bytecode_in_cwd

if [ "$failures" -ne 0 ]; then
    printf '%d check(s) failed\n' "$failures"
    exit 1
fi
printf 'all checks passed\n'
