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

# Type-checks hooks/register.js and tests/mod/ against the API types Claude Code generates.
#
# The types are not committed: Claude Code writes them into .claude-plugin/types/ each time it
# loads this plugin from a folder, at the version of Claude Code doing the loading. Without them
# tsc would check nothing that matters, so their absence is a failure with a remedy, not a pass.

set -euo pipefail

root=$(git rev-parse --show-toplevel)
types="$root/.claude-plugin/types/claude-code/index.d.ts"

if [ ! -f "$types" ]; then
    printf 'typecheck-mod: %s is missing.\n' "${types#"$root"/}" >&2
    printf 'Claude Code writes it when it loads this plugin from the checkout, for example:\n' >&2
    printf '  claude -p "Reply ok" --plugin-dir %s --model haiku\n' "$root" >&2
    exit 1
fi

exec tsc -p "$root/tsconfig.json"
