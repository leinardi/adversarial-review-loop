# Working efficiently

Every message you send re-reads your entire context — the attachments, the repository files you have opened, the whole conversation so far. Ten tool calls sent one per message therefore cost roughly ten times what the same ten calls cost sent together. A review that takes fifty turns costs several times one that takes fifteen and finds the same defects.

- **Put every independent tool call in the same message.** Decide what you need before you call, then issue the whole batch at once: all the type definitions together, all the call sites together, all the test files together. Only chain a call on a previous result when it genuinely depends on that result. Two greps for two different symbols are independent. A grep and the read of the file it just located are not.
- **`Grep` to locate, `Read` to confirm.** Use `-n` and `-C` so the grep result itself carries enough context, then `Read` with `offset`/`limit` for the region it points at. Do not read a large file whole to find one symbol.
- **Never re-open a file already in this session.** It is still in your context.
- Open `AGENTS.md`, `CLAUDE.md` or `README.md` when a finding turns on a convention they state — not as a routine checklist.

Thoroughness is judged on the findings you produce, not on the number of calls you make.
