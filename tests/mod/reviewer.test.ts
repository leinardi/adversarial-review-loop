// This file is part of adversarial-review-loop.
//
// Copyright (c) 2026 Roberto Leinardi
//
// adversarial-review-loop is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// adversarial-review-loop is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.

// The reviewer subagent: how it is defined and hidden, how its answer is waited for, how its
// end is classified, and what it may do.

import { expect, test } from 'claude-code/testing'
import { answerOf, classify, countStep, deliver, HANDBACK_REFUSALS, isModelOf, normalize, noteToolResult, permit, resetCheckedModels, REVIEWER_TOOLS, reviewerDecision, runHandback, runReviewer, within } from '../../hooks/reviewer'
import { REVIEWER_SYSTEM_PROMPT } from '../../hooks/text'
import { apiError, fakeEngine, PLUGIN, start, tool, world } from './world'

const DONE = { answer: 'VERDICT APPROVED', reason: 'answer', durationMs: 10 }

test("a subagent's model requests are counted until its answer, which carries them", async () => {
  const { $ } = fakeEngine({ engine: {} })
  countStep('agent-steps')
  countStep('agent-steps')
  countStep('agent-other')
  deliver('agent-steps', DONE)
  expect(await answerOf($, 'agent-steps', 900)).toEqual({ ...DONE, requests: 2 })
  // Delivered, the count is gone: a later run of the same id starts from nothing.
  deliver('agent-steps', DONE)
  expect(await answerOf($, 'agent-steps', 900)).toEqual(DONE)
  deliver('agent-other', DONE)
  expect(await answerOf($, 'agent-other', 900)).toEqual({ ...DONE, requests: 1 })
})

// --------------------------------------------------------------------------
// Waiting for the answer
// --------------------------------------------------------------------------

test('the answer delivered by turn.complete ends the wait and the clock', async () => {
  const { $, w } = fakeEngine({ engine: {} })
  const waiting = answerOf($, 'agent-1', 900)
  deliver('agent-2', { ...DONE, answer: 'someone else' })
  deliver('agent-1', DONE)
  expect(await waiting).toEqual(DONE)
  expect(w.expired).toBe(0)
})

test('an answer that came before the wait began is not lost', async () => {
  const { $ } = fakeEngine({ engine: {} })
  deliver('agent-early', DONE)
  expect(await answerOf($, 'agent-early', 900)).toEqual(DONE)
})

test('a reviewer that does not answer before its clock runs out times out', async () => {
  const { $, w } = fakeEngine({ engine: {} })
  expect(await answerOf($, 'agent-silent', 1)).toBeUndefined()
  expect(w.expired).toBe(1)
  // Its late answer then waits for nobody and changes nothing.
  deliver('agent-silent', DONE)
})

const ENDINGS = [
  [undefined, { ok: false, kind: 'transient' }],
  [{ ...DONE, reason: 'error' }, { ok: false, kind: 'transient' }],
  [{ ...DONE, reason: 'refusal' }, { ok: false, kind: 'operational' }],
  [{ ...DONE, reason: 'aborted' }, { ok: false, kind: 'operational' }],
  [DONE, { ok: true, answer: 'VERDICT APPROVED' }],
] as const

for (const [completion, expected] of ENDINGS) {
  test(`a run that ended ${completion?.reason ?? 'unanswered'} is ${expected.ok ? 'an answer' : `a ${expected.kind} failure`}`, () => {
    expect(classify(completion, 60)).toEqual(expect.objectContaining(expected))
  })
}

test('only a run that answered carries an answer: an API error with text is still a failure', () => {
  const ran = classify({ answer: 'VERDICT APPROVED', reason: 'error', durationMs: 1 }, 60)
  expect(ran.ok).toBe(false)
})

// --------------------------------------------------------------------------
// What the reviewer may do
// --------------------------------------------------------------------------

const DIRS = ['/work/.arl/reviews/r1-001/bundle', '/work', '/elsewhere/web']

test('a read-only command is allowed, any other refused', () => {
  expect(permit(DIRS, { tool: 'Bash', input: { command: 'git show HEAD' }, isReadOnly: true })).toEqual(expect.objectContaining({ decision: 'allow' }))
  expect(permit(DIRS, { tool: 'Bash', input: { command: 'rm -rf src' } })).toEqual(expect.objectContaining({ decision: 'deny' }))
})

// In live runs every chained command was refused as not read-only, while the prompt offered it.
test('the reviewer is told which commands are refused, and never told to chain them', () => {
  expect(REVIEWER_SYSTEM_PROMPT).toContain('one plain command per call')
  expect(REVIEWER_SYSTEM_PROMPT).toContain("`git -C <the root's absolute path> <subcommand> ...`")
  expect(REVIEWER_SYSTEM_PROMPT).not.toContain('several commands in one Bash call (')
  const refusal = permit(DIRS, { tool: 'Bash', input: { command: 'cd /elsewhere/web && git log' } })
  expect(refusal).toEqual(expect.objectContaining({ decision: 'deny', reason: expect.stringContaining('git -C <root>') }))
})

// Under Code Mode a script's `$\`git log\`.cwd(root)` arrives as `cd '<root>' && git log` (tests/STEP0.md),
// which the session does not judge read-only.
test("a script's git read with .cwd(<root>) is allowed, and nothing else after a cd", () => {
  for (const command of [
    "cd '/elsewhere/web' && git log --no-ext-diff --no-textconv -1 --oneline",
    "cd '/work/src' && git show --no-ext-diff --no-textconv HEAD:a.ts",
    "cd '/work/.arl/reviews/r1-001/bundle' && git diff --no-ext-diff --no-textconv --stat",
    // A -C after the subcommand is grep's context, not a directory.
    "cd '/work' && git grep -n -C 2 needle",
  ]) {
    expect(permit(DIRS, { tool: 'Bash', input: { command } })).toEqual(expect.objectContaining({ decision: 'allow' }))
  }
  for (const command of [
    "cd '/etc' && git log",
    "cd '/elsewhere/web' && git commit -m x",
    "cd '/elsewhere/web' && git -C /etc log --no-ext-diff --no-textconv",
    "cd '/elsewhere/web' && git -C /elsewhere/web log --no-ext-diff --no-textconv",
    "cd '/elsewhere/web' && git diff HEAD~1",
    "cd '/elsewhere/web' && git log | head",
    "cd '/elsewhere/web' && cd '/work' && git log",
    "cd '/elsewhere/web' && ls",
    "cd 'web' && git log",
    'cd /elsewhere/web && git log',
    "cd '/elsewhere/web' && git -c core.pager=x log",
    "cd '/elsewhere/web'&&git log",
  ]) {
    expect([command, permit(DIRS, { tool: 'Bash', input: { command } })]).toEqual([command, expect.objectContaining({ decision: 'deny' })])
  }
  expect(REVIEWER_SYSTEM_PROMPT).toContain('If your session runs tool calls through scripts (a REPL tool), the same rules hold for every call in a script')
  expect(REVIEWER_SYSTEM_PROMPT).toContain(".cwd(<the root's absolute path>)")
})

// The session refused every `git -C`, so a reviewer could not check a tag or a commit of another
// root. One plain reading git call in a root is the plugin's to allow.
test('a plain reading git call in a root is allowed', () => {
  for (const command of [
    'git -C /elsewhere/web log --no-ext-diff --no-textconv --oneline -3 v1.4.0',
    "git -C /elsewhere/web rev-parse 'v1.4.0^{commit}'",
    'git -C /elsewhere/web show --no-ext-diff --no-textconv v1.4.0:crates/docker-client/Cargo.toml',
    'git -C "/elsewhere/web" ls-tree -r v1.4.0 --name-only',
    "git -C /elsewhere/web tag -l 'v1.4*'",
    'git --no-pager -C /work diff --no-ext-diff --no-textconv HEAD~1 -- src',
    'git log --no-ext-diff --no-textconv --format=%h%x20%s -5',
    'git -C /work blame --no-textconv -L 1,20 src/a.ts',
    'git -C /work grep -n -C 2 needle',
    'git -C /elsewhere/web/sub/../ cat-file -t 6ad52461',
  ]) {
    expect({ command, decision: permit(DIRS, { tool: 'Bash', input: { command } }) }).toEqual({ command, decision: expect.objectContaining({ decision: 'allow' }) })
  }
})

test('a git call that writes, leaves the roots or is more than one plain call is refused', () => {
  for (const command of [
    'git -C /outside log',
    'git -C relative/path log',
    'git -C /elsewhere/web/../../etc log',
    'git -C /elsewhere/web commit -m x',
    'git -C /elsewhere/web tag v9.9.9',
    'git -C /elsewhere/web tag -l -d v1.4.0',
    'git -C /elsewhere/web status',
    'git -C /elsewhere/web checkout main',
    'git -C /elsewhere/web diff --output=/tmp/x',
    'git -C /elsewhere/web diff --no-index /etc/passwd /work/a',
    'git -C /elsewhere/web grep -O x',
    'git -c core.pager=sh -C /elsewhere/web log',
    'git --git-dir=/elsewhere/web/.git log',
    'git -C /elsewhere/web log | head',
    'git -C /elsewhere/web log; rm -rf /work',
    'git -C /elsewhere/web log $(touch /work/x)',
    'git -C /elsewhere/web log > /work/out',
    "git -C /elsewhere/web log 'unclosed",
    'gitk -C /elsewhere/web',
    // Each can start another program: a diff driver, a conversion filter, gpg.
    'git -C /elsewhere/web diff HEAD~1',
    'git -C /elsewhere/web show HEAD',
    'git -C /elsewhere/web log -p',
    'git -C /elsewhere/web diff --no-ext-diff HEAD~1',
    'git -C /elsewhere/web diff --no-textconv HEAD~1',
    'git -C /elsewhere/web diff -- --no-ext-diff --no-textconv',
    'git -C /elsewhere/web diff --no-ext-diff --no-textconv --ext-diff',
    'git -C /elsewhere/web show --no-ext-diff --no-textconv --textconv HEAD',
    'git -C /elsewhere/web log --no-ext-diff --no-textconv --show-signature',
    // A required option taken as another option's value is not that option.
    'git -C /elsewhere/web log --grep --no-textconv --no-ext-diff -p',
    'git -C /elsewhere/web log -p --grep --no-textconv --no-ext-diff',
    'git -C /elsewhere/web diff HEAD~1 --no-ext-diff --no-textconv',
    "cd '/elsewhere/web' && git log --author --no-ext-diff --no-textconv -p",
    'git -C /elsewhere/web blame src/a.ts',
    'git -C /elsewhere/web grep --textconv x',
    'git -C /elsewhere/web cat-file --textconv HEAD:a.ts',
    'git -C /elsewhere/web cat-file --filters HEAD:a.ts',
  ]) {
    expect({ command, decision: permit(DIRS, { tool: 'Bash', input: { command } }) }).toEqual({ command, decision: expect.objectContaining({ decision: 'deny' }) })
  }
})

test('a read inside the bundle or a root is allowed; outside, the session decides', () => {
  expect(permit(DIRS, { tool: 'Read', input: { file_path: '/elsewhere/web/src/a.ts' } })).toEqual(expect.objectContaining({ decision: 'allow' }))
  expect(permit(DIRS, { tool: 'Grep', input: { pattern: 'x', path: '/work/.arl/reviews/r1-001/bundle' } })).toEqual(expect.objectContaining({ decision: 'allow' }))
  expect(permit(DIRS, { tool: 'Read', input: { file_path: '/home/me/.ssh/id_rsa' } })).toBe('theirs')
  expect(permit(DIRS, { tool: 'Read', input: { file_path: '/work/../home/me/.ssh/id_rsa' } })).toBe('theirs')
  expect(permit(DIRS, { tool: 'Read', input: { file_path: '/workshop/x' } })).toBe('theirs')
})

test('a tool that writes is refused outright', () => {
  for (const name of ['Write', 'Edit', 'NotebookEdit', 'WebFetch']) {
    expect(permit(DIRS, { tool: name, input: { file_path: '/work/a' } })).toEqual(expect.objectContaining({ decision: 'deny' }))
  }
})

test('paths are compared segment by segment, after resolving dots', () => {
  expect(normalize('/a/./b/../c/')).toBe('/a/c')
  expect(normalize('relative/path')).toBeUndefined()
  expect(within('/a/b', '/a')).toBe(true)
  expect(within('/ab', '/a')).toBe(false)
  expect(within('/a/../b', '/a')).toBe(false)
  expect(within('src/a', '/')).toBe(false)
})

// --------------------------------------------------------------------------
// Registration, as the session sees it
// --------------------------------------------------------------------------

test('the reviewer is told to work in few messages, within a budget: its cost is its context times its messages', async ($, on) => {
  const w = world(on, { engine: {} })
  await start($)
  const prompt = String((w.registered.agents[0] as { prompt?: string } | undefined)?.prompt ?? '')
  expect(prompt).toContain('Work in few messages.')
  expect(prompt).toContain('read `range.txt` and every other bundle file your task message says to read in full, together')
  expect(prompt).toContain('If you are past 12, stop investigating outside the bundle')
  // A read cut short is finished: the budget never excuses leaving part of the diff unread.
  expect(prompt).toContain('read on from the line it stopped at')
  expect(prompt).toContain('no budget below excuses leaving part of it unread')
  expect(prompt).not.toContain('never re-read a file')
  // What to read in full is the task message's to say: a later round reads the increment.
  expect(prompt).not.toContain('whole diff')
  expect(prompt).toContain('once you have read what your task message says to read in full')
  // Still a reviewer that only reads.
  expect(prompt).toContain('You only read.')
})

test('the reviewer is registered read-only with no CLAUDE.md, and hidden from Claude', async ($, on) => {
  const w = world(on, { engine: {} })
  await start($)

  expect(w.registered.agents).toEqual([expect.objectContaining({ name: 'reviewer', tools: REVIEWER_TOOLS, omitClaudeMd: true })])
  const offered = await $.agent.offer({ agent: `${PLUGIN}:reviewer`, description: '', source: 'plugin', provider: { plugin: PLUGIN, tier: 'plugin' } } as never)
  expect(offered.isOffered).toBe(false)
  const other = await $.agent.offer({ agent: 'Explore', description: '', source: 'built-in', provider: { plugin: 'engine', tier: 'core' } } as never)
  expect(other.isOffered).toBe(true)
})

test("the plugin's own tools are allowed on the main loop, whatever the session's rules", async ($, on) => {
  world(on, { engine: {} })
  on('tool.check', () => ({ decision: 'ask', reason: 'the host asks' }))
  await start($)
  for (const name of ['set_phases', 'add_root', 'review']) {
    expect((await $.tool.check({ tool: tool(name), input: {} })).decision).toBe('allow')
  }
  expect((await $.tool.check({ tool: 'WebFetch', input: {} })).decision).toBe('ask')
})

test("a subagent that is not the plugin's reviewer is left to the session", async ($, on) => {
  world(on, { engine: {} })
  on('tool.check', () => ({ decision: 'ask', reason: 'the host asks' }))
  await start($)
  const check = await $.tool.check({ tool: 'Bash', input: { command: 'rm -rf /' }, agentId: 'someone-else' } as never)
  expect(check.decision).toBe('ask')
})

test('a clock that cannot run is a failure to wait, never a hang or an approval', async () => {
  const { $ } = fakeEngine({ engine: {} })
  const broken = { ...$, process: { ...$.process, spawn: () => ({ next: () => Promise.reject(new Error('sleep: not found')), return: () => Promise.resolve() }) } }
  await expect(answerOf(broken as never, 'agent-x', 900)).rejects.toThrow('sleep: not found')
})

// --------------------------------------------------------------------------
// One run, end to end on a hand-made session
// --------------------------------------------------------------------------

const TASK = { prompt: 'Review it.', description: 'Review phase1, round 1', model: 'haiku', timeoutSec: 900, dirs: DIRS, bundle: DIRS[0] ?? '' }

test('a reviewer is spawned as the plugin type with the task, and its answer comes back', async () => {
  const { $, w } = fakeEngine({ engine: {}, agents: [DONE] })
  expect(await runReviewer($, TASK)).toEqual({ ok: true, answer: 'VERDICT APPROVED', usage: undefined, durationMs: 10 })
  expect(w.spawns).toEqual([{ prompt: 'Review it.', type: `${PLUGIN}:reviewer`, model: 'haiku' }])
})

test('while it runs, the reviewer reads the review and runs read-only commands, and nothing else', async () => {
  const seen: unknown[] = []
  const { $ } = fakeEngine({
    engine: {},
    agents: [DONE],
    whileRunning: async agentId => {
      seen.push(await reviewerDecision($, { tool: 'Bash', input: { command: 'ls' }, agentId, isReadOnly: true }))
      seen.push(await reviewerDecision($, { tool: 'Bash', input: { command: 'make' }, agentId }))
      seen.push(await reviewerDecision($, { tool: 'Read', input: { file_path: '/work/a.py' }, agentId }))
      seen.push(await reviewerDecision($, { tool: 'Bash', input: { command: 'make' }, agentId: 'not-a-reviewer' }))
    },
  })
  await runReviewer($, TASK)
  expect(seen).toEqual([
    expect.objectContaining({ decision: 'allow' }),
    expect.objectContaining({ decision: 'deny' }),
    expect.objectContaining({ decision: 'allow' }),
    undefined,
  ])
})

test('a reviewer whose review is over, a timed-out one included, may do nothing at all', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: [DONE, undefined] })
  await runReviewer($, TASK)
  await runReviewer($, { ...TASK, timeoutSec: 1 })
  for (const agentId of ['agent-1', 'agent-2']) {
    expect(await reviewerDecision($, { tool: 'Bash', input: { command: 'ls' }, agentId, isReadOnly: true })).toEqual(expect.objectContaining({ decision: 'deny' }))
    expect(await reviewerDecision($, { tool: 'Read', input: { file_path: '/work/a.py' }, agentId })).toEqual(expect.objectContaining({ decision: 'deny' }))
  }
})

test('an interrupted review is neither a timeout nor an answer: nothing is classified', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: [undefined] })
  const aborted = new AbortController()
  aborted.abort()
  await expect(runReviewer($, { ...TASK, timeoutSec: 1, signal: aborted.signal })).rejects.toThrow('interrupted')
})

test('a spawn that is refused or fails is an operational failure', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: ['deny', 'throw'] })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, kind: 'operational', error: expect.stringContaining('no agents here') }))
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, kind: 'operational', error: expect.stringContaining('the session is ending') }))
})

test('a reviewer that never answers within its timeout is a transient failure', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: [undefined] })
  expect(await runReviewer($, { ...TASK, timeoutSec: 1 })).toEqual(expect.objectContaining({ ok: false, kind: 'transient' }))
})

test("an interrupt that ends the reviewer's own run first is still an interrupt, not a failure", async () => {
  const aborted = new AbortController()
  const { $ } = fakeEngine({
    engine: {},
    agents: [{ ...DONE, reason: 'aborted' }],
    whileRunning: async () => {
      aborted.abort()
    },
  })
  await expect(runReviewer($, { ...TASK, signal: aborted.signal })).rejects.toThrow('interrupted')
})

test("handing the report back is left to the session: auto mode refuses a hook's allow of it", () => {
  expect(permit(DIRS, { tool: 'SubagentHandback', input: {} })).toBe('theirs')
  expect(permit(DIRS, { tool: 'SubagentHandback', input: { message: 'Prose.\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>' }, agentId: 'whole' })).toBe('theirs')
})

// In live runs a reviewer handed back its prose alone, and a review that had approved became a
// failed round. A hook may refuse a handback, so the reviewer is sent back to hand in the block.
test('a handback without the findings block is sent back, a bounded number of times', () => {
  const call = { tool: 'SubagentHandback', input: { message: 'Both findings are fixed.' }, agentId: 'prose-only' }
  for (let refusal = 0; refusal < HANDBACK_REFUSALS; refusal++) {
    expect(permit(DIRS, call)).toEqual(expect.objectContaining({ decision: 'deny', reason: expect.stringContaining('<<<ARL-FINDINGS>>>') }))
  }
  // Past the bound the session decides, and the round fails on the missing block.
  expect(permit(DIRS, call)).toBe('theirs')
  // Another reviewer has its own count.
  expect(permit(DIRS, { ...call, agentId: 'another' })).toEqual(expect.objectContaining({ decision: 'deny' }))
})

/** Let pending promise callbacks run (the model check before a spawn). */
const settleSoon = async (): Promise<void> => {
  for (let turn = 0; turn < 20; turn++) await Promise.resolve()
}

const REPORT = '<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>'
const NOTE = { ...DONE, answer: 'The report was handed back.' }

test("the report a reviewer handed back is its answer, not the note it ends on", async () => {
  const { $ } = fakeEngine({
    engine: {},
    agents: [NOTE],
    whileRunning: async agentId => {
      await runHandback(agentId, REPORT, async () => ({ result: 'delivered', isError: false }))
    },
  })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true, answer: REPORT }))
})

for (const [why, outcome] of [
  ['the session refused it', { deny: 'Only the auto-mode classifier can allow SubagentHandback' }],
  ['it failed', { result: 'no', isError: true }],
] as const) {
  test(`a handback is not the answer when ${why}: it delivered nothing`, async () => {
    const { $ } = fakeEngine({
      engine: {},
      agents: [NOTE],
      whileRunning: async agentId => {
        await runHandback(agentId, REPORT, async () => outcome)
      },
    })
    expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true, answer: NOTE.answer }))
  })
}

test('a report handed back before the spawn resolved is still the answer, as an early completion is', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: [] })
  let spawned: (value: { agentId: string; model: string }) => void = () => undefined
  ;($.agent as { spawn: unknown }).spawn = () =>
    new Promise(resolve => {
      spawned = resolve
    })
  const ran = runReviewer($, TASK)
  await settleSoon()
  // The reviewer reads, hands back and ends while its spawn is still pending: its read counts.
  noteToolResult('agent-fast', 'Read', { file_path: `${TASK.bundle}/changes.00.diff` }, { result: { file: { content: 'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n' } } })
  await runHandback('agent-fast', REPORT, async () => ({ result: 'delivered', isError: false }))
  deliver('agent-fast', NOTE)
  spawned({ agentId: 'agent-fast', model: 'claude-haiku-5-5' })
  expect(await ran).toEqual(expect.objectContaining({ ok: true, answer: REPORT }))
})

test('a reviewer that answers APPROVED having read none of the bundle gives no verdict', async () => {
  const { $ } = fakeEngine({ engine: {}, agents: [{ answer: REPORT, reason: 'answer', durationMs: 5 }], reads: 'none' })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, kind: 'operational', error: 'the reviewer read none of the bundle' }))
})

const SHOWN = { file: { content: 'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n' } }

test('only a read of change evidence that ran counts: a failed or refused read, a listing, a search, orientation or a root does not', async () => {
  const B = TASK.bundle
  const misses: [string, unknown, { deny?: string; isError?: boolean; result?: unknown }][] = [
    ['Read', { file_path: `${B}/changes.00.diff` }, { isError: true, result: SHOWN }],
    ['Read', { file_path: `${B}/changes.00.diff` }, { deny: 'no', result: SHOWN }],
    // Ran, and printed nothing of the diff: an empty read, or one past the file's end.
    ['Read', { file_path: `${B}/changes.00.diff`, offset: 9000 }, { result: { file: { content: '' } } }],
    ['Bash', { command: `head -n 0 ${B}/changes.00.diff` }, { result: { stdout: '', stderr: '' } }],
    ['Read', { file_path: `${B}/repair.txt` }, { result: { file: { content: '  \n' } } }],
    ['Read', { file_path: `${B}/range.txt` }, { result: SHOWN }],
    ['Read', { file_path: `${B}/nested/changes.00.diff` }, { result: SHOWN }],
    ['Read', { file_path: '/work/src/a.ts' }, { result: SHOWN }],
    ['Bash', { command: `ls ${B}` }, { result: { stdout: 'changes.00.diff\nrange.txt\n' } }],
    ['Bash', { command: `grep -n x ${B}/changes.00.diff` }, { result: SHOWN }],
    ['Bash', { command: `cat ${B}/changes.00.diff > /tmp/x` }, { result: SHOWN }],
    ['Grep', { pattern: 'x', path: B }, { result: SHOWN }],
  ]
  const { $ } = fakeEngine({
    engine: {},
    agents: [{ answer: REPORT, reason: 'answer', durationMs: 5 }],
    reads: 'none',
    whileRunning: async agentId => {
      for (const [name, input, outcome] of misses) noteToolResult(agentId, name, input, outcome)
    },
  })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, error: 'the reviewer read none of the bundle' }))
})

test('a read of a diff chunk, the increment or a repair transcript that ran counts as reading the phase', async () => {
  const B = TASK.bundle
  for (const [name, input, outcome] of [
    ['Read', { file_path: `${B}/changes.03.diff` }, { result: { file: { content: '     1\tdiff --git a/x b/x\n     2\t@@ -1 +1 @@\n' } } }],
    ['Read', { file_path: `${B}/incremental.diff`, offset: 200 }, { result: { file: { content: '@@ -40,3 +40,4 @@\n+x\n' } } }],
    ['Read', { file_path: `${B}/incremental.diff` }, { result: { file: { content: '(no change since the previous round)\n' } } }],
    ['Read', { file_path: `${B}/repair.txt` }, { result: { file: { content: 'FINDING severity=high ...' } } }],
    ['Bash', { command: `sed -n 1,400p ${B}/changes.00.diff` }, { result: { stdout: 'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n' } }],
  ] as const) {
    const { $ } = fakeEngine({
      engine: {},
      agents: [{ answer: REPORT, reason: 'answer', durationMs: 5 }],
      reads: 'none',
      whileRunning: async agentId => {
        noteToolResult(agentId, name, input, outcome)
      },
    })
    expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true, answer: REPORT }))
  }
})

test("a handback by an agent that is not a running reviewer is not kept", async () => {
  const { $ } = fakeEngine({ engine: {} })
  await runHandback('agent-stranger', REPORT, async () => ({ result: 'delivered', isError: false }))
  deliver('agent-stranger', NOTE)
  expect(await answerOf($, 'agent-stranger', 900)).toEqual(NOTE)
})

// --------------------------------------------------------------------------
// The reviewer's model is checked before it may answer
// --------------------------------------------------------------------------

test("a model's first spawn in a session is checked once, and a second spawn is not checked again", async () => {
  resetCheckedModels()
  const { $, w } = fakeEngine({ engine: {}, agents: [DONE, DONE, DONE, DONE] })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true }))
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true }))
  expect(w.checked).toEqual(['haiku'])
  expect(await runReviewer($, { ...TASK, model: 'opus' })).toEqual(expect.objectContaining({ ok: true }))
  expect(w.checked).toEqual(['haiku', 'opus'])
  // A new session checks again.
  resetCheckedModels()
  await runReviewer($, TASK)
  expect(w.checked).toEqual(['haiku', 'opus', 'haiku'])
})

for (const [status, error] of [
  [404, 'model_not_found'],
  [403, 'permission_denied'],
  [401, 'authentication_failed'],
  [400, 'invalid_request'],
] as const) {
  test(`a check the API answers ${status} is a model failure, and nothing is spawned`, async () => {
    resetCheckedModels()
    const { $, w } = fakeEngine({ engine: {}, agents: [DONE], checks: [apiError(status, error)] })
    const ran = await runReviewer($, TASK)
    expect(ran).toEqual(expect.objectContaining({ ok: false, kind: 'model', error: expect.stringContaining(`haiku (${status} ${error})`) }))
    expect(w.spawns).toEqual([])
  })
}

test('a model the session refuses to send to is a model failure', async () => {
  resetCheckedModels()
  const { $, w } = fakeEngine({ engine: {}, agents: [DONE], checks: ['reject'] })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, kind: 'model', error: expect.stringContaining('allowlist') }))
  expect(w.spawns).toEqual([])
})

for (const [name, check] of [
  ['429', apiError(429, 'rate_limit')],
  ['529', apiError(529, 'overloaded')],
  ['with no response', apiError(null, 'unknown')],
  ['aborted', { isAnswered: false as const, reason: 'aborted' as const, usage: {} }],
] as const) {
  test(`a check that ends ${name} is transient and is tried again next time`, async () => {
    resetCheckedModels()
    const { $, w } = fakeEngine({ engine: {}, agents: [DONE, DONE], checks: [check] })
    expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: false, kind: 'transient' }))
    expect(w.spawns).toEqual([])
    expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true }))
    expect(w.checked).toEqual(['haiku', 'haiku'])
  })
}

test('a check that answers with no text still shows the model is there', async () => {
  resetCheckedModels()
  const { $ } = fakeEngine({ engine: {}, agents: [DONE], checks: [{ isAnswered: false, reason: 'empty-reply', usage: {} }] })
  expect(await runReviewer($, TASK)).toEqual(expect.objectContaining({ ok: true }))
})

test('a check interrupted by the person is an interrupt, not a failure', async () => {
  resetCheckedModels()
  const aborted = new AbortController()
  aborted.abort()
  const { $ } = fakeEngine({ engine: {}, agents: [DONE], checks: [{ isAnswered: false, reason: 'aborted', usage: {} }] })
  await expect(runReviewer($, { ...TASK, signal: aborted.signal })).rejects.toThrow('interrupted')
})

test('a reviewer the session started on another model is a model failure: its answer is never read, and it may do nothing', async () => {
  resetCheckedModels()
  const { $, w } = fakeEngine({ engine: {}, agents: [DONE], ranOn: ['claude-haiku-5-5'] })
  const ran = await runReviewer($, { ...TASK, model: 'opus' })
  expect(ran).toEqual({ ok: false, kind: 'model', error: 'the session started the reviewer on claude-haiku-5-5, not opus', usage: undefined, durationMs: undefined, requests: undefined })
  expect(w.spawns).toHaveLength(1)
  expect(await reviewerDecision($, { tool: 'Read', input: { file_path: '/work/a.py' }, agentId: 'agent-1' })).toEqual(expect.objectContaining({ decision: 'deny' }))
})

test('a model that is no alias is never spawned: the host would refuse it, and nothing could check it', async () => {
  resetCheckedModels()
  const { $, w } = fakeEngine({ engine: {}, agents: [DONE] })
  for (const model of ['claude-opus-5-5', '']) {
    expect(await runReviewer($, { ...TASK, model })).toEqual(expect.objectContaining({ ok: false, kind: 'model' }))
  }
  expect([w.checked, w.spawns]).toEqual([[], []])
})

test("a reported model is the alias's own only under its family's prefix", () => {
  expect(isModelOf('opus', 'claude-opus-5-5')).toBe(true)
  expect(isModelOf('sonnet', 'claude-sonnet-4-5-20250929')).toBe(true)
  expect(isModelOf('opus', 'claude-haiku-5-5')).toBe(false)
  expect(isModelOf('opus', 'opus')).toBe(false)
  expect(isModelOf('opus', '')).toBe(false)
})
