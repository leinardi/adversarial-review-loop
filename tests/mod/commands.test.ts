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

// The person's slash commands, the session's start (reattaching a run), compaction before a new
// phase, and the root a refused write leaves behind.

import { expect, test } from 'claude-code/testing'
import { implementArgs, runCommand } from '../../hooks/commands'
import { continueRun, reviewWith, session } from '../../hooks/flow'
import { fakeEngine, LAUNCH, PREPARED, PROMPTS, RECORDED, reviewer, settle, start, summary, world } from './world'

function at(isInteractive = false): void {
  session.dir = LAUNCH
  session.id = 'session-1'
  session.isInteractive = isInteractive
}

const CONTINUE = { ok: true, action: 'continue', stage: 'phase', phase: 2, prompt: 'Implement phase 2 of 2: two', token: 't', compact: false }

// --------------------------------------------------------------------------
// Registration and the session's start
// --------------------------------------------------------------------------

test('every command is registered with the arl- prefix: Claude Code ships /status, /resume and /config', async ($, on) => {
  const w = world(on, { engine: {} })
  await start($)
  const names = w.registered.commands.map(command => command.name)
  expect(names).toEqual([
    'arl-implement',
    'arl-resume',
    'arl-status',
    'arl-report',
    'arl-pause',
    'arl-accept',
    'arl-finish',
    'arl-stop',
    'arl-config',
    'arl-remove-root',
  ])
})

test('a session start reattaches: it names its session, and the reviewer gets the configured effort', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: 'high' } } })
  await start($)
  expect(w.attached.map(call => call.args)).toEqual([['attach', '--session', 'session-1']])
  expect(w.registered.agents).toEqual([expect.objectContaining({ name: 'reviewer', effort: 'high' })])
})

test('a review another session started is said at the start, and left to /arl-resume', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: true, effort: '', effort_note: "effort 'ultra' is no level" } } })
  await start($)
  expect(w.logged.join('\n')).toContain('/arl-resume abandons it')
  expect(w.logged.join('\n')).toContain("effort 'ultra' is no level")
  expect(w.commands()).not.toContain('review abandon')
  expect(w.registered.agents.map(agent => 'effort' in agent)).toEqual([false])
})

test('a run the engine cannot read is said at the start, and the session still starts', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: false, code: 'state-corrupt', error: 'bad json' } } })
  await start($)
  expect(w.logged.join('\n')).toContain('could not read the run here: bad json')
  expect(w.registered.agents).toHaveLength(1)
})

test('a command runs through the kit and answers its text', async ($, on) => {
  world(on, { engine: { status: { ok: true, run: summary() } } })
  await start($)
  const { text } = await $.command.run({ command: 'arl-status' } as never)
  expect(text).toContain('Run r1 · active · phase 1/2: one')
})

// --------------------------------------------------------------------------
// The commands
// --------------------------------------------------------------------------

test('/arl-implement parses its flags and refuses what it cannot read', () => {
  expect(implementArgs('--new --root ../web plan.md')).toEqual({ plan: 'plan.md', isNew: true, roots: ['../web'] })
  expect(implementArgs('')).toContain('Usage')
  expect(implementArgs('--root')).toBe('--root needs a directory.')
  expect(implementArgs('--fast plan.md')).toBe('Unknown flag --fast.')
  expect(implementArgs('a.md b.md')).toContain('One plan at a time')
  expect(implementArgs('--root "my web" "my plan.md"')).toEqual({ plan: 'my plan.md', isNew: false, roots: ['my web'] })
  expect(implementArgs('"open.md')).toContain('Unbalanced quote')
})

test('/arl-implement starts the run, opens the pane, and starts Claude on the plan once the command ends', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { init: { ok: true, run: summary(), archived: 'old1', prompt: 'Implement the plan.' } } })
  const text = await runCommand($, 'arl-implement', '--new plan.md')
  expect(w.calls[0]?.args).toEqual(['init', '--plan', 'plan.md', '--new'])
  expect(text).toContain('Run r1 started on plan.md, with roots work (/work).')
  expect(text).toContain('.arl/runs/old1/')
  expect(w.opened).toEqual(['arl'])
  // The pane's latest review belonged to the run before.
  expect(w.state.get('adversarial-review-loop/last')).toEqual({ shape: 'last-1', value: null })
  expect(w.submitted).toEqual([])
  await settle()
  expect(w.submitted).toEqual(['Implement the plan.'])
})

test('/arl-implement over an active run is refused, and nothing starts', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { init: { ok: false, code: 'run-exists', error: 'run r1 is active: use /arl-resume' } } })
  expect(await runCommand($, 'arl-implement', 'plan.md')).toContain('run-exists')
  await settle()
  expect(w.submitted).toEqual([])
})

test('/arl-resume abandons any pending review and tells Claude to call review again', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { resume: { ok: true, run: summary(), abandoned_review: true, pending_review: false }, 'next-action': CONTINUE } })
  const text = await runCommand($, 'arl-resume', '')
  // No session id: a command waits for the turn, so no review of this session is in flight.
  expect(w.calls[0]?.args).toEqual(['resume'])
  expect(text).toContain('Resumed run r1 at phase 1/2: one, round 1 (the pending review was abandoned)')
  await settle()
  expect(w.submitted).toHaveLength(1)
  expect(w.submitted[0]).toContain('call review again')
  expect(w.submitted[0]).toContain('Implement phase 2 of 2: two')
})

test('/arl-resume that cannot continue says why, and submits nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { resume: { ok: true, run: summary() }, 'next-action': { ok: true, action: 'yield', reason: 'complete' } } })
  expect(await runCommand($, 'arl-resume', '')).toContain('does not continue now (complete)')
  await settle()
  expect(w.submitted).toEqual([])
})

test('/arl-resume asks for a missing baseline, then continues', async () => {
  at(true)
  let asked = 0
  const unknown = summary({ roots: [{ alias: 'web', path: '/elsewhere/web', kind: 'dir', baseline: 'unknown', proposed: '' }] })
  const { $, w } = fakeEngine({
    engine: {
      resume: { ok: true, run: unknown },
      status: { ok: true, run: unknown },
      'root-baseline': { ok: true, run: summary() },
      'next-action': () => (asked++ === 0 ? { ok: true, action: 'yield', reason: 'baseline-unknown' } : CONTINUE),
    },
    choices: ['Everything here is new'],
  })
  expect(await runCommand($, 'arl-resume', '')).toContain('Claude continues the run.')
  expect(w.calls.find(call => call.args[0] === 'root-baseline')?.args).toEqual(['root-baseline', 'web', '--empty'])
  await settle()
  expect(w.submitted).toHaveLength(1)
})

test('/arl-status shows the run to the person and the pane, and tells Claude one line', async () => {
  at(true)
  const run = summary({ approved_phases: 1, phase: 2, rounds: 1, roots: [{ alias: 'web', path: '/w', kind: 'git', baseline: 'unknown', proposed: '' }] })
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run } } })
  const text = String(await runCommand($, 'arl-status', ''))
  expect(text).toBe('Run r1 (active): phase 2/2: two, round 2. The pane shows it.')
  const shown = w.logged.join('\n')
  expect(shown).toContain('✓ 1. one')
  expect(shown).toContain('▶ 2. two  (round 2)')
  expect(shown).toContain('web  /w  (git)  baseline unknown')
  expect(w.opened).toEqual(['arl'])
})

test('/arl-status headless answers the whole status as its text, and opens no pane', async () => {
  at(false)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: null } } })
  expect(await runCommand($, 'arl-status', '')).toContain('No adversarial-review-loop run here')
  expect(w.opened).toEqual([])
})

test('/arl-report puts the report in the pane, never in Claude\'s context', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { report: { ok: true, text: '# Report 1\nVERDICT APPROVED', reports: [] } } })
  expect(await runCommand($, 'arl-report', '1')).toBe('Report 1 is in the review pane.')
  expect(w.state.get('adversarial-review-loop/report')).toBe('# Report 1\nVERDICT APPROVED')
  expect(w.opened).toEqual(['arl'])
})

test('/arl-report headless answers the engine text, by number when given', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { report: { ok: true, text: '# Report 2', reports: [] } } })
  expect(await runCommand($, 'arl-report', ' 2 ')).toBe('# Report 2')
  expect(w.calls[0]?.args).toEqual(['report', '2'])
  expect(await runCommand($, 'arl-report', 'last')).toBe('Usage: /arl-report [N]')
})

test('/arl-pause and /arl-stop say what they did, or why not', async () => {
  at()
  const { $ } = fakeEngine({ engine: { pause: { ok: true, run: summary({ status: 'paused' }) }, stop: { ok: false, code: 'run-stopped', error: 'the run is stopped' } } })
  expect(await runCommand($, 'arl-pause', '')).toContain('Paused')
  expect(await runCommand($, 'arl-stop', '')).toContain('run-stopped')
})

test('/arl-accept approves and continues with the next stage', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { accept: { ok: true, run: summary({ phase: 2, approved_phases: 1 }) }, 'next-action': CONTINUE } })
  expect(await runCommand($, 'arl-accept', '')).toBe('Accepted. Claude continues the run.')
  await settle()
  expect(w.submitted[0]).toContain('The user accepted the stage as it stands.')
})

test('/arl-accept of the last stage completes the run and prompts nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { accept: { ok: true, run: summary({ stage: 'done', status: 'complete' }) } } })
  expect(await runCommand($, 'arl-accept', '')).toBe('Accepted. The run is complete.')
  expect(w.notified.join('\n')).toContain('the run is complete')
  await settle()
  expect(w.commands()).toEqual(['accept'])
  expect(w.submitted).toEqual([])
})

test('a stage whose changes are all ignored is shown, and the run it completes is notified', async () => {
  at()
  const skipped = { ok: true, status: 'skipped', verdict: 'APPROVED', message: 'Every change is ignored: approved.', run: summary({ status: 'complete', stage: 'done' }) }
  const { $, w } = fakeEngine({ engine: { 'review prepare': skipped } })
  expect(await reviewWith($, {}, reviewer([]).run)).toBe(skipped.message)
  expect(w.notified.join('\n')).toContain('the run is complete')
})

test('/arl-finish before the last approval only records the request', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { finish: { ok: true, run: summary() } } })
  expect(await runCommand($, 'arl-finish', '')).toContain('once the last phase is approved')
  expect(w.commands()).toEqual(['finish'])
})

test('/arl-finish after the last approval starts the final review', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { finish: { ok: true, run: summary({ stage: 'final' }) }, 'next-action': { ...CONTINUE, stage: 'final', prompt: 'Run the final review.' } } })
  expect(await runCommand($, 'arl-finish', '')).toContain('The final review is next.')
  await settle()
  expect(w.submitted).toEqual(['Run the final review.'])
})

test('/arl-config passes its arguments as text, and a new effort re-registers the reviewer', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { config: { ok: true, text: 'effort: "" -> max', key: 'effort', effort: 'max' } } })
  expect(await runCommand($, 'arl-config', 'effort max')).toBe('effort: "" -> max')
  expect(w.calls[0]).toEqual({ args: ['config'], stdin: 'effort max' })
  expect(w.registered.agents).toEqual([expect.objectContaining({ name: 'reviewer', effort: 'max' })])
})

test('/arl-config whose new effort the host refuses says the config is written, and the command still answers', async () => {
  at()
  const { $ } = fakeEngine({ engine: { config: { ok: true, text: 'effort: "" -> max', key: 'effort', effort: 'max' } } })
  ;($ as unknown as { agent: { register: () => Promise<never> } }).agent.register = async () => {
    throw new Error('refused')
  }
  const text = String(await runCommand($, 'arl-config', 'effort max'))
  expect(text).toContain('effort: "" -> max')
  expect(text).toContain('a new session uses the setting')
})

test('/arl-config with no key shows the listing to the person, and leaves the reviewer as it is', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { config: { ok: true, text: 'every key', effort: '' } } })
  expect(await runCommand($, 'arl-config', '')).toBe('The configuration is shown above.')
  expect(w.logged).toEqual(['every key'])
  expect(w.registered.agents).toEqual([])
})

test('/arl-remove-root drops the root it names', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { 'root-remove': { ok: true, alias: 'web', path: '/elsewhere/web', run: summary() }, 'next-action': CONTINUE } })
  const text = await runCommand($, 'arl-remove-root', 'web')
  expect(text).toContain('Removed root web (/elsewhere/web)')
  // The root may be what held the loop: it goes on.
  expect(text).toContain('Claude continues the run.')
  expect(w.calls[0]?.args).toEqual(['root-remove', 'web'])
  await settle()
  expect(w.submitted).toEqual([CONTINUE.prompt])
  expect(await runCommand($, 'arl-remove-root', ' ')).toContain('Usage')
})

test('a name the plugin does not register is not its to answer', async () => {
  at()
  const { $ } = fakeEngine({ engine: {} })
  expect(await runCommand($, 'arl-nope', '')).toBeUndefined()
})

// --------------------------------------------------------------------------
// Compaction, the pane's review, and refused writes
// --------------------------------------------------------------------------

test('with auto_compact, the next phase starts on a compacted conversation: compact, then submit', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } } })
  await continueRun($)
  expect(w.compacted).toHaveLength(1)
  expect(w.submitted).toEqual([CONTINUE.prompt])
})

test('headless, where compaction is refused, the next phase is submitted from the hook', async () => {
  at(false)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } } })
  await continueRun($)
  expect(w.submitted).toEqual([CONTINUE.prompt])
  expect(w.compacted).toEqual([])
})

test('a compaction that fails is said, and the run continues uncompacted', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } }, compactFails: true })
  await continueRun($)
  await settle()
  expect(w.logged.join('\n')).toContain('could not compact')
  expect(w.submitted).toEqual([CONTINUE.prompt])
})

test('a recorded review lands in the pane, is logged, and a completed run is notified', async () => {
  at()
  const done = { ...RECORDED, run: summary({ status: 'complete', stage: 'done' }) }
  const { $, w } = fakeEngine({ engine: { 'review prepare': PREPARED, 'review record': done }, files: PROMPTS })
  await reviewWith($, {}, reviewer([{ ok: true, answer: 'VERDICT APPROVED', durationMs: 1 }]).run)
  // A shaped atom stores its value under its shape tag.
  expect(w.state.get('adversarial-review-loop/last')).toEqual({ shape: 'last-1', value: { label: 'phase1', verdict: 'APPROVED', message: RECORDED.message, report: '/r.md' } })
  expect(w.state.get('adversarial-review-loop/activity')).toBe('')
  expect(w.logged).toEqual(['adversarial-review-loop: phase1 APPROVED'])
  expect(w.notified.join('\n')).toContain('the run is complete')
})

const ADDED = { ok: true, status: 'added', alias: 'web', baseline: 'set', proposed: '' }
const STATUS = { ok: true, run: summary() }

test('a write the session denies registers no root', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': ADDED }, check: 'deny', writeOutcome: 'deny' })
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  expect(w.commands()).not.toContain('root-add')
})

test('a write the person refuses drops the root registered for it', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': ADDED, 'root-remove': { ok: true, alias: 'web', path: '/elsewhere/web', run: summary() } }, check: 'ask', writeOutcome: 'deny' })
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  // The kit, as the host, hands a refused call back as an error result.
  expect(result.isError).toBe(true)
  expect(w.commands()).toEqual(['status', 'root-add', 'status', 'root-remove', 'status'])
  // Undo: the engine drops it only while unchanged, and moves nothing else.
  expect(w.calls.find(call => call.args[0] === 'root-remove')?.args).toEqual(['root-remove', 'web', '--undo'])
})

test('a write that runs keeps its root', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': ADDED }, check: 'allow', writeOutcome: 'run' })
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  expect(result.context?.join('\n')).toContain('registered /elsewhere/web as root web')
  expect(w.commands()).not.toContain('root-remove')
})
