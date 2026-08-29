# How it works (no engineering background required)

## The problem

Coding agents like Claude are good at implementing a plan, and bad at knowing when they've
gotten it subtly wrong. Left alone, an agent will confidently save (commit) work that looks
right and isn't — a bug it didn't notice, a shortcut that breaks a security assumption, a
piece of the plan it quietly skipped. By the time a human looks at it, the wrong version is
already the one everyone is building on top of.

The obvious fix — "ask the agent to review its own work" — doesn't hold up, for the same
reason it doesn't work for a person: the same blind spots that produced the mistake are
usually what stops you from seeing it as a mistake.

## The fix: a second, independent opinion, with a lock on the door

This plugin puts a *different* AI reviewer between "Claude thinks this phase is done" and
"this phase is permanently part of the project." It's not a suggestion box. It's a lock:
Claude physically cannot save the work until the reviewer has looked at exactly what's
about to be saved and said yes.

Think of it like a strict editor at a newspaper who has to sign off on a story before it
goes to print. The reporter (Claude) can write and rewrite freely. But the presses
(`git commit`) don't run until the editor (OpenCode, a separate AI, invoked fresh with no
memory of the conversation) has read the exact copy and approved it. If the editor finds a
problem, the story is bounced back — not published, not partially published — and the
reporter tries again.

## What actually happens, step by step

1. **You hand it a plan.** A markdown file describing what should get built. You start it
   with a command; nothing runs on its own.
2. **The plan gets frozen and split into phases.** Claude reads it and proposes a sequence
   of milestones — "phase 1: add the login form," "phase 2: wire it to the API," and so on.
   Once you've effectively agreed to that split (by letting it freeze), it can't quietly
   change later to match whatever Claude happened to build.
3. **Claude implements a phase, then tries to commit it.** The moment it runs
   `git commit`, the plugin intercepts that exact command *before* it runs.
4. **A snapshot is taken of everything that would be committed** — not a description of it,
   the actual content — and handed to the second AI reviewer with no other context except
   the plan and the diff.
5. **The reviewer reads it like an adversarial code reviewer would**, looking for bugs,
   security issues, corners cut, and places the work doesn't actually match what the plan
   asked for. It cannot run anything or change anything — it can only read and report.
6. **If it's clean, the commit is allowed to happen and the next phase starts.** If it
   finds a real problem, the commit is blocked and Claude is told exactly what's wrong, with
   the same specificity a strict human reviewer would give. Claude fixes it and tries again.
   This can repeat as many times as it takes. From the second attempt on, the reviewer is
   told exactly which files changed since it last looked, and a *new* medium-level complaint
   about a file that didn't change — the kind of thing it could have said the first time —
   is written down and shown to you but no longer holds the commit up on its own; anything
   serious still does, wherever it is, and so does anything the reviewer already raised
   earlier.
7. **Once every phase is committed, the tool considers itself done and steps out of the
   way.** You can also ask for one more review first — looking at the *entire* change from
   start to finish, the way a final sign-off would, rather than phase by phase. That one is
   off by default because on a long plan it becomes too big to be read properly; turn it on
   with `final_review true` if you want it every time, or run
   `/opencode-review-loop:finish` to get it for a single plan. It has to be *before* the
   tool steps out, though — once it's done, that review can't be run after the fact.

## Some things worth knowing

- **You can pause it.** Tell it to stop after phase 2 of 5, and it will — the loop won't
  push you into "just one more phase" on its own.
- **You can pick it back up later**, even days later, in a brand-new conversation, without
  losing any of the approvals already earned or restarting from scratch.
- **You can revise the plan partway through** if you realize phase 3 needs to change —
  but only for phases that haven't started yet. Whatever's already been built and approved
  stays exactly as it was reviewed.
- **While a phase is actively being worked on, it only blocks the commit.** Editing files,
  running tests, thinking out loud — none of that is touched; it stands between "finished
  draft" and "permanent record," not in front of the drafting itself. Outside that normal
  working state, it locks down harder: before phases are even agreed on, right after a
  pause, or if something's gone wrong and needs a person, *every* change is blocked, not
  just a commit — because there's nothing current yet for a review to check that change
  against.
- **It assumes the agent is trying to cooperate, not trying to cheat.** This is a
  quality gate for an agent that's making an honest attempt and might get something wrong —
  not a jail cell for one that's actively trying to escape it. See
  [security.md](security.md) if you need to know exactly where that line is.

## Why bother with all this?

Because "I'll just read the diff at the end" doesn't scale, and "trust the agent" isn't a
policy — it's the absence of one. This turns code review from something that happens
*after* the fact, when unwinding a bad decision is expensive, into something that has to
happen *before* anything becomes permanent, when fixing it just means trying again.
