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

// The loop: what each tool Claude calls does, how a review round runs on either harness, when the
// loop asks the user, and how a turn's end continues the run. The engine decides everything that
// is state; this file only runs things in order and reports what the engine answered.

import type { EngineInterface, TurnUsage } from 'claude-code'
import { type Answer, engine, engineStream } from './engine'
import { Interrupted, normalize, type Ran, runReviewer, type Task, within } from './reviewer'
import { ACCEPT, KEEP_GOING, STOP } from './text'

/** What the session told the mod at its start. */
export const session = {
  /** The launch directory: the run lives in `<dir>/.arl/`. */
  dir: '',
  /** Whether a person can be asked (`$.ui.ask` rejects under `-p`). */
  isInteractive: false,
  id: '',
}

/** A run summary, as the engine prints it (`RunState.summary`). */
type RunSummary = {
  run_id: string
  status: string
  stage: string
  waiting: { kind: string; detail?: string } | null
  roots: { alias: string; path: string; kind: string; baseline: string; proposed: string }[]
}

/** The active run's root paths, or `null` when no active run is known. Refreshed from answers. */
let roots: string[] | null = null
/** The state file's modification time when the engine last said no run is active here. */
let closedAt: number | undefined

/** Remember the run an answer describes. */
export function track(answer: Answer): void {
  const run = answer.run as RunSummary | null | undefined
  if (run === undefined) return
  roots = run !== null && (run.status === 'active' || run.status === 'paused') ? run.roots.map(root => root.path) : null
}

/**
 * Whether an active run lives here, asking the engine only when a state file exists: `'yes'`,
 * `'no'`, or `'unknown'` when the engine could not say (its answer's `error`).
 */
async function runHere($: EngineInterface): Promise<'yes' | 'no' | { unknown: string }> {
  if (roots !== null) return 'yes'
  const file = `${session.dir}/.arl/state.json`
  if (session.dir === '' || !(await $.fs.exists(file))) return 'no'
  // A stopped or complete run leaves its state behind: the engine is asked again only once that
  // state has changed, not at every write and every turn's end.
  const mtime = (await $.fs.stat(file).catch(() => undefined))?.mtimeMs
  if (mtime !== undefined && mtime === closedAt) return 'no'
  const status = await engine($, session.dir, ['status'])
  if (!status.ok) return { unknown: status.error ?? 'the engine could not read the run' }
  track(status)
  closedAt = roots === null ? mtime : undefined
  return roots !== null ? 'yes' : 'no'
}

async function hasRun($: EngineInterface): Promise<boolean> {
  return (await runHere($)) === 'yes'
}

async function run($: EngineInterface, args: readonly string[], stdin = ''): Promise<Answer> {
  const answer = await engine($, session.dir, args, stdin)
  track(answer)
  return answer
}

/** `run`, for a command that runs a reviewer and may outlast ten minutes. */
async function runStream($: EngineInterface, args: readonly string[], input = ''): Promise<Answer> {
  const answer = await engineStream($, session.dir, args, input)
  track(answer)
  return answer
}

function refused(answer: Answer): string {
  return `adversarial-review-loop refused (${answer.code ?? 'error'}): ${answer.error ?? 'no reason given'}`
}

// --------------------------------------------------------------------------
// When the loop needs the user
// --------------------------------------------------------------------------

const QUESTIONS: Record<string, string> = {
  stall: 'The review keeps raising the same findings. How should the loop go on?',
  failures: 'The reviewer keeps failing. How should the loop go on?',
  'needs-human': 'The reviewer asked for your judgement. How should the loop go on?',
  oversized: 'A file is too large to review. How should the loop go on?',
}

/**
 * The run waits for the user (`waiting`): ask them, act on their choice, and answer what Claude is
 * told. Headless, or with no clear answer, Claude is told to stop and hand over.
 */
export async function decide($: EngineInterface, waiting: { kind: string; detail?: string }): Promise<string> {
  const detail = waiting.detail ?? ''
  const choices = waiting.kind === 'oversized' ? 'whether to keep going once the file is removed or ignored, or to stop the run' : 'whether to accept this stage as it stands, keep going, or stop the run'
  const handOver = `\n\nThe run now waits for the user (${waiting.kind}). Stop working on the plan and tell the user what happened${detail ? `: ${detail}` : '.'} They decide ${choices}.`
  if (!session.isInteractive) return handOver
  const options = waiting.kind === 'oversized' ? [KEEP_GOING, STOP] : [ACCEPT, KEEP_GOING, STOP]
  void $.ui.notify(`adversarial-review-loop needs you: ${QUESTIONS[waiting.kind] ?? waiting.kind}`).catch(() => undefined)
  // Dismissed, or "Chat about this": no decision.
  const choice = await $.ui
    .ask(`${QUESTIONS[waiting.kind] ?? 'The review loop needs you. How should it go on?'}${detail ? `\n\n${detail}` : ''}`, options)
    .catch(() => '')
  if (choice === ACCEPT) {
    const accepted = await run($, ['accept'])
    if (!accepted.ok) return `\n\nThe user chose to accept, but accepting failed: ${accepted.error ?? ''}${handOver}`
    const after = (accepted.run as RunSummary | undefined)?.stage
    return `\n\nThe user accepted this stage as it stands. Commit each changed git repository by its own conventions${after === 'done' ? '. The run is complete.' : after === 'final' ? ', then call review for the final review.' : ', then go on with the next phase.'}`
  }
  if (choice === KEEP_GOING) {
    const resumed = await run($, ['resume', '--session', session.id])
    if (!resumed.ok) return `\n\nThe user chose to keep going, but resuming failed: ${resumed.error ?? ''}${handOver}`
    return waiting.kind === 'oversized'
      ? '\n\nThe user wants you to keep going: remove the oversized file, or ignore it in its root, then call review again.'
      : waiting.kind === 'failures'
        ? '\n\nThe user wants you to keep going: call review again.'
        : '\n\nThe user wants you to keep going: address the findings, then call review again.'
  }
  if (choice === STOP) {
    await run($, ['stop'])
    return '\n\nThe user stopped the run. Stop working on the plan.'
  }
  return handOver
}

/** When a refusal left the run waiting for the user, put it to them; else nothing. */
async function waitingDecision($: EngineInterface): Promise<string> {
  const status = await run($, ['status'])
  const waiting = (status.run as RunSummary | null | undefined)?.waiting
  return waiting ? decide($, waiting) : ''
}

/** Ask the user for the baseline of each root that has none; false when any is left unchosen. */
async function chooseBaselines($: EngineInterface): Promise<boolean> {
  const status = await run($, ['status'])
  const summary = status.run as RunSummary | null | undefined
  const unknown = summary?.roots.filter(root => root.baseline === 'unknown') ?? []
  if (!session.isInteractive) return unknown.length === 0
  for (const root of unknown) {
    const since = root.proposed ? `Changes since ${root.proposed}` : ''
    const options = [...(since ? [since] : []), 'Everything here is new', 'The current content is the baseline']
    const choice = await $.ui
      .ask(`Root ${root.alias} (${root.path}) joined the run after it changed. What should its review compare against?`, options)
      .catch(() => '')
    const args =
      choice === since && since
        ? ['root-baseline', root.alias, '--commit', root.proposed]
        : choice === 'Everything here is new'
          ? ['root-baseline', root.alias, '--empty']
          : choice === 'The current content is the baseline'
            ? ['root-baseline', root.alias, '--current']
            : undefined
    if (args === undefined || !(await run($, args)).ok) return false
  }
  return true
}

// --------------------------------------------------------------------------
// The tools
// --------------------------------------------------------------------------

export async function setPhases($: EngineInterface, input: Record<string, unknown>): Promise<string> {
  const args = ['set-phases', ...(input.replan === true ? ['--replan'] : [])]
  const answer = await run($, args, JSON.stringify(input.phases ?? null))
  if (!answer.ok) return refused(answer)
  const summary = answer.run as RunSummary & { phases: string[]; phases_revision: number; phase: number }
  return `Phases recorded (revision ${summary.phases_revision}): ${summary.phases.map((phase, index) => `${index + 1}. ${phase}`).join('; ')}. ${summary.stage === 'final' ? 'Every phase is approved: call review for the final review.' : `Implement phase ${summary.phase}, then call review.`}`
}

export async function addRoot($: EngineInterface, input: Record<string, unknown>): Promise<string> {
  if (typeof input.path !== 'string' || input.path === '') return 'add_root needs a path.'
  const answer = await run($, ['root-add', input.path])
  if (!answer.ok) return refused(answer)
  // `root-add` answers no run summary: refresh the cached roots.
  await run($, ['status'])
  if (answer.status !== 'added') {
    if (answer.baseline !== 'unknown') return `${input.path} is already covered by root ${String(answer.alias)}.`
    // A root with no baseline yet reviews nothing: settle it now rather than at the next review.
    if (await chooseBaselines($)) return `${input.path} is covered by root ${String(answer.alias)}, whose baseline the user just chose.`
    return `${input.path} is covered by root ${String(answer.alias)}, whose baseline is unknown: nothing in it can be reviewed until the user picks one. Tell the user.`
  }
  if (answer.baseline !== 'unknown') return `Registered root ${String(answer.alias)}; its current content is the baseline.`
  if (await chooseBaselines($)) return `Registered root ${String(answer.alias)} with the baseline the user chose.`
  return `Registered root ${String(answer.alias)}, but it already holds changes, so its baseline is unknown and nothing in it can be reviewed until the user picks one. Tell the user.`
}

/** Reviewer usage as the engine's `--usage` reads it. */
function usageArgs(usage: TurnUsage | undefined, durationMs: number | undefined): string[] {
  if (usage === undefined && durationMs === undefined) return []
  return [
    '--usage',
    JSON.stringify({
      input_tokens: usage?.input_tokens,
      output_tokens: usage?.output_tokens,
      cache_read_tokens: usage?.cache_read_input_tokens,
      cache_creation_tokens: usage?.cache_creation_input_tokens,
      duration_ms: durationMs,
    }),
  ]
}

function sum(a: TurnUsage | undefined, b: TurnUsage | undefined): TurnUsage | undefined {
  if (a === undefined) return b
  if (b === undefined) return a
  return {
    ...b,
    input_tokens: a.input_tokens + b.input_tokens,
    output_tokens: a.output_tokens + b.output_tokens,
    cache_read_input_tokens: a.cache_read_input_tokens + b.cache_read_input_tokens,
    cache_creation_input_tokens: a.cache_creation_input_tokens + b.cache_creation_input_tokens,
  }
}

/** A prepared review or clarify call, as the engine answered `prepare`. */
type Prepared = Answer & {
  prompt_file: string
  bundle_dir: string
  roots: string[]
  answer_file: string
  /** The configured model, empty for the session's subagent default. */
  reviewer_model?: string
  timeout_sec?: number
}

/** Runs one reviewer: the subagent in the session, a stand-in in a test. */
export type Reviewer = (task: Task) => Promise<Ran>

async function ask($: EngineInterface, reviewer: Reviewer, prepared: Prepared, step: { prompt_file: string }, description: string): Promise<Ran> {
  let prompt: string
  try {
    prompt = await $.fs.read(step.prompt_file)
  } catch (error) {
    return { ok: false, kind: 'operational', error: `the review prompt could not be read: ${String(error)}` }
  }
  return reviewer({
    prompt,
    description,
    model: prepared.reviewer_model ?? '',
    timeoutSec: prepared.timeout_sec ?? 900,
    dirs: [prepared.bundle_dir, ...prepared.roots],
  })
}

/** What Claude is told when the person interrupted a question: its reply was not recorded. */
const QUESTION_INTERRUPTED = 'The question was interrupted; its reply was not recorded.'

/** What Claude is told when the person interrupted a review: nothing was recorded. */
const INTERRUPTED = 'The review was interrupted, and nothing was recorded. Call review again when the user wants it.'

/** Run the subagent on a prepared review, through the one repair a malformed answer gets. */
async function reviewBySubagent($: EngineInterface, reviewer: Reviewer, prepared: Prepared): Promise<Answer> {
  const id = String(prepared.review_id)
  let step: { prompt_file: string; answer_file: string } = prepared
  let usage: TurnUsage | undefined
  let durationMs: number | undefined
  let recorded: Answer = { ok: false, code: 'engine', error: 'no review ran' }
  for (const attempt of [1, 2]) {
    const ran = await ask($, reviewer, prepared, step, `Review ${String(prepared.label)}, round ${String(prepared.round)}${attempt > 1 ? ' (repair)' : ''}`)
    usage = sum(usage, ran.usage)
    if (ran.durationMs !== undefined) durationMs = (durationMs ?? 0) + ran.durationMs
    if (!ran.ok) {
      return run($, ['review', 'record', id, '--failed', ran.kind, '--error', ran.error, ...usageArgs(usage, durationMs)])
    }
    try {
      await $.fs.write(step.answer_file, ran.answer)
    } catch (error) {
      return run($, ['review', 'record', id, '--failed', 'operational', '--error', `the answer could not be saved: ${String(error)}`])
    }
    recorded = await run($, ['review', 'record', id, '--answer', step.answer_file, ...usageArgs(usage, durationMs)])
    if (recorded.status !== 'repair') return recorded
    step = recorded as Answer & { prompt_file: string; answer_file: string }
  }
  return recorded
}

async function prepareReview($: EngineInterface, verification: unknown): Promise<Answer> {
  return run($, ['review', 'prepare', '--session', session.id], JSON.stringify({ verification: verification ?? [] }))
}

export async function review($: EngineInterface, input: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  return reviewWith($, input, task => runReviewer($, { ...task, signal }), signal)
}

/** `signal` is the tool call's own: aborted when the person interrupts the review. */
export async function reviewWith($: EngineInterface, input: Record<string, unknown>, reviewer: Reviewer, signal?: AbortSignal): Promise<string> {
  let prepared = await prepareReview($, input.verification)
  if (!prepared.ok && prepared.code === 'baseline-unknown') {
    if (!(await chooseBaselines($))) return `${refused(prepared)}\n\nStop and tell the user: a root's baseline must be chosen before the review can run.`
    prepared = await prepareReview($, input.verification)
  }
  if (!prepared.ok) {
    // Whatever refused it (a stall, an oversized file, the failure budget a broken bundle spent),
    // a run that now waits for the user is put to them.
    return refused(prepared) + (await waitingDecision($))
  }
  if (prepared.status === 'skipped') return String(prepared.message)
  let recorded: Answer
  try {
    recorded =
      prepared.harness === 'subagent'
        ? await reviewBySubagent($, reviewer, prepared as Prepared)
        : await runStream($, ['review', 'run-external', String(prepared.review_id)])
    // An interrupt kills the external reviewer's engine, which then answers nothing readable. A
    // record that landed first stands, whatever the signal says.
    if (!recorded.ok && signal?.aborted) throw new Interrupted()
  } catch (error) {
    if (!(error instanceof Interrupted)) throw error
    // Retire the ticket, or the loop waits on a review no reviewer runs. Best effort: the
    // dispatch is ending, and a ticket left pending is abandoned by the next review.
    await run($, ['review', 'abandon', String(prepared.review_id)]).catch(() => undefined)
    return INTERRUPTED
  }
  if (!recorded.ok) return refused(recorded) + (await waitingDecision($))
  const message = String(recorded.message ?? '')
  const waiting = (recorded.run as RunSummary | undefined)?.waiting
  return waiting ? message + (await decide($, waiting)) : message
}


function clarifyText(answer: Answer): string {
  const retracted = Array.isArray(answer.retracted) ? answer.retracted.length : 0
  const notes = [
    retracted ? `The reviewer retracted ${retracted} finding(s); the next round is told.` : '',
    typeof answer.problem === 'string' && answer.problem ? `Note: ${answer.problem}` : '',
  ].filter(note => note !== '')
  return `The reviewer's reply:\n\n${String(answer.reply ?? '')}${notes.length ? `\n\n${notes.join('\n')}` : ''}`
}

export async function clarify($: EngineInterface, input: Record<string, unknown>, signal?: AbortSignal): Promise<string> {
  return clarifyWith($, input, task => runReviewer($, { ...task, signal }), signal)
}

/** `signal` is the tool call's own: aborted when the person interrupts the question. */
export async function clarifyWith($: EngineInterface, input: Record<string, unknown>, reviewer: Reviewer, signal?: AbortSignal): Promise<string> {
  const question = typeof input.question === 'string' ? input.question : ''
  if (question.trim() === '') return 'clarify needs a question.'
  // An external reviewer is run by the engine, which prepares the question itself; the subagent
  // is refused there (`not-external`) before anything is prepared.
  const external = await runStream($, ['clarify', 'run-external'], question)
  if (external.ok) return clarifyText(external)
  // An interrupt kills the external reviewer's engine, which then answers nothing readable.
  if (signal?.aborted) return QUESTION_INTERRUPTED
  if (external.code !== 'not-external') return refused(external)
  const prepared = (await run($, ['clarify', 'prepare'], question)) as Prepared
  if (!prepared.ok) return refused(prepared)
  let ran: Ran
  try {
    ran = await ask($, reviewer, prepared, prepared, `Clarify review round ${String(prepared.round_seq)}`)
  } catch (error) {
    if (error instanceof Interrupted) return QUESTION_INTERRUPTED
    throw error
  }
  if (!ran.ok) return `The reviewer could not answer: ${ran.error}`
  try {
    await $.fs.write(prepared.answer_file, ran.answer)
  } catch (error) {
    return `The reviewer's reply could not be saved: ${String(error)}`
  }
  const recorded = await run($, [
    'clarify',
    'record',
    String(prepared.round_seq),
    String(prepared.question),
    '--answer',
    prepared.answer_file,
    '--run-id',
    String(prepared.run_id),
  ])
  return recorded.ok ? clarifyText(recorded) : refused(recorded)
}

// --------------------------------------------------------------------------
// Roots, and the turn's end
// --------------------------------------------------------------------------

/** The directory a file lives in. */
export function parentOf(path: string): string {
  const cut = path.replace(/\/+$/, '').lastIndexOf('/')
  return cut <= 0 ? '/' : path.slice(0, cut)
}

/**
 * Before a file is written, make sure the directory it lives in is a root of the active run, so
 * its baseline is taken before the change. Answers a note for Claude when a root was added.
 */
export async function ensureRoot($: EngineInterface, directory: string, { explicit = false } = {}): Promise<string | undefined> {
  if (!directory.startsWith('/')) return undefined
  const here = await runHere($)
  if (here === 'no') return undefined
  if (here !== 'yes') return unregistered(directory, here.unknown)
  if ((roots ?? []).some(root => within(directory, root))) return undefined
  // A folder the person added to the session is meant to be worked in, wherever it is.
  if (!explicit && (await isScratch($, directory))) return undefined
  // A folder being created registers as the folder, not as its deepest new subfolder: a later
  // file beside that subfolder would otherwise join late, with an unknown baseline.
  let target = directory
  while (parentOf(target) !== target && !(await $.fs.exists(parentOf(target)))) target = parentOf(target)
  const answer = await run($, ['root-add', target])
  if (!answer.ok) return unregistered(target, answer.error ?? answer.code ?? 'no reason given')
  // `root-add` answers no run summary: refresh the cached roots.
  await run($, ['status'])
  if (answer.status !== 'added') return undefined
  directory = target
  if (answer.baseline === 'unknown') {
    return `adversarial-review-loop registered ${directory} as root ${String(answer.alias)}, but it already holds changes: its baseline is unknown, and the next review asks the user for one.`
  }
  return `adversarial-review-loop registered ${directory} as root ${String(answer.alias)}; its content before this change is the baseline.`
}

/**
 * Whether `directory` is a place for scratch files: temp folders and Claude Code's own (plans,
 * memory, the session scratchpad). A write there is not part of the plan, and registering it
 * would make a root of a folder other sessions fill. `add_root` still registers one on purpose.
 *
 * A run may itself live in such a place (a workspace under `/tmp`): a folder that shares a
 * top-level folder of the place with one of the run's roots is the run's, not scratch.
 */
async function isScratch($: EngineInterface, directory: string): Promise<boolean> {
  const [home, tmp] = await Promise.all([$.env.get('HOME'), $.env.get('TMPDIR')])
  const places = ['/tmp', '/var/tmp', ...(tmp ? [tmp] : []), ...(home ? [`${home}/.claude`] : [])]
  const place = places.filter(candidate => within(directory, candidate)).sort((a, b) => b.length - a.length)[0]
  if (place === undefined) return false
  const top = topBelow(directory, place)
  return top === undefined || !(roots ?? []).some(root => within(root, top))
}

/** The folder directly below `place` that `path` lies in; `undefined` for `place` itself. */
function topBelow(path: string, place: string): string | undefined {
  const inner = normalize(path)
  const outer = normalize(place)
  if (inner === undefined || outer === undefined || inner === outer) return undefined
  const rest = inner.slice(outer === '/' ? 1 : outer.length + 1)
  return `${outer === '/' ? '' : outer}/${rest.split('/')[0]}`
}

/** What Claude is told when a folder about to be written could not become a root. */
export function unregistered(directory: string, why: string): string {
  return `adversarial-review-loop could not register ${directory} as a root of the run (${why}). Nothing written there is reviewed: call add_root for it, or tell the user.`
}

/** After a main-loop turn: continue the run when the engine says so. */
export async function continueRun($: EngineInterface): Promise<void> {
  if (!(await hasRun($))) return
  const next = await engine($, session.dir, ['next-action'])
  if (next.ok && next.action === 'continue' && typeof next.prompt === 'string') {
    await $.prompt.submit({ text: next.prompt })
  }
}
