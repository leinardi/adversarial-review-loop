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
}

/** A reviewer run: its answer, or why it gave none and how the failure budget counts it. */
export type Ran =
  | { ok: true; answer: string; usage?: TurnUsage; durationMs: number }
  | { ok: false; kind: 'transient' | 'operational'; error: string; usage?: TurnUsage; durationMs?: number }

/** One review round's run. */
export type Task = {
  prompt: string
  description: string
  model: string
  timeoutSec: number
  /** What the reviewer may read: the bundle and every root. */
  dirs: readonly string[]
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
/** Spawns not resolved yet: a reviewer's first tool call can come before its id is known. */
let spawning = 0
let spawningDirs: readonly string[] = []

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

/** Hand a subagent's answer to the review waiting on it (from a `turn.complete` hook). */
export function deliver(agentId: string, completion: Completion): void {
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

function failed(kind: 'transient' | 'operational', error: string, completion?: Completion): Ran {
  return { ok: false, kind, error, usage: completion?.usage, durationMs: completion?.durationMs }
}

/** The person interrupted the review: no failure is counted, and its ticket is left to be abandoned. */
export class Interrupted extends Error {
  constructor() {
    super('the review was interrupted')
  }
}

/** Run one fresh reviewer on `task` and answer what it said, or why it said nothing. */
export async function runReviewer($: EngineInterface, task: Task): Promise<Ran> {
  spawning += 1
  spawningDirs = task.dirs
  let agentId: string | undefined
  try {
    const spawned = await $.agent.spawn({
      subagentType: reviewerType($),
      prompt: task.prompt,
      description: task.description,
      ...(task.model ? { model: task.model } : {}),
    })
    if (spawned.deny !== undefined) return failed('operational', `the reviewer could not be started: ${spawned.deny}`)
    agentId = spawned.agentId
  } catch (error) {
    return failed('operational', `the reviewer could not be started: ${String(error)}`)
  } finally {
    spawning -= 1
  }
  if (agentId === undefined) return failed('operational', 'the reviewer could not be started: no agent id came back')
  everSpawned.add(agentId)
  running.set(agentId, task.dirs)
  try {
    const completion = await answerOf($, agentId, task.timeoutSec, task.signal)
    // An interrupt may also end the reviewer's own run before its clock: still an interrupt.
    if (task.signal?.aborted) throw new Interrupted()
    return classify(completion, task.timeoutSec)
  } catch (error) {
    if (error instanceof Interrupted || task.signal?.aborted) throw new Interrupted()
    // The clock could not run, or the wait was interrupted: no answer either way. An interrupted
    // review's record may not land (the dispatch is ending), and its ticket is then abandoned.
    return failed('operational', `the reviewer's answer could not be waited for: ${String(error)}`)
  } finally {
    running.delete(agentId)
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
      return { ok: true, answer: completion.answer, usage: completion.usage, durationMs: completion.durationMs }
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
  return dirs === undefined ? undefined : permit(dirs, call)
}

/** What a reviewer that may read `dirs` may do: read, and run read-only commands. */
export function permit(dirs: readonly string[], call: Check): Decision {
  if (call.tool === 'Bash') {
    return call.isReadOnly
      ? { decision: 'allow', reason: 'a read-only command of the review' }
      : { decision: 'deny', reason: 'The reviewer only reads: this command is not read-only. Use Read, or a read-only command such as grep, find, ls or git show.' }
  }
  if (call.tool === 'Read' || call.tool === 'Grep' || call.tool === 'Glob') {
    const path = pathOf(call.input)
    // A read outside the bundle and the roots is left to the session's own rules.
    return path !== undefined && dirs.some(dir => within(path, dir)) ? { decision: 'allow', reason: 'a file of the review' } : 'theirs'
  }
  // Handing the report back is how some sessions deliver a subagent's answer, not a write.
  if (call.tool === 'SubagentHandback') return { decision: 'allow', reason: "the reviewer's report" }
  return { decision: 'deny', reason: 'The reviewer only reads.' }
}
