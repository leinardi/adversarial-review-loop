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

// The reviewer subagent: its definition, one fresh run per review round, and what it may do.
//
// A run is spawned with `$.agent.spawn` and answers through the subagent's `turn.complete`. The
// tool call that spawned it waits for that answer, and a hook's own 10 s budget runs on while it
// awaits a plain promise: only a `$` call in flight stops the clock. So the wait is raced against
// a `sleep <timeout>` child: its pull is the `$` call in flight, its exit is the review's
// timeout, and an interrupt (`next.signal`) kills it and ends the wait.
//
// The model is checked before the reviewer may answer (tests/STEP0.md, L1 to L3): a spawn takes
// only an alias, the first spawn of a model in this session is preceded by one tiny completion on
// it, and the model the spawn reports must be the alias's own. A model that fails any of these is
// a `model` failure: the run asks the user, nothing is retried, and no answer of it is read.

import type { EngineInterface, TurnUsage } from 'claude-code'
import { REVIEWER_DESCRIPTION, REVIEWER_SYSTEM_PROMPT } from './text'

/** The reviewer's short name; its type is `<plugin>:reviewer`. */
export const REVIEWER = 'reviewer'

/**
 * Tools the reviewer is given. A native build registers no Grep or Glob, and searching then
 * goes through Bash, which the permission hook below holds to read-only commands.
 */
export const REVIEWER_TOOLS: readonly string[] = ['Read', 'Grep', 'Glob', 'Bash']

/** How a subagent's run ended, as its `turn.complete` said. */
export type Completion = {
  answer: string
  reason: string
  usage?: TurnUsage
  durationMs: number
  /** The model requests the run made, as its `turn.step`s counted them. */
  requests?: number
}

/** How a reviewer that gave no answer failed: retried, counted, or (`model`) put to the user at once. */
export type FailureKind = 'transient' | 'operational' | 'model'

/** A reviewer run: its answer, or why it gave none and how the failure budget counts it. */
export type Ran =
  | { ok: true; answer: string; usage?: TurnUsage; durationMs: number; requests?: number }
  | { ok: false; kind: FailureKind; error: string; usage?: TurnUsage; durationMs?: number; requests?: number }

/** The models a spawn takes (`$.agent.spawn` refuses a full id: tests/STEP0.md, L2). */
export const ALIASES: readonly string[] = ['opus', 'sonnet', 'haiku', 'fable']

/** HTTP statuses of a check that refuse the model itself; any other error may pass on a retry. */
const REFUSING_STATUSES: readonly number[] = [400, 401, 403, 404]

/** One review round's run. */
export type Task = {
  prompt: string
  description: string
  model: string
  timeoutSec: number
  /** What the reviewer may read: the bundle and every root. */
  dirs: readonly string[]
  /** The review bundle: a reviewer that read none of it gives no verdict. */
  bundle: string
  /** The calling hook's `next.signal`: aborted when the person interrupts the review. */
  signal?: AbortSignal
}

const waiting = new Map<string, (completion: Completion) => void>()
/** Answers that came before their waiter was set up; bounded, and dropped once read. */
const early = new Map<string, Completion>()
const EARLY_LIMIT = 32
/** Running reviewers, by agent id, with the directories each may read. */
const running = new Map<string, readonly string[]>()
/**
 * Every reviewer this session spawned. Nothing can stop a subagent, so one that timed out runs on:
 * once its review is over it may do nothing at all, rather than fall back on the session's rules.
 */
const everSpawned = new Set<string>()
/** Models this session's check answered for: each is checked once, before its first spawn. */
const checkedModels = new Set<string>()
/** Model requests per reviewer, counted from its `turn.step`s until its answer is delivered. */
const steps = new Map<string, number>()
/**
 * Reports reviewers handed back through `SubagentHandback`, by agent id, until their answer is
 * delivered. Only a call that ran is kept: one the session refused delivered nothing.
 */
const handbacks = new Map<string, string>()
/** The bundle each running reviewer reviews, by agent id. */
const bundles = new Map<string, string>()
/**
 * Reviewers that read part of their bundle, by agent id. The prompt lists the bundle's files and
 * holds none of them: an answer from a reviewer that never read one is a verdict on nothing, and
 * a well-formed APPROVED from it would approve a phase nobody saw.
 */
const readTheBundle = new Set<string>()
/** Spawns not resolved yet: a reviewer's first tool call can come before its id is known. */
let spawning = 0
let spawningDirs: readonly string[] = []
let spawningBundle = ''

export function reviewerType($: EngineInterface): string {
  return `${$.plugin.name}:${REVIEWER}`
}

/** Register the reviewer agent type; `effort` is the configured level, empty for the default. */
export async function registerReviewer($: EngineInterface, effort = ''): Promise<void> {
  await $.agent.register({
    name: REVIEWER,
    description: REVIEWER_DESCRIPTION,
    prompt: REVIEWER_SYSTEM_PROMPT,
    tools: REVIEWER_TOOLS,
    omitClaudeMd: true,
    ...(effort ? { effort } : {}),
  })
}

/**
 * Count one model request of a subagent (from a `turn.step` hook). Every subagent's is counted:
 * a reviewer's first request can come before its spawn resolves with its id. A count leaves on
 * the agent's `turn.complete`, and the map is bounded for one that never sends it.
 */
export function countStep(agentId: string): void {
  steps.set(agentId, (steps.get(agentId) ?? 0) + 1)
  if (steps.size > EARLY_LIMIT) {
    const oldest = steps.keys().next().value
    if (oldest !== undefined && oldest !== agentId) steps.delete(oldest)
  }
}

/**
 * Keep the report a reviewer handed back (from a `tool.call` hook, once the call ran).
 *
 * In auto mode the host tells a subagent that only a `SubagentHandback` call reaches its caller,
 * and the subagent's last text is then often a note about the handback, not its report. The
 * report it handed back is its answer.
 */
export function handBack(agentId: string, message: string): void {
  // A reviewer can hand back before its spawn resolves with its id, as its answer can (`early`):
  // kept while a spawn is pending too. Only the review waiting on this id ever reads it, after
  // the spawn's checks; any other agent's is dropped with its own completion (`deliver`).
  if (!running.has(agentId) && spawning === 0) return
  handbacks.set(agentId, message)
  if (handbacks.size > EARLY_LIMIT) {
    const oldest = handbacks.keys().next().value
    if (oldest !== undefined && oldest !== agentId) handbacks.delete(oldest)
  }
}

/** How a tool call came out, as far as a handback or a reviewer's read needs it. */
export type CallOutcome = { deny?: string; isError?: boolean; result?: unknown }

/**
 * Run a subagent's `SubagentHandback` call (`run` is the hook's `next`) and keep its report once
 * it ran: a handback the session refused, or one that failed, delivered nothing.
 */
export async function runHandback<T extends CallOutcome>(agentId: string, message: unknown, run: () => Promise<T>): Promise<T> {
  const outcome = await run()
  if (outcome.deny === undefined && outcome.isError !== true && typeof message === 'string') handBack(agentId, message)
  return outcome
}

/** Hand a subagent's answer to the review waiting on it (from a `turn.complete` hook). */
export function deliver(agentId: string, delivered: Completion): void {
  const counted = steps.get(agentId)
  steps.delete(agentId)
  const handed = handbacks.get(agentId)
  handbacks.delete(agentId)
  const answered = handed === undefined ? delivered : { ...delivered, answer: handed }
  const completion = counted === undefined ? answered : { ...answered, requests: counted }
  const resolve = waiting.get(agentId)
  if (resolve !== undefined) {
    waiting.delete(agentId)
    resolve(completion)
    return
  }
  early.set(agentId, completion)
  if (early.size > EARLY_LIMIT) {
    const oldest = early.keys().next().value
    if (oldest !== undefined) early.delete(oldest)
  }
}

/** Wait for `agentId`'s answer, at most `timeoutSec`; `undefined` on timeout. */
export async function answerOf($: EngineInterface, agentId: string, timeoutSec: number, signal?: AbortSignal): Promise<Completion | undefined> {
  const came = early.get(agentId)
  if (came !== undefined) {
    early.delete(agentId)
    return came
  }
  let resolve: (completion: Completion) => void = () => undefined
  const answered = new Promise<Completion>(done => {
    resolve = done
  })
  waiting.set(agentId, resolve)
  const clock = $.process.spawn({ argv: ['sleep', String(Math.max(1, Math.floor(timeoutSec)))] })
  try {
    const expired = clock.next().then(() => undefined)
    // Once the answer wins, ending the clock may reject this pull: nothing waits on it then.
    expired.catch(() => undefined)
    const came = await Promise.race([answered, expired])
    // An interrupt kills the clock, which may end its pull like a timeout: it is neither a
    // timeout nor an answer, and nothing is recorded for it.
    if (came === undefined && signal?.aborted) throw new Interrupted()
    return came
  } finally {
    waiting.delete(agentId)
    // Ends the sleep when the answer came first; a no-op once it has exited. Not awaited: the
    // review's outcome does not wait on the clock's teardown.
    void clock.return({ code: null, signal: null }).catch(() => undefined)
  }
}

function failed(kind: FailureKind, error: string, completion?: Completion): Ran {
  return { ok: false, kind, error, usage: completion?.usage, durationMs: completion?.durationMs, requests: completion?.requests }
}

/** The person interrupted the review: no failure is counted, and its ticket is left to be abandoned. */
export class Interrupted extends Error {
  constructor() {
    super('the review was interrupted')
  }
}

/** Forget which models were checked: a session that starts again checks them again. */
export function resetCheckedModels(): void {
  checkedModels.clear()
}

/**
 * Whether `model` can be used, by one tiny completion on it, once per session: `undefined` when
 * it answered, else why not. A refusal of the model (404, 403, 401, 400, or a model the session
 * blocks) is a `model` failure; a rate limit, a server error or no response is transient.
 */
export async function checkModel($: EngineInterface, model: string, signal?: AbortSignal): Promise<Ran | undefined> {
  if (checkedModels.has(model)) return undefined
  let result: Awaited<ReturnType<EngineInterface['model']['complete']>>
  try {
    result = await $.model.complete({ model, prompt: 'Reply OK.', maxTokens: 1, timeoutMs: 30000 }, signal ? { signal } : undefined)
  } catch (error) {
    // Only a request the engine refuses to send rejects: a model the session does not allow.
    return failed('model', `the session refused the model ${model}: ${String(error)}`)
  }
  if (result.isAnswered || result.reason === 'empty-reply') {
    checkedModels.add(model)
    return undefined
  }
  if (result.reason === 'aborted') {
    if (signal?.aborted) throw new Interrupted()
    return failed('transient', `the check of the model ${model} did not answer in time`)
  }
  const status = result.status
  if (status !== null && REFUSING_STATUSES.includes(status)) {
    return failed('model', `the API refused the model ${model} (${status} ${result.error})`)
  }
  return failed('transient', `the check of the model ${model} failed (${status ?? 'no response'} ${result.error})`)
}

/** Whether the model a spawn reports (a full id) is the alias's own: `opus` runs `claude-opus-…`. */
export function isModelOf(alias: string, reported: string): boolean {
  return reported.startsWith(`claude-${alias}-`)
}

/** Run one fresh reviewer on `task` and answer what it said, or why it said nothing. */
export async function runReviewer($: EngineInterface, task: Task): Promise<Ran> {
  if (!ALIASES.includes(task.model)) {
    return failed('model', `the subagent reviewer runs on one of ${ALIASES.join(', ')}, not ${task.model ? `'${task.model}'` : 'no model'}`)
  }
  const refused = await checkModel($, task.model, task.signal)
  if (refused !== undefined) return refused
  spawning += 1
  spawningDirs = task.dirs
  spawningBundle = task.bundle
  let agentId: string | undefined
  let ranOn: string
  try {
    const spawned = await $.agent.spawn({ subagentType: reviewerType($), prompt: task.prompt, description: task.description, model: task.model })
    if (spawned.deny !== undefined) return failed('operational', `the reviewer could not be started: ${spawned.deny}`)
    agentId = spawned.agentId
    ranOn = spawned.model
  } catch (error) {
    return failed('operational', `the reviewer could not be started: ${String(error)}`)
  } finally {
    spawning -= 1
  }
  if (agentId === undefined) return failed('operational', 'the reviewer could not be started: no agent id came back')
  everSpawned.add(agentId)
  if (!isModelOf(task.model, ranOn)) {
    // The session put another model in its place (an allowlist does, silently). Its answer is
    // never read, and once it is not running, its every tool call is denied.
    return failed('model', `the session started the reviewer on ${ranOn || 'an unnamed model'}, not ${task.model}`)
  }
  running.set(agentId, task.dirs)
  bundles.set(agentId, task.bundle)
  try {
    const completion = await answerOf($, agentId, task.timeoutSec, task.signal)
    // An interrupt may also end the reviewer's own run before its clock: still an interrupt.
    if (task.signal?.aborted) throw new Interrupted()
    const ran = classify(completion, task.timeoutSec)
    // Never a verdict from a reviewer that read none of the evidence (AGENTS.md rule 1).
    if (ran.ok && !readTheBundle.has(agentId)) return failed('operational', 'the reviewer read none of the bundle', completion)
    return ran
  } catch (error) {
    if (error instanceof Interrupted || task.signal?.aborted) throw new Interrupted()
    // The clock could not run, or the wait was interrupted: no answer either way. An interrupted
    // review's record may not land (the dispatch is ending), and its ticket is then abandoned.
    return failed('operational', `the reviewer's answer could not be waited for: ${String(error)}`)
  } finally {
    running.delete(agentId)
    bundles.delete(agentId)
    readTheBundle.delete(agentId)
  }
}

/**
 * What a reviewer run's end means for the review: an answer, or a failure the budget counts as
 * transient (a timeout, an API error: worth retrying) or operational (anything else).
 */
export function classify(completion: Completion | undefined, timeoutSec: number): Ran {
  if (completion === undefined) return failed('transient', `the reviewer did not answer within ${timeoutSec} s`)
  switch (completion.reason) {
    case 'answer':
      return { ok: true, answer: completion.answer, usage: completion.usage, durationMs: completion.durationMs, requests: completion.requests }
    case 'error':
      return failed('transient', 'the reviewer stopped on an API error', completion)
    case 'refusal':
      return failed('operational', 'the reviewer refused the task', completion)
    default:
      return failed('operational', `the reviewer's run ended early (${completion.reason})`, completion)
  }
}

/** `path` with `.` and `..` segments resolved; `undefined` for a relative path. */
export function normalize(path: string): string | undefined {
  if (!path.startsWith('/')) return undefined
  const parts: string[] = []
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') parts.pop()
    else parts.push(part)
  }
  return `/${parts.join('/')}`
}

/** Whether `path` is `dir` or lies under it. */
export function within(path: string, dir: string): boolean {
  const inner = normalize(path)
  const outer = normalize(dir)
  if (inner === undefined || outer === undefined) return false
  return outer === '/' || inner === outer || inner.startsWith(`${outer}/`)
}

async function dirsOf($: EngineInterface, agentId: string): Promise<readonly string[] | undefined> {
  const known = running.get(agentId)
  if (known !== undefined) return known
  if (spawning === 0) return undefined
  const agents = await $.agent.list()
  return agents.some(agent => agent.id === agentId && agent.type === reviewerType($)) ? spawningDirs : undefined
}

/** The path a reading tool's input names, if any. */
function pathOf(input: unknown): string | undefined {
  if (typeof input !== 'object' || input === null) return undefined
  const fields = input as Record<string, unknown>
  for (const key of ['file_path', 'path']) {
    if (typeof fields[key] === 'string') return fields[key]
  }
  return undefined
}

/** A permission check, as `tool.check` carries it. */
export type Check = { tool: string; input: unknown; agentId?: string; isReadOnly?: true }

/** A verdict for the session, or `'theirs'`: the session's own rules decide. */
export type Decision = { decision: 'allow' | 'deny'; reason: string } | 'theirs'

/** The reviewer's own verdict on a call, or `undefined` for a call that is not a reviewer's. */
export async function reviewerDecision($: EngineInterface, call: Check): Promise<Decision | undefined> {
  if (call.agentId === undefined) return undefined
  if (everSpawned.has(call.agentId) && !running.has(call.agentId)) {
    return { decision: 'deny', reason: 'This review is over: its answer is no longer waited for.' }
  }
  const dirs = await dirsOf($, call.agentId)
  if (dirs === undefined) return undefined
  return permit(dirs, call)
}

/**
 * The bundle files that hold the change itself: the diff chunks, a later round's increment, and
 * a repair's transcript. `range.txt` and the rest are orientation, not evidence.
 */
const EVIDENCE = /^(changes\.\d+\.diff|incremental\.diff|repair\.txt)$/
/** Commands that print a file's content, as a reviewer reads a diff chunk through Bash. */
const PRINTERS = new Set(['cat', 'head', 'tail', 'sed'])

/**
 * What a diff's content looks like: a file header or a hunk, possibly after a line number and a
 * tab (as Read numbers its lines), or the engine's own line for an increment with no change.
 */
const DIFF_CONTENT = /(^|\t)(diff --git |@@ -\d)|\(no change since the previous round\)/m

/** Every string in a tool result, one per line: Read's content, Bash's output, wherever the session puts them. */
function textOf(value: unknown, depth = 0): string {
  if (typeof value === 'string') return value
  if (depth > 6 || typeof value !== 'object' || value === null) return ''
  return Object.values(value)
    .map(inner => textOf(inner, depth + 1))
    .filter(text => text !== '')
    .join('\n')
}

/** Whether what a read of `name` printed is its evidence: a diff's content, or a repair transcript that is not empty. */
function showsEvidence(name: string, result: unknown): boolean {
  const text = textOf(result)
  return name.endsWith('.diff') ? DIFF_CONTENT.test(text) : text.trim() !== ''
}

function isEvidence(path: string, bundle: string): boolean {
  const normal = normalize(path)
  return normal !== undefined && within(normal, bundle) && parentOf(normal) === normalize(bundle) && EVIDENCE.test(normal.slice(normal.lastIndexOf('/') + 1))
}

function parentOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut <= 0 ? '/' : path.slice(0, cut)
}

/**
 * The evidence file in `bundle` a call reads: a Read of one, or a printing command naming one;
 * `undefined` for any other call.
 */
function evidenceRead(tool: string, input: unknown, bundle: string): string | undefined {
  if (tool === 'Read') {
    const path = pathOf(input)
    return path !== undefined && isEvidence(path, bundle) ? path : undefined
  }
  if (tool !== 'Bash') return undefined
  const command = (input as { command?: unknown } | null)?.command
  if (typeof command !== 'string' || SHELL_SYNTAX.test(command)) return undefined
  const argv = words(command.trim())
  if (argv === undefined || !PRINTERS.has(argv[0] ?? '')) return undefined
  return argv.slice(1).find(word => isEvidence(word, bundle))
}

/**
 * A subagent's tool call ran (from a `tool.call` hook, once `next(e)` answered): a reviewer
 * whose call read change evidence, succeeded and printed it has read the phase. A call that was
 * refused, failed (a file not there) or printed none of it read nothing, and neither did a
 * listing or a search.
 */
export function noteToolResult(agentId: string, tool: string, input: unknown, outcome: CallOutcome): void {
  if (outcome.deny !== undefined || outcome.isError === true) return
  const bundle = bundles.get(agentId) ?? (spawning > 0 ? spawningBundle : '')
  if (bundle === '') return
  const read = evidenceRead(tool, input, bundle)
  // A read that ran and printed nothing (`head -n 0`, an offset past the end) read nothing.
  if (read !== undefined && showsEvidence(read, outcome.result)) noteRead(agentId)
}

/** Run a subagent's tool call (`run` is the hook's `next`) and note what it read, once it ran. */
export async function runSubagentCall<T extends CallOutcome>(agentId: string, tool: string, input: unknown, run: () => Promise<T>): Promise<T> {
  const outcome = await run()
  noteToolResult(agentId, tool, input, outcome)
  return outcome
}

function noteRead(agentId: string): void {
  readTheBundle.add(agentId)
  if (readTheBundle.size > EARLY_LIMIT) {
    const oldest = readTheBundle.values().next().value
    if (oldest !== undefined && oldest !== agentId && !running.has(oldest)) readTheBundle.delete(oldest)
  }
}

/**
 * git subcommands a reviewer may run in a root: each only reads the repository. Anything that can
 * write (a ref, the index, a file, a tag or branch) is left out, and so is `status`, which
 * refreshes the index.
 */
const GIT_READS = new Set(['log', 'show', 'diff', 'rev-parse', 'ls-tree', 'ls-files', 'cat-file', 'merge-base', 'blame', 'grep', 'describe', 'show-ref', 'for-each-ref', 'name-rev', 'tag'])
/**
 * Options that make a reading subcommand write a file, start another program, or read files
 * outside the repository (`--no-index`).
 */
const GIT_UNSAFE_OPTIONS = /^(--output(=|$)|-O|--open-files-in-pager|--no-index)/
/** `git tag` options that create, delete or sign a tag: with `-l` it only lists. */
const TAG_WRITING_OPTIONS = /^(-[dfasumFe]$|--(delete|force|annotate|sign|local-user|message|file|edit))/
/** Characters that would make the command more than one plain git call. */
const SHELL_SYNTAX = /[;&|<>`$()\\\n\r]/

/** Split a command line on spaces, honouring single and double quotes; `undefined` if one is unclosed. */
function words(command: string): string[] | undefined {
  const found: string[] = []
  let current = ''
  let quote = ''
  let started = false
  for (const char of command) {
    if (quote !== '') {
      if (char === quote) quote = ''
      else current += char
    } else if (char === "'" || char === '"') {
      quote = char
      started = true
    } else if (char === ' ' || char === '\t') {
      if (started) found.push(current)
      current = ''
      started = false
    } else {
      current += char
      started = true
    }
  }
  if (quote !== '') return undefined
  if (started) found.push(current)
  return found
}

/**
 * Whether `command` is one plain read-only git call in a root the reviewer may read: `git -C <root>
 * <subcommand> ...`, or with no `-C` in the session's own directory. The session refuses every
 * `git -C`, so a reviewer could not check a commit or a tag of another root; the plugin trusts
 * its agents, and this lets the reviewer read history without a refused call.
 */
export function gitRead(command: string, dirs: readonly string[]): boolean {
  if (SHELL_SYNTAX.test(command)) return false
  const argv = words(command.trim())
  if (argv === undefined || argv[0] !== 'git') return false
  let at = 1
  let directory: string | undefined
  while (at < argv.length && argv[at]?.startsWith('-')) {
    const option = argv[at]
    if (option === '--no-pager') {
      at += 1
    } else if (option === '-C' && directory === undefined && at + 1 < argv.length) {
      directory = argv[at + 1]
      at += 2
    } else {
      // `-c` sets config, `--git-dir` and `--work-tree` point elsewhere, `--exec-path` runs others.
      return false
    }
  }
  if (directory !== undefined && !(directory.startsWith('/') && dirs.some(dir => within(directory, dir)))) return false
  const subcommand = argv[at]
  if (subcommand === undefined || !GIT_READS.has(subcommand)) return false
  const rest = argv.slice(at + 1)
  if (rest.some(arg => GIT_UNSAFE_OPTIONS.test(arg))) return false
  // `git tag` creates a tag unless it lists: only the listing form is a read.
  if (subcommand === 'tag') return rest.some(arg => arg === '-l' || arg === '--list') && !rest.some(arg => TAG_WRITING_OPTIONS.test(arg))
  return true
}

/** How many times a reviewer's handback can be sent back for its findings block. */
export const HANDBACK_REFUSALS = 2
/** Handbacks sent back so far, by agent id; bounded like the other per-agent maps. */
const refusedHandbacks = new Map<string, number>()

/** Whether a handback's report lacks the findings block the engine parses. */
function blockMissing(call: Check): boolean {
  const input = call.input as { message?: unknown } | null
  const message = typeof input === 'object' && input !== null ? input.message : undefined
  return typeof message === 'string' && !(message.includes('<<<ARL-FINDINGS>>>') && message.includes('<<<ARL-END>>>'))
}

/**
 * Send a handback without the findings block back to its reviewer, or, past the bound, leave it to
 * the session: the report then arrives without the block and the round fails, never approves.
 */
function refuseHandback(agentId: string | undefined): Decision {
  if (agentId !== undefined) {
    const count = refusedHandbacks.get(agentId) ?? 0
    if (count >= HANDBACK_REFUSALS) return 'theirs'
    refusedHandbacks.set(agentId, count + 1)
    if (refusedHandbacks.size > EARLY_LIMIT) {
      const oldest = refusedHandbacks.keys().next().value
      if (oldest !== undefined && oldest !== agentId) refusedHandbacks.delete(oldest)
    }
  }
  return {
    decision: 'deny',
    reason: 'This report has no findings block, so it cannot be read and the round would fail. Hand it back again, whole: your prose, then the <<<ARL-FINDINGS>>> ... <<<ARL-END>>> block exactly as your task message gives it.',
  }
}

/** What a reviewer that may read `dirs` may do: read, and run read-only commands. */
export function permit(dirs: readonly string[], call: Check): Decision {
  if (call.tool === 'Bash') {
    const command = (call.input as { command?: unknown } | null)?.command
    return call.isReadOnly || (typeof command === 'string' && gitRead(command, dirs))
      ? { decision: 'allow', reason: 'a read-only command of the review' }
      : { decision: 'deny', reason: 'The reviewer only reads: this command is not read-only. Use Read, Grep or Glob, one plain read-only command per call with no cd, variables, echo or redirection, or git -C <root> with a reading subcommand such as log, show, diff or rev-parse.' }
  }
  if (call.tool === 'Read' || call.tool === 'Grep' || call.tool === 'Glob') {
    const path = pathOf(call.input)
    // A read outside the bundle and the roots is left to the session's own rules.
    return path !== undefined && dirs.some(dir => within(path, dir)) ? { decision: 'allow', reason: 'a file of the review' } : 'theirs'
  }
  // Handing the report back is how auto mode delivers a subagent's answer, not a write. Only auto
  // mode's own classifier may allow it: a hook's allow is refused, and the report never arrives.
  // A hook may refuse one, though: a report without the findings block cannot be read, so the
  // reviewer is sent back to hand it in whole, a bounded number of times.
  if (call.tool === 'SubagentHandback') return blockMissing(call) ? refuseHandback(call.agentId) : 'theirs'
  return { decision: 'deny', reason: 'The reviewer only reads.' }
}
