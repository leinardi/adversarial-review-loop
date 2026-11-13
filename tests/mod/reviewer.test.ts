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
import { answerOf, classify, deliver, normalize, permit, REVIEWER_TOOLS, reviewerDecision, runReviewer, within } from '../../hooks/reviewer'
import { fakeEngine, PLUGIN, start, tool, world } from './world'

const DONE = { answer: 'VERDICT APPROVED', reason: 'answer', durationMs: 10 }

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
  for (const name of ['set_phases', 'add_root', 'review', 'clarify']) {
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

const TASK = { prompt: 'Review it.', description: 'Review phase1, round 1', model: 'haiku', timeoutSec: 900, dirs: DIRS }

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

test('handing the report back is allowed: some sessions deliver a subagent answer that way', () => {
  expect(permit(DIRS, { tool: 'SubagentHandback', input: {} })).toEqual(expect.objectContaining({ decision: 'allow' }))
})
