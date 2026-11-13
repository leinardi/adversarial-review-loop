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

// What the person sees of the run: the band above the prompt and the review pane. Both draw the
// session's state (`$.state`), which the loop writes from every engine answer; nothing here asks
// the engine anything.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'
import type { LastReview, RunView } from '../types'

export const PANE = 'arl'
export const PANE_TITLE = 'Adversarial review'

// The shape tags decline what an earlier version stored, which outlives a reload: change one when
// its value's fields change.
const runAtom = atom({ plugin: 'adversarial-review-loop', key: 'run' } as const, null, { shape: 'run-1' })
const lastAtom = atom({ plugin: 'adversarial-review-loop', key: 'last' } as const, null, { shape: 'last-1' })
const activityAtom = atom({ plugin: 'adversarial-review-loop', key: 'activity' } as const, '')
const reportAtom = atom({ plugin: 'adversarial-review-loop', key: 'report' } as const, '')

export async function showRun($: EngineInterface, run: RunView | null): Promise<void> {
  await update($, runAtom, () => run)
}

export async function showLast($: EngineInterface, last: LastReview | null): Promise<void> {
  await update($, lastAtom, () => last)
}

export async function showActivity($: EngineInterface, activity: string): Promise<void> {
  await update($, activityAtom, () => activity)
}

/** Show a stored report in the pane, in place of the latest review; empty takes it down. */
export async function showReport($: EngineInterface, report: string): Promise<void> {
  await update($, reportAtom, () => report)
}

/** Where the run stands and its round, in one line: `phase 2/5: title, round 1`. */
export function briefly(run: RunView): string {
  return run.stage === 'phase' || run.stage === 'final' ? `${where(run)}, round ${run.rounds + 1}` : where(run)
}

/** Where the run stands, in a few words: the phase, the final review, or the plan. */
function where(run: RunView): string {
  if (run.stage === 'plan') return 'waiting for the phases'
  if (run.stage === 'final') return 'final review'
  if (run.stage === 'done') return 'every phase approved'
  return `phase ${run.phase}/${run.phase_count}: ${run.phases[run.phase - 1] ?? ''}`
}

/** What the run waits on, when it waits on the person or a pending review. */
function hold(run: RunView): string | undefined {
  if (run.status === 'paused') return 'paused: /arl-resume continues it'
  if (run.waiting !== null) return `waits for you (${run.waiting.kind}): /arl-resume, /arl-accept or /arl-stop`
  const unknown = run.roots.filter(root => root.baseline === 'unknown').map(root => root.alias)
  if (unknown.length > 0) return `needs a baseline for ${unknown.join(', ')}`
  if (run.pending_review) return 'a review is pending (if none runs, /arl-resume abandons it)'
  return undefined
}

/** The band's one line, or `undefined` for no band. */
export function bandLine(run: RunView | null, activity: string): string | undefined {
  if (run === null || run.status === 'stopped') return undefined
  if (run.status === 'complete') return 'adversarial-review-loop · run complete'
  const parts = [where(run)]
  if (run.stage === 'phase' || run.stage === 'final') parts.push(`round ${run.rounds + 1}`)
  parts.push(activity || (hold(run) ?? 'working'))
  return `adversarial-review-loop · ${parts.join(' · ')}`
}

type Line = { text: string; dim?: boolean; bold?: boolean; color?: 'success' | 'error' | 'warning' }

/** The pane's lines: the run, its phases, its roots, and the latest review or the report asked for. */
export function paneLines(run: RunView | null, last: LastReview | null, activity: string, report = ''): Line[] {
  if (run === null) return [{ text: 'No adversarial-review-loop run here. Start one with /arl-implement <plan.md>.', dim: true }]
  const lines: Line[] = [{ text: `Run ${run.run_id} · ${run.status} · ${where(run)}`, bold: true }]
  const note = activity || hold(run)
  if (note) lines.push({ text: note, color: run.waiting !== null ? 'warning' : undefined })
  lines.push({ text: '' }, { text: 'Phases', bold: true })
  if (run.phases.length === 0) lines.push({ text: '  none yet: Claude records them with set_phases', dim: true })
  run.phases.forEach((phase, index) => {
    const number = index + 1
    const done = number <= run.approved_phases
    const current = run.stage === 'phase' && number === run.phase
    const mark = done ? '✓' : current ? '▶' : '·'
    const round = current ? `  (round ${run.rounds + 1})` : ''
    lines.push({ text: `  ${mark} ${number}. ${phase}${round}`, dim: !done && !current, color: done ? 'success' : undefined })
  })
  if (run.final_required) {
    const mark = run.stage === 'done' ? '✓' : run.stage === 'final' ? '▶' : '·'
    lines.push({ text: `  ${mark} final review${run.stage === 'final' ? `  (round ${run.rounds + 1})` : ''}`, dim: run.stage !== 'final' && run.stage !== 'done' })
  }
  lines.push({ text: '' }, { text: 'Roots', bold: true })
  for (const root of run.roots) {
    const unknown = root.baseline === 'unknown'
    lines.push({ text: `  ${root.alias}  ${root.path}  (${root.kind})${unknown ? '  baseline unknown' : ''}`, color: unknown ? 'warning' : undefined })
  }
  if (report !== '') {
    lines.push({ text: '' }, { text: 'Report', bold: true })
    for (const text of report.trimEnd().split('\n')) lines.push({ text })
  } else if (last !== null) {
    const approved = last.verdict === 'APPROVED'
    lines.push({ text: '' }, { text: `Latest review: ${last.label}, ${last.verdict}`, bold: true, color: approved ? 'success' : 'error' })
    for (const text of last.message.trimEnd().split('\n')) lines.push({ text })
    if (last.report) lines.push({ text: `Report: ${last.report}`, dim: true })
  }
  return lines
}

export function registerUi(on: On): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const line = bandLine(await read($, runAtom), await read($, activityAtom))
    if (line === undefined) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text dimColor wrap="truncate-end">
          {line}
        </Text>
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const lines = paneLines(await read($, runAtom), await read($, lastAtom), await read($, activityAtom), await read($, reportAtom))
    return (
      <Box flexDirection="column">
        {lines.map(line => (
          <Text dimColor={line.dim} bold={line.bold} color={line.color}>
            {line.text === '' ? ' ' : line.text}
          </Text>
        ))}
      </Box>
    )
  })
}
