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

// The band above the prompt and the review pane: what they say for each state of the run, and
// that they draw what the session's state holds.

import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'
import type { RunView } from '../../types'
import { bandLine, blockingFindings, paneLines, phasesText, statusText } from '../../hooks/ui'
import { PLUGIN, summary } from './world'

const run = (fields: Record<string, unknown> = {}): RunView => summary(fields) as unknown as RunView

test('the band is gone without a run, and for a stopped one', () => {
  expect(bandLine(null, '')).toBeUndefined()
  expect(bandLine(run({ status: 'stopped' }), '')).toBeUndefined()
})

test('the band names the phase, the round and what the loop does', () => {
  expect(bandLine(run(), '')).toBe('adversarial-review-loop · phase 1/2: one · round 1 · working')
  expect(bandLine(run({ rounds: 2 }), 'reviewing phase1, round 3')).toBe('adversarial-review-loop · phase 1/2: one · round 3 · reviewing phase1, round 3')
  expect(bandLine(run({ stage: 'plan', phase: 0, phases: [] }), '')).toBe('adversarial-review-loop · waiting for the phases · working')
  expect(bandLine(run({ stage: 'final', phase: 0 }), '')).toBe('adversarial-review-loop · final review · round 1 · working')
  expect(bandLine(run({ status: 'complete', stage: 'done' }), '')).toBe('adversarial-review-loop · run complete')
})

test('the band says what the run waits on, and how to go on', () => {
  expect(bandLine(run({ status: 'paused' }), '')).toContain('paused: /arl-resume continues it')
  expect(bandLine(run({ waiting: { kind: 'stall' } }), '')).toContain('waits for you (stall): /arl-resume, /arl-accept or /arl-stop')
  expect(bandLine(run({ roots: [{ alias: 'web', path: '/w', kind: 'dir', baseline: 'unknown', proposed: '' }] }), '')).toContain('needs a baseline for web')
  expect(bandLine(run({ pending_review: true }), '')).toContain('a review is pending')
})

test('the pane without a run says how to start one', () => {
  expect(paneLines(null, null, '').map(line => line.text)).toEqual(['No adversarial-review-loop run here. Start one with /arl-implement <plan.md>.'])
})

test('the pane shows the phases around the current one on one line, and unknown baselines', () => {
  const lines = paneLines(
    run({ phases: ['one', 'two', 'three'], phase_count: 3, phase: 2, approved_phases: 1, rounds: 1, final_required: true, roots: [{ alias: 'web', path: '/w', kind: 'dir', baseline: 'unknown', proposed: '' }] }),
    null,
    '',
  ).map(line => line.text)
  expect(lines[0]).toBe('Run r1 · active · phase 2/3 · round 2')
  expect(lines).toContain('  ✓ 1. one  ▶ 2. two  · 3. three  · final review')
  expect(lines).toContain('Roots: web (baseline unknown)')
})

test('a long plan shows only the previous, current and next phase in the pane, and lists them all on request', () => {
  const phases = Array.from({ length: 30 }, (_, index) => `p${index + 1}`)
  const long = run({ phases, phase_count: 30, phase: 12, approved_phases: 11 })
  const lines = paneLines(long, null, '').map(line => line.text)
  expect(lines).toContain('  … ✓ 11. p11  ▶ 12. p12  · 13. p13 …')
  expect(lines.length).toBeLessThan(8)
  const all = phasesText(long).split('\n')
  expect(all).toHaveLength(30)
  expect(all[11]).toBe('▶ 12. p12  (round 1)')
})

test('the pane lists at most three blocking findings of the latest review, one line each', () => {
  const message = [
    'The review of phase 1 asks for changes.',
    '',
    'Blocking findings (actionable, severity >= medium) -- every one must be resolved:',
    '',
    ...[1, 2, 3, 4].map(n => `FINDING severity=high actionable=yes file=api/a.py:${n} | bug ${n}`),
    '',
    'All findings reported (non-blocking ones included, for context):',
    '',
    'FINDING severity=low actionable=no file=api/b.py | nit',
  ].join('\n')
  const lines = paneLines(run(), { label: 'phase1', verdict: 'CHANGES_REQUIRED', message, report: '/r.md' }, '')
  const texts = lines.map(line => line.text)
  expect(texts).toContain('Latest: phase1 CHANGES_REQUIRED · 4 blocking')
  expect(texts).toContain('  high api/a.py:1  bug 1')
  expect(texts).not.toContain('  high api/a.py:4  bug 4')
  expect(texts).toContain('  … 1 more')
  expect(texts.join('\n')).not.toContain('nit')
  expect(texts.join('\n')).not.toContain('/r.md')
  const latest = lines.find(line => line.text.startsWith('Latest'))
  expect(latest?.segments.find(segment => segment.text === 'CHANGES_REQUIRED')?.color).toBe('error')
  const finding = lines.find(line => line.text.startsWith('  high'))
  expect(finding?.segments.find(segment => segment.text === 'high')?.color).toBe('error')
})

test('deferred findings and an approval\'s listed findings are never counted as blocking', () => {
  const deferred = 'Deferred findings -- actionable, at or above block_severity, but new:\n\nFINDING severity=medium actionable=yes file=api/c.py | later\n'
  const approved = `The review of phase 1 approved the change.\n\n${deferred}Full report: /r.md\n`
  expect(blockingFindings(approved, 'APPROVED')).toEqual([])
  const changes = `The review of phase 1 asks for changes.\n\nBlocking findings (actionable, severity >= medium) -- every one must be resolved:\n\nFINDING severity=high actionable=yes file=api/a.py | bug\n\n${deferred}`
  expect(blockingFindings(changes, 'CHANGES_REQUIRED')).toEqual(['high api/a.py  bug'])
  const texts = paneLines(run(), { label: 'phase1', verdict: 'APPROVED', message: approved, report: '/r.md' }, '').map(line => line.text)
  expect(texts).toContain('Latest: phase1 APPROVED')
})

test('phase marks are coloured: approved green, current blue and bold, later grey', () => {
  const lines = paneLines(run({ phases: ['one', 'two', 'three'], phase_count: 3, phase: 2, approved_phases: 1 }), null, '')
  const window = lines.find(line => line.text.includes('▶ 2. two'))?.segments ?? []
  expect(window.find(segment => segment.text === '✓ 1. one')?.color).toBe('success')
  expect(window.find(segment => segment.text === '▶ 2. two')).toEqual({ text: '▶ 2. two', color: 'suggestion', bold: true })
  expect(window.find(segment => segment.text === '· 3. three')?.color).toBe('inactive')
})

test('the final review is drawn after every phase, the elided ones included', () => {
  const phases = Array.from({ length: 10 }, (_, index) => `p${index + 1}`)
  const lines = paneLines(run({ phases, phase_count: 10, phase: 2, approved_phases: 1, final_required: true }), null, '').map(line => line.text)
  expect(lines).toContain('  ✓ 1. p1  ▶ 2. p2  · 3. p3 …  · final review')
})

test('the band says working only while Claude\'s turn runs', () => {
  expect(bandLine(run(), '', false)).toBe('adversarial-review-loop · phase 1/2: one · round 1 · idle')
  expect(bandLine(run(), '', true)).toBe('adversarial-review-loop · phase 1/2: one · round 1 · working')
})

test('/arl-status text holds every phase and every root path', () => {
  const text = statusText(run({ roots: [{ alias: 'web', path: '/w', kind: 'dir', baseline: 'set', proposed: '' }] }))
  expect(text).toContain('▶ 1. one  (round 1)')
  expect(text).toContain('· 2. two')
  expect(text).toContain('  web  /w  (dir)')
})

const stored = (key: string, value: unknown): unknown => (key === 'run' ? { shape: 'run-1', value } : key === 'last' ? { shape: 'last-1', value } : value)

function seed(on: On, values: Record<string, unknown>): void {
  on('state.get', (_, e) => {
    const key = (e as unknown as { key: string }).key
    return { value: { value: key in values ? stored(key, values[key]) : undefined, version: 1 } }
  })
}

test('the band draws the run the session state holds', async ($, on) => {
  seed(on, { run: summary(), activity: 'reviewing phase1, round 1' })
  const ui = await $.ui.mount({
    plugin: PLUGIN,
    component: 'AbovePrompt',
    surface: 'terminal',
    props: { hasSurvey: false, isCollapsed: false, isWorking: true, maxRows: 10 },
  } as never)
  expect((await ui.find({ type: 'Text', text: /phase 1\/2/ }))?.text).toBe('adversarial-review-loop · phase 1/2: one · round 1 · reviewing phase1, round 1')
  await ui.unmount()
})

test('the pane draws the run on every surface', async ($, on) => {
  seed(on, { run: summary(), last: { label: 'phase1', verdict: 'APPROVED', message: 'Approved.', report: '/r.md' } })
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({
      plugin: PLUGIN,
      component: 'Pane',
      requestId: 'arl',
      surface,
      props: { title: 'Adversarial review', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
    } as never)
    expect((await ui.find({ type: 'Text', text: /^Run r1/ }))?.text).toBe('Run r1')
    expect((await ui.find({ type: 'Text', text: /^active$/ }))?.props.color).toBe('suggestion')
    expect((await ui.find({ type: 'Text', text: /^APPROVED$/ }))?.props.color).toBe('success')
    await ui.unmount()
  }
})

test('a run stored by an earlier version is declined: the pane says there is none', async ($, on) => {
  on('state.get', () => ({ value: { value: { shape: 'run-0', value: { status: 'active' } }, version: 1 } }))
  const ui = await $.ui.mount({
    plugin: PLUGIN,
    component: 'Pane',
    requestId: 'arl',
    surface: 'desktop',
    props: { title: 'Adversarial review', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
  } as never)
  expect((await ui.find({ type: 'Text' }))?.text).toContain('No adversarial-review-loop run here')
  await ui.unmount()
})
