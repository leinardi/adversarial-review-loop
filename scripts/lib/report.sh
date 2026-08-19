# shellcheck shell=bash
# Storing reports on disk and rendering the text Claude actually sees.
#
# Every FINDING line is returned inline. Prose is what truncates, never the
# actionable set.

# ocrl_report_store <seq> <scope> <phase> <base> <head> <raw_file>
# shellcheck disable=SC2016  # backticks in the format strings are markdown
ocrl_report_store() {
    local seq=$1 scope=$2 phase=$3 base=$4 head=$5 raw=$6
    local verdict path label
    verdict=${OCRL_REVIEW_VERDICT:-UNKNOWN}
    label=$scope
    [ "$scope" = 'phase' ] && label="phase$phase"
    path="$OCRL_ACT_DIR/reports/$seq-$label-${verdict,,}.md"
    mkdir -p "$OCRL_ACT_DIR/reports"

    {
        printf '# OpenCode review %s (%s)\n\n' "$seq" "$label"
        printf -- '- verdict (recomputed by the gate): **%s**\n' "$verdict"
        printf -- '- base tree: `%s`\n' "$base"
        printf -- '- head tree: `%s`\n' "$head"
        printf -- '- model: `%s`%s\n' "$(ocrl_cfg model)" \
            "$([ -n "$(ocrl_cfg variant)" ] && printf ' (variant `%s`)' "$(ocrl_cfg variant)")"
        printf -- '- block_severity: `%s`\n' "$(ocrl_cfg block_severity)"
        printf -- '- generated: %s\n' "$(date -Is)"
        [ -n "$OCRL_REVIEW_ERROR" ] && printf -- '- gate note: %s\n' "$OCRL_REVIEW_ERROR"
        printf '\n## Blocking findings\n\n'
        if [ -n "$OCRL_REVIEW_FINDINGS" ]; then
            printf '```\n%s```\n' "$OCRL_REVIEW_FINDINGS"
        else
            printf '(none)\n'
        fi
        printf '\n## Raw reviewer output\n\n'
        printf '````\n'
        cat "$raw" 2>/dev/null
        printf '\n````\n'
    } >"$path"

    # Deliberately silent: this runs inside the hook entrypoints, whose stdout
    # is the hook's JSON response.
    OCRL_REVIEW_REPORT=$path
}

# ocrl_report_reason <headline>
# The message handed back through a deny or a Stop block.
ocrl_report_reason() {
    local headline=$1 max
    max=$(ocrl_cfg max_reason_bytes)
    {
        printf '%s\n' "$headline"
        if [ -n "$OCRL_REVIEW_ERROR" ]; then
            printf '\nGate note: %s\n' "$OCRL_REVIEW_ERROR"
        fi
        if [ -n "$OCRL_REVIEW_FINDINGS" ]; then
            printf '\nBlocking findings (actionable, severity >= %s) -- every one must be resolved:\n\n' \
                "$(ocrl_cfg block_severity)"
            printf '%s' "$OCRL_REVIEW_FINDINGS"
        fi
        if [ -n "$OCRL_REVIEW_ALL" ] && [ "$OCRL_REVIEW_ALL" != "$OCRL_REVIEW_FINDINGS" ]; then
            printf '\nAll findings reported (non-blocking ones included, for context):\n\n'
            printf '%s' "$OCRL_REVIEW_ALL"
        fi
        if [ -n "$OCRL_REVIEW_PROSE" ]; then
            printf '\nReviewer prose:\n\n'
            printf '%s' "$OCRL_REVIEW_PROSE" | ocrl_truncate_bytes "$max"
            printf '\n'
        fi
        [ -n "$OCRL_REVIEW_REPORT" ] && printf '\nFull report: %s\n' "$OCRL_REVIEW_REPORT"
        printf '\nFix the findings above, then commit again. The commit is gated until the review passes.\n'
    }
}

ocrl_report_list() {
    local d="$OCRL_ACT_DIR/reports"
    [ -d "$d" ] || return 0
    find "$d" -maxdepth 1 -name '*.md' -printf '%f\n' 2>/dev/null | sort
}

# ocrl_report_print [n] -- n is the report sequence number; newest when omitted.
ocrl_report_print() {
    local n=${1:-} d="$OCRL_ACT_DIR/reports" f
    if [ ! -d "$d" ]; then
        printf 'No reports have been produced for this activation yet.\n'
        return 0
    fi
    if [ -n "$n" ]; then
        f=$(find "$d" -maxdepth 1 -name "$(printf '%03d' "$n")-*.md" | sort | head -n 1)
    else
        f=$(find "$d" -maxdepth 1 -name '*.md' | sort | tail -n 1)
    fi
    if [ -z "$f" ]; then
        printf 'No such report. Available:\n'
        ocrl_report_list
        return 0
    fi
    cat "$f"
}
