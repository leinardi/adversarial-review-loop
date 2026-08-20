#!/usr/bin/env bash
# Builds a throwaway repository for working through tests/STEP0.md.
#
# The live checks in STEP0 need a real Claude Code session, which means a real
# repository that the loop is allowed to gate. Never point them at work you care
# about: the whole exercise is about provoking denials and bad commits.
#
# usage: tests/step0-fixture.sh [target-dir]      (default: ~/ocrl-step0)

set -euo pipefail

TARGET=${1:-$HOME/ocrl-step0}
REPO="$TARGET/repo"
STATE_ROOT=${XDG_STATE_HOME:-$HOME/.local/state}/opencode-review-loop

if [ -e "$TARGET" ]; then
    printf 'refusing to touch an existing path: %s\n' "$TARGET" >&2
    printf 'remove it yourself, or pass a different target directory.\n' >&2
    exit 1
fi

# The plan lives beside the repository, not inside it: that is where plans
# normally are, and a plan file committed into the repo under review would
# otherwise show up in its own phase-1 diff.
mkdir -p "$REPO"
cd "$REPO"
git init -q -b main
git config user.email 'step0@example.invalid'
git config user.name 'ocrl step0'

# Tooling that writes into a worktree will otherwise land you in RECONCILE
# mid-test, which is a real behaviour but not the one being tested here.
cat >.gitignore <<'EOF'
.serena/
.idea/
.vscode/
__pycache__/
EOF

cat >greet.py <<'EOF'
def greet(name):
    return "Hello, " + name


def farewell(name):
    return "Bye, " + name
EOF

cat >README.md <<'EOF'
# step0 fixture

A throwaway repository for exercising the opencode-review-loop gate.
EOF

git add -A
git commit -qm 'chore: seed the step0 fixture'

cat >"$TARGET/plan.md" <<'EOF'
# Plan: make greet.py handle a missing name

Two small phases, each ending in one commit.

## Phase 1

`greet()` crashes when `name` is `None`, because it concatenates `None` to a
string. Make it return `"Hello, there"` when no name is supplied. Keep the
behaviour for a real name exactly as it is.

## Phase 2

Do the same for `farewell()`: return `"Bye, there"` when `name` is `None`.
Keep the two functions consistent with each other.
EOF

# A path containing a backtick, for the argument-safety probe in STEP0 item 3.
# If the harness ever lets the backtick reach a shell, `id` runs and its output
# appears in the arm banner -- which is exactly what the probe looks for.
# shellcheck disable=SC2016  # a literal backtick in the filename is the point
printf 'not a real plan\n' >"$TARGET/"'pl`id`an.md'

if [ -n "$(git status --porcelain)" ]; then
    printf 'BUG: the fixture repository is not clean; arming would refuse.\n' >&2
    git status --short >&2
    exit 1
fi

printf '\nfixture ready\n\n'
printf '  repository:    %s   (clean)\n' "$REPO"
printf '  plan:          %s/plan.md\n' "$TARGET"
# shellcheck disable=SC2016  # a literal backtick in the filename is the point
printf '  backtick probe: %s/pl`id`an.md\n' "$TARGET"
printf '  state root:    %s\n\n' "$STATE_ROOT"
printf 'Start the session with:\n\n    cd %s && claude\n\n' "$REPO"
printf 'Reset between runs:\n\n    rm -rf %s %s\n\n' "$TARGET" "$STATE_ROOT"
printf 'After an end-to-end run the plan is already implemented, so re-running\n'
printf 'needs the repository rewound or Claude will correctly find nothing to do:\n\n'
printf '    git -C %s reset --hard %s\n' "$REPO" "$(git -C "$REPO" rev-parse --short HEAD)"
