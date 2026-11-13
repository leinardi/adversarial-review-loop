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
import { bandLine, paneLines } from '../../hooks/ui'
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

test('the pane marks approved, current and later phases, the final review, and unknown baselines', () => {
  const lines = paneLines(
    run({ phases: ['one', 'two', 'three'], phase_count: 3, phase: 2, approved_phases: 1, rounds: 1, final_required: true, roots: [{ alias: 'web', path: '/w', kind: 'dir', baseline: 'unknown', proposed: '' }] }),
    null,
    '',
  ).map(line => line.text)
  expect(lines).toContain('  ✓ 1. one')
  expect(lines).toContain('  ▶ 2. two  (round 2)')
  expect(lines).toContain('  · 3. three')
  expect(lines).toContain('  · final review')
  expect(lines).toContain('  web  /w  (dir)  baseline unknown')
})

test('a report the person asked for takes the latest review\'s place in the pane', () => {
  const texts = paneLines(run(), { label: 'phase1', verdict: 'APPROVED', message: 'ok', report: '/r.md' }, '', '# Review 001\nraw').map(line => line.text)
  expect(texts).toContain('# Review 001')
  expect(texts).not.toContain('Latest review: phase1, APPROVED')
})

test('the pane shows the latest review: its verdict, what Claude was told, and the report', () => {
  const lines = paneLines(run(), { label: 'phase1', verdict: 'CHANGES_REQUIRED', message: 'Fix:\n- a.py:3 bug\n', report: '/work/.arl/reports/001.md' }, '')
  const texts = lines.map(line => line.text)
  expect(texts).toContain('Latest review: phase1, CHANGES_REQUIRED')
  expect(texts).toContain('- a.py:3 bug')
  expect(texts.at(-1)).toBe('Report: /work/.arl/reports/001.md')
  expect(lines.find(line => line.text.startsWith('Latest review'))?.color).toBe('error')
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
    expect((await ui.find({ type: 'Text', text: /^Run r1/ }))?.text).toBe('Run r1 · active · phase 1/2: one')
    expect((await ui.find({ type: 'Text', text: /Latest review/ }))?.text).toBe('Latest review: phase1, APPROVED')
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
