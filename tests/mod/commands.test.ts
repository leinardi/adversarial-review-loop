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
import { asOf, implementArgs, modelArgs, openPane, runCommand, showNow } from '../../hooks/commands'
import { pane } from '../../hooks/ui'
import { announce, baselineQuestion, chooseBaselines, continueRun, nameTools, resetWrites, reviewWith, run, session, writeIn } from '../../hooks/flow'
import { callTool, fakeEngine, LAUNCH, PREPARED, PROMPTS, RECORDED, reviewer, settle, start, summary, world } from './world'

function at(isInteractive = false): void {
  session.dir = LAUNCH
  session.id = 'session-1'
  session.isInteractive = isInteractive
  // The flows' module is shared by every test: no test sees another's writes.
  resetWrites()
  pane.dismissed = false
  session.turnRunning = false
  nameTools([])
}

const NAMES: [string, string][] = [
  ['set_phases', 'mcp__arl__set_phases'],
  ['add_root', 'mcp__arl__add_root'],
  ['review', 'mcp__arl__review'],
]
const NAMES_LINE = "The loop's tools, by their full names: set_phases is mcp__arl__set_phases, add_root is mcp__arl__add_root, review is mcp__arl__review."

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
    'arl-stats',
    'arl-phases',
    'arl-pane',
    'arl-pause',
    'arl-accept',
    'arl-finish',
    'arl-stop',
    'arl-config',
    'arl-model',
    'arl-remove-root',
  ])
})

test('a session start reattaches: it names its session, and the reviewer gets the configured effort', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: 'high' } } })
  await start($)
  expect(w.attached.map(call => call.args)).toEqual([['attach', '--session', 'session-1']])
  expect(w.registered.agents).toEqual([expect.objectContaining({ name: 'reviewer', effort: 'high' })])
})

test('an interactive session that starts on an active run opens the pane', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: '' } } })
  await start($, true)
  expect(w.opened).toEqual(['arl'])
})

test('a headless session that starts on a run opens no pane', async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: '' } } })
  await start($)
  expect(w.opened).toEqual([])
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
  expect(w.logged.join('\n')).toContain('Could not read the run here: bad json')
  expect(w.registered.agents).toHaveLength(1)
})

test('a command runs through the kit and answers its text', async ($, on) => {
  world(on, { engine: { status: { ok: true, run: summary() } } })
  await start($)
  const { text } = await $.command.run({ command: 'arl-status' } as never)
  expect(text).toContain('Run r1 · active · phase 1/2: one')
})

// --------------------------------------------------------------------------
// An immediate command while Claude's turn runs
// --------------------------------------------------------------------------

test("while Claude's turn runs, a command's lines are shown in the pane at once: the host prints its text only when the turn ends", async () => {
  at(true)
  session.turnRunning = true
  const { $, w } = fakeEngine({ engine: {} })
  await showNow($, 'arl-phases', '✓ 1. one\n▶ 2. two')
  expect(w.opened).toEqual(['arl-report'])
  expect(w.openArgs.get('arl-report')).toEqual(expect.objectContaining({ title: '/arl-phases', focus: true }))
  expect([...w.state.values()]).toContain('✓ 1. one\n▶ 2. two')
})

test("while Claude's turn runs, a one-line answer goes to the log, and a pane command is left alone", async () => {
  at(true)
  session.turnRunning = true
  const { $, w } = fakeEngine({ engine: {} })
  await showNow($, 'arl-pause', 'Paused.\n')
  await showNow($, 'arl-report', 'Report (newest) is in the report pane.')
  expect(w.logged).toEqual(['Paused.'])
  expect(w.opened).toEqual([])
})

test('at rest, or headless, a command only answers its text: the host prints it at once', async () => {
  for (const [interactive, running] of [[true, false], [false, true]] as const) {
    at(interactive)
    session.turnRunning = running
    const { $, w } = fakeEngine({ engine: {} })
    await showNow($, 'arl-phases', 'a\nb')
    expect(w.opened).toEqual([])
    expect(w.logged).toEqual([])
  }
})

test("only a command reporting the run is dated, and only while Claude's turn runs in an interactive session", () => {
  const now = new Date(2026, 10, 23, 9, 5, 7)
  at(true)
  session.turnRunning = true
  expect(asOf('arl-phases', '▶ 1. one', now)).toBe("As of 09:05:07, while Claude's turn was running. The run may have moved on: /arl-phases shows it now.\n▶ 1. one")
  expect(asOf('arl-status', 'Run r1', now)).toContain('As of 09:05:07')
  expect(asOf('arl-pause', 'Paused.', now)).toBe('Paused.')
  expect(asOf('arl-phases', ' ', now)).toBe(' ')
  session.turnRunning = false
  expect(asOf('arl-phases', '▶ 1. one', now)).toBe('▶ 1. one')
  at(false)
  session.turnRunning = true
  expect(asOf('arl-phases', '▶ 1. one', now)).toBe('▶ 1. one')
})

test("/arl-phases typed while Claude's turn runs opens the pane at once, and not once the turn ended", async ($, on) => {
  const w = world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: '' }, status: { ok: true, run: summary() }, 'next-action': { ok: true, action: 'yield', reason: 'unchanged' } } })
  on('turn.start', (_, e) => e as never)
  await start($, true)
  const reports = (): number => w.opened.filter(id => id === 'arl-report').length
  await $.turn.start({ text: 'go', turnId: 't1' } as never)
  const { text } = await $.command.run({ command: 'arl-phases' } as never)
  expect(reports()).toBe(1)
  // Still answered as its text: the host prints that once the turn ends.
  expect(text).toContain('1. one')
  // Printed that late, it says when it was true.
  expect(text).toMatch(/^As of \d\d:\d\d:\d\d, while Claude's turn was running\./)
  await $.turn.complete({ answer: 'done', reason: 'aborted', durationMs: 5, isAborted: true, turnId: 't1' } as never)
  const atRest = await $.command.run({ command: 'arl-phases' } as never)
  expect(reports()).toBe(1)
  expect(atRest.text).not.toContain('As of')
})

// --------------------------------------------------------------------------
// The commands
// --------------------------------------------------------------------------

test('/arl-implement parses its flags and refuses what it cannot read', () => {
  expect(implementArgs('--new --root ../web plan.md')).toEqual({ plan: 'plan.md', isNew: true, roots: ['../web'], model: '' })
  expect(implementArgs('--model weak plan.md')).toEqual({ plan: 'plan.md', isNew: false, roots: [], model: 'weak' })
  expect(implementArgs('plan.md --model')).toContain('--model needs a model')
  expect(implementArgs('--model weak --model strong plan.md')).toContain('One model at a time')
  expect(implementArgs('')).toContain('Usage')
  expect(implementArgs('--root')).toBe('--root needs a directory.')
  expect(implementArgs('--fast plan.md')).toBe('Unknown flag --fast.')
  expect(implementArgs('a.md b.md')).toContain('One plan at a time')
  expect(implementArgs('--root "my web" "my plan.md"')).toEqual({ plan: 'my plan.md', isNew: false, roots: ['my web'], model: '' })
  expect(implementArgs('"open.md')).toContain('Unbalanced quote')
})

test('/arl-implement starts the run, opens the pane, and starts Claude on the plan once the command ends', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { init: { ok: true, run: summary(), archived: 'old1', prompt: 'Implement the plan.' } } })
  w.state.set('adversarial-review-loop/report', 'Report 3 of the old run')
  const text = await runCommand($, 'arl-implement', '--new plan.md')
  expect(w.calls[0]?.args).toEqual(['init', '--plan', 'plan.md', '--new'])
  expect(text).toContain('Run r1 started on plan.md, with roots work (/work).')
  expect(text).toContain('.arl/runs/old1/')
  expect(w.opened).toEqual(['arl'])
  // The pane's latest review belonged to the run before.
  expect(w.state.get('adversarial-review-loop/last')).toEqual({ shape: 'last-1', value: null })
  // So did any report shown, and its pane.
  expect(w.state.get('adversarial-review-loop/report')).toBe('')
  expect(w.closed).toEqual(['arl-report'])
  expect(w.submitted).toEqual([])
  await settle()
  expect(w.submitted).toEqual(['Implement the plan.'])
})

test('the start, resume and compacted prompts name the loop\'s tools in full, once each', async () => {
  at(true)
  nameTools(NAMES)
  const started = fakeEngine({ engine: { init: { ok: true, run: summary(), prompt: 'Implement the plan.' } } })
  await runCommand(started.$, 'arl-implement', 'plan.md')
  await settle()
  expect(started.w.submitted).toEqual([`Implement the plan.\n\n${NAMES_LINE}`])

  const resumed = fakeEngine({ engine: { resume: { ok: true, run: summary(), abandoned_review: false, context: 'The run.' }, 'next-action': { ...CONTINUE, compact: true } } })
  await runCommand(resumed.$, 'arl-resume', '')
  await settle()
  expect(resumed.w.compacted).toHaveLength(1)
  expect(resumed.w.submitted).toHaveLength(1)
  expect(resumed.w.submitted[0]?.split(NAMES_LINE)).toHaveLength(2)

  const compacted = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } } })
  await continueRun(compacted.$)
  expect(compacted.w.submitted).toEqual([`${CONTINUE.prompt}\n\n${NAMES_LINE}`])

  // A continuation on the same conversation keeps what it was told: no line added.
  const plain = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': CONTINUE } })
  await continueRun(plain.$)
  expect(plain.w.submitted).toEqual([CONTINUE.prompt])
})

test('the tools\' full names come from the host\'s registration', async ($, on) => {
  const w = world(on, { engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } }, toolServer: 'renamed' })
  await start($, true)
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  expect(w.submitted).toEqual([`${CONTINUE.prompt}\n\nThe loop's tools, by their full names: set_phases is mcp__renamed__set_phases, add_root is mcp__renamed__add_root, review is mcp__renamed__review.`])
})

test('/arl-implement --model passes the run its model', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { init: { ok: true, run: summary(), archived: '', prompt: 'Implement the plan.' } } })
  await runCommand($, 'arl-implement', '--model weak plan.md')
  expect(w.calls[0]?.args).toEqual(['init', '--plan', 'plan.md', '--model', 'weak'])
  await settle()
})

test('/arl-model reads which rounds and the model, and refuses anything else', () => {
  expect(modelArgs('')).toEqual([])
  expect(modelArgs('weak')).toEqual(['set', 'all', 'weak'])
  expect(modelArgs('first opus')).toEqual(['set', 'first', 'opus'])
  expect(modelArgs('late default')).toEqual(['set', 'late', 'default'])
  expect(modelArgs('reset')).toEqual(['reset'])
  expect(modelArgs('reset late')).toEqual(['reset', 'late'])
  expect(modelArgs('reset sometimes')).toContain('Usage')
  expect(modelArgs('weak strong')).toContain('Usage')
  expect(modelArgs('first')).toContain('Usage')
  expect(modelArgs('all weak extra')).toContain('Usage')
})

test('/arl-model sets the run model, says when it applies and passes on the warnings', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { model: { ok: true, text: 'Reviewer model for this run (subagent):', overlay: {}, warnings: ['ARL_MODEL is set, so it beats this run\'s model'] } } })
  const text = await runCommand($, 'arl-model', 'first weak')
  expect(w.calls[0]?.args).toEqual(['model', 'set', 'first', 'weak'])
  expect(text).toContain('Reviewer model for this run')
  expect(text).toContain('from the next review on')
  expect(text).toContain("Note: ARL_MODEL is set, so it beats this run's model.")
  expect(await runCommand($, 'arl-model', '')).not.toContain('next review')
  expect(w.calls[1]?.args).toEqual(['model'])
})

test('/arl-model passes on a refusal', async () => {
  at()
  const { $ } = fakeEngine({ engine: { model: { ok: false, code: 'bad-model', error: 'the subagent reviewer runs on one of the aliases' } } })
  expect(await runCommand($, 'arl-model', 'claude-opus-5-5')).toContain('bad-model')
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

test('/arl-resume tells Claude what the run is: a new session knows nothing of it', async () => {
  at()
  const context = 'This is the adversarial-review-loop run r1 on the plan /work/plan.md (a copy is at /work/.arl/plan.md)'
  const { $, w } = fakeEngine({ engine: { resume: { ok: true, run: summary(), abandoned_review: false, context }, 'next-action': CONTINUE } })
  await runCommand($, 'arl-resume', '')
  await settle()
  expect(w.submitted[0]).toContain(context)
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
  const unknown = summary({ roots: [{ alias: 'web', path: '/elsewhere/web', kind: 'dir', baseline: 'unknown', candidates: [] }] })
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
  expect(w.calls.find(call => call.args[0] === 'root-baseline')?.args).toEqual(['root-baseline', 'web', '--empty', '--run', 'r1'])
  await settle()
  expect(w.submitted).toHaveLength(1)
})

test('/arl-status answers the run as several lines of its own text, and opens the pane', async () => {
  // Measured live: a log line is one line, and its newlines drew as stray characters.
  at(true)
  const run = summary({ approved_phases: 1, phase: 2, rounds: 1, roots: [{ alias: 'web', path: '/w', kind: 'git', baseline: 'unknown', candidates: [] }] })
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run } } })
  const shown = String(await runCommand($, 'arl-status', ''))
  expect(w.logged).toEqual([])
  expect(shown).toContain('✓ 1. one\n')
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

test('/arl-report shows the report in a pane of its own, which scrolls, never in Claude\'s context', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { report: { ok: true, text: '# Report 1\nVERDICT APPROVED', reports: [] } } })
  expect(await runCommand($, 'arl-report', '1')).toBe('Report 1 is in the report pane.')
  expect(w.state.get('adversarial-review-loop/report')).toBe('# Report 1\nVERDICT APPROVED')
  expect(w.opened).toEqual(['arl-report'])
  // A dialog: it takes the keys, so it scrolls, and Escape closes it.
  expect(w.openArgs.get('arl-report')).toEqual(expect.objectContaining({ focus: true, closeOnEscape: true }))
  expect(w.logged).toEqual([])
})

test('/arl-stats shows the statistics in the report pane, never in Claude\'s context', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { stats: { ok: true, text: 'Run r1: 2 recorded round(s).', stats: {} } } })
  expect(await runCommand($, 'arl-stats', '')).toBe('The run statistics are in the report pane.')
  expect(w.calls[0]?.args).toEqual(['stats'])
  expect(w.state.get('adversarial-review-loop/report')).toBe('Run r1: 2 recorded round(s).')
  expect(w.opened).toEqual(['arl-report'])
  expect(w.openArgs.get('arl-report')).toEqual(expect.objectContaining({ title: 'Review statistics', focus: true, closeOnEscape: true }))
  expect(w.logged).toEqual([])
})

test('/arl-stats headless answers the engine text, or why there is none', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { stats: { ok: true, text: 'Run r1: 2 recorded round(s).', stats: {} } } })
  expect(await runCommand($, 'arl-stats', '')).toBe('Run r1: 2 recorded round(s).')
  expect(w.opened).toEqual([])
  const none = fakeEngine({ engine: { stats: { ok: false, code: 'no-run', error: 'there is no run here' } } })
  expect(await runCommand(none.$, 'arl-stats', '')).toContain('there is no run here')
})

test('/arl-pane closes the open review pane, and the loop leaves it closed until it is asked for', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() } } })
  expect(await runCommand($, 'arl-pane', '')).toBe('The review pane is open.')
  expect(await runCommand($, 'arl-pane', '')).toContain('The review pane is closed')
  expect(w.closed).toEqual(['arl'])
  // What the loop opens by itself (a session's start, /arl-implement, /arl-resume) stays shut.
  await openPane($)
  expect(w.opened).toEqual(['arl'])
  // Asked for again, it opens.
  await runCommand($, 'arl-status', '')
  expect(w.opened).toEqual(['arl', 'arl'])
  await openPane($)
  expect(w.opened).toEqual(['arl', 'arl', 'arl'])
})

test('/arl-pane headless says there is none', async () => {
  at(false)
  const { $, w } = fakeEngine({ engine: {} })
  expect(await runCommand($, 'arl-pane', '')).toContain('headless')
  expect(w.opened).toEqual([])
})

test('/arl-resume opens the review pane: a conversation back from the background starts no session', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { resume: { ok: true, run: summary() }, 'next-action': { ok: true, action: 'yield', reason: 'waiting' } } })
  await runCommand($, 'arl-resume', '')
  expect(w.opened).toEqual(['arl'])
})

test('/arl-phases lists every phase to the person', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: { ...summary(), phases: ['one', 'two'], phase_count: 2 } } } })
  expect(await runCommand($, 'arl-phases', '')).toBe('▶ 1. one  (round 1)\n· 2. two')
  expect(w.logged).toEqual([])
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

test('/arl-accept of the last stage completes the run and prompts nothing when the engine says nothing', async () => {
  at()
  const { $, w } = fakeEngine({ engine: { accept: { ok: true, run: summary({ stage: 'done', status: 'complete' }) } } })
  expect(await runCommand($, 'arl-accept', '')).toBe('Accepted. The run is complete.')
  expect(w.notified.join('\n')).toContain('the run is complete')
  await settle()
  expect(w.commands()).toEqual(['accept'])
  expect(w.submitted).toEqual([])
})

// Measured live: told nothing, Claude waited for the loop to ask for the plan's after-the-run step.
test('/arl-accept of the last stage tells Claude the run is complete', async () => {
  at()
  const next = 'Every stage is approved: the run is complete, and the loop asks for nothing more.'
  const { $, w } = fakeEngine({ engine: { accept: { ok: true, next, run: summary({ stage: 'done', status: 'complete' }) } } })
  expect(await runCommand($, 'arl-accept', '')).toBe('Accepted. The run is complete.')
  await settle()
  expect(w.commands()).toEqual(['accept'])
  expect(w.submitted).toEqual([`The user accepted the last stage as it stands. ${next}`])
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
  expect(await runCommand($, 'arl-config', '')).toBe('every key')
  expect(w.logged).toEqual([])
  expect(w.registered.agents).toEqual([])
})

test('/arl-remove-root drops the root it names, guarded by what the engine resolved', async () => {
  at()
  const { $, w } = fakeEngine({
    engine: {
      'root-remove': (call: { args: string[] }) =>
        call.args.includes('--dry-run')
          ? { ok: true, alias: 'web', path: '/elsewhere/web', removing: ['web'], run_id: 'r1' }
          : { ok: true, alias: 'web', path: '/elsewhere/web', removed: ['web'], run: summary() },
      'next-action': CONTINUE,
    },
  })
  const text = await runCommand($, 'arl-remove-root', 'web')
  expect(text).toContain('Removed root web (/elsewhere/web)')
  // The root may be what held the loop: it goes on.
  expect(text).toContain('Claude continues the run.')
  expect(w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args)).toEqual([
    ['root-remove', 'web', '--with-nested', '--dry-run'],
    ['root-remove', 'web', '--with-nested', '--expect', 'web', '--run', 'r1'],
  ])
  // Nothing asked: one root, nothing inside it.
  expect(w.asked).toEqual([])
  await settle()
  expect(w.submitted).toEqual([CONTINUE.prompt])
  expect(await runCommand($, 'arl-remove-root', ' ')).toContain('Usage')
})

test('/arl-remove-root whose root gained a root inside since the dry run removes nothing', async () => {
  at()
  const { $, w } = fakeEngine({
    engine: {
      'root-remove': (call: { args: string[] }) =>
        call.args.includes('--dry-run')
          ? { ok: true, alias: 'web', path: '/elsewhere/web', removing: ['web'], run_id: 'r1' }
          : { ok: false, code: 'removal-changed', error: "removing 'web' with what it holds now removes web, clone, not web: nothing was removed" },
    },
  })
  const text = await runCommand($, 'arl-remove-root', 'web')
  expect(text).toContain('removal-changed')
  expect(text).not.toContain('Removed')
  expect(w.commands()).not.toContain('next-action')
})

// --------------------------------------------------------------------------
// Removing roots that are not the plan's work
// --------------------------------------------------------------------------

const WORK = { alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', candidates: [] }
const SCRATCH = { alias: 'scratchpad', path: '/tmp/s/scratchpad', kind: 'dir', baseline: 'set', candidates: [] }
const CLONE = { alias: 'clone', path: '/tmp/s/scratchpad/clone', kind: 'git', baseline: 'unknown', candidates: [{ commit: 'abc', why: 'clone' }] }
const CLONE2 = { alias: 'clone2', path: '/tmp/s/scratchpad/clone2', kind: 'git', baseline: 'unknown', candidates: [] }
const REMOVE = "Remove scratchpad, clone, clone2 (not the plan's work)"

/** A status that shows `before` until `root-remove` ran, then `after`; and that removal's answer. */
function removal(before: unknown[], after: unknown[], answer: Record<string, unknown>): { status: () => unknown; 'root-remove': () => unknown } {
  let removed = false
  return {
    status: () => ({ ok: true, run: summary({ roots: removed ? after : before }) }),
    'root-remove': () => {
      removed = true
      return answer
    },
  }
}

test('the baseline question offers to remove the outermost root holding a late one, with every root inside, in one call', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: removal([WORK, SCRATCH, CLONE, CLONE2], [WORK], { ok: true, alias: 'scratchpad', path: SCRATCH.path, removed: ['scratchpad', 'clone', 'clone2'], run: summary() }),
    choices: [REMOVE],
  })
  const chosen = await chooseBaselines($)
  expect(chosen.settled).toBe(true)
  expect(chosen.removed).toEqual(['scratchpad', 'clone', 'clone2'])
  expect([...chosen.outcomes]).toEqual([['scratchpad', 'removed'], ['clone', 'removed'], ['clone2', 'removed']])
  // A nested root's option names its container and itself; nothing is asked about the sibling.
  expect(w.asked).toHaveLength(1)
  expect(w.asked[0]?.options).toEqual(['Changes since abc (as cloned)', 'Everything here is new', 'The current content is the baseline', REMOVE])
  // Guarded by the set shown and by the run it was shown for.
  expect(w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args)).toEqual([['root-remove', 'scratchpad', '--with-nested', '--expect', 'scratchpad,clone,clone2', '--expect-baselines', 'scratchpad=set,clone=unknown,clone2=unknown', '--run', 'r1']])
})

test('a removal whose set changed while the question was open is asked again with the new set', async () => {
  at(true)
  const late = { alias: 'late', path: '/tmp/s/scratchpad/late', kind: 'git', baseline: 'unknown', candidates: [] }
  let removals = 0
  let statuses = 0
  const { $, w } = fakeEngine({
    engine: {
      status: () => ({ ok: true, run: summary({ roots: removals === 2 ? [WORK] : ++statuses === 1 ? [WORK, SCRATCH, CLONE] : [WORK, SCRATCH, CLONE, late] }) }),
      'root-remove': () =>
        ++removals === 1
          ? { ok: false, code: 'removal-changed', error: 'now removes scratchpad, clone, late' }
          : { ok: true, alias: 'scratchpad', removed: ['scratchpad', 'clone', 'late'], run: summary() },
    },
    choices: ["Remove scratchpad, clone (not the plan's work)", "Remove scratchpad, clone, late (not the plan's work)"],
  })
  expect((await chooseBaselines($)).removed).toEqual(['scratchpad', 'clone', 'late'])
  const expected = w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args[call.args.indexOf('--expect') + 1])
  expect(expected).toEqual(['scratchpad,clone', 'scratchpad,clone,late'])
})

test('no removal is offered when it would leave the run no root, and a question that fails is logged', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary({ roots: [SCRATCH, CLONE] }) } }, choices: [] })
  const chosen = await chooseBaselines($)
  expect([chosen.settled, chosen.removed, [...chosen.outcomes]]).toEqual([false, [], [['clone', 'unchosen']]])
  expect(w.asked[0]?.options.some(option => option.startsWith('Remove'))).toBe(false)
  expect(w.logged.join('\n')).toContain('Could not ask for the baseline of root clone')
  expect(w.commands()).not.toContain('root-remove')
})

test('a status the engine cannot answer settles nothing', async () => {
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: false, code: 'state-corrupt', error: 'bad state' } } })
  const chosen = await chooseBaselines($)
  expect([chosen.settled, [...chosen.outcomes]]).toEqual([false, []])
  expect(w.asked).toEqual([])
})

test('/arl-resume that removes a root at the baseline question tells Claude the roots as they are now', async () => {
  at(true)
  let removed = false
  let nexts = 0
  const { $, w } = fakeEngine({
    engine: {
      resume: { ok: true, run: summary(), abandoned_review: false, context: `Workspace roots: work = ${LAUNCH}; scratchpad = ${SCRATCH.path}; clone = ${CLONE.path}.` },
      'next-action': () => (++nexts === 1 ? { ok: true, action: 'yield', reason: 'baseline-unknown' } : CONTINUE),
      status: (call: { args: string[] }) => ({
        ok: true,
        run: summary({ roots: removed ? [WORK] : [WORK, SCRATCH, CLONE] }),
        ...(call.args.includes('--context') ? { context: `Workspace roots: work = ${LAUNCH}.` } : {}),
      }),
      'root-remove': () => {
        removed = true
        return { ok: true, alias: 'scratchpad', removed: ['scratchpad', 'clone'], run: summary() }
      },
    },
    choices: ["Remove scratchpad, clone (not the plan's work)"],
  })
  await runCommand($, 'arl-resume', '')
  await settle()
  expect(w.submitted).toHaveLength(1)
  expect(w.submitted[0]).toContain('The user removed roots scratchpad, clone from the run')
  expect(w.submitted[0]).toContain(`Workspace roots: work = ${LAUNCH}.`)
  expect(w.submitted[0]).not.toContain('clone = ')
  expect(w.submitted[0]).toContain(CONTINUE.prompt)
})

test('the baseline question sends the choice for the run it showed', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: { ...removal([WORK, CLONE], [WORK], { ok: true }), 'root-baseline': { ok: true, run: summary() } },
    choices: ['The current content is the baseline'],
  })
  await chooseBaselines($)
  expect(w.calls.find(call => call.args[0] === 'root-baseline')?.args).toEqual(['root-baseline', 'clone', '--current', '--run', 'r1'])
})

test('/arl-remove-root on a root that holds others removes them together, on confirmation, and continues once', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: {
      'root-remove': (call: { args: string[] }) =>
        call.args.includes('--dry-run')
          ? { ok: true, alias: 'scratchpad', path: SCRATCH.path, removing: ['scratchpad', 'clone'], run_id: 'r1' }
          : { ok: true, alias: 'scratchpad', path: SCRATCH.path, removed: ['scratchpad', 'clone'], run: summary() },
      'next-action': CONTINUE,
    },
    choices: ['Remove scratchpad, clone'],
  })
  // A relative path: the engine resolves it, as the removal will.
  const text = await runCommand($, 'arl-remove-root', '../scratchpad')
  expect(text).toContain('Removed roots scratchpad, clone')
  expect(text).toContain('Claude continues the run.')
  expect(w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args)).toEqual([
    ['root-remove', '../scratchpad', '--with-nested', '--dry-run'],
    ['root-remove', 'scratchpad', '--with-nested', '--expect', 'scratchpad,clone', '--run', 'r1'],
  ])
  expect(w.commands().filter(command => command === 'next-action')).toHaveLength(1)
  await settle()
  expect(w.submitted).toEqual([CONTINUE.prompt])
})

test('/arl-remove-root on a root that holds others removes nothing when declined, or headless', async () => {
  at(true)
  const plan = { ok: true, alias: 'scratchpad', path: SCRATCH.path, removing: ['scratchpad', 'clone'], run_id: 'r1' }
  const declined = fakeEngine({ engine: { 'root-remove': plan }, choices: ['Keep them'] })
  expect(await runCommand(declined.$, 'arl-remove-root', 'scratchpad')).toContain('Nothing removed')
  expect(declined.w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args.includes('--dry-run'))).toEqual([true])
  at(false)
  const headless = fakeEngine({ engine: { 'root-remove': plan } })
  expect(await runCommand(headless.$, 'arl-remove-root', 'scratchpad')).toContain('interactive session to confirm')
  expect(headless.w.calls.filter(call => call.args[0] === 'root-remove').map(call => call.args.includes('--dry-run'))).toEqual([true])
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

test('a turn that ends after Claude wrote files for the next phase is not compacted mid-phase', async () => {
  // Claude went on with the next phase in the approval's turn and ended it before its review:
  // compacting then would summarise the phase's work away.
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } } })
  await run($, ['status'])
  await announce($, { ok: true, label: 'phase1', verdict: 'APPROVED' }, 'Approved.')
  await writeIn($, `${LAUNCH}/src`, async () => ({}), async () => 'written')
  await continueRun($)
  expect(w.compacted).toEqual([])
  expect(w.submitted).toEqual([CONTINUE.prompt])
  // The next review starts the count again.
  await announce($, { ok: true, label: 'phase2', verdict: 'APPROVED' }, 'Approved.')
  await continueRun($)
  expect(w.compacted).toHaveLength(1)
})

test('a scratch write or a refused one after an approval still lets the turn compact', async () => {
  // The plan's notes, a memory file, a write the person refused: none is the next phase's work.
  at(true)
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, 'next-action': { ...CONTINUE, compact: true } } })
  await run($, ['status'])
  await announce($, { ok: true, label: 'phase1', verdict: 'APPROVED' }, 'Approved.')
  await writeIn($, '/home/user/.claude/plans', async () => ({}), async () => 'written')
  await writeIn($, `${LAUNCH}/src`, async () => ({}), async () => 'refused', written => written !== 'refused')
  await continueRun($)
  expect(w.compacted).toHaveLength(1)
})

test('/arl-accept after Claude wrote the stage\'s files keeps the compaction that approval is due', async () => {
  at(true)
  let asks = 0
  const next = (): unknown => (++asks === 1 ? { ok: true, action: 'yield', reason: 'baseline-unknown' } : { ...CONTINUE, compact: true })
  const { $, w } = fakeEngine({ engine: { status: { ok: true, run: summary() }, accept: { ok: true, run: summary(), next: '' }, 'next-action': next } })
  await run($, ['status'])
  await writeIn($, `${LAUNCH}/src`, async () => ({}), async () => 'written')
  await runCommand($, 'arl-accept', '')
  await continueRun($)
  expect(w.compacted).toHaveLength(1)
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
  expect(w.logged.join('\n')).toContain('Could not compact')
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
  expect(w.logged).toEqual(['phase1 APPROVED'])
  expect(w.notified.join('\n')).toContain('the run is complete')
})

const ADDED = { ok: true, status: 'added', alias: 'web', baseline: 'set', candidates: [] }
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
  // Undo, by path: the engine drops it only while unchanged, and moves nothing else.
  expect(w.calls.find(call => call.args[0] === 'root-remove')?.args).toEqual(['root-remove', '/elsewhere/web', '--undo'])
})

test('a write that runs keeps its root: the engine refuses to drop a folder that changed', async ($, on) => {
  const w = world(on, {
    engine: { status: STATUS, 'root-add': ADDED, 'root-remove': { ok: false, code: 'kept', error: "root 'web' changed since it joined; it stays" } },
    check: 'allow',
    writeOutcome: 'run',
  })
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  expect(result.context?.join('\n')).toContain('registered /elsewhere/web as root web')
  expect(w.commands()).toContain('root-remove')
})

test('a write refused in a shape that says nothing still gives its root back', async ($, on) => {
  // Measured live: the person's "No" in the write dialog reached the hook as neither `deny` nor
  // `isError`, and the folder stayed a root of the run although nothing was written.
  const w = world(on, { engine: { status: STATUS, 'root-add': ADDED, 'root-remove': { ok: true, alias: 'web' } }, check: 'allow', writeOutcome: 'run' })
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  expect(w.commands()).toContain('root-remove')
  expect(result.context?.join('\n') ?? '').not.toContain('registered /elsewhere/web')
})

test('a write whose refusal ends the call with an error still gives its root back', async ($, on) => {
  // Measured live: after "No" in the write dialog nothing after the write ran in the hook.
  const w = world(on, { engine: { status: STATUS, 'root-add': ADDED, 'root-remove': { ok: true, alias: 'web' } }, check: 'allow', writeOutcome: 'throw' })
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' }).catch(() => undefined)
  expect(w.commands()).toContain('root-remove')
})

test('a root whose undo got no engine answer is offered back again when the turn ends', async ($, on) => {
  // Measured live: the person's "No" aborts the write's dispatch, and the undo's engine call with it.
  let removes = 0
  const remove = (): unknown => (++removes === 1 ? { ok: false, code: 'engine', error: 'the engine could not run: aborted' } : { ok: true, alias: 'web' })
  const w = world(on, {
    engine: { status: STATUS, 'root-add': ADDED, 'root-remove': remove, 'next-action': { ok: true, action: 'yield', reason: 'unchanged' } },
    check: 'allow',
    writeOutcome: 'deny',
  })
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  expect(removes).toBe(1)
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  expect(removes).toBe(2)
  expect(w.commands().filter(command => command === 'root-remove')).toHaveLength(2)
  // Offered back by its path: the alias may name another folder by then.
  expect(w.calls.filter(call => call.args[0] === 'root-remove').at(-1)?.args).toEqual(['root-remove', '/elsewhere/web', '--undo'])
})

test('a write still waiting when the main turn ends keeps the root it registered', async ($, on) => {
  // A background subagent's write can sit in its dialog past the main turn's end: offering its
  // root back then would drop the folder before the write lands, and nobody would review it.
  let release: () => void = () => undefined
  const waiting = new Promise<void>(resolve => {
    release = resolve
  })
  const w = world(on, {
    engine: { status: STATUS, 'root-add': ADDED, 'root-remove': { ok: false, code: 'kept', error: 'changed' }, 'next-action': { ok: true, action: 'yield', reason: 'unchanged' } },
    check: 'allow',
  })
  on('tool.call', { tool: 'Write' }, async () => {
    await waiting
    return { result: 'written' } as never
  })
  await start($)
  const write = $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x', agentId: 'sub-1' })
  await settle()
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  expect(w.commands()).not.toContain('root-remove')
  release()
  await write
  expect(w.commands()).toContain('root-remove')
})

test('two writes into one new folder, the first refused, keep the root the second relies on', async ($, on) => {
  // The first write's root is offered back only once no write into it is running: dropped while
  // the second waited in its dialog, the second would land outside every root.
  const gates: Record<string, () => void> = {}
  const gate = (name: string): Promise<void> =>
    new Promise<void>(resolve => {
      gates[name] = resolve
    })
  let adds = 0
  const w = world(on, {
    engine: {
      status: STATUS,
      'root-add': () => (++adds === 1 ? ADDED : { ok: true, status: 'known', alias: 'web', baseline: 'set' }),
      'root-remove': { ok: false, code: 'kept', error: "root 'web' changed since it joined; it stays" },
      'next-action': { ok: true, action: 'yield', reason: 'unchanged' },
    },
    check: 'allow',
  })
  on('tool.call', { tool: 'Write' }, async (_, e) => {
    const file = String((e as { file_path?: unknown }).file_path)
    await gate(file)
    return (file.endsWith('a.ts') ? { deny: 'The user refused the write.' } : { result: 'written' }) as never
  })
  await start($)
  const first = $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  await settle()
  const second = $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/b.ts', content: 'y' })
  await settle()
  gates['/elsewhere/web/a.ts']?.()
  await first
  expect(w.commands()).not.toContain('root-remove')
  gates['/elsewhere/web/b.ts']?.()
  await second
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  // Offered back at the turn's end; the engine keeps it, since the second write changed it.
  expect(w.commands().filter(command => command === 'root-remove')).toHaveLength(1)
})

test('a root add_root names again is not offered back at the turn\'s end', async ($, on) => {
  const w = world(on, {
    engine: {
      status: STATUS,
      'root-add': () => (w.commands().includes('root-remove') ? { ok: true, status: 'known', alias: 'web', baseline: 'set' } : ADDED),
      'root-remove': { ok: false, code: 'engine', error: 'the engine could not run: aborted' },
      'next-action': { ok: true, action: 'yield', reason: 'unchanged' },
    },
    check: 'allow',
    writeOutcome: 'deny',
  })
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x' })
  const again = await callTool($, 'add_root', { path: '/elsewhere/web' })
  expect(again).toContain('already covered by root web')
  expect(again).toContain('ask them to run /arl-remove-root web')
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  expect(w.commands().filter(command => command === 'root-remove')).toHaveLength(1)
})

test('a root add_root names while the write that registered it runs stays when that write is refused', async ($, on) => {
  let release: () => void = () => undefined
  const waiting = new Promise<void>(resolve => {
    release = resolve
  })
  const w = world(on, {
    engine: {
      status: STATUS,
      'root-add': () => (w.commands().filter(command => command === 'root-add').length > 1 ? { ok: true, status: 'known', alias: 'web', baseline: 'set' } : ADDED),
      'root-remove': { ok: true, alias: 'web' },
      'next-action': { ok: true, action: 'yield', reason: 'unchanged' },
    },
    check: 'allow',
  })
  on('tool.call', { tool: 'Write' }, async () => {
    await waiting
    return { deny: 'The user refused the write.' } as never
  })
  await start($)
  const write = $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/a.ts', content: 'x', agentId: 'sub-1' })
  await settle()
  await callTool($, 'add_root', { path: '/elsewhere/web' })
  release()
  await write
  await $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1' } as never)
  expect(w.commands()).not.toContain('root-remove')
})

test('/arl-resume that cannot continue still tells Claude what the run is', async () => {
  at()
  const context = 'This is the adversarial-review-loop run r1 on the plan /work/plan.md'
  const { $ } = fakeEngine({ engine: { resume: { ok: true, run: summary(), context }, 'next-action': { ok: true, action: 'yield', reason: 'waiting' } } })
  const text = String(await runCommand($, 'arl-resume', ''))
  expect(text).toContain('does not continue now (waiting)')
  expect(text).toContain(context)
})

test('the baseline question offers every commit it can fit, and the rest can be typed', () => {
  const root = { alias: 'clone', path: '/c', kind: 'git', baseline: 'unknown', candidates: [{ commit: 'a'.repeat(40), why: 'checkout' }, { commit: 'b'.repeat(40), why: 'clone' }] }
  const full = baselineQuestion(root, 'Remove clone')
  expect(full.options).toEqual([`Changes since ${'a'.repeat(12)} (after the clone's checkout; a guess)`, `Changes since ${'b'.repeat(12)} (as cloned)`, 'The current content is the baseline', 'Remove clone'])
  expect(full.question).toContain("Or type a commit to compare against, or 'empty' for everything new.")
  expect(full.choose(full.options[1] ?? '')).toEqual({ kind: 'commit', commit: 'b'.repeat(40) })
  expect(full.choose(' Empty ')).toEqual({ kind: 'empty' })
  expect(full.choose('Remove clone')).toEqual({ kind: 'remove' })
  expect(full.choose(' 1234abcd ')).toEqual({ kind: 'commit', commit: '1234abcd', typed: true })
  expect(full.choose('')).toBeUndefined()

  const roomy = baselineQuestion(root, '')
  expect(roomy.options).toEqual([...full.options.slice(0, 2), 'Everything here is new', 'The current content is the baseline'])
  expect(roomy.question).not.toContain("'empty'")
  expect(roomy.choose('remove')).toEqual({ kind: 'commit', commit: 'remove', typed: true })

  const folder = baselineQuestion({ alias: 'web', path: '/w', kind: 'dir', baseline: 'unknown', candidates: [] }, '')
  expect(folder.question).not.toContain('Or type')
  expect(folder.choose('abc123')).toEqual({ kind: 'not-git', text: 'abc123' })

  const suggested = baselineQuestion({ ...root, suggested: 'd'.repeat(40) }, 'Remove clone')
  expect(suggested.options.slice(0, 3)).toEqual([
    `Changes since ${'d'.repeat(12)} (Claude's suggestion)`,
    `Changes since ${'a'.repeat(12)} (after the clone's checkout; a guess)`,
    `Changes since ${'b'.repeat(12)} (as cloned)`,
  ])
  expect(suggested.question).toContain("'current' for its current content")

  // A suggestion of a commit the engine offers too is listed once, as Claude's.
  const same = baselineQuestion({ ...root, suggested: 'b'.repeat(40) }, '')
  expect(same.options.slice(0, 2)).toEqual([`Changes since ${'b'.repeat(12)} (Claude's suggestion)`, `Changes since ${'a'.repeat(12)} (after the clone's checkout; a guess)`])

  const twice = baselineQuestion({ ...root, candidates: [{ commit: 'c'.repeat(40), why: 'start' }, { commit: 'c'.repeat(40), why: 'clone' }] }, '')
  expect(twice.options).toEqual([`Changes since ${'c'.repeat(12)} (when the run started)`, 'Everything here is new', 'The current content is the baseline'])
})

test('a commit typed at the baseline question goes to the engine as one argument, and a refused one is asked again', async () => {
  at(true)
  let tries = 0
  const { $, w } = fakeEngine({
    engine: {
      ...removal([WORK, CLONE], [WORK], { ok: true }),
      'root-baseline': () => (++tries === 1 ? { ok: false, code: 'bad-commit', error: "'nope' does not name a commit in 'clone'" } : { ok: true, run: summary(), note: 'clone: commit 55f8ddf is not in the history of HEAD.' }),
    },
    choices: ['nope', '--empty'],
  })
  const chosen = await chooseBaselines($)
  expect([...chosen.outcomes]).toEqual([['clone', 'chosen']])
  expect(w.calls.filter(call => call.args[0] === 'root-baseline').map(call => call.args)).toEqual([
    ['root-baseline', 'clone', '--commit=nope', '--run', 'r1'],
    ['root-baseline', 'clone', '--commit=--empty', '--run', 'r1'],
  ])
  expect(w.asked).toHaveLength(2)
  expect(w.logged.join('\n')).toContain("Not a commit in clone: 'nope' does not name a commit in 'clone'")
  expect(w.logged.join('\n')).toContain('not in the history of HEAD')
})

test('three refused typed commits end the question unchosen', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: { ...removal([WORK, CLONE], [WORK], { ok: true }), 'root-baseline': { ok: false, code: 'bad-commit', error: 'no' } },
    choices: ['a', 'b', 'c', 'd'],
  })
  const chosen = await chooseBaselines($)
  expect([chosen.settled, [...chosen.outcomes]]).toEqual([false, [['clone', 'unchosen']]])
  expect(w.asked).toHaveLength(3)
})

test('text typed for a folder root never reaches the engine as a commit', async () => {
  at(true)
  const web = { alias: 'web', path: '/tmp/s/web', kind: 'dir', baseline: 'unknown', candidates: [] }
  const { $, w } = fakeEngine({
    engine: { ...removal([WORK, web], [WORK], { ok: true }), 'root-baseline': { ok: true, run: summary() } },
    choices: ['abc123', 'empty'],
  })
  const chosen = await chooseBaselines($)
  expect([...chosen.outcomes]).toEqual([['web', 'chosen']])
  expect(w.calls.filter(call => call.args[0] === 'root-baseline').map(call => call.args)).toEqual([['root-baseline', 'web', '--empty', '--run', 'r1']])
  expect(w.logged.join('\n')).toContain('type a commit only for a git repository')
})

test('a baseline set elsewhere while its question was open counts as chosen, and the next root is asked', async () => {
  at(true)
  const { $, w } = fakeEngine({
    engine: {
      ...removal([WORK, SCRATCH, CLONE, CLONE2], [WORK], { ok: true }),
      'root-baseline': (call: { args: string[] }) => (call.args[1] === 'clone' ? { ok: false, code: 'baseline-set', error: "root 'clone' already has a baseline" } : { ok: true, run: summary() }),
    },
    choices: ['The current content is the baseline', 'The current content is the baseline'],
  })
  const chosen = await chooseBaselines($)
  expect([chosen.settled, [...chosen.outcomes]]).toEqual([true, [['clone', 'chosen'], ['clone2', 'chosen']]])
  expect(w.asked).toHaveLength(2)
})

test('a removal refused because a baseline inside was set meanwhile asks again, saying so in the option', async () => {
  at(true)
  let statuses = 0
  const clone2Set = { ...CLONE2, baseline: 'set' }
  const { $, w } = fakeEngine({
    engine: {
      status: () => ({ ok: true, run: summary({ roots: ++statuses === 1 ? [WORK, SCRATCH, CLONE, CLONE2] : [WORK, SCRATCH, CLONE, clone2Set] }) }),
      'root-remove': { ok: false, code: 'removal-changed', error: 'the baselines of clone2 are not what was shown: nothing was removed' },
    },
    choices: [REMOVE, 'Something else'],
  })
  await chooseBaselines($)
  expect(w.asked).toHaveLength(2)
  expect(w.asked[1]?.options).toContain("Remove scratchpad, clone, clone2 (not the plan's work; clone2 already has a baseline)")
  expect(w.asked[1]?.options).not.toContain(REMOVE)
  expect(w.logged.join('\n')).toContain('the baselines of clone2 are not what was shown')
})

test('the baseline questions stop when the run is replaced meanwhile, whatever its roots are called', async () => {
  at(true)
  let statuses = 0
  const { $, w } = fakeEngine({
    engine: {
      status: () => ({ ok: true, run: summary({ run_id: ++statuses === 1 ? 'r1' : 'r2', roots: [WORK, CLONE] }) }),
      'root-baseline': { ok: false, code: 'baseline-set', error: 'already' },
    },
    choices: ['The current content is the baseline', 'The current content is the baseline'],
  })
  const chosen = await chooseBaselines($)
  expect(chosen.settled).toBe(false)
  expect(w.asked).toHaveLength(1)
  expect(w.logged.join('\n')).toContain('The run changed while its baselines were asked')
})

test('a root another session removed while it was asked about is not reported unresolved', async () => {
  at(true)
  let statuses = 0
  const { $, w } = fakeEngine({
    engine: {
      status: () => ({ ok: true, run: summary({ roots: ++statuses === 1 ? [WORK, CLONE] : [WORK] }) }),
      'root-baseline': { ok: false, code: 'unknown-root', error: "no root named 'clone'" },
    },
    choices: ['The current content is the baseline'],
  })
  const chosen = await chooseBaselines($)
  expect(chosen.settled).toBe(true)
  expect(w.asked).toHaveLength(1)
})

test('a removal whose target another session removed already reads the roots again', async () => {
  at(true)
  let statuses = 0
  const { $ } = fakeEngine({
    engine: {
      status: () => ({ ok: true, run: summary({ roots: ++statuses === 1 ? [WORK, SCRATCH, CLONE, CLONE2] : [WORK] }) }),
      'root-remove': { ok: false, code: 'unknown-root', error: "no root named or at 'scratchpad'" },
    },
    choices: [REMOVE],
  })
  expect((await chooseBaselines($)).settled).toBe(true)
})
