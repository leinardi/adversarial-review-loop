# The argument channel

The long form of the `$ARGUMENTS` invariants in [`AGENTS.md`](../../AGENTS.md): why every skill that takes an argument passes it through a quoted here-document.

Claude Code substitutes `$ARGUMENTS` into a skill body **textually**. The only transform it applies to the value neutralises `!` shell-exec markers; nothing escapes it for the shell, so whatever the user typed becomes shell source. The skills used to spell this `--args "$ARGUMENTS"`, and a plan path of `x"; id; echo "` therefore executed `id`. That was confirmed, not theoretical — and the same defect broke an ordinary `/adversarial-review-loop:accept` whose reason merely contained a quote and a `;`.

No *single-line* body fixes it: double quotes are broken by `"`, single quotes by `'`, and the substitution precedes any shell. What does fix it is a here-document with a quoted delimiter — the one shell construct whose body is never parsed — which needs more than one line, so each of these skills now uses a ` ```! ` fenced block:

```text
${CLAUDE_PLUGIN_ROOT}/scripts/arl.sh accept --reason-stdin <<'ARL-ARGUMENTS-EOF'
$ARGUMENTS
ARL-ARGUMENTS-EOF
```

Claude Code hands a fenced block to one shell as one script (its extractor captures the whole block), which is what makes the here-document possible; `tests/unit/test_skill_arguments.py` pins that parse and runs the result through a real `bash` with hostile arguments. The receiving end is `util.stdin_argument`, reached by `--args-stdin` (`arm`, `resume`, `pause`, `config`) and `--reason-stdin` (`accept`). **The argv spellings `--args` and `--reason` stay**: an install serves `skills/*/SKILL.md` from the version-pinned cache (see "The install cache" in [`AGENTS.md`](../../AGENTS.md)), so an older cached body will keep passing arguments on argv long after this repository stopped.

Two things this does *not* change. `disable-model-invocation: true` on the skill is still the containment for everything the here-document does not cover — the block still expands `${CLAUDE_PLUGIN_ROOT}`, and a body edited back to one line loses the protection silently. **Do not remove that flag**, and do not add an interpolated argument without re-reading this. And `commands/arm.py`'s character-set check still runs after the shell, so it protects the loop's state, not the machine.
