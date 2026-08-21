#!/usr/bin/env bash
# Drive the shell's argument splitter directly, so the Python port is tested against the
# real implementation rather than against a reading of it.
#
# `ocrl_split_args` lived in scripts/ocrl.sh itself rather than in a library, and only there
# -- as a function inside the dispatcher, not something a library file exported. Since Phase
# 6, scripts/ocrl.sh at HEAD is the guarded shim: sourcing it no longer defines the function,
# and even if it did, its default-subcommand branch execs Python and would replace this
# process before `ocrl_split_args` was ever called below. So this pins the exact commit where
# scripts/ocrl.sh was last the full Bash dispatcher and reconstitutes it from git history --
# alongside a symlink to scripts/lib/, which that file sources by relative path and which
# stays on disk, unreferenced, for exactly this.
#
# usage: bash-args.sh <raw argument string>
#   stdout: <plan>\0<flag>\0   (NUL-separated: a plan path may contain spaces)

set -uo pipefail

HERE=$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)
PLUGIN_ROOT=$(cd -- "$HERE/../.." && pwd)
OCRL_LAST_BASH_SHA='eea08d35d42a0351a468fcc64e349185fdfaf090'

FROZEN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ocrl-bash-args.XXXXXX")
trap 'rm -rf "$FROZEN_DIR"' EXIT
ln -s "$PLUGIN_ROOT/scripts/lib" "$FROZEN_DIR/lib"
git -C "$PLUGIN_ROOT" show "$OCRL_LAST_BASH_SHA:scripts/ocrl.sh" >"$FROZEN_DIR/ocrl.sh"
chmod +x "$FROZEN_DIR/ocrl.sh"

# shellcheck disable=SC1091 # reconstituted from git history, not a repo path shellcheck can see
. "$FROZEN_DIR/ocrl.sh" help >/dev/null

ocrl_split_args "${1-}"
printf '%s\0%s\0' "$OCRL_ARG_PLAN" "$OCRL_ARG_FLAG"
