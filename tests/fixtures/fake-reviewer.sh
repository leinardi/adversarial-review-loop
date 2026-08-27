#!/usr/bin/env bash
# Stand-in reviewer used by the selftest, so the loop logic can be exercised
# without spending a model call. Behaviour is chosen with OCRL_FAKE_MODE.
#
# Invoked as: fake-reviewer.sh <bundle_dir> <prompt_file>

set -uo pipefail

bundle=${1:-}
mode=${OCRL_FAKE_MODE:-approve}

case "$mode" in
    approve)
        printf 'Read the whole diff. Nothing blocking.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    approve-with-nit)
        printf 'One taste-level remark, nothing that must change.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Could be named better\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    changes)
        printf 'The error path is wrong.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    changes-file)
        # Same shape as `changes`, but the finding's anchor (file) is named by
        # OCRL_FAKE_FILE, so a test can drive a sequence of rounds that either repeat the
        # same anchor (stall detection) or raise a fresh one every round (no cap).
        f=${OCRL_FAKE_FILE:-a.txt}
        printf 'A problem in %s.\n\n' "$f"
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=%s:1 | Problem in %s\n' "$f" "$f"
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    approve-with-critical)
        # A reviewer that contradicts itself: the gate must recompute and block.
        printf 'Looks fine overall.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVE\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    critical-nonactionable)
        printf 'A worry I cannot pin down.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=no file=- | General unease about the design\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    malformed)
        printf 'I reviewed it and it seems fine to me.\n'
        ;;
    # Every mode below emits a block the contract does not allow, alongside the reviewer's
    # own APPROVED. Each one used to be read as "no findings" and approved the commit.
    bad-actionable)
        printf 'Looks fine to me.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=maybe file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    bad-severity)
        printf 'Looks fine to me.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=spicy actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    mangled-finding)
        # One stray colon, and the whole finding stopped counting.
        printf 'Looks fine to me.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING: severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    stray-end)
        # The real findings sit above a stray end marker, so the sed range never saw them.
        printf 'Serious problems below.\n\n'
        printf '<<<OCRL-END>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    two-blocks)
        printf 'Two blocks, one of them empty.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf '<<<OCRL-END>>>\n'
        printf 'On reflection:\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    inline-start-marker)
        # The marker is real, but it is buried in a sentence rather than on its own line.
        printf 'Serious problems below.\n\n'
        printf 'As requested: <<<OCRL-FINDINGS>>> here it comes\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    suffixed-end-marker)
        printf 'Serious problems below.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>> -- end of block\n'
        ;;
    nul-byte)
        # A NUL inside `actionable=no` is invisible in a terminal, and command substitution
        # deletes it -- so the shell used to validate a repaired copy of this line.
        printf 'Looks fine to me.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=n\000o file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    two-verdicts)
        printf 'Undecided.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    chatty-block)
        printf 'Prose, then a block with commentary in it.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'Nothing worth reporting, honestly.\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    no-verdict)
        printf 'Review body.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Something\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    empty) ;;
    nonzero)
        printf 'boom\n' >&2
        exit 3
        ;;
    rate-limited)
        # A plain non-zero exit whose own output names the reason -- classified "transient"
        # (phase 6), unlike `nonzero` above, which carries no such signal and is
        # "operational". Real provider CLIs report a rate limit this way, not with a
        # dedicated exit status.
        printf 'Error: rate limit exceeded, please retry later\n' >&2
        exit 1
        ;;
    rate-limited-elsewhere)
        # Simulates a genuinely concurrent, winning review of the same label approving --
        # writing `pending_approved_tree`, one of `hooks.Activation`'s own fields -- while
        # this one is still deciding it hit a rate limit. `pretool._review_failed`'s
        # fingerprint guard must refuse to count this against the transient budget once it
        # notices the activation moved underneath it. Same technique as `clarify-mutate`.
        root="${OCRL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/opencode-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.pending_approved_tree = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Error: rate limit exceeded, please retry later\n' >&2
        exit 1
        ;;
    slow)
        sleep 30
        ;;
    many)
        printf 'Very many findings.\n\n'
        printf '<<<OCRL-FINDINGS>>>\n'
        for i in $(seq 1 "${OCRL_FAKE_COUNT:-6}"); do
            printf 'FINDING severity=medium actionable=yes file=a.txt:%s | Finding number %s\n' "$i" "$i"
        done
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    big-prose)
        # Prose far past max_reason_bytes; every FINDING line must still arrive.
        for i in $(seq 1 2000); do
            printf 'Prose line %s: padding that exists only to overflow the prose budget.\n' "$i"
        done
        printf '\n<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Must survive truncation\n'
        printf 'FINDING severity=critical actionable=yes file=b.txt:2 | Must also survive truncation\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    echo-bundle)
        printf 'Bundle contents:\n'
        ls -1 "$bundle"
        printf '\n<<<OCRL-FINDINGS>>>\nVERDICT APPROVED\n<<<OCRL-END>>>\n'
        ;;
    echo-context)
        # Dumps the context/ attachments the real path would pass with -f, so the selftest
        # can assert round 2 is actually shown round 1's findings. Still a blocking verdict
        # so the denial reason carries the dump back.
        printf 'Prior rounds seen by this reviewer:\n'
        if [ -n "${OCRL_CONTEXT_FILES:-}" ]; then
            printf '%s\n' "$OCRL_CONTEXT_FILES" | while IFS= read -r f; do
                [ -n "$f" ] && cat "$f"
            done
        else
            printf '(no context files were passed)\n'
        fi
        printf '\n<<<OCRL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | round two is still unhappy\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<OCRL-END>>>\n'
        ;;
    clarify-mutate)
        # Simulates a resume/accept/stop landing while the reviewer answers: bumps
        # activation_generation in state.json so clarify's post-invoke fingerprint check
        # fires and the reply is discarded rather than printed.
        root="${OCRL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/opencode-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.activation_generation = ((.activation_generation // 0) + 1)' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Clarification, but the activation moved underneath it.\n'
        ;;
    clarify-supersede)
        # Simulates a concurrent reviewer.execute finishing a newer round while this clarify
        # runs: appends a round_history entry with the next seq, without touching any
        # hooks.Activation field -- so clarify's post-invoke "still the latest round?" check
        # fires (not the fingerprint check).
        root="${OCRL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/opencode-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.round_history += [{
                "seq": ((.round_history | map(.seq) | max) + 1),
                "label": ("phase" + (.phase | tostring)),
                "phase": .phase, "generation": .activation_generation,
                "round": ((.round_history | length) + 1),
                "verdict": "CHANGES_REQUIRED", "tree": "x", "base": "y",
                "at": 0, "findings": [], "supersedes": []
            }]' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Clarification about a round that just got superseded.\n'
        ;;
    clarify)
        # A clarify run: prose only, no findings block. Echoes the bundle it was pointed at
        # and the question it was handed, so the selftest and unit tests can assert both.
        printf 'Clarification.\n\n'
        printf 'bundle: %s\n' "$bundle"
        if [ -f "$bundle/range.txt" ]; then grep -E '^(base_tree|head_tree|round):' "$bundle/range.txt" || true; fi
        if [ -n "${OCRL_QUESTION_FILE:-}" ] && [ -f "$OCRL_QUESTION_FILE" ]; then
            printf 'question seen:\n'
            cat "$OCRL_QUESTION_FILE"
        else
            printf '(no question file was passed)\n'
        fi
        ;;
    *)
        printf 'unknown OCRL_FAKE_MODE: %s\n' "$mode" >&2
        exit 2
        ;;
esac
