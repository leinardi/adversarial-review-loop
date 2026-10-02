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

// hooks/register.js against the engine, under `make test-mod` (`claude plugin test`).
// `status --json` is stubbed beneath the module: what is under test is what the band draws
// from each answer, and that the module passes every event through untouched.

import type { Engine } from 'claude-code/testing'
import { expect, test } from 'claude-code/testing'
import type { On, ProcessRunResult, RenderElement } from 'claude-code'

const PLUGIN = 'adversarial-review-loop'
const SESSION = 'session-under-test'

const BAND = {
  hasSurvey: false,
  isWorking: false,
  maxRows: 10,
  bodyColumns: 200,
  scroll: { offset: 0, bodyRows: 10 },
  view: {},
}

const BOUND = {
  binding: 'bound',
  session: SESSION,
  status: 'ACTIVE',
  stored_status: 'ACTIVE',
  phase: 2,
  phase_count: 5,
  rounds_this_phase: 3,
  failures: 0,
  max_failures: 2,
  transient_failures: 0,
  max_transient_failures: 5,
  stop_blocks: 0,
  max_stop_blocks: 3,
  pause_target: 0,
  retry_backoff_sec: 0,
  alerts: [],
}

type Status = Pick<ProcessRunResult, 'exitCode' | 'stdout' | 'stderr'> | 'reject'

// The world beneath the module: a session, and a `status` that answers `answer`. Records
// every argv the module ran and every transcript line it logged.
function world(on: On, answer: () => Status) {
  const ran: string[][] = []
  const logged: string[] = []
  on('session.id', () => ({ value: SESSION }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('process.run', (_$, e) => {
    ran.push([...e.argv])
    const result = answer()
    if (result === 'reject') {
      throw new Error('status could not start')
    }
    return { value: { ...result, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.log', (_$, e) => {
    logged.push(e.text)
    return { value: undefined }
  })
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  // What the engine draws when the module passes: a line the tests can tell from the module's.
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return h(Text, {}, 'engine band') as RenderElement
  })
  return { ran, logged }
}

function says(document: object): Status {
  return { exitCode: 0, stdout: JSON.stringify(document) + '\n', stderr: '' }
}

async function band($: Engine, props: Partial<typeof BAND> = {}) {
  await $.turn.start({ text: 'go on', turnId: 'turn-1' })
  return $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: { ...BAND, ...props } })
}

test('asks status for this session, by the shim, bound to the session id', async ($, on) => {
  const { ran } = world(on, () => says(BOUND))
  await band($)
  expect(ran.length).toBeGreaterThan(0)
  const argv = ran[0] ?? []
  expect(argv[0]?.endsWith('/scripts/arl.sh')).toBe(true)
  expect(argv.slice(1)).toEqual(['status', '--json', '--session', SESSION])
})

test('a bound activation draws its status, phase, round and failures', async ($, on) => {
  world(on, () => says(BOUND))
  const ui = await band($)
  expect(await ui.find({ type: 'Text', text: 'ARL ACTIVE · phase 2/5 · round 3 · failures 0/2' })).toBeDefined()
})

test('the line sits above whatever else draws in the band, which stays', async ($, on) => {
  world(on, () => says(BOUND))
  const ui = await band($)
  expect(await ui.find({ type: 'Text', text: /^ARL ACTIVE/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'engine band' })).toBeDefined()
})

test('a survey holds the band alone', async ($, on) => {
  world(on, () => says(BOUND))
  const ui = await band($, { hasSurvey: true })
  expect(await ui.find({ type: 'Text', text: /ARL/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'engine band' })).toBeDefined()
})

test('past the last phase the band says the plan is committed, not phase total+1', async ($, on) => {
  world(on, () => says({ ...BOUND, phase: 2, phase_count: 1, rounds_this_phase: 0 }))
  const ui = await band($)
  expect(await ui.find({ type: 'Text', text: 'ARL ACTIVE · all 1 phases committed · round 0 · failures 0/2' })).toBeDefined()
})

test('another session’s activation draws as unbound, pointing at resume', async ($, on) => {
  world(on, () => says({ binding: 'unbound', session: 'other', status: 'ACTIVE', alerts: [] }))
  const ui = await band($)
  expect(await ui.find({ type: 'Text', text: /armed by another session \(ACTIVE\).*resume/ })).toBeDefined()
})

test('an unarmed worktree draws nothing of ours', async ($, on) => {
  world(on, () => says({ binding: 'unarmed', alerts: [] }))
  const ui = await band($)
  expect(await ui.find({ type: 'Text', text: /ARL/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'engine band' })).toBeDefined()
})

for (const [name, answer, cause] of [
  ['an unknown binding', says({ binding: 'unknown', cause: 'document_malformed', alerts: ['state_unknown'] }), 'document_malformed'],
  ['a non-zero exit', { exitCode: 1, stdout: '', stderr: 'boom' }, 'status_failed'],
  ['output that is not JSON', { exitCode: 0, stdout: 'adversarial-review-loop: not armed\n', stderr: '' }, 'unparseable'],
  ['a binding it does not know', says({ binding: 'maybe', alerts: [] }), 'unparseable'],
  ['a status it does not know', says({ ...BOUND, status: '\u001b[2J' }), 'unparseable'],
  ['an alert it has no line for', says({ ...BOUND, alerts: ['raised_by_a_newer_gate'] }), 'unparseable'],
  ['alerts that are not a list', says({ ...BOUND, alerts: 'needs_human' }), 'unparseable'],
  ['a process that cannot start', 'reject', 'status_failed'],
] as const) {
  test(`${name} draws "state unknown", never unarmed`, async ($, on) => {
    world(on, () => answer)
    const ui = await band($)
    expect(await ui.find({ type: 'Text', text: `ARL: state unknown (${cause})` })).toBeDefined()
  })
}

test('a new alert is logged to the transcript once, and drawn on the band', async ($, on) => {
  const { logged } = world(on, () => says({ ...BOUND, status: 'DISARMED', alerts: ['unreviewed_at_exit'] }))
  const ui = await band($)
  await $.turn.start({ text: 'again', turnId: 'turn-2' })
  expect(logged.filter(line => line.includes('no review approved'))).toHaveLength(1)
  // The engine prefixes a log row with the plugin's name; the text must not repeat it.
  expect(logged.some(line => line.startsWith(PLUGIN))).toBe(false)
  expect(await ui.find({ type: 'Text', text: /alert: unreviewed_at_exit/ })).toBeDefined()
})

test('a Bash call passes through untouched, and refreshes only while armed', async ($, on) => {
  let calls = 0
  world(on, () => {
    calls += 1
    return says({ binding: 'unarmed', alerts: [] })
  })
  const answered = { result: { stdout: 'untouched' }, text: 'untouched' }
  on('tool.call', () => answered)
  await $.turn.start({ text: 'go', turnId: 'turn-1' })
  const before = calls
  const result = await $.tool.call({ tool: 'Bash', command: 'true' })
  expect(result).toEqual(answered)
  expect(calls).toBe(before)
})

test('while armed, a Bash call refreshes the band and still passes through untouched', async ($, on) => {
  let calls = 0
  world(on, () => {
    calls += 1
    return says(BOUND)
  })
  const answered = { result: { stdout: 'untouched' }, text: 'untouched' }
  on('tool.call', () => answered)
  await $.turn.start({ text: 'go', turnId: 'turn-1' })
  const before = calls
  const result = await $.tool.call({ tool: 'Bash', command: 'git commit -m x' })
  expect(result).toEqual(answered)
  expect(calls).toBe(before + 1)
})
