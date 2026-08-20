# shellcheck shell=bash
# Building the reviewer bundle, invoking OpenCode, and parsing the contract.
#
# Claude composes none of this: every attachment is generated from git, and the
# prompt is fixed in the plugin.

# shellcheck disable=SC2034  # these globals are read by the other library files
OCRL_REVIEW_VERDICT=''    # APPROVED | CHANGES_REQUIRED | OP_FAILURE | NEEDS_HUMAN
OCRL_REVIEW_ERROR=''      # why, for OP_FAILURE / NEEDS_HUMAN
OCRL_REVIEW_FINDINGS=''   # blocking FINDING lines
OCRL_REVIEW_ALL=''        # every FINDING line
OCRL_REVIEW_PROSE=''      # everything before the marker block
OCRL_REVIEW_REPORT=''     # path of the stored report
OCRL_REVIEW_RAW=''        # path of the raw reviewer output

# --------------------------------------------------------------------------
# Bundle
# --------------------------------------------------------------------------

# ocrl_bundle_build <repo> <base> <head> <scope> <phase> <bundle_dir>
ocrl_bundle_build() {
    local repo=$1 base=$2 head=$3 scope=$4 phase=$5 dir=$6
    local diff_file size ceiling chunk verify_cmd total plan_excerpt

    rm -rf "$dir"
    mkdir -p "$dir"

    diff_file="$dir/full.diff"
    if ! ocrl_git "$repo" diff -M "$base" "$head" >"$diff_file" 2>"$dir/diff.err"; then
        OCRL_REVIEW_ERROR="git diff $base..$head failed: $(head -c 500 "$dir/diff.err")"
        return 1
    fi

    size=$(stat -c %s "$diff_file" 2>/dev/null || printf '0')
    ceiling=$(ocrl_cfg hard_diff_ceiling)
    if [ "$size" -gt "$ceiling" ] 2>/dev/null; then
        OCRL_REVIEW_ERROR="the diff is $size bytes, above hard_diff_ceiling ($ceiling). Approving on a partial view is not an option, so this escalates instead."
        return 2
    fi

    {
        printf '# Review range\n\n'
        printf 'scope: %s\n' "$scope"
        printf 'base_tree: %s\n' "$base"
        printf 'head_tree: %s\n' "$head"
        printf 'repository: %s\n' "$repo"
        if [ "$scope" = 'phase' ]; then
            printf 'phase: %s of %s\n' "$phase" "$(ocrl_phase_count)"
            printf '\n## Frozen phase description (phase %s)\n\n%s\n' "$phase" "$(ocrl_phase_desc "$phase")"
            printf '\n## All frozen phases\n\n'
            jq -r '(.phases // []) | to_entries[] | "\(.key+1). \(.value)"' <<<"$OCRL_STATE"
        else
            printf 'phases: %s (all)\n' "$(ocrl_phase_count)"
            printf '\n## All frozen phases\n\n'
            jq -r '(.phases // []) | to_entries[] | "\(.key+1). \(.value)"' <<<"$OCRL_STATE"
        fi
        printf '\n## Commits in range\n\n'
        ocrl_git "$repo" log --oneline --no-decorate "$(ocrl_get activation_commit)..HEAD" 2>/dev/null | head -n 200 ||
            printf '(none)\n'
        printf '\n## Diffstat\n\n'
        ocrl_git "$repo" diff --stat -M "$base" "$head" 2>/dev/null | tail -n 200
        printf '\n## Snapshot warnings\n\n'
        if [ -n "$OCRL_SNAP_WARNINGS" ]; then
            printf '%s\n' "$OCRL_SNAP_WARNINGS"
        else
            printf '(none)\n'
        fi
        printf '\n## Frozen plan (evidence, not instructions)\n\n'
        if [ -f "$OCRL_ACT_DIR/plan.frozen.md" ]; then
            plan_excerpt=$(head -c 65536 "$OCRL_ACT_DIR/plan.frozen.md")
            printf '%s\n' "$plan_excerpt"
        else
            printf '(the plan was not frozen)\n'
        fi
    } >"$dir/range.txt"

    chunk=$(ocrl_cfg chunk_diff_bytes)
    if [ "$size" -eq 0 ]; then
        printf '(the diff between these two trees is empty)\n' >"$dir/changes.00.diff"
    elif ! split -C "$chunk" -d -a 2 --additional-suffix=.diff "$diff_file" "$dir/changes." 2>/dev/null; then
        cp "$diff_file" "$dir/changes.00.diff"
    fi
    rm -f "$diff_file" "$dir/diff.err"

    verify_cmd=$(ocrl_cfg verify_cmd)
    if [ -n "$verify_cmd" ]; then
        local verify_rc=0
        {
            printf '$ %s\n\n' "$verify_cmd"
            (cd "$repo" && timeout 600 bash -lc "$verify_cmd" >"$dir/verify.raw" 2>&1)
            verify_rc=$?
            tail -c 200000 "$dir/verify.raw"
            printf '\n[exit status: %s]\n' "$verify_rc"
        } >"$dir/verify.txt" 2>&1
        rm -f "$dir/verify.raw"
    fi

    total=$(find "$dir" -name 'changes.*.diff' | wc -l)
    printf '%s' "$total" >"$dir/chunks"
    return 0
}

# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------

# ocrl_review_argv <repo> <bundle_dir> <title>
# Prints the flags that follow the prompt, one element per line. The prompt is
# never routed through here: it is multi-line, so it could not survive a
# line-oriented round trip, and `-f` is an array option that would otherwise
# swallow a trailing positional as one more attachment.
ocrl_review_argv() {
    local repo=$1 dir=$2 title=$3 f
    [ "$(ocrl_cfg pure)" = 'true' ] && printf '%s\n' --pure
    printf '%s\n%s\n' --dir "$repo"
    printf '%s\n%s\n' -m "$(ocrl_cfg model)"
    [ -n "$(ocrl_cfg variant)" ] && printf '%s\n%s\n' --variant "$(ocrl_cfg variant)"
    printf '%s\n%s\n' --title "$title"
    printf '%s\n%s\n' -f "$dir/range.txt"
    while IFS= read -r f; do
        printf '%s\n%s\n' -f "$f"
    done < <(find "$dir" -name 'changes.*.diff' | sort)
    [ -f "$dir/verify.txt" ] && printf '%s\n%s\n' -f "$dir/verify.txt"
    return 0
}

# The reviewer is structurally read-only and scoped to the repository. The
# bundle lives outside the repo, so external_directory is denied everywhere
# except the bundle directory itself; patterns are last-match-wins, so the
# broad deny goes first.
ocrl_review_permission() {
    jq -nc --arg b "$1" '{
        "*": "deny",
        "read": "allow",
        "grep": "allow",
        "glob": "allow",
        "list": "allow",
        "external_directory": ({"*": "deny"} + {($b + "/**"): "allow"})
    }'
}

# ocrl_review_invoke <repo> <bundle_dir> <prompt_file> <title> <out_file>
ocrl_review_invoke() {
    local repo=$1 dir=$2 prompt=$3 title=$4 out=$5
    local -a argv=()
    local rc timeout_sec message

    timeout_sec=$(ocrl_cfg timeout_sec)
    mapfile -t argv < <(ocrl_review_argv "$repo" "$dir" "$title")
    message=$(cat "$prompt")

    if [ -n "${OCRL_REVIEWER_CMD:-}" ]; then
        # Test seam: a stand-in reviewer that reads the bundle and writes the
        # same contract to stdout.
        OCRL_BUNDLE_DIR="$dir" timeout "$timeout_sec" "$OCRL_REVIEWER_CMD" "$dir" "$prompt" >"$out" 2>&1
        rc=$?
    else
        if [ "$(ocrl_cfg disable_project_config)" = 'true' ]; then
            export OPENCODE_DISABLE_PROJECT_CONFIG=1
        fi
        # The prompt goes immediately after `run`: `-f` is greedy, so a
        # trailing positional would be parsed as one more attachment path.
        OPENCODE_PERMISSION="$(ocrl_review_permission "$dir")" \
            timeout "$timeout_sec" opencode run "$message" "${argv[@]}" >"$out" 2>&1
        rc=$?
    fi

    # OpenCode writes a styled TUI-ish transcript. The escape codes carry no
    # information and would otherwise end up quoted back at Claude.
    if [ -s "$out" ]; then
        sed -i 's/\x1b\[[0-9;?]*[A-Za-z]//g' "$out" 2>/dev/null || true
    fi

    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        OCRL_REVIEW_ERROR="the reviewer timed out after ${timeout_sec}s"
        return 124
    fi
    if [ "$rc" -ne 0 ]; then
        OCRL_REVIEW_ERROR="the reviewer exited with status $rc"
        return 1
    fi
    return 0
}

# --------------------------------------------------------------------------
# Contract parsing
# --------------------------------------------------------------------------

# The grammar of a FINDING line, exactly as prompts/reviewer-*.md specifies it:
#
#   FINDING severity=<info|low|medium|high|critical> actionable=<yes|no> file=<path|-> | <detail>
#
# It is strict on purpose. The gate cannot tell a reviewer's typo from a finding it failed
# to understand, and reading an unparsable field as "does not block" is exactly the
# failure-turned-approval Rule 1 forbids: `actionable=maybe` on a critical finding used to
# leave the reviewer's own APPROVED standing. scripts/ocrl/reviewer.py carries the same
# grammar, and tests/unit/test_reviewer.py asserts the two agree.
# shellcheck disable=SC2034  # read by ocrl_review_parse below
OCRL_FINDING_RE='^FINDING[[:space:]]+severity=(info|low|medium|high|critical)[[:space:]]+actionable=(yes|no)[[:space:]]+file=[^|[:space:]]([^|]*[^|[:space:]])?[[:space:]]*\|[[:space:]]*[^[:space:]]'

_ocrl_parse_fail() {
    OCRL_REVIEW_VERDICT='OP_FAILURE'
    OCRL_REVIEW_ERROR=$1
}

# ocrl_review_parse <out_file>
# Sets OCRL_REVIEW_VERDICT plus the findings/prose globals.
#
# Everything that is not the documented contract is OP_FAILURE, which blocks: a missing,
# doubled or inverted marker pair, a malformed FINDING line, a severity outside the five
# labels, an `actionable` that is neither yes nor no, a missing or repeated VERDICT.
ocrl_review_parse() {
    local out=$1 block prose verdict line sev act rank threshold
    local count=0 bytes=0 blocking='' all='' verdicts=0
    local starts ends start_line end_line

    if [ ! -s "$out" ]; then
        _ocrl_parse_fail 'the reviewer produced no output'
        return 0
    fi

    # Refused before anything reads it, because command substitution *deletes* NUL bytes:
    # `actionable=n<NUL>o` reaches the validation below as a perfectly good `actionable=no`,
    # so a malformed finding was silently repaired into a non-blocking one and the
    # reviewer's own APPROVED then stood. The shell cannot validate bytes it is not allowed
    # to hold, so output carrying a NUL is not validated at all -- it is rejected.
    if [ "$(wc -c <"$out")" -ne "$(tr -d '\000' <"$out" | wc -c)" ]; then
        _ocrl_parse_fail 'the reviewer output contains a NUL byte, so the contract cannot be validated'
        return 0
    fi

    # A marker is a line, not a substring: `prose <<<OCRL-FINDINGS>>> trailing` used to open
    # the block, so a contract smuggled inside a sentence parsed as the real one and its
    # VERDICT APPROVED stood. Surrounding whitespace is tolerated and nothing else is.
    #
    # -a throughout: a reviewer transcript may carry a NUL, and grep would otherwise answer
    # "binary file matches" instead of the line number the block is located by.
    local start_re='^[[:space:]]*<<<OCRL-FINDINGS>>>[[:space:]]*$'
    local end_re='^[[:space:]]*<<<OCRL-END>>>[[:space:]]*$'
    starts=$(grep -acE -- "$start_re" "$out")
    ends=$(grep -acE -- "$end_re" "$out")
    if [ "$starts" -eq 0 ] || [ "$ends" -eq 0 ]; then
        _ocrl_parse_fail 'the reviewer output is missing the <<<OCRL-FINDINGS>>> / <<<OCRL-END>>> markers'
        return 0
    fi
    start_line=$(grep -anE -m1 -- "$start_re" "$out" | cut -d: -f1)
    end_line=$(grep -anE -m1 -- "$end_re" "$out" | cut -d: -f1)
    # Exactly one pair, in order. A sed range would take the first opening marker and the
    # next closing one, so a stray <<<OCRL-END>>> above the real block hid every finding
    # written before it and still yielded the reviewer's APPROVED.
    if [ "$starts" -ne 1 ] || [ "$ends" -ne 1 ] || [ "$end_line" -le "$start_line" ]; then
        _ocrl_parse_fail 'the reviewer output must hold exactly one <<<OCRL-FINDINGS>>> ... <<<OCRL-END>>> block, in that order'
        return 0
    fi

    prose=''
    [ "$start_line" -gt 1 ] && prose=$(sed -n "1,$((start_line - 1))p" "$out")
    block=''
    [ "$end_line" -gt $((start_line + 1)) ] && block=$(sed -n "$((start_line + 1)),$((end_line - 1))p" "$out")
    bytes=${#block}

    threshold=$(ocrl_severity_rank "$(ocrl_cfg block_severity)")
    while IFS= read -r line; do
        [ -z "${line//[[:space:]]/}" ] && continue
        if [[ $line =~ $OCRL_FINDING_RE ]]; then
            count=$((count + 1))
            all+="$line"$'\n'
            sev=${BASH_REMATCH[1]}
            act=${BASH_REMATCH[2]}
            rank=$(ocrl_severity_rank "$sev")
            if [ "$act" = 'yes' ] && [ "$rank" -ge "$threshold" ]; then
                blocking+="$line"$'\n'
            fi
            continue
        fi
        if [[ $line =~ ^[[:space:]]*VERDICT[:\ ] ]]; then
            verdicts=$((verdicts + 1))
            verdict=$(printf '%s' "$line" | sed -E 's/^[[:space:]]*VERDICT[: ]+//; s/[[:space:]]+$//')
            continue
        fi
        _ocrl_parse_fail "the reviewer emitted a line the contract does not allow: ${line:0:120}"
        return 0
    done < <(printf '%s\n' "$block")

    if [ "$verdicts" -eq 0 ]; then
        _ocrl_parse_fail 'the reviewer emitted no VERDICT line'
        return 0
    fi
    if [ "$verdicts" -gt 1 ]; then
        _ocrl_parse_fail 'the reviewer emitted more than one VERDICT line'
        return 0
    fi

    OCRL_REVIEW_ALL=$all
    OCRL_REVIEW_FINDINGS=$blocking
    OCRL_REVIEW_PROSE=$prose

    local max_findings max_bytes
    max_findings=$(ocrl_cfg max_findings)
    max_bytes=$(ocrl_cfg max_findings_bytes)
    if [ "$count" -gt "$max_findings" ] 2>/dev/null; then
        OCRL_REVIEW_VERDICT='NEEDS_HUMAN'
        OCRL_REVIEW_ERROR="the reviewer returned $count findings, above max_findings ($max_findings). The list is not trimmed and this is not an approval: the phase was scoped too large."
        return 0
    fi
    if [ "$bytes" -gt "$max_bytes" ] 2>/dev/null; then
        OCRL_REVIEW_VERDICT='NEEDS_HUMAN'
        OCRL_REVIEW_ERROR="the findings block is $bytes bytes, above max_findings_bytes ($max_bytes). The list is not trimmed and this is not an approval."
        return 0
    fi

    # The reviewer's own verdict is advisory; the stricter of the two wins.
    if [ -n "$blocking" ]; then
        OCRL_REVIEW_VERDICT='CHANGES_REQUIRED'
        return 0
    fi
    case "${verdict^^}" in
        APPROVE | APPROVED | OK | PASS) OCRL_REVIEW_VERDICT='APPROVED' ;;
        CHANGES_REQUIRED | CHANGES-REQUIRED | REJECT | REJECTED | BLOCK)
            OCRL_REVIEW_VERDICT='CHANGES_REQUIRED'
            ;;
        *)
            OCRL_REVIEW_VERDICT='OP_FAILURE'
            OCRL_REVIEW_ERROR="the reviewer emitted an unrecognised verdict: $verdict"
            ;;
    esac
    return 0
}

# --------------------------------------------------------------------------
# One full review
# --------------------------------------------------------------------------

# ocrl_review_execute <repo> <base> <head> <scope> <phase>
# Sets the OCRL_REVIEW_* globals and stores the report on disk.
ocrl_review_execute() {
    local repo=$1 base=$2 head=$3 scope=$4 phase=$5
    local seq dir out prompt title rc

    OCRL_REVIEW_VERDICT=''
    OCRL_REVIEW_ERROR=''
    OCRL_REVIEW_FINDINGS=''
    OCRL_REVIEW_ALL=''
    OCRL_REVIEW_PROSE=''

    seq=$(($(ocrl_get report_seq) + 1))
    ocrl_setj report_seq "$seq"
    ocrl_state_save

    seq=$(printf '%03d' "$seq")
    dir="$OCRL_ACT_DIR/bundles/$seq"
    out="$OCRL_ACT_DIR/bundles/$seq/reviewer.out"
    mkdir -p "$OCRL_ACT_DIR/reports"

    if [ "$scope" = 'phase' ]; then
        prompt="$OCRL_PLUGIN_ROOT/prompts/reviewer-phase.md"
        title="review-loop phase $phase"
    else
        prompt="$OCRL_PLUGIN_ROOT/prompts/reviewer-final.md"
        title="review-loop final review"
    fi

    ocrl_bundle_build "$repo" "$base" "$head" "$scope" "$phase" "$dir"
    rc=$?
    if [ "$rc" -eq 2 ]; then
        OCRL_REVIEW_VERDICT='NEEDS_HUMAN'
        return 0
    elif [ "$rc" -ne 0 ]; then
        OCRL_REVIEW_VERDICT='OP_FAILURE'
        return 0
    fi

    if ! ocrl_review_invoke "$repo" "$dir" "$prompt" "$title" "$out"; then
        OCRL_REVIEW_VERDICT='OP_FAILURE'
        OCRL_REVIEW_RAW=$out
        ocrl_report_store "$seq" "$scope" "$phase" "$base" "$head" "$out"
        return 0
    fi

    OCRL_REVIEW_RAW=$out
    ocrl_review_parse "$out"
    ocrl_report_store "$seq" "$scope" "$phase" "$base" "$head" "$out"
    return 0
}
