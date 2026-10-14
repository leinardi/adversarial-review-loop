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

# Licence header checker.
#
# adversarial-review-loop is GPL-3.0-or-later (LICENSE, .claude-plugin/plugin.json), and every
# source file says so in its own header. This fails on any tracked source file whose header does
# not carry the project's notice with the "or (at your option) any later version" grant and
# Roberto Leinardi's copyright, so a header copied from a GPL-3.0-only project, or from another
# project altogether, is caught as surely as a missing one.
#
# Governed: Python, shell, TypeScript and JavaScript sources, the Makefile and the repo-local .mk
# fragments. A Python file carries the header after its module docstring (the IntelliJ "GNU
# GPLv3" profile in .idea/copyright places it there), so for Python the window starts where the
# docstring ends. Exempt, each for a stated reason: the vendored bashlex under
# scripts/arl/_vendor/ (upstream's code under upstream's licence, kept diffable), the API types
# Claude Code generates into .claude-plugin/types/, and the make-common files the bootstrap
# fetches (make-common's, under its MIT licence, named in the Makefile's MK_COMMON_FILES).
# YAML, JSON and Markdown are never governed.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/check-license-headers.sh

Checks every tracked source file for the adversarial-review-loop GPL-3.0-or-later header and
lists the files that lack it. Run from anywhere inside the repository.
EOF
}

if [[ $# -gt 0 ]]; then
    usage >&2
    exit 2
fi

cd "$(git rev-parse --show-toplevel)"

# How many lines the header must sit in: a shebang (or, in Python, the module docstring) and a
# blank line may precede it.
HEADER_WINDOW=25

REQUIRED_LINES=(
    "This file is part of adversarial-review-loop."
    "adversarial-review-loop is free software: you can redistribute it and/or modify"
    "it under the terms of the GNU General Public License as published by"
    "the Free Software Foundation, either version 3 of the License, or"
    "(at your option) any later version."
)
# The year is the file's first commit; a range (2026-2027) is allowed too.
COPYRIGHT_RE='Copyright \(c\) [0-9]{4}(-[0-9]{4})? Roberto Leinardi$'

# Path prefixes and exact paths that are not this project's own source.
EXEMPT_PREFIXES=(
    ".claude-plugin/types/"
    "scripts/arl/_vendor/"
)
EXEMPT_PATHS=(
    "scripts/bootstrap-mk-common.sh"
)

# The shared make fragments, read from the Makefile so the list cannot drift from what the
# bootstrap actually fetches.
mk_common_files() {
    sed -n 's/^MK_COMMON_FILES[[:space:]]*:=[[:space:]]*//p' Makefile
}

is_governed() {
    local path="$1"

    case "${path}" in
    *.py | *.sh | *.ts | *.mts | *.cts | *.js | *.mjs | *.cjs | .mk/*.mk | Makefile)
        return 0
        ;;
    esac

    return 1
}

is_exempt() {
    local path="$1" entry

    for entry in "${EXEMPT_PREFIXES[@]}"; do
        if [[ ${path} == "${entry}"* ]]; then
            return 0
        fi
    done

    for entry in "${EXEMPT_PATHS[@]}"; do
        if [[ ${path} == "${entry}" ]]; then
            return 0
        fi
    done

    for entry in ${COMMON_FILES}; do
        if [[ ${path} == ".mk/${entry}" ]]; then
            return 0
        fi
    done

    return 1
}

# Prints the lines the header must sit in: the first HEADER_WINDOW lines, or for Python the
# HEADER_WINDOW lines after the shebang and the module docstring.
header_window() {
    local path="$1"

    if [[ ${path} != *.py ]]; then
        head -n "${HEADER_WINDOW}" -- "${path}"
        return
    fi

    awk -v window="${HEADER_WINDOW}" '
        NR == 1 && /^#!/ { next }
        state == 0 {
            if ($0 ~ /^[rRuU]?"""/) {
                rest = substr($0, index($0, "\"\"\"") + 3)
                state = index(rest, "\"\"\"") ? 2 : 1
                next
            }
            state = 2
        }
        state == 1 {
            if (index($0, "\"\"\"")) {
                state = 2
            }
            next
        }
        state == 2 {
            if (++printed > window) {
                exit
            }
            print
        }
    ' "${path}"
}

has_header() {
    local path="$1" head line
    head="$(header_window "${path}")"

    for line in "${REQUIRED_LINES[@]}"; do
        if [[ ${head} != *"${line}"* ]]; then
            return 1
        fi
    done

    grep -qE -- "${COPYRIGHT_RE}" <<<"${head}"
}

COMMON_FILES="$(mk_common_files)"
missing=()
checked=0

while IFS= read -r -d '' path; do
    if ! is_governed "${path}" || is_exempt "${path}"; then
        continue
    fi

    # A tracked file deleted in the working tree, or a tracked symlink, is not this check's
    # concern.
    if [[ ! -f ${path} || -L ${path} ]]; then
        continue
    fi

    checked=$((checked + 1))

    if ! has_header "${path}"; then
        missing+=("${path}")
    fi
done < <(git ls-files -z)

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "These files lack the adversarial-review-loop GPL-3.0-or-later header:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    echo "Copy the header from any source file of the same kind (IntelliJ: the GNU GPLv3 copyright" >&2
    echo "profile), with the year of the file's first commit." >&2
    exit 1
fi

echo "License headers: ${checked} files checked, all carry the GPL-3.0-or-later header."
