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

// The review and clarify flows: one round on the subagent, its repair, its failures, the external
// harness, and what Claude is told when the loop needs the user.

import type { EngineInterface } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import { clarifyWith, reviewWith, session } from '../../hooks/flow'
import { Interrupted } from '../../hooks/reviewer'
import { callTool, fakeEngine, LAUNCH, PREPARED, PROMPTS, RECORDED, reviewer, start, summary, world } from './world'

function at(isInteractive = false): void {
  session.dir = LAUNCH
  session.id = 'session-1'
  session.isInteractive = isInteractive
}

function review($: EngineInterface, input: Record<string, unknown>, run: ReturnType<typeof reviewer>['run']): Promise<string> {
  return reviewWith($, input, run)
}


const APPROVED = { ok: true as const, answer: 'All good.\nVERDICT APPROVED', usage: { input_tokens: 1, output_tokens: 2, cache_read_input_tokens: 3, cache_creation_input_tokens: 4, model: 'haiku' }, durationMs: 1000 }

test('a review runs one fresh reviewer on the prepared prompt and records its answer', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  const r = reviewer([APPROVED])

  const text = await review($, { verification: [{ command: 'make test', status: 0 }] }, r.run)

  expect(text).toBe(RECORDED.message)
  expect(r.tasks).toEqual([
    { prompt: 'Review phase 1.', description: 'Review phase1, round 1', model: '', timeoutSec: 900, dirs: [PREPARED.bundle_dir, ...PREPARED.roots] },
  ])
  expect(w.written.get(PREPARED.answer_file)).toBe(APPROVED.answer)
  expect(w.commands()).toEqual(['review prepare', 'review record'])
  const [prepare, record] = w.calls
  expect(JSON.parse(prepare?.stdin ?? '')).toEqual({ verification: [{ command: 'make test', status: 0 }] })
  expect(prepare?.args).toEqual(['review', 'prepare', '--session', 'session-1'])
  expect(record?.args.slice(0, 5)).toEqual(['review', 'record', 'r1-1', '--answer', PREPARED.answer_file])
  expect(JSON.parse(record?.args.at(-1) ?? '{}')).toEqual({
    input_tokens: 1,
    output_tokens: 2,
    cache_read_tokens: 3,
    cache_creation_tokens: 4,
    duration_ms: 1000,
  })
})

test('a review with no verification sends an empty list, never nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': RECORDED }, files: PROMPTS })
  await review($, {}, reviewer([APPROVED]).run)
  expect(JSON.parse(w.calls[0]?.stdin ?? '')).toEqual({ verification: [] })
})

test('the configured model and timeout reach the reviewer', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'review prepare': { ...PREPARED, reviewer_model: 'sonnet', timeout_sec: 60 }, 'review record': RECORDED }, files: PROMPTS })
  const r = reviewer([APPROVED])
  await review($, {}, r.run)
  expect([r.tasks[0]?.model, r.tasks[0]?.timeoutSec]).toEqual(['sonnet', 60])
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

test('an empty question runs nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: {} })
  expect(await clarifyWith($, { question: '  ' }, reviewer([]).run)).toBe('clarify needs a question.')
  expect(w.calls).toEqual([])
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
      { alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', proposed: '' },
      { alias: 'lib', path: '/work2/lib', kind: 'git', baseline: 'unknown', proposed: 'HEAD@{2026-11-12 10:00}' },
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
    choices: ['Changes since HEAD@{2026-11-12 10:00}'],
  })
  expect(await review($, {}, reviewer([APPROVED]).run)).toBe(RECORDED.message)
  expect(w.calls.find(call => call.args[0] === 'root-baseline')?.args).toEqual(['root-baseline', 'lib', '--commit', 'HEAD@{2026-11-12 10:00}'])
})

test('headless, an unknown baseline stops the review and says why', async () => {
  at(false)
  const { $, w } = fakeEngine({
    engine: {
      'review prepare': { ok: false, code: 'baseline-unknown', error: 'baseline unknown for lib' },
      status: { ok: true, run: summary({ roots: [{ alias: 'lib', path: '/l', kind: 'dir', baseline: 'unknown', proposed: '' }] }) },
    },
  })
  const text = await review($, {}, reviewer([]).run)
  expect(text).toContain('a root\'s baseline must be chosen')
  expect(w.commands().filter(name => name === 'review prepare').length).toBe(1)
})

// --------------------------------------------------------------------------
// clarify
// --------------------------------------------------------------------------

const ASKED = {
  ok: true,
  run_id: 'r1',
  round_seq: 3,
  question: 1,
  reviewer_model: '',
  timeout_sec: 900,
  prompt_file: '/work/.arl/reviews/r1-003/clarify-1-prompt.md',
  bundle_dir: '/work/.arl/reviews/r1-003/bundle',
  roots: [LAUNCH],
  answer_file: '/work/.arl/reviews/r1-003/clarify-1-answer.md',
}

test('a question goes to a fresh reviewer, and its reply to Claude', async () => {
  at()
  const { $, w } = fakeEngine({
    engine: {
      'clarify run-external': { ok: false, code: 'not-external', error: 'the subagent' },
      'clarify prepare': ASKED,
      'clarify record': { ok: true, reply: 'Line 3 still races.', retracted: [{ ordinal: 1 }], problem: '' },
    },
    files: { [ASKED.prompt_file]: 'Answer the question.' },
  })
  const r = reviewer([{ ok: true, answer: 'Line 3 still races.', durationMs: 5 }])

  const text = await clarifyWith($, { question: 'Is the lock not enough?' }, r.run)

  expect(text).toContain('Line 3 still races.')
  expect(text).toContain('retracted 1 finding')
  expect(w.calls.map(call => call.stdin).slice(0, 2)).toEqual(['Is the lock not enough?', 'Is the lock not enough?'])
  expect(w.calls.at(-1)?.args).toEqual(['clarify', 'record', '3', '1', '--answer', ASKED.answer_file, '--run-id', 'r1'])
  expect(r.tasks[0]?.prompt).toBe('Answer the question.')
})

test('the external reviewer answers a question through the engine alone', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'clarify run-external': { ok: true, reply: 'Yes.', retracted: [], problem: '' } } })
  const r = reviewer([])
  expect(await clarifyWith($, { question: 'Sure?' }, r.run)).toContain('Yes.')
  expect(w.commands()).toEqual(['clarify run-external'])
  expect(r.tasks).toEqual([])
})

test('a reviewer that could not answer a question records nothing', async () => {
  at()
  const { $, w } = fakeEngine({
    engine: { 'clarify run-external': { ok: false, code: 'not-external', error: '' }, 'clarify prepare': ASKED },
    files: { [ASKED.prompt_file]: 'Answer.' },
  })
  const text = await clarifyWith($, { question: 'Why?' }, reviewer([{ ok: false, kind: 'transient', error: 'timed out' }]).run)
  expect(text).toBe('The reviewer could not answer: timed out')
  expect(w.commands()).not.toContain('clarify record')
})

// --------------------------------------------------------------------------
// The tool, as Claude calls it
// --------------------------------------------------------------------------

test('the review tool answers through the mod, and a reviewer the kit cannot start fails, never approves', async ($, on) => {
  const failed = { ...RECORDED, verdict: 'OP_FAILURE', message: 'The reviewer failed.' }
  const w = world(on, { engine: { 'review prepare': PREPARED, 'review record': failed }, files: PROMPTS })
  await start($)

  expect(await callTool($, 'review')).toBe('The reviewer failed.')
  expect(w.spawns).toEqual([{ prompt: 'Review phase 1.', type: 'adversarial-review-loop:reviewer', model: undefined }])
  expect(w.calls.at(-1)?.args.slice(3, 5)).toEqual(['--failed', 'operational'])
})

test('an interrupted external question is said to be interrupted, not an engine refusal', async () => {
  at()
  const { $ } = fakeEngine({ engine: { 'clarify run-external': { ok: false, code: 'engine', error: 'the engine was stopped' } } })
  const interrupted = new AbortController()
  interrupted.abort()
  expect(await clarifyWith($, { question: 'Why?' }, reviewer([]).run, interrupted.signal)).toBe('The question was interrupted; its reply was not recorded.')
})
