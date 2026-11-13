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
import { implementArgs, openPane, runCommand } from '../../hooks/commands'
import { pane } from '../../hooks/ui'
import { announce, continueRun, resetWrites, reviewWith, run, session, writeIn } from '../../hooks/flow'
import { callTool, fakeEngine, LAUNCH, PREPARED, PROMPTS, RECORDED, reviewer, settle, start, summary, world } from './world'

function at(isInteractive = false): void {
  session.dir = LAUNCH
  session.id = 'session-1'
  session.isInteractive = isInteractive
  // The flows' module is shared by every test: no test sees another's writes.
  resetWrites()
  pane.dismissed = false
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
    'arl-phases',
    'arl-pane',
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

test('/arl-status answers the run as several lines of its own text, and opens the pane', async () => {
  // Measured live: a log line is one line, and its newlines drew as stray characters.
  at(true)
  const run = summary({ approved_phases: 1, phase: 2, rounds: 1, roots: [{ alias: 'web', path: '/w', kind: 'git', baseline: 'unknown', proposed: '' }] })
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
  expect(await runCommand($, 'arl-config', '')).toBe('every key')
  expect(w.logged).toEqual([])
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
  expect(await callTool($, 'add_root', { path: '/elsewhere/web' })).toContain('already covered by root web')
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
