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

# Runs the adversarial-review-loop engine: finds a Python 3.12 or newer and hands every
# argument to the bootstrap. The engine always runs isolated (`-I`) from an absolute path, so
# nothing in the current directory or the environment can shadow its modules.

set -euo pipefail

script_dir=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)
bootstrap="$script_dir/arl-bootstrap.py"

for candidate in python3 python3.14 python3.13 python3.12; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -I -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        exec "$candidate" -I "$bootstrap" "$@"
    fi
done

printf 'arl: no Python 3.12 or newer found on PATH (tried python3, python3.14, python3.13, python3.12)\n' >&2
exit 127
