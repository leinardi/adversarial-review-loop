---
name: config
description: Read or write the opencode-review-loop configuration — every key's resolved value and which layer set it, or set/unset one key in the user config or (with --repo) the repository config.
argument-hint: "[<key> <value> [--repo] [--force] | <key> --unset [--repo]]"
disable-model-invocation: true
user-invocable: true
---

# opencode-review-loop configuration

!`${CLAUDE_PLUGIN_ROOT}/scripts/ocrl.sh config $ARGUMENTS`

This command is unrelated to any armed activation — it never registers the review gate, and it does not require one to be running. It only ever touches the user's own config file (`~/.config/opencode-review-loop/config.json` by default) or, with `--repo`, the repository's own `.opencode-review-loop.json`.

- **No arguments**: prints every supported key, its currently resolved value, and which layer set it — `default`, `user`, `repo`, `activation` (an override an armed session was given via `--model`/`--variant`), or `env`.
- **`<key> <value>`**: writes `key` into the user config. Add `--repo` to write the repository config instead. If the value itself contains a token that looks like a flag (a `verify_cmd` such as `pytest --maxfail=1`), put `--` before it: `config verify_cmd --repo -- pytest --maxfail=1`.
- **`<key> --unset`**: removes `key` from that layer (add `--repo` for the repository config).
- Setting `model` additionally checks the name against `opencode models`. An unreachable reviewer only warns — arming will refuse on its own if it is still unreachable then — but a name the reviewer actively rejects is refused; pass `--force` to set it anyway.

Report the command's own output to the user; do not reformat or summarise away a warning or a refusal.
