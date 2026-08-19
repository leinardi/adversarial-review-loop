# shellcheck shell=bash
# Command-shape classification.
#
# A commit is only let through when every segment of the command is provably
# incapable of changing working-tree content after the snapshot was taken.
# Anything the classifier cannot categorise confidently is denied.

# shellcheck disable=SC2034  # these globals are read by the other library files
OCRL_TOKENS=()
OCRL_CMD_ERROR=''

# --------------------------------------------------------------------------
# Tokenizer
#
# Quote-aware, and hostile to everything that could run a second program or
# touch a file: substitutions, redirections, pipelines, globs, comments.
# "&&" survives as its own token so segments can be split safely.
# --------------------------------------------------------------------------

ocrl_cmd_tokenize() {
    local s=$1
    local -i i=0 n=${#s} started=0
    local c q='' tok=''
    OCRL_TOKENS=()
    OCRL_CMD_ERROR=''

    case "$s" in
        *'$'*)
            OCRL_CMD_ERROR='the command contains "$" (variable or command substitution)'
            return 1
            ;;
        *'`'*)
            OCRL_CMD_ERROR='the command contains a backtick (command substitution)'
            return 1
            ;;
        *$'\n'* | *$'\r'*)
            OCRL_CMD_ERROR='the command spans multiple lines'
            return 1
            ;;
    esac

    while [ "$i" -lt "$n" ]; do
        c=${s:i:1}
        if [ -n "$q" ]; then
            if [ "$c" = "$q" ]; then
                q=''
            else
                tok+=$c
            fi
            started=1
        else
            case "$c" in
                "'" | '"')
                    q=$c
                    started=1
                    ;;
                ' ' | $'\t')
                    if [ "$started" -eq 1 ]; then
                        OCRL_TOKENS+=("$tok")
                        tok=''
                        started=0
                    fi
                    ;;
                \\)
                    i=$((i + 1))
                    tok+=${s:i:1}
                    started=1
                    ;;
                '&')
                    if [ "${s:i+1:1}" = '&' ]; then
                        if [ "$started" -eq 1 ]; then
                            OCRL_TOKENS+=("$tok")
                            tok=''
                            started=0
                        fi
                        OCRL_TOKENS+=('&&')
                        i=$((i + 1))
                    else
                        OCRL_CMD_ERROR='the command backgrounds a process ("&")'
                        return 1
                    fi
                    ;;
                ';' | '|' | '<' | '>' | '(' | ')' | '{' | '}')
                    OCRL_CMD_ERROR="the command contains the shell metacharacter \"$c\" (pipeline, redirection, subshell or sequencing)"
                    return 1
                    ;;
                '*' | '?' | '[' | ']')
                    OCRL_CMD_ERROR="the command contains an unquoted glob character \"$c\""
                    return 1
                    ;;
                '#')
                    if [ "$started" -eq 0 ]; then
                        OCRL_CMD_ERROR='the command contains a comment'
                        return 1
                    fi
                    tok+=$c
                    ;;
                *)
                    tok+=$c
                    started=1
                    ;;
            esac
        fi
        i=$((i + 1))
    done

    if [ -n "$q" ]; then
        OCRL_CMD_ERROR='the command has an unterminated quote'
        return 1
    fi
    [ "$started" -eq 1 ] && OCRL_TOKENS+=("$tok")
    return 0
}

# --------------------------------------------------------------------------
# Cheap detection: does this command try to create a commit at all?
# Deliberately loose -- anything it flags still has to pass full validation.
# --------------------------------------------------------------------------

ocrl_cmd_mentions_commit() {
    printf '%s' "$1" | grep -Eq '(^|[[:space:];&|(])git([[:space:]]+-[^[:space:]]+)*[[:space:]]+commit([[:space:]]|$)'
}

ocrl_cmd_mentions_reset() {
    printf '%s' "$1" | grep -Eq '(^|[[:space:];&|(])git([[:space:]]+-[^[:space:]]+)*[[:space:]]+reset([[:space:]]|$)'
}

# The user-only escapes. Claude's own route -- Bash -- is denied.
ocrl_cmd_is_escape() {
    printf '%s' "$1" | grep -Eq 'ocrl(\.sh)?[[:space:]]+(finish|deactivate)([[:space:]]|$)'
}

# --------------------------------------------------------------------------
# Per-subcommand flag allowlists (default-deny on anything unknown)
# --------------------------------------------------------------------------

_ocrl_short_ok() {
    local sub=$1 ch=$2
    case "$sub" in
        add) [[ $ch == [Auvn] ]] ;;
        status) [[ $ch == [sbzuv] ]] ;;
        commit) [[ $ch == [asqvnmS] ]] ;;
        *) return 1 ;;
    esac
}

# Short flags whose remainder (or next token) is a value.
_ocrl_short_takes_value() {
    local sub=$1 ch=$2
    case "$sub:$ch" in
        commit:m) return 0 ;;
        commit:S) return 0 ;; # optional attached key id
        status:u) return 0 ;; # -uall / -uno, value optional
        *) return 1 ;;
    esac
}

_ocrl_short_value_required() {
    [ "$1:$2" = "commit:m" ]
}

_ocrl_long_ok() {
    local sub=$1 opt=$2
    case "$sub" in
        add)
            case "$opt" in
                --all | --no-ignore-removal | --ignore-removal | --update | --verbose | --dry-run) return 0 ;;
            esac
            ;;
        status)
            case "$opt" in
                --short | --long | --branch | --no-branch | --verbose | --ignored | --ignored=* | \
                    --porcelain | --porcelain=* | --untracked-files | --untracked-files=* | --null) return 0 ;;
            esac
            ;;
        commit)
            case "$opt" in
                --all | --signoff | --no-signoff | --quiet | --verbose | --no-verify | --verify | \
                    --allow-empty | --allow-empty-message | --no-post-rewrite | --no-gpg-sign | \
                    --gpg-sign | --gpg-sign=* | --author=* | --date=* | --message=* | --trailer | \
                    --trailer=* | --cleanup=* | --status | --no-status) return 0 ;;
            esac
            ;;
    esac
    return 1
}

# Long options that get a specific explanation instead of the generic one.
_ocrl_long_reason() {
    case "$2" in
        --amend) printf 'amending rewrites the commit that was already reviewed, so the reviewed tree can no longer be verified against it' ;;
        --only | --include) printf 'partial commits (%s) commit something other than the reviewed snapshot' "$2" ;;
        --interactive | --patch) printf 'interactive staging cannot be reconciled with the snapshot that was reviewed' ;;
        --file | --file=*) printf '--file reads the message from a path that may change after the snapshot' ;;
        --fixup | --fixup=* | --squash | --squash=*) printf '%s produces a commit that is meant to be rewritten later' "$2" ;;
        --template | --template=*) printf '--template opens an editor, which stalls the hook' ;;
        --pathspec-from-file | --pathspec-from-file=*) printf '--pathspec-from-file stages a set the gate cannot see' ;;
        --chmod | --chmod=*) printf '--chmod changes modes outside the snapshot' ;;
        --force | --renormalize) printf '%s can stage content the snapshot deliberately excluded' "$2" ;;
        *) printf '' ;;
    esac
}

# Validate a single already-tokenized segment. Tokens arrive as "$@".
_ocrl_validate_segment() {
    local -a t=("$@")
    local sub i tok reason ch j allow_positional after_ddash=0

    if [ "${#t[@]}" -eq 0 ]; then
        OCRL_CMD_ERROR='the command contains an empty segment'
        return 1
    fi
    if [ "${t[0]}" != 'git' ]; then
        OCRL_CMD_ERROR="segment starts with \"${t[0]}\"; only git add, git status and git commit may appear alongside a commit"
        return 1
    fi
    if [ "${#t[@]}" -lt 2 ]; then
        OCRL_CMD_ERROR='a bare "git" with no subcommand'
        return 1
    fi

    sub=${t[1]}
    case "$sub" in
        -*)
            OCRL_CMD_ERROR="git global options before the subcommand (\"$sub\") are not allowed: -C, -c, --git-dir and --work-tree can retarget the commit away from the reviewed worktree"
            return 1
            ;;
        add | status | commit) ;;
        rm)
            OCRL_CMD_ERROR='git rm deletes from the working tree after the snapshot was taken; run it as a separate command and let the next gate pick it up'
            return 1
            ;;
        diff)
            OCRL_CMD_ERROR='git diff can write files (--output, --ext-diff) and buys nothing in a commit sequence'
            return 1
            ;;
        *)
            OCRL_CMD_ERROR="git $sub is not one of the allowed subcommands (add, status, commit)"
            return 1
            ;;
    esac

    allow_positional=1
    [ "$sub" = 'commit' ] && allow_positional=0

    i=2
    while [ "$i" -lt "${#t[@]}" ]; do
        tok=${t[i]}
        if [ "$after_ddash" -eq 1 ]; then
            if [ "$allow_positional" -eq 0 ]; then
                OCRL_CMD_ERROR="git commit with a pathspec (\"$tok\") is a partial commit; it would commit something other than the reviewed snapshot"
                return 1
            fi
            i=$((i + 1))
            continue
        fi
        case "$tok" in
            --)
                after_ddash=1
                ;;
            --*)
                reason=$(_ocrl_long_reason "$sub" "$tok")
                if [ -n "$reason" ]; then
                    OCRL_CMD_ERROR="git $sub $tok is not allowed: $reason"
                    return 1
                fi
                if ! _ocrl_long_ok "$sub" "$tok"; then
                    OCRL_CMD_ERROR="git $sub $tok is not on the allowlist for this gate"
                    return 1
                fi
                if [ "$tok" = '--trailer' ] || [ "$tok" = '--untracked-files' ]; then
                    i=$((i + 1)) # consumes its value
                fi
                ;;
            -?*)
                j=1
                while [ "$j" -lt "${#tok}" ]; do
                    ch=${tok:j:1}
                    if ! _ocrl_short_ok "$sub" "$ch"; then
                        reason=$(_ocrl_long_reason "$sub" "-$ch")
                        if [ -n "$reason" ]; then
                            OCRL_CMD_ERROR="git $sub -$ch is not allowed: $reason"
                        else
                            OCRL_CMD_ERROR="git $sub -$ch is not on the allowlist for this gate"
                        fi
                        return 1
                    fi
                    if _ocrl_short_takes_value "$sub" "$ch"; then
                        if [ "$j" -lt $((${#tok} - 1)) ]; then
                            break # rest of the token is the value
                        fi
                        if _ocrl_short_value_required "$sub" "$ch"; then
                            i=$((i + 1))
                            if [ "$i" -ge "${#t[@]}" ]; then
                                OCRL_CMD_ERROR="git $sub -$ch is missing its value"
                                return 1
                            fi
                        fi
                        break
                    fi
                    j=$((j + 1))
                done
                ;;
            -)
                OCRL_CMD_ERROR='a bare "-" argument reads from stdin'
                return 1
                ;;
            *)
                if [ "$allow_positional" -eq 0 ]; then
                    OCRL_CMD_ERROR="git commit with a pathspec (\"$tok\") is a partial commit; it would commit something other than the reviewed snapshot"
                    return 1
                fi
                ;;
        esac
        i=$((i + 1))
    done
    return 0
}

# ocrl_cmd_validate_commit <command>
#   0 -- the shape is accepted
#   1 -- denied, with the explanation in $OCRL_CMD_ERROR
ocrl_cmd_validate_commit() {
    local cmd=$1
    local -a seg=()
    local tok saw_commit=0 segments=0

    ocrl_cmd_tokenize "$cmd" || return 1

    if [ "${#OCRL_TOKENS[@]}" -eq 0 ]; then
        OCRL_CMD_ERROR='empty command'
        return 1
    fi

    for tok in "${OCRL_TOKENS[@]}"; do
        if [ "$tok" = '&&' ]; then
            segments=$((segments + 1))
            if [ "$segments" -gt 8 ]; then
                OCRL_CMD_ERROR='too many chained segments; keep the commit sequence short'
                return 1
            fi
            _ocrl_validate_segment "${seg[@]}" || return 1
            [ "${seg[1]:-}" = 'commit' ] && saw_commit=1
            seg=()
        else
            seg+=("$tok")
        fi
    done
    _ocrl_validate_segment "${seg[@]}" || return 1
    [ "${seg[1]:-}" = 'commit' ] && saw_commit=1

    if [ "$saw_commit" -ne 1 ]; then
        OCRL_CMD_ERROR='no "git commit" segment found'
        return 1
    fi
    return 0
}

# ocrl_cmd_reset_target <command> -- prints the requested target for a bounded
# `git reset --soft <target>`, or fails with the reason in $OCRL_CMD_ERROR.
ocrl_cmd_reset_target() {
    local cmd=$1
    local -a t
    local i tok target='' soft=0

    ocrl_cmd_tokenize "$cmd" || return 1
    t=("${OCRL_TOKENS[@]}")

    for tok in "${t[@]}"; do
        if [ "$tok" = '&&' ]; then
            OCRL_CMD_ERROR='the recovery reset must be a single command on its own'
            return 1
        fi
    done
    if [ "${#t[@]}" -lt 3 ] || [ "${t[0]}" != 'git' ] || [ "${t[1]}" != 'reset' ]; then
        OCRL_CMD_ERROR='not a plain "git reset" command'
        return 1
    fi

    i=2
    while [ "$i" -lt "${#t[@]}" ]; do
        tok=${t[i]}
        case "$tok" in
            --soft) soft=1 ;;
            --quiet | -q) ;;
            --hard | --mixed | --merge | --keep)
                OCRL_CMD_ERROR="git reset $tok would discard working-tree content; only --soft is permitted during reconcile"
                return 1
                ;;
            -*)
                OCRL_CMD_ERROR="git reset $tok is not permitted during reconcile"
                return 1
                ;;
            *)
                if [ -n "$target" ]; then
                    OCRL_CMD_ERROR='git reset accepts exactly one target during reconcile'
                    return 1
                fi
                target=$tok
                ;;
        esac
        i=$((i + 1))
    done

    if [ "$soft" -ne 1 ]; then
        OCRL_CMD_ERROR='only "git reset --soft <target>" is permitted during reconcile'
        return 1
    fi
    if [ -z "$target" ]; then
        OCRL_CMD_ERROR='git reset --soft needs an explicit target during reconcile'
        return 1
    fi
    printf '%s' "$target"
}
