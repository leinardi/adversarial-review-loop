#!/usr/bin/env bash
# Stand-in reviewer used by the selftest, so the loop logic can be exercised
# without spending a model call. Behaviour is chosen with ARL_FAKE_MODE.
#
# Invoked as: fake-reviewer.sh <bundle_dir> <prompt_file>

set -uo pipefail

bundle=${1:-}
prompt=${2:-}
mode=${ARL_FAKE_MODE:-approve}

case "$mode" in
    approve)
        printf 'Read the whole diff. Nothing blocking.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    approve-with-nit)
        printf 'One taste-level remark, nothing that must change.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Could be named better\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    changes)
        printf 'The error path is wrong.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    changes-file)
        # Same shape as `changes`, but the finding's anchor (file) is named by
        # ARL_FAKE_FILE, so a test can drive a sequence of rounds that either repeat the
        # same anchor (stall detection) or raise a fresh one every round (no cap).
        f=${ARL_FAKE_FILE:-a.txt}
        printf 'A problem in %s.\n\n' "$f"
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=%s:1 | Problem in %s\n' "$f" "$f"
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    medium-file)
        # A medium, actionable finding at ARL_FAKE_FILE (default a.txt:1) under the
        # reviewer's own APPROVED -- so the gate's recomputation alone decides whether it
        # blocks. Drives the late-round rule: whether such a finding blocks from round 2 on
        # depends on where it is, not on what the reviewer concluded.
        f=${ARL_FAKE_FILE:-a.txt:1}
        printf 'A medium problem in %s.\n\n' "$f"
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=medium actionable=yes file=%s | Medium problem in %s\n' "$f" "$f"
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    contract-repair)
        # A primary call that runs to completion and then writes a block the gate cannot
        # parse (`severity=P1 location=` -- the real shape seen after an OpenCode context
        # compaction), plus a repair call whose behaviour ARL_FAKE_REPAIR chooses. The two
        # are told apart by the prompt file the gate hands over, which is the only thing that
        # distinguishes them from the reviewer's side.
        case "$prompt" in
            */reviewer-repair.md)
                case "${ARL_FAKE_REPAIR:-ok}" in
                    ok)
                        printf '<<<ARL-FINDINGS>>>\n'
                        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
                        printf 'VERDICT CHANGES_REQUIRED\n'
                        printf '<<<ARL-END>>>\n'
                        ;;
                    approve)
                        # Rule: no approval may ever originate from a repair. Discarded.
                        printf '<<<ARL-FINDINGS>>>\n'
                        printf 'VERDICT APPROVED\n'
                        printf '<<<ARL-END>>>\n'
                        ;;
                    no-findings)
                        # A blocking verdict with nothing blocking in it. Also discarded: the
                        # tail may simply have been cut above the findings.
                        printf '<<<ARL-FINDINGS>>>\n'
                        printf 'VERDICT CHANGES_REQUIRED\n'
                        printf '<<<ARL-END>>>\n'
                        ;;
                    supersedes)
                        # A valid blocking block with one reversal bolted on. The repair has
                        # no earlier round to reverse -- it sees a truncated tail -- so this
                        # must fail the contract rather than record fabricated reversal
                        # evidence that `oscillation` would later count towards a stall.
                        printf '<<<ARL-FINDINGS>>>\n'
                        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
                        printf 'SUPERSEDES round=1 file=a.txt:9 | on reflection the earlier round was wrong\n'
                        printf 'VERDICT CHANGES_REQUIRED\n'
                        printf '<<<ARL-END>>>\n'
                        ;;
                    malformed)
                        printf 'I re-read it and it still seems fine to me.\n'
                        ;;
                    slow)
                        sleep "${ARL_FAKE_REPAIR_SLEEP:-30}"
                        ;;
                    nonzero)
                        printf 'boom\n' >&2
                        exit 3
                        ;;
                    echo-attachments)
                        # Names every -f attachment the repair call was launched with, so a
                        # test can assert it is shown range.txt and the fenced tail, nothing
                        # else. Still a valid blocking block so the round is recovered.
                        printf 'attachments:\n'
                        printf '%s\n' "${ARL_CONTEXT_FILES:-(none)}"
                        printf '<<<ARL-FINDINGS>>>\n'
                        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Returns success on a failed lookup\n'
                        printf 'VERDICT CHANGES_REQUIRED\n'
                        printf '<<<ARL-END>>>\n'
                        ;;
                    *)
                        printf 'unknown ARL_FAKE_REPAIR: %s\n' "${ARL_FAKE_REPAIR:-}" >&2
                        exit 2
                        ;;
                esac
                ;;
            *)
                printf 'The error path is wrong, and the lookup on line 1 returns success.\n\n'
                printf '<<<ARL-FINDINGS>>>\n'
                printf 'FINDING severity=P1 location=a.txt:1 | Returns success on a failed lookup\n'
                printf 'VERDICT CHANGES_REQUIRED\n'
                printf '<<<ARL-END>>>\n'
                ;;
        esac
        ;;
    approve-with-critical)
        # A reviewer that contradicts itself: the gate must recompute and block.
        printf 'Looks fine overall.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVE\n'
        printf '<<<ARL-END>>>\n'
        ;;
    critical-nonactionable)
        printf 'A worry I cannot pin down.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=no file=- | General unease about the design\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    malformed)
        printf 'I reviewed it and it seems fine to me.\n'
        ;;
    # Every mode below emits a block the contract does not allow, alongside the reviewer's
    # own APPROVED. Each one used to be read as "no findings" and approved the commit.
    bad-actionable)
        printf 'Looks fine to me.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=maybe file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    bad-severity)
        printf 'Looks fine to me.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=spicy actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    mangled-finding)
        # One stray colon, and the whole finding stopped counting.
        printf 'Looks fine to me.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING: severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    stray-end)
        # The real findings sit above a stray end marker, so the sed range never saw them.
        printf 'Serious problems below.\n\n'
        printf '<<<ARL-END>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    two-blocks)
        printf 'Two blocks, one of them empty.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf '<<<ARL-END>>>\n'
        printf 'On reflection:\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    inline-start-marker)
        # The marker is real, but it is buried in a sentence rather than on its own line.
        printf 'Serious problems below.\n\n'
        printf 'As requested: <<<ARL-FINDINGS>>> here it comes\n'
        printf 'FINDING severity=critical actionable=yes file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    suffixed-end-marker)
        printf 'Serious problems below.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>> -- end of block\n'
        ;;
    nul-byte)
        # A NUL inside `actionable=no` is invisible in a terminal, and command substitution
        # deletes it -- so the shell used to validate a repaired copy of this line.
        printf 'Looks fine to me.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=critical actionable=n\000o file=a.txt:7 | Nil deref when the token is absent\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    two-verdicts)
        printf 'Undecided.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    chatty-block)
        printf 'Prose, then a block with commentary in it.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'Nothing worth reporting, honestly.\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    no-verdict)
        printf 'Review body.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=low actionable=no file=a.txt:1 | Something\n'
        printf '<<<ARL-END>>>\n'
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
        root="${ARL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/adversarial-review-loop}"
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
        printf '<<<ARL-FINDINGS>>>\n'
        for i in $(seq 1 "${ARL_FAKE_COUNT:-6}"); do
            printf 'FINDING severity=medium actionable=yes file=a.txt:%s | Finding number %s\n' "$i" "$i"
        done
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    big-prose)
        # Prose far past max_reason_bytes; every FINDING line must still arrive.
        for i in $(seq 1 2000); do
            printf 'Prose line %s: padding that exists only to overflow the prose budget.\n' "$i"
        done
        printf '\n<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | Must survive truncation\n'
        printf 'FINDING severity=critical actionable=yes file=b.txt:2 | Must also survive truncation\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    echo-bundle)
        printf 'Bundle contents:\n'
        ls -1 "$bundle"
        printf '\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>\n'
        ;;
    echo-context)
        # Dumps the context/ attachments the real path would pass with -f, so the selftest
        # can assert round 2 is actually shown round 1's findings. Still a blocking verdict
        # so the denial reason carries the dump back.
        printf 'Prior rounds seen by this reviewer:\n'
        if [ -n "${ARL_CONTEXT_FILES:-}" ]; then
            printf '%s\n' "$ARL_CONTEXT_FILES" | while IFS= read -r f; do
                [ -n "$f" ] && cat "$f"
            done
        else
            printf '(no context files were passed)\n'
        fi
        printf '\n<<<ARL-FINDINGS>>>\n'
        printf 'FINDING severity=high actionable=yes file=a.txt:1 | round two is still unhappy\n'
        printf 'VERDICT CHANGES_REQUIRED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    clarify-mutate)
        # Simulates a resume/accept/stop landing while the reviewer answers: bumps
        # activation_generation in state.json so clarify's post-invoke fingerprint check
        # fires and the reply is discarded rather than printed.
        root="${ARL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/adversarial-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.activation_generation = ((.activation_generation // 0) + 1)' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Clarification, but the activation moved underneath it.\n'
        ;;
    approve-superseded)
        # Simulates a concurrent review of the same label finishing a *newer*, blocking round
        # while this one is still deciding, then returns APPROVED anyway. Touches no
        # hooks.Activation field, so the caller's fingerprint check cannot see it -- only the
        # "is this still the newest verdict?" check inside the approval transaction can.
        # seq 999 so it is unambiguously newer than anything this activation allocates.
        root="${ARL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/adversarial-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.round_history += [{
                "seq": 999,
                "label": ("phase" + (.phase | tostring)),
                "phase": .phase, "generation": .activation_generation,
                "round": ((.round_history | length) + 1),
                "verdict": "CHANGES_REQUIRED", "tree": "x", "base": "y",
                "at": 0, "findings": [], "supersedes": []
            }]' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Nothing blocking, from where I sat.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    approve-superseded-final)
        # As approve-superseded, but for the `final` label -- the completion path writes
        # COMPLETE, which disarms the gate permanently, so a stale approval landing over a
        # newer final verdict there cannot be corrected by a later round.
        root="${ARL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/adversarial-review-loop}"
        sf=$(find "$root" -name state.json 2>/dev/null | head -n1)
        if [ -n "$sf" ]; then
            tmp=$(mktemp)
            jq '.round_history += [{
                "seq": 999, "label": "final",
                "phase": .phase, "generation": .activation_generation,
                "round": ((.round_history | length) + 1),
                "verdict": "CHANGES_REQUIRED", "tree": "x", "base": "y",
                "at": 0, "findings": [], "supersedes": []
            }]' "$sf" >"$tmp" && mv "$tmp" "$sf"
        fi
        printf 'Nothing blocking, from where I sat.\n\n'
        printf '<<<ARL-FINDINGS>>>\n'
        printf 'VERDICT APPROVED\n'
        printf '<<<ARL-END>>>\n'
        ;;
    clarify-supersede)
        # Simulates a concurrent reviewer.execute finishing a newer round while this clarify
        # runs: appends a round_history entry with the next seq, without touching any
        # hooks.Activation field -- so clarify's post-invoke "still the latest round?" check
        # fires (not the fingerprint check).
        root="${ARL_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/adversarial-review-loop}"
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
        if [ -n "${ARL_QUESTION_FILE:-}" ] && [ -f "$ARL_QUESTION_FILE" ]; then
            printf 'question seen:\n'
            cat "$ARL_QUESTION_FILE"
        else
            printf '(no question file was passed)\n'
        fi
        ;;
    *)
        printf 'unknown ARL_FAKE_MODE: %s\n' "$mode" >&2
        exit 2
        ;;
esac
