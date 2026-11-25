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

// The review flow: one round on the subagent, its repair, its failures, the external
// harness, and what Claude is told when the loop needs the user.

import type { EngineInterface } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { reviewWith, session } from '../../hooks/flow'
import { Interrupted, type Ran, resetCheckedModels, runReviewer, runSubagentCall } from '../../hooks/reviewer'
import { callTool, fakeEngine, LAUNCH, PREPARED, PROMPTS, RECORDED, reviewer, start, summary, tool, world } from './world'

declare function setTimeout(callback: () => void, ms: number): unknown

function at(isInteractive = false): void {
  session.dir = LAUNCH
  session.id = 'session-1'
  session.isInteractive = isInteractive
}

function review($: EngineInterface, input: Record<string, unknown>, run: ReturnType<typeof reviewer>['run']): Promise<string> {
  return reviewWith($, input, run)
}


const APPROVED = { ok: true as const, answer: 'All good.\nVERDICT APPROVED', usage: { input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4, model: 'haiku' }, durationMs: 1000 }

/** An approval the engine's contract parser accepts: only the evidence gate can stop it. */
const VALID_APPROVAL = 'Nothing to fix.\n<<<ARL-FINDINGS>>>\nVERDICT APPROVED\n<<<ARL-END>>>'

for (const [label, outcome, recorded] of [
  ['a read of the diff that failed', { result: 'File does not exist.', isError: true }, '--failed'],
  ['a read of the diff that ran and printed nothing', { result: { stdout: '', stderr: '' } }, '--failed'],
  ['a read of the diff that ran', { result: { file: { content: 'diff --git a/x b/x\n@@ -1 +1 @@\n' } } }, '--answer'],
] as const) {
  test(`a reviewer's valid approval after ${label} is recorded with ${recorded}`, async () => {
    at()
    const { $, w } = fakeEngine({
      engine: { 'review prepare': PREPARED, 'review record': RECORDED },
      files: PROMPTS,
      agents: [{ answer: VALID_APPROVAL, reason: 'answer', durationMs: 5 }],
      reads: 'none',
      // The read goes through the call the mod's tool.call hook makes around the session's own.
      whileRunning: async agentId => {
        await runSubagentCall(agentId, 'Read', { file_path: `${PREPARED.bundle_dir}/changes.00.diff` }, async () => outcome)
      },
    })
    await review($, {}, task => runReviewer($, task))
    const record = w.calls.find(call => call.args[1] === 'record')
    expect(record?.args[3]).toBe(recorded)
  })
}

test('a subagent\'s scripted read goes through the registered hook to the session and back unchanged', async ($, on) => {
  const w = world(on, { engine: {} })
  const shown = { result: { type: 'text', file: { content: 'diff --git a/x b/x\n' } } }
  const ran: unknown[] = []
  on('tool.call', { tool: 'Read' }, (_, e) => {
    ran.push(e)
    return shown as never
  })
  await start($)
  // As a script's inner call arrives (tests/STEP0.md): the agent's id, and a repl_ tool-use id.
  const result = await $.tool.call({ tool: 'Read', file_path: `${PREPARED.bundle_dir}/changes.00.diff`, agentId: 'agent-r', tool_use_id: 'repl_0877ffc1' } as never)
  expect(result).toEqual(expect.objectContaining(shown))
  expect(ran).toHaveLength(1)
  // Not taken for a write: no root is registered for it.
  expect(w.commands()).not.toContain('root-add')
})

test('a review with a deadline gives its reviewer only the time left, less what recording needs', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  const r = reviewer([APPROVED])
  await reviewWith($, {}, r.run, undefined, Date.now() + 120_000)
  expect(r.tasks[0]?.timeoutSec).toBeGreaterThan(100)
  expect(r.tasks[0]?.timeoutSec).toBeLessThanOrEqual(105)
})

test('a review whose deadline leaves no time for a reviewer fails as a timeout, recorded, and runs none', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': { ...RECORDED, verdict: 'OP_FAILURE', message: 'The review failed.' } }, files: PROMPTS })
  const r = reviewer([APPROVED])
  await reviewWith($, {}, r.run, undefined, Date.now() + 20_000)
  expect(r.tasks).toEqual([])
  const record = w.calls.find(call => call.args[1] === 'record')
  expect(record?.args.slice(3, 6)).toEqual(['--failed', 'transient', '--error'])
  expect(record?.args[6]).toContain('Code Mode stops a tool call at ten minutes')
})

test('an external review with a deadline passes it to the engine; one without passes none', async () => {
  at()
  const external = { ...PREPARED, harness: 'opencode' }
  const timed = fakeEngine({ engine: { 'review prepare': external, 'review run-external': RECORDED } })
  await reviewWith(timed.$, {}, reviewer([]).run, undefined, Date.now() + 300_000)
  const args = timed.w.calls.find(call => call.args[1] === 'run-external')?.args ?? []
  expect(args.slice(0, 4)).toEqual(['review', 'run-external', 'r1-1', '--deadline-sec'])
  expect(Number(args[4])).toBeGreaterThan(290)
  const plain = fakeEngine({ engine: { 'review prepare': external, 'review run-external': RECORDED } })
  await reviewWith(plain.$, {}, reviewer([]).run)
  expect(plain.w.calls.find(call => call.args[1] === 'run-external')?.args).toEqual(['review', 'run-external', 'r1-1'])
})

test('the review tool sets the eight-minute deadline only for a call from a script', async ($, on) => {
  const external = { ...PREPARED, harness: 'opencode' }
  const w = world(on, { engine: { 'review prepare': external, 'review run-external': RECORDED } })
  await start($)
  await $.tool.call({ tool: tool('review'), tool_use_id: 'repl_ced53612' } as never)
  await $.tool.call({ tool: tool('review'), tool_use_id: 'toolu_01abc' } as never)
  const runs = w.calls.filter(call => call.args[1] === 'run-external').map(call => call.args)
  expect(runs[0]?.[3]).toBe('--deadline-sec')
  expect(Number(runs[0]?.[4])).toBeGreaterThan(470)
  expect(Number(runs[0]?.[4])).toBeLessThanOrEqual(480)
  expect(runs[1]).toEqual(['review', 'run-external', 'r1-1'])
})

test('one deadline covers the model check and the spawn: a slow setup shortens the wait for the answer', async () => {
  for (const [delay, most] of [[0, 85], [3000, 82]] as const) {
    resetCheckedModels()
    const { $, w } = fakeEngine({ engine: {}, agents: [{ answer: VALID_APPROVAL, reason: 'answer', durationMs: 5 }], checkDelayMs: delay })
    await runReviewer($, {
      prompt: 'Review it.',
      description: 'Review',
      model: 'opus',
      timeoutSec: 900,
      dirs: [PREPARED.bundle_dir],
      bundle: PREPARED.bundle_dir,
      deadlineMs: Date.now() + 100_000,
      marginSec: 15,
      minSec: 30,
    })
    expect(w.clocks[0]).toBeLessThanOrEqual(most)
    expect(w.clocks[0]).toBeGreaterThan(most - 3)
  }
})

test('a setup that leaves too little of the deadline fails as out of time, never waits', async () => {
  resetCheckedModels()
  const { $, w } = fakeEngine({ engine: {}, agents: [{ answer: VALID_APPROVAL, reason: 'answer', durationMs: 5 }], checkDelayMs: 1500 })
  const ran = await runReviewer($, {
    prompt: 'Review it.',
    description: 'Review',
    model: 'opus',
    timeoutSec: 900,
    dirs: [PREPARED.bundle_dir],
    bundle: PREPARED.bundle_dir,
    deadlineMs: Date.now() + 46_000,
    marginSec: 15,
    minSec: 30,
    outOfTime: 'out of time',
  })
  expect(ran).toEqual(expect.objectContaining({ ok: false, kind: 'transient', error: 'out of time' }))
  expect(w.clocks).toEqual([])
})

test('the repair gets only what the review left of the deadline', async () => {
  at()
  const { $ } = fakeEngine({
    engine: {
      'review prepare': PREPARED,
      'review record': (call: { args: string[] }) =>
        call.args.includes('/work/.arl/reviews/r1-001/answer.md')
          ? { ok: true, status: 'repair', prompt_file: '/work/.arl/reviews/r1-001/repair-prompt.md', answer_file: '/work/.arl/reviews/r1-001/repair-answer.md' }
          : RECORDED,
    },
    files: PROMPTS,
  })
  const tasks: { timeoutSec: number; deadlineMs?: number }[] = []
  const slow = async (task: { timeoutSec: number; deadlineMs?: number }): Promise<Ran> => {
    tasks.push(task)
    await new Promise(resolve => setTimeout(() => resolve(undefined), 2100))
    return { ok: true, answer: 'no block', durationMs: 2100 }
  }
  const deadline = Date.now() + 100_000
  await reviewWith($, {}, slow as never, undefined, deadline)
  expect(tasks).toHaveLength(2)
  expect(tasks.map(task => task.deadlineMs)).toEqual([deadline, deadline])
  expect(tasks[1]!.timeoutSec).toBeLessThanOrEqual(tasks[0]!.timeoutSec - 2)
})

test('a reviewer that read none of the bundle is recorded as a failure, never its APPROVED', async () => {
  at()
  const failed = { ok: true, status: 'failed', message: 'The review failed: the reviewer read none of the bundle.', run: summary() }
  const { $, w } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': failed },
    files: PROMPTS,
    agents: [{ answer: VALID_APPROVAL, reason: 'answer', durationMs: 5 }],
    reads: 'none',
  })
  await review($, {}, task => runReviewer($, task))
  const record = w.calls.find(call => call.args[1] === 'record')
  expect(record?.args.slice(0, 7)).toEqual(['review', 'record', 'r1-1', '--failed', 'operational', '--error', 'the reviewer read none of the bundle'])
  expect(w.written.has(PREPARED.answer_file)).toBe(false)
})

test('a review runs one fresh reviewer on the prepared prompt and records its answer', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  const r = reviewer([APPROVED])

  const text = await review($, { verification: [{ command: 'make test', status: 0 }] }, r.run)

  expect(text).toBe(RECORDED.message)
  expect(r.tasks).toEqual([
    { prompt: 'Review phase 1.', description: 'Review phase1, round 1', model: 'opus', timeoutSec: 900, dirs: [PREPARED.bundle_dir, ...PREPARED.roots], bundle: PREPARED.bundle_dir },
  ])
  expect(w.written.get(PREPARED.answer_file)).toBe(APPROVED.answer)
  expect(w.commands()).toEqual(['review prepare', 'review record'])
  const [prepare, record] = w.calls
  expect(JSON.parse(prepare?.stdin ?? '')).toEqual({ verification: [{ command: 'make test', status: 0 }], dispositions: [] })
  expect(prepare?.args).toEqual(['review', 'prepare', '--session', 'session-1'])
  expect(record?.args.slice(0, 5)).toEqual(['review', 'record', 'r1-1', '--answer', PREPARED.answer_file])
  expect(JSON.parse(record?.args.at(-1) ?? '{}')).toEqual({
    input_tokens: 1,
    output_tokens: 2,
    cache_read_tokens: 3,
    cache_creation_tokens: 4,
    duration_ms: 1000,
    // The model that answered: what /arl-stats names the round by.
    model: 'haiku',
  })
})

test('the model requests a round made reach the engine, summed over its repair', async () => {
  at()
  const repair = {
    ok: true,
    status: 'repair',
    review_id: 'r1-1',
    prompt_file: '/work/.arl/reviews/r1-001/repair-prompt.md',
    bundle_dir: PREPARED.bundle_dir,
    answer_file: '/work/.arl/reviews/r1-001/repair-answer.md',
  }
  let records = 0
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': () => (++records === 1 ? repair : RECORDED) }, files: PROMPTS })
  await review($, {}, reviewer([{ ...APPROVED, answer: 'no block', requests: 7 }, { ...APPROVED, requests: 2 }]).run)
  const [first, second] = w.calls.filter(call => call.args[1] === 'record').map(call => JSON.parse(call.args.at(-1) ?? '{}'))
  expect(first.turns).toBe(7)
  expect(second.turns).toBe(9)
})

test('an answer that cannot be saved is a failure that still reports what the round cost', async () => {
  at()
  const failed = { ...RECORDED, verdict: 'OP_FAILURE', message: 'The reviewer failed.' }
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': failed }, files: PROMPTS })
  const broken = { ...$, fs: { ...$.fs, write: () => Promise.reject(new Error('disk full')) } } as typeof $
  await review(broken, {}, reviewer([{ ...APPROVED, requests: 4 }]).run)
  const last = w.calls.at(-1)?.args ?? []
  expect(last.slice(0, 5)).toEqual(['review', 'record', 'r1-1', '--failed', 'operational'])
  expect(JSON.parse(last.at(-1) ?? '{}')).toEqual(expect.objectContaining({ turns: 4, output_tokens: 2, model: 'haiku' }))
})

test('a failed round reports what it cost too', async () => {
  at()
  const failed = { ...RECORDED, verdict: 'OP_FAILURE', message: 'The reviewer failed.' }
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': failed }, files: PROMPTS })
  await review($, {}, reviewer([{ ok: false, kind: 'transient', error: 'API error', requests: 3, durationMs: 50 }]).run)
  expect(JSON.parse(w.calls.at(-1)?.args.at(-1) ?? '{}')).toEqual({ turns: 3, duration_ms: 50 })
})

test('a review with no verification sends an empty list, never nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  await review($, {}, reviewer([APPROVED]).run)
  expect(JSON.parse(w.calls[0]?.stdin ?? '')).toEqual({ verification: [], dispositions: [] })
})

test('dispositions are passed to the engine beside the verification', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  const stated = [{ id: 'r1.2', status: 'rejected', note: 'api/x.py:9 already checks it' }]
  await review($, { dispositions: stated }, reviewer([APPROVED]).run)
  expect(JSON.parse(w.calls[0]?.stdin ?? '')).toEqual({ verification: [], dispositions: stated })
})

test('the configured model and timeout reach the reviewer', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'review prepare': { ...PREPARED, reviewer_model: 'sonnet', timeout_sec: 60 }, 'review record': RECORDED }, files: PROMPTS })
  const r = reviewer([APPROVED])
  await review($, {}, r.run)
  expect([r.tasks[0]?.model, r.tasks[0]?.timeoutSec]).toEqual(['sonnet', 60])
})

test('round 2 spawns the reviewer on the late model the engine names', async () => {
  at()
  let prepares = 0
  const late = { ...PREPARED, review_id: 'r1-2', round: 2, model: 'sonnet', reviewer_model: 'sonnet' }
  const { $ } = fakeEngine({
    engine: { 'review prepare': () => (++prepares === 1 ? { ...PREPARED, reviewer_model: 'opus' } : late), 'review record': RECORDED },
    files: PROMPTS,
  })
  const r = reviewer([APPROVED, APPROVED])
  await review($, {}, r.run)
  await review($, {}, r.run)
  expect(r.tasks.map(task => [task.description, task.model])).toEqual([
    ['Review phase1, round 1', 'opus'],
    ['Review phase1, round 2', 'sonnet'],
  ])
})

test('a malformed answer gets one repair run, recorded under the same review with the usage of both', async () => {
  at()
  const repair = {
    ok: true,
    status: 'repair',
    review_id: 'r1-1',
    prompt_file: '/work/.arl/reviews/r1-001/repair-prompt.md',
    bundle_dir: PREPARED.bundle_dir,
    answer_file: '/work/.arl/reviews/r1-001/repair-answer.md',
  }
  let records = 0
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': () => (++records === 1 ? repair : RECORDED) }, files: PROMPTS })
  const r = reviewer([{ ...APPROVED, answer: 'no block' }, APPROVED])

  expect(await review($, {}, r.run)).toBe(RECORDED.message)
  expect(r.tasks.map(task => task.prompt)).toEqual(['Review phase 1.', 'Repair it.'])
  expect(w.written.get(PREPARED.answer_file)).toBe('no block')
  expect(w.written.get(repair.answer_file)).toBe(APPROVED.answer)
  const last = w.calls.at(-1)?.args ?? []
  expect(last.slice(0, 5)).toEqual(['review', 'record', 'r1-1', '--answer', repair.answer_file])
  expect(JSON.parse(last.at(-1) ?? '{}').output_tokens).toBe(4)
})

test('a reviewer that gave no answer is recorded as a failure of its kind, never an approval', async () => {
  at()
  const failed = { ...RECORDED, verdict: 'OP_FAILURE', message: 'The reviewer failed.' }
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': failed }, files: PROMPTS })

  expect(await review($, {}, reviewer([{ ok: false, kind: 'transient', error: 'timed out' }]).run)).toBe('The reviewer failed.')
  expect(w.calls.at(-1)?.args.slice(0, 7)).toEqual(['review', 'record', 'r1-1', '--failed', 'transient', '--error', 'timed out'])
  expect(w.written.size).toBe(0)
})

test('a prompt the mod cannot read is an operational failure', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': { ...RECORDED, verdict: 'OP_FAILURE' } } })
  const r = reviewer([APPROVED])
  await review($, {}, r.run)
  expect(r.tasks).toEqual([])
  expect(w.calls.at(-1)?.args.slice(3, 5)).toEqual(['--failed', 'operational'])
})

test('a refused prepare is reported with its code and runs no reviewer', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'review prepare': { ok: false, code: 'nothing-to-review', error: 'set the phases first' } } })
  const r = reviewer([APPROVED])
  expect(await review($, {}, r.run)).toContain('(nothing-to-review): set the phases first')
  expect(r.tasks).toEqual([])
})

test('a stale answer is reported as refused, not as a verdict', async () => {
  at()
  const { $ } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': { ok: false, code: 'stale-review', error: 'a root changed while the review ran' } },
    files: PROMPTS,
  })
  expect(await review($, {}, reviewer([APPROVED]).run)).toContain('(stale-review)')
})

test('a review whose changes are all ignored is approved without a reviewer', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'review prepare': { ok: true, status: 'skipped', verdict: 'APPROVED', message: 'Only ignored files changed.' } } })
  const r = reviewer([APPROVED])
  expect(await review($, {}, r.run)).toBe('Only ignored files changed.')
  expect(r.tasks).toEqual([])
})

test('the external harness is run by the engine, streamed', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': { ...PREPARED, harness: 'opencode' }, 'review run-external': RECORDED } })
  const r = reviewer([APPROVED])
  expect(await review($, {}, r.run)).toBe(RECORDED.message)
  expect(w.calls.map(call => call.args)).toEqual([
    ['review', 'prepare', '--session', 'session-1'],
    ['review', 'run-external', 'r1-1'],
  ])
  expect(r.tasks).toEqual([])
})

// --------------------------------------------------------------------------
// When the loop needs the user
// --------------------------------------------------------------------------

const STALLED = { ...RECORDED, verdict: 'CHANGES_REQUIRED', message: 'Findings.', run: summary({ waiting: { kind: 'stall', detail: 'the same finding for 5 rounds' } }) }

const MODEL_REFUSED: Ran = { ok: false, kind: 'model', error: 'the API refused the model opus (404 model_not_found)' }
const modelWait = (detail: string, switched?: { which: string; word: string }) => ({
  ...RECORDED,
  verdict: 'OP_FAILURE',
  message: 'The review failed.',
  run: summary({ waiting: { kind: 'model', detail, ...(switched ? { switch: switched } : {}) } }),
})
const MODEL_WAIT = modelWait("The reviewer's model weak (sonnet) cannot be used (/arl-model default, or /arl-model first <model>)", { which: 'all', word: 'default' })

test('a model failure is recorded as one, and headless Claude is told to hand over with /arl-model', async () => {
  at(false)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': MODEL_WAIT }, files: PROMPTS })
  const text = await review($, {}, reviewer([MODEL_REFUSED]).run)
  expect(w.calls.at(-1)?.args.slice(3, 7)).toEqual(['--failed', 'model', '--error', MODEL_REFUSED.error])
  expect(text).toContain('The run now waits for the user (model)')
  expect(text).toContain('/arl-model default')
})

test('the user choosing the default model sets it for the run and Claude calls review again', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': MODEL_WAIT, model: { ok: true, text: 'Reviewer model', overlay: { model: 'default', late_model: 'default' } } },
    files: PROMPTS,
    choices: ['Use the default model'],
  })
  const text = await review($, {}, reviewer([MODEL_REFUSED]).run)
  expect(w.asked[0]?.options).toEqual(['Use the default model', 'Accept', 'Keep going', 'Stop'])
  expect(w.calls.at(-1)?.args).toEqual(['model', 'set', 'all', 'default'])
  expect(text).toContain("switched this run's all-round reviewer to the default model: call review again")
})

test('when the default is the refused model, the user is offered the other tier, for the rounds that failed', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': PREPARED,
      'review record': modelWait("The reviewer's model strong (opus) cannot be used (/arl-model first weak, or /arl-model first <model>)", { which: 'first', word: 'weak' }),
      model: { ok: true, text: 'Reviewer model', overlay: { model: 'weak' } },
    },
    files: PROMPTS,
    choices: ['Use the weak model'],
  })
  const text = await review($, {}, reviewer([MODEL_REFUSED]).run)
  expect(w.asked[0]?.options).toEqual(['Use the weak model', 'Accept', 'Keep going', 'Stop'])
  expect(w.calls.at(-1)?.args).toEqual(['model', 'set', 'first', 'weak'])
  expect(text).toContain("switched this run's first-round reviewer to the weak model")
})

test('a model wait that offers no other model has no button for one', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': modelWait("The reviewer's model x cannot be used (/arl-model first <model>)") },
    files: PROMPTS,
    choices: ['Stop'],
  })
  await review($, {}, reviewer([MODEL_REFUSED]).run)
  expect(w.asked[0]?.options).toEqual(['Accept', 'Keep going', 'Stop'])
})

test('headless, a stall tells Claude to stop and hand over, and asks nobody', async () => {
  at(false)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED }, files: PROMPTS })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('Findings.')
  expect(text).toContain('The run now waits for the user (stall)')
  expect(text).toContain('the same finding for 5 rounds')
  expect(w.asked).toEqual([])
})

test('the user accepting a stalled phase approves it through the engine', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED, accept: { ok: true, run: summary() } }, files: PROMPTS, choices: ['Accept'] })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(w.asked[0]?.options).toEqual(['Accept', 'Keep going', 'Stop'])
  expect(w.commands().at(-1)).toBe('accept')
  expect(text).toContain('The user accepted this stage as it stands')
})

test('the user accepting a stage of a paused run tells Claude to stop after the commit, not to go on', async () => {
  // Pausing is the user's stop: "go on with the next phase" would undo it.
  at(true)
  const { $ } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED, accept: { ok: true, run: summary({ status: 'paused' }) } }, files: PROMPTS, choices: ['Accept'] })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('then stop: the user paused the run')
  expect(text).not.toContain('go on with the next phase')
})

test('the user accepting a stage under auto_compact tells Claude to end its turn, not to go on', async () => {
  // The compaction comes only at a turn's end: going on with the next phase in this turn would
  // leave it for none.
  at(true)
  const next = '\nCommit each changed git repository by its own conventions, then end your turn without starting the next stage: the loop compacts the conversation and prompts it.\n'
  const { $ } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED, accept: { ok: true, run: summary(), next } }, files: PROMPTS, choices: ['Accept'] })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('The user accepted this stage as it stands. Commit each changed git repository')
  expect(text).toContain('end your turn without starting the next stage')
  expect(text).not.toContain('go on with the next phase')
})

test('keep going on a paused run keeps it paused', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': STALLED, status: { ok: true, run: summary({ status: 'paused' }) }, resume: { ok: true, run: summary() }, pause: { ok: true, run: summary({ status: 'paused' }) } },
    files: PROMPTS,
    choices: ['Keep going'],
  })
  await review($, {}, reviewer([APPROVED]).run)
  expect(w.commands().slice(-2)).toEqual(['resume', 'pause'])
})

test('keep going resumes the run in this session', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED, resume: { ok: true, run: summary() } }, files: PROMPTS, choices: ['Keep going'] })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(w.calls.at(-1)?.args).toEqual(['resume', '--session', 'session-1'])
  expect(text).toContain('keep going')
})

test('stop ends the run', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED, stop: { ok: true, run: summary({ status: 'stopped' }) } }, files: PROMPTS, choices: ['Stop'] })
  expect(await review($, {}, reviewer([APPROVED]).run)).toContain('The user stopped the run')
  expect(w.commands().at(-1)).toBe('stop')
})

test('a dismissed question, or an answer that is no option, decides nothing', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': STALLED }, files: PROMPTS, choices: ['accept please'] })
  const r = reviewer([APPROVED, APPROVED])
  expect(await review($, {}, r.run)).toContain('The run now waits for the user (stall)')
  expect(await review($, {}, r.run)).toContain('The run now waits for the user (stall)')
  expect(w.asked.length).toBe(2)
  expect(w.commands()).not.toContain('accept')
  expect(w.commands()).not.toContain('resume')
})

test('a failure budget spent while preparing is put to the user like any other wait', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': { ok: false, code: 'bundle', error: 'git failed. The run now waits for the user (max_failures exceeded).' },
      status: { ok: true, run: summary({ waiting: { kind: 'failures', detail: '3 bundle failures in a row' } }) },
      resume: { ok: true, run: summary() },
    },
    choices: ['Keep going'],
  })
  const text = await review($, {}, reviewer([]).run)
  expect(w.asked[0]?.options).toEqual(['Accept', 'Keep going', 'Stop'])
  expect(text).toContain('keep going: call review again')
  expect(text).not.toContain('address the findings')
})

test('accepting the last stage says the run is complete', async () => {
  at(true)
  const { $ } = fakeEngine({
    engine: { 'review prepare': PREPARED, 'review record': STALLED, accept: { ok: true, run: summary({ stage: 'done', status: 'complete' }) } },
    files: PROMPTS,
    choices: ['Accept'],
  })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('The run is complete')
  expect(text).not.toContain('next phase')
})

test('an interrupted review records nothing, and retires its ticket so the loop is not held on it', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED, 'review abandon': { ok: true, abandoned: true, run: summary() } }, files: PROMPTS })
  const text = await review($, {}, async () => {
    throw new Interrupted()
  })
  expect(text).toContain('interrupted, and nothing was recorded')
  expect(w.calls.map(call => call.args)).toEqual([
    ['review', 'prepare', '--session', 'session-1'],
    ['review', 'abandon', 'r1-1'],
  ])
})

// Measured under Code Mode (tests/STEP0.md, M5): after the ten-minute cut every engine call in the
// cancelled dispatch throws, so the abandon fails as a rule there, not as an edge.
for (const [label, abandon] of [
  ['throws', () => {
    throw new Error('$.process.run(arl.sh) aborted')
  }],
  ['answers an engine failure', () => ({ ok: false, code: 'engine', error: 'the engine was stopped' })],
] as const) {
  test(`an interrupted review whose abandon ${label} still says interrupted, and claims no retired ticket`, async () => {
    at()
    const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review abandon': abandon }, files: PROMPTS })
    const text = await review($, {}, async () => {
      throw new Interrupted()
    })
    expect(text).toBe('The review was interrupted, and nothing was recorded. Call review again when the user wants it.')
    expect(text).not.toMatch(/retired|abandoned/)
    expect(w.commands()).toEqual(['review prepare', 'review abandon'])
  })
}

test('an interrupted external review is retired as well, not reported as an engine refusal', async () => {
  at()
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': { ...PREPARED, harness: 'opencode' },
      'review run-external': { ok: false, code: 'engine', error: 'the engine was stopped' },
      'review abandon': { ok: true, abandoned: true, run: summary() },
    },
  })
  const interrupted = new AbortController()
  interrupted.abort()
  const text = await reviewWith($, {}, reviewer([]).run, interrupted.signal)
  expect(text).toContain('interrupted, and nothing was recorded')
  expect(w.commands()).toEqual(['review prepare', 'review run-external', 'review abandon'])
})

test('an approval recorded just before an interrupt stands: it is reported, not called interrupted', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': { ...PREPARED, harness: 'opencode' }, 'review run-external': RECORDED } })
  const interrupted = new AbortController()
  interrupted.abort()
  expect(await reviewWith($, {}, reviewer([]).run, interrupted.signal)).toBe(RECORDED.message)
  expect(w.commands()).not.toContain('review abandon')
})

test('a record the engine refused, which spent the failure budget, is put to the user', async () => {
  at(false)
  const { $ } = fakeEngine({
    engine: {
      'review prepare': PREPARED,
      'review record': { ok: false, code: 'record-failed', error: 'the answer could not be recorded. The run now waits for the user (max_failures exceeded).' },
      status: { ok: true, run: summary({ waiting: { kind: 'failures', detail: '3 operational failures in a row' } }) },
    },
    files: PROMPTS,
  })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('(record-failed)')
  expect(text).toContain('The run now waits for the user (failures)')
})

test('a round that never answered claims no duration', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': { ...RECORDED, verdict: 'OP_FAILURE' } }, files: PROMPTS })
  await review($, {}, reviewer([{ ok: false, kind: 'transient', error: 'timed out' }]).run)
  expect(w.calls.at(-1)?.args).not.toContain('--usage')
})

test('headless, an oversized file is handed over without offering to accept it', async () => {
  at(false)
  const { $ } = fakeEngine({
    engine: {
      'review prepare': { ok: false, code: 'oversized', error: 'big.bin is 20 MB' },
      status: { ok: true, run: summary({ waiting: { kind: 'oversized', detail: 'big.bin is 20 MB' } }) },
    },
  })
  const text = await review($, {}, reviewer([]).run)
  expect(text).toContain('waits for the user (oversized)')
  expect(text).not.toContain('accept')
})

test('an oversized file cannot be accepted: the choices are to keep going or stop', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': { ok: false, code: 'oversized', error: 'big.bin is 20 MB' },
      status: { ok: true, run: summary({ waiting: { kind: 'oversized', detail: 'big.bin is 20 MB' } }) },
      resume: { ok: true, run: summary() },
    },
    choices: ['Keep going'],
  })
  const text = await review($, {}, reviewer([]).run)
  expect(w.asked[0]?.options).toEqual(['Keep going', 'Stop'])
  expect(text).toContain('remove the oversized file')
})

test('a root without a baseline is settled by the user, then the review runs', async () => {
  at(true)
  const late = summary({
    roots: [
      { alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', candidates: [] },
      { alias: 'lib', path: '/work2/lib', kind: 'git', baseline: 'unknown', candidates: [{ commit: '0123456789abcdef0123', why: 'start' }] },
    ],
  })
  let prepares = 0
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': () => (++prepares === 1 ? { ok: false, code: 'baseline-unknown', error: 'baseline unknown for lib' } : PREPARED),
      status: { ok: true, run: late },
      'root-baseline': { ok: true, run: summary() },
      'review record': RECORDED,
    },
    files: PROMPTS,
    choices: ['Changes since 0123456789ab (when the run started)'],
  })
  expect(await review($, {}, reviewer([APPROVED]).run)).toBe(RECORDED.message)
  expect(w.calls.find(call => call.args[0] === 'root-baseline')?.args).toEqual(['root-baseline', 'lib', '--commit=0123456789abcdef0123', '--run', 'r1'])
})

test('a late root the user removes instead is not asked about again, and the review runs on what is left', async () => {
  at(true)
  let removed = false
  let prepares = 0
  const roots = [
    { alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', candidates: [] },
    { alias: 'clone', path: '/tmp/s/clone', kind: 'git', baseline: 'unknown', candidates: [] },
  ]
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': () => (++prepares === 1 ? { ok: false, code: 'baseline-unknown', error: 'baseline unknown for clone' } : PREPARED),
      status: () => ({ ok: true, run: summary({ roots: removed ? roots.slice(0, 1) : roots }) }),
      'root-remove': () => {
        removed = true
        return { ok: true, alias: 'clone', removed: ['clone'], run: summary() }
      },
      'review record': RECORDED,
    },
    files: PROMPTS,
    choices: ["Remove clone (not the plan's work)"],
  })
  const text = await review($, {}, reviewer([APPROVED]).run)
  expect(text).toContain('The user removed root clone from the run')
  expect(text).toContain(String(RECORDED.message))
  expect(prepares).toBe(2)
  expect(w.asked).toHaveLength(1)
})

test('headless, an unknown baseline stops the review and says why', async () => {
  at(false)
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': { ok: false, code: 'baseline-unknown', error: 'baseline unknown for lib' },
      status: { ok: true, run: summary({ roots: [{ alias: 'lib', path: '/l', kind: 'dir', baseline: 'unknown', candidates: [] }] }) },
    },
  })
  const text = await review($, {}, reviewer([]).run)
  expect(text).toContain('a root\'s baseline must be chosen')
  expect(w.commands().filter(name => name === 'review prepare').length).toBe(1)
})

// --------------------------------------------------------------------------
// The tool, as Claude calls it
// --------------------------------------------------------------------------

test('the review tool answers through the mod, and a reviewer the kit cannot start fails, never approves', async ($, on) => {
  const failed = { ...RECORDED, verdict: 'OP_FAILURE', message: 'The reviewer failed.' }
  const w = world(on, { engine: { 'review prepare': PREPARED, 'review record': failed }, files: PROMPTS })
  await start($)

  expect(await callTool($, 'review')).toBe('The reviewer failed.')
  expect(w.spawns).toEqual([{ prompt: 'Review phase 1.', type: 'adversarial-review-loop:reviewer', model: 'opus' }])
  expect(w.calls.at(-1)?.args.slice(3, 5)).toEqual(['--failed', 'operational'])
})
