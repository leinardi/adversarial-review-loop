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

// The slash commands the person runs. Each is one engine command and a line of text; the ones that
// move the run on (/arl-implement, /arl-resume, /arl-accept, /arl-finish) then continue Claude.
// Claude Code ships /status, /resume and /config, and refuses a plugin command of the same name:
// every command here is prefixed `arl-`.

import type { CommandSpec, EngineInterface } from 'claude-code'
import type { RunView } from '../types'
import { type Answer } from './engine'
import { announce, proceed, refused, run, session } from './flow'
import { registerReviewer } from './reviewer'
import { briefly, PANE, PANE_TITLE, phasesText, REPORT_PANE, REPORT_TITLE, showLast, showReport, statusText } from './ui'

export const COMMANDS: readonly CommandSpec[] = [
  { name: 'arl-implement', description: 'Implement a plan phase by phase, each phase reviewed before the next', argumentHint: '[--new] [--root DIR]... <plan.md>' },
  { name: 'arl-resume', description: 'Continue the adversarial-review-loop run in this session' },
  { name: 'arl-status', description: 'Show the adversarial-review-loop run and open its pane', immediate: true },
  { name: 'arl-report', description: 'Show the newest review report, or the N-th', argumentHint: '[N]', immediate: true },
  { name: 'arl-phases', description: 'List every phase of the adversarial-review-loop run', immediate: true },
  { name: 'arl-pause', description: 'Pause the run: Claude is not prompted again until /arl-resume', immediate: true },
  { name: 'arl-accept', description: 'Approve the current phase (or the final review) as it stands' },
  { name: 'arl-finish', description: 'Require a final review of the whole run before it completes' },
  { name: 'arl-stop', description: 'Stop the run; it cannot be resumed', immediate: true },
  { name: 'arl-config', description: 'Show the adversarial-review-loop configuration, or set or unset a key', argumentHint: '[<key> <value> [--repo] | <key> --unset [--repo]]' },
  { name: 'arl-remove-root', description: 'Drop a workspace root registered by mistake', argumentHint: '<alias or path>' },
]

/** Register each command; one the host refuses is said, and the rest still register. */
export async function registerCommands($: EngineInterface): Promise<void> {
  for (const spec of COMMANDS) {
    await $.command.register(spec).catch((error: unknown) => {
      $.ui.log(`/${spec.name} could not be registered (${String(error)}).`)
    })
  }
}

function summaryOf(answer: Answer): RunView | null {
  return (answer.run as RunView | null | undefined) ?? null
}

/** Open the review pane where a person can see it; nothing headless. */
async function openPane($: EngineInterface): Promise<void> {
  if (!session.isInteractive) return
  await $.ui.open({ id: PANE, title: PANE_TITLE }).catch(() => undefined)
}

/** Split arguments as a shell would for words and quotes: `"my plan.md"` is one word. */
export function words(args: string): string[] | string {
  const tokens: string[] = []
  for (const match of args.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)) {
    const [whole, double, single, bare] = match
    if (bare !== undefined && /["']/.test(bare)) return `Unbalanced quote in ${whole}.`
    tokens.push(double ?? single ?? bare ?? '')
  }
  return tokens
}

/** `/arl-implement` arguments: `--new`, `--root DIR` (repeated), and the plan. */
export function implementArgs(args: string): { plan: string; isNew: boolean; roots: string[] } | string {
  const tokens = words(args)
  if (typeof tokens === 'string') return tokens
  const roots: string[] = []
  let plan = ''
  let isNew = false
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index] ?? ''
    if (token === '--new') isNew = true
    else if (token === '--root') {
      const root = tokens[index + 1]
      if (root === undefined) return '--root needs a directory.'
      roots.push(root)
      index += 1
    } else if (token.startsWith('--')) return `Unknown flag ${token}.`
    else if (plan === '') plan = token
    else return `One plan at a time: ${plan} and ${token} were given.`
  }
  return plan === '' ? 'Usage: /arl-implement [--new] [--root DIR]... <plan.md>' : { plan, isNew, roots }
}

async function implement($: EngineInterface, args: string): Promise<string> {
  const parsed = implementArgs(args)
  if (typeof parsed === 'string') return parsed
  const answer = await run($, ['init', '--plan', parsed.plan, ...(parsed.isNew ? ['--new'] : []), ...parsed.roots.flatMap(root => ['--root', root])])
  if (!answer.ok) return refused(answer)
  const view = summaryOf(answer)
  // The pane's latest review was the old run's.
  await showLast($, null).catch(() => undefined)
  const prompt = String(answer.prompt ?? '')
  $.clock.after(0, () => {
    void $.prompt.submit({ text: prompt }).catch((error: unknown) => {
      $.ui.log(`Could not start Claude on the plan (${String(error)}).`)
    })
  })
  await openPane($)
  const archived = typeof answer.archived === 'string' && answer.archived ? ` The previous run was archived under .arl/runs/${answer.archived}/.` : ''
  const roots = view?.roots.map(root => `${root.alias} (${root.path})`).join(', ') ?? ''
  return `Run ${view?.run_id ?? ''} started on ${parsed.plan}, with roots ${roots}.${archived} Claude starts on the plan now.`
}

async function resume($: EngineInterface): Promise<string> {
  // A command waits for the turn to end, so no review of this session is in flight: any
  // pending one is abandoned, and Claude is told to ask for it again.
  const answer = await run($, ['resume'])
  if (!answer.ok) return refused(answer)
  const abandoned = answer.abandoned_review === true
  const resumes = abandoned ? 'The run resumes. The review that was in flight was abandoned: when the phase is ready, call review again.' : 'The run resumes.'
  // This session may not be the one that started the run: it is told what the run is.
  const lead = typeof answer.context === 'string' && answer.context ? `${resumes}\n\n${answer.context}` : resumes
  const view = summaryOf(answer)
  const where = view === null ? '' : ` at ${briefly(view)}`
  return `Resumed run ${view?.run_id ?? ''}${where}${abandoned ? ' (the pending review was abandoned)' : ''}. ${await proceed($, lead)}`
}

async function status($: EngineInterface): Promise<string> {
  const answer = await run($, ['status'])
  if (!answer.ok) return refused(answer)
  await openPane($)
  const view = summaryOf(answer)
  // The command's own text: a log line is one line (measured: its newlines drew as stray
  // characters). A short text costs little in the requests it goes into.
  return statusText(view)
}

async function phases($: EngineInterface): Promise<string> {
  const answer = await run($, ['status'])
  if (!answer.ok) return refused(answer)
  return phasesText(summaryOf(answer))
}

async function report($: EngineInterface, args: string): Promise<string> {
  const number = args.trim()
  if (number !== '' && !/^\d+$/.test(number)) return 'Usage: /arl-report [N]'
  const answer = await run($, ['report', ...(number ? [number] : [])])
  if (!answer.ok) return refused(answer)
  const text = String(answer.text ?? '')
  if (!session.isInteractive) return text
  // A report holds the reviewer's raw output, too long for Claude's context and for a log line:
  // it gets a pane of its own, which scrolls.
  await showReport($, text).catch(() => undefined)
  await $.ui.open({ id: REPORT_PANE, title: REPORT_TITLE }).catch(() => undefined)
  return `Report ${number || '(newest)'} is in the report pane.`
}

async function transition($: EngineInterface, command: 'pause' | 'stop'): Promise<string> {
  const answer = await run($, [command])
  if (!answer.ok) return refused(answer)
  return command === 'pause' ? 'Paused: Claude is not prompted again until /arl-resume.' : 'Stopped: the run is over, and Claude is not prompted again.'
}

async function accept($: EngineInterface): Promise<string> {
  const answer = await run($, ['accept'])
  if (!answer.ok) return refused(answer)
  const stage = summaryOf(answer)?.stage
  if (stage === 'done') {
    await announce($, { ...answer, label: 'run', verdict: 'APPROVED' }, 'The user accepted the last stage: the run is complete.')
    return 'Accepted. The run is complete.'
  }
  return `Accepted. ${await proceed($, 'The user accepted the stage as it stands. Commit each changed git repository by its own conventions.')}`
}

async function finish($: EngineInterface): Promise<string> {
  const answer = await run($, ['finish'])
  if (!answer.ok) return refused(answer)
  const stage = summaryOf(answer)?.stage
  if (stage !== 'final') return 'The run now ends with a final review of every change, once the last phase is approved.'
  return `The final review is next. ${await proceed($)}`
}

async function configure($: EngineInterface, args: string): Promise<string> {
  const answer = await run($, ['config'], args)
  if (!answer.ok) return refused(answer)
  // The effort is the agent type's own: a new one takes a new registration.
  if (answer.key === 'effort') {
    const effort = String(answer.effort ?? '')
    const refused = await registerReviewer($, effort).then(
      () => '',
      (error: unknown) => `\nThe reviewer could not take effort ${effort} now (${String(error)}); a new session uses the setting.`,
    )
    if (refused) return `${String(answer.text ?? '')}${refused}`
  }
  const text = String(answer.text ?? '')
  return text
}

async function removeRoot($: EngineInterface, args: string): Promise<string> {
  const root = args.trim()
  if (root === '') return 'Usage: /arl-remove-root <alias or path>'
  const answer = await run($, ['root-remove', root])
  if (!answer.ok) return refused(answer)
  return `Removed root ${String(answer.alias)} (${String(answer.path)}): nothing in it is reviewed from now on. ${await proceed($)}`
}

/** Run one of the commands; `undefined` for a name this plugin does not register. */
export async function runCommand($: EngineInterface, name: string, args: string): Promise<string | undefined> {
  switch (name) {
    case 'arl-implement':
      return implement($, args)
    case 'arl-resume':
      return resume($)
    case 'arl-status':
      return status($)
    case 'arl-report':
      return report($, args)
    case 'arl-phases':
      return phases($)
    case 'arl-pause':
      return transition($, 'pause')
    case 'arl-stop':
      return transition($, 'stop')
    case 'arl-accept':
      return accept($)
    case 'arl-finish':
      return finish($)
    case 'arl-config':
      return configure($, args)
    case 'arl-remove-root':
      return removeRoot($, args)
    default:
      return undefined
  }
}
