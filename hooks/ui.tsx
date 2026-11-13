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
// the engine anything. The pane is kept to a few lines, since a terminal often has no more room:
// the full phase list, the roots' paths and a report go to the transcript, where they scroll.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'
import type { LastReview, RunView } from '../types'

export const PANE = 'arl'
export const PANE_TITLE = 'Adversarial review'
export const REPORT_PANE = 'arl-report'
export const REPORT_TITLE = 'Review report'

/**
 * Whether the person closed the review pane in this session: the loop then stops opening it by
 * itself, until they ask for it (`/arl-pane`, `/arl-status`).
 */
export const pane = { dismissed: false }

/** Blocking findings the pane lists; the rest are counted. */
const PANE_FINDINGS = 3

// The shape tags decline what an earlier version stored, which outlives a reload: change one when
// its value's fields change.
const runAtom = atom({ plugin: 'adversarial-review-loop', key: 'run' } as const, null, { shape: 'run-1' })
const lastAtom = atom({ plugin: 'adversarial-review-loop', key: 'last' } as const, null, { shape: 'last-1' })
const activityAtom = atom({ plugin: 'adversarial-review-loop', key: 'activity' } as const, '')
const busyAtom = atom({ plugin: 'adversarial-review-loop', key: 'busy' } as const, false)
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

/** Whether Claude's main turn is running: the band says `working` only then. */
export async function showBusy($: EngineInterface, busy: boolean): Promise<void> {
  await update($, busyAtom, () => busy)
}

/** The report the report pane shows (`/arl-report`). */
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
export function bandLine(run: RunView | null, activity: string, busy = true): string | undefined {
  if (run === null || run.status === 'stopped') return undefined
  if (run.status === 'complete') return 'adversarial-review-loop · run complete'
  const parts = [where(run)]
  if (run.stage === 'phase' || run.stage === 'final') parts.push(`round ${run.rounds + 1}`)
  parts.push(activity || (hold(run) ?? (busy ? 'working' : 'idle')))
  return `adversarial-review-loop · ${parts.join(' · ')}`
}

/** A run of text in one style. Colours are the terminal theme's names. */
type Segment = { text: string; color?: string; bold?: boolean; dim?: boolean }
/** One line of the pane: its segments, and their text joined (what a test reads). */
type Line = { text: string; segments: Segment[] }

function line(...segments: Segment[]): Line {
  return { text: segments.map(segment => segment.text).join(''), segments }
}

// The palette, by what a colour means: done is green, now is blue, waiting on the person is amber,
// a problem is red, and what is only there for reference is grey.
const DONE = 'success'
const NOW = 'suggestion'
const WAITS = 'warning'
const BAD = 'error'
const QUIET = 'inactive'

const STATUS_COLOR: Record<string, string> = { active: NOW, paused: WAITS, complete: DONE, stopped: QUIET }
const VERDICT_COLOR: Record<string, string> = { APPROVED: DONE, CHANGES_REQUIRED: BAD, NEEDS_HUMAN: WAITS, OP_FAILURE: WAITS }
const SEVERITY_COLOR: Record<string, string> = { critical: BAD, high: BAD, medium: WAITS, low: QUIET, info: QUIET }

function phaseMark(run: RunView, number: number): string {
  if (number <= run.approved_phases) return '✓'
  return run.stage === 'phase' && number === run.phase ? '▶' : '·'
}

/** A phase as the pane draws it: green when approved, blue and bold when current, grey after. */
function phaseSegment(mark: string, label: string): Segment {
  if (mark === '✓') return { text: `${mark} ${label}`, color: DONE }
  if (mark === '▶') return { text: `${mark} ${label}`, color: NOW, bold: true }
  return { text: `${mark} ${label}`, color: QUIET }
}

/** The phases around the current one, on one line: the previous, the current and the next. */
function phaseWindow(run: RunView): Segment[] {
  const count = run.phases.length
  if (count === 0) return [{ text: 'no phases yet: Claude records them with set_phases', color: QUIET }]
  const current = run.stage === 'phase' ? run.phase : run.stage === 'plan' ? 1 : count
  const first = Math.max(1, Math.min(current - 1, count - 2))
  const shown: Segment[] = []
  for (let number = first; number <= Math.min(count, first + 2); number += 1) shown.push(phaseSegment(phaseMark(run, number), `${number}. ${run.phases[number - 1] ?? ''}`))
  const segments: Segment[] = [{ text: '  ' }]
  if (first > 1) segments.push({ text: '… ', color: QUIET })
  shown.forEach((segment, index) => {
    if (index > 0) segments.push({ text: '  ' })
    segments.push(segment)
  })
  if (first + 2 < count) segments.push({ text: ' …', color: QUIET })
  // After the phases it follows, the elided ones included.
  if (run.final_required) segments.push({ text: '  ' }, phaseSegment(run.stage === 'done' ? '✓' : run.stage === 'final' ? '▶' : '·', 'final review'))
  return segments
}

/** A blocking finding, parsed from what Claude was told. */
type Finding = { severity: string; file: string; detail: string }

/**
 * A review message's blocking findings: the lines under its "Blocking findings" heading, up to
 * the next section. An approval blocks on nothing, whatever it lists.
 */
export function blockingFindings(message: string, verdict = ''): string[] {
  return findingsOf(message, verdict).map(finding => `${finding.severity} ${finding.file}  ${finding.detail}`)
}

function findingsOf(message: string, verdict: string): Finding[] {
  if (verdict === 'APPROVED') return []
  const start = message.indexOf('Blocking findings (')
  if (start === -1) return []
  const rest = message.slice(start)
  const ends = ['\nDeferred findings', '\nAll findings reported', '\nReversals of earlier rounds', '\nOscillating points', '\nReviewer prose:']
    .map(heading => rest.indexOf(heading))
    .filter(index => index !== -1)
  const blocking = ends.length > 0 ? rest.slice(0, Math.min(...ends)) : rest
  const found: Finding[] = []
  for (const text of blocking.split('\n')) {
    const match = /^FINDING severity=(\S+) actionable=\S+ file=(\S+) \| (.*)$/.exec(text.trim())
    if (match) found.push({ severity: match[1] ?? '', file: match[2] ?? '', detail: match[3] ?? '' })
  }
  return found
}

/** The pane's lines: the run on one line, the phases around the current one, the roots, and the latest review. */
export function paneLines(run: RunView | null, last: LastReview | null, activity: string, busy = true): Line[] {
  if (run === null) return [line({ text: 'No adversarial-review-loop run here. Start one with /arl-implement <plan.md>.', color: QUIET })]
  const round = run.stage === 'phase' || run.stage === 'final' ? ` · round ${run.rounds + 1}` : ''
  const at = run.stage === 'phase' ? `phase ${run.phase}/${run.phase_count}` : where(run)
  const lines: Line[] = [
    line({ text: `Run ${run.run_id}`, bold: true }, { text: ' · ' }, { text: run.status, bold: true, color: STATUS_COLOR[run.status] }, { text: ` · ${at}${round}`, bold: true }),
  ]
  const note = activity || hold(run)
  if (activity) lines.push(line({ text: activity, color: NOW }))
  else if (note) lines.push(line({ text: note, color: WAITS }))
  else if (run.status === 'active' && run.stage !== 'done') lines.push(line(busy ? { text: 'Claude is working', color: NOW } : { text: 'idle', color: QUIET }))
  lines.push(line(...phaseWindow(run)))
  const roots: Segment[] = [{ text: 'Roots: ', color: QUIET }]
  run.roots.forEach((root, index) => {
    if (index > 0) roots.push({ text: ', ' })
    roots.push(root.baseline === 'unknown' ? { text: `${root.alias} (baseline unknown)`, color: WAITS } : { text: root.alias })
  })
  lines.push(line(...roots))
  if (last !== null) {
    const findings = findingsOf(last.message, last.verdict)
    const latest: Segment[] = [{ text: 'Latest: ', color: QUIET }, { text: `${last.label} ` }, { text: last.verdict, bold: true, color: VERDICT_COLOR[last.verdict] ?? WAITS }]
    if (findings.length > 0) latest.push({ text: ` · ${findings.length} blocking`, color: BAD })
    lines.push(line(...latest))
    for (const finding of findings.slice(0, PANE_FINDINGS)) {
      lines.push(line({ text: '  ' }, { text: finding.severity, color: SEVERITY_COLOR[finding.severity] ?? WAITS }, { text: ' ' }, { text: finding.file, color: NOW }, { text: `  ${finding.detail}` }))
    }
    if (findings.length > PANE_FINDINGS) lines.push(line({ text: `  … ${findings.length - PANE_FINDINGS} more`, color: QUIET }))
  }
  lines.push(line({ text: '/arl-report full report · /arl-phases all phases · /arl-status roots', color: QUIET }))
  return lines
}

/** Every phase, one per line, with its mark: what `/arl-phases` prints. */
export function phasesText(run: RunView | null): string {
  if (run === null) return 'No run here.'
  if (run.phases.length === 0) return 'No phases yet: Claude records them with set_phases.'
  const lines = run.phases.map((phase, index) => {
    const number = index + 1
    const current = run.stage === 'phase' && number === run.phase
    return `${phaseMark(run, number)} ${number}. ${phase}${current ? `  (round ${run.rounds + 1})` : ''}`
  })
  if (run.final_required) lines.push(`${run.stage === 'done' ? '✓' : run.stage === 'final' ? '▶' : '·'} final review`)
  return lines.join('\n')
}

/** The run in full, as `/arl-status` prints it: the state, every phase and every root's path. */
export function statusText(run: RunView | null): string {
  if (run === null) return 'No adversarial-review-loop run here. Start one with /arl-implement <plan.md>.'
  const note = hold(run)
  const roots = run.roots.map(root => `  ${root.alias}  ${root.path}  (${root.kind})${root.baseline === 'unknown' ? '  baseline unknown' : ''}`)
  return [`Run ${run.run_id} · ${run.status} · ${briefly(run)}`, ...(note ? [note] : []), '', 'Phases', phasesText(run), '', 'Roots', ...roots].join('\n')
}

export function registerUi(on: On): void {
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const line = bandLine(await read($, runAtom), await read($, activityAtom), await read($, busyAtom))
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
    const lines = paneLines(await read($, runAtom), await read($, lastAtom), await read($, activityAtom), await read($, busyAtom))
    return (
      <Box flexDirection="column">
        {lines.map(row => (
          <Box flexDirection="row">
            {row.segments.map(segment => (
              <Text color={segment.color} bold={segment.bold} dimColor={segment.dim} wrap="truncate-end">
                {segment.text === '' ? ' ' : segment.text}
              </Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })

  // The person's own close (its mark, ctrl+x x, or Escape) is their choice: kept for the session.
  on('ui.close', ($, e, next) => {
    if (e.id === PANE && e.origin.kind === 'person') pane.dismissed = true
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: REPORT_PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const report = await read($, reportAtom)
    const rows = report === '' ? ['No report shown: /arl-report [N] shows one.'] : report.trimEnd().split('\n')
    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={1}>
          <Button key="close" role="dismiss" onPress={() => void $.ui.close({ id: REPORT_PANE }).catch(() => undefined)}>
            Close
          </Button>
          <Text color={QUIET}>Esc closes it · ctrl+x tab focuses it</Text>
        </Box>
        {rows.map(row => (
          <Text>{row === '' ? ' ' : row}</Text>
        ))}
      </Box>
    )
  })
}
