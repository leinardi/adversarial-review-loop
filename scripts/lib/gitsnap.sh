# shellcheck shell=bash
# Snapshotting the working state into an immutable tree id.
#
# Committed + staged + unstaged + non-ignored untracked content, captured via a
# throwaway index so the real index is never touched.

# shellcheck disable=SC2034  # these globals are read by the other library files
OCRL_SNAP_WARNINGS=''
OCRL_SNAP_ERROR=''

ocrl_git() { git -C "$1" "${@:2}"; }

ocrl_head_commit() {
    ocrl_git "$1" rev-parse --verify --quiet HEAD 2>/dev/null || true
}

ocrl_head_tree() {
    ocrl_git "$1" rev-parse --verify --quiet 'HEAD^{tree}' 2>/dev/null || true
}

# Files that a snapshot would newly stage: worktree-modified tracked files plus
# non-ignored untracked ones. Already-committed content is not re-checked, so an
# existing large blob does not wedge the gate forever.
ocrl_snap_stageable() {
    ocrl_git "$1" ls-files -z --others --exclude-standard --modified 2>/dev/null || true
}

# Prints "path<TAB>bytes" for every stageable file above the limit.
ocrl_snap_oversized() {
    local repo=$1 limit=$2 f size
    while IFS= read -r -d '' f; do
        [ -f "$repo/$f" ] || continue
        [ -L "$repo/$f" ] && continue
        size=$(stat -c %s -- "$repo/$f" 2>/dev/null || printf '0')
        if [ "$size" -gt "$limit" ] 2>/dev/null; then
            printf '%s\t%s\n' "$f" "$size"
        fi
    done < <(ocrl_snap_stageable "$repo")
}

# Submodule declaration for the report header. Content is never diffed.
ocrl_snap_submodules() {
    local repo=$1 out
    out=$(ocrl_git "$repo" submodule status --recursive 2>/dev/null || true)
    [ -n "$out" ] || return 0
    printf '%s\n' "$out" | while IFS= read -r line; do
        case "$line" in
            '+'*) printf 'submodule out of sync (content NOT diffed): %s\n' "${line# }" ;;
            'U'*) printf 'submodule has merge conflicts (content NOT diffed): %s\n' "${line# }" ;;
            '-'*) printf 'submodule not initialised (content NOT diffed): %s\n' "${line# }" ;;
            *) printf 'submodule present (content NOT diffed): %s\n' "$line" ;;
        esac
    done
}

# ocrl_snap_tree <repo> -- prints the snapshot tree id, or nothing on failure
# with the reason in $OCRL_SNAP_ERROR. Warnings land in $OCRL_SNAP_WARNINGS.
ocrl_snap_tree() {
    local repo=$1 idx tree rc=0
    OCRL_SNAP_ERROR=''
    OCRL_SNAP_WARNINGS=''

    idx=$(mktemp "${TMPDIR:-/tmp}/ocrl-index.XXXXXX") || {
        OCRL_SNAP_ERROR='could not create a temporary index'
        return 1
    }

    if [ -n "$(ocrl_head_commit "$repo")" ]; then
        GIT_INDEX_FILE="$idx" ocrl_git "$repo" read-tree HEAD >/dev/null 2>&1 || rc=1
    else
        GIT_INDEX_FILE="$idx" ocrl_git "$repo" read-tree --empty >/dev/null 2>&1 || rc=1
    fi
    if [ "$rc" -ne 0 ]; then
        rm -f "$idx"
        OCRL_SNAP_ERROR='could not seed the temporary index from HEAD'
        return 1
    fi

    if ! GIT_INDEX_FILE="$idx" ocrl_git "$repo" add -A >/dev/null 2>&1; then
        rm -f "$idx"
        OCRL_SNAP_ERROR='git add -A failed against the temporary index'
        return 1
    fi

    tree=$(GIT_INDEX_FILE="$idx" ocrl_git "$repo" write-tree 2>/dev/null || true)
    rm -f "$idx"

    if [ -z "$tree" ]; then
        OCRL_SNAP_ERROR='git write-tree failed against the temporary index'
        return 1
    fi

    OCRL_SNAP_WARNINGS=$(ocrl_snap_submodules "$repo")
    printf '%s' "$tree"
}

# True when the worktree holds nothing beyond HEAD (no staged, unstaged or
# non-ignored untracked content).
ocrl_worktree_clean() {
    local repo=$1 snap head
    snap=$(ocrl_snap_tree "$repo") || return 1
    head=$(ocrl_head_tree "$repo")
    [ -n "$snap" ] && [ "$snap" = "$head" ]
}

# Human-readable summary of what makes the worktree dirty.
ocrl_dirty_summary() {
    local repo=$1
    ocrl_git "$repo" status --porcelain=v1 2>/dev/null | head -n 40
}

ocrl_changed_paths() {
    local repo=$1 base=$2 head=$3
    ocrl_git "$repo" diff --name-only "$base" "$head" 2>/dev/null || true
}

# True when every changed path matches one of the configured ignore globs.
ocrl_all_paths_ignored() {
    local repo=$1 base=$2 head=$3
    local -a globs=()
    local p g matched any=0
    mapfile -t globs < <(ocrl_cfg_array ignore_globs)
    [ "${#globs[@]}" -gt 0 ] || return 1
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        any=1
        matched=0
        for g in "${globs[@]}"; do
            # shellcheck disable=SC2053  # glob matching is the point
            if [[ $p == $g ]]; then
                matched=1
                break
            fi
        done
        [ "$matched" = "1" ] || return 1
    done < <(ocrl_changed_paths "$repo" "$base" "$head")
    [ "$any" = "1" ]
}
