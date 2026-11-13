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

// The world beneath the mod in its tests: a scripted engine, files in memory, and the user's
// answers. Nothing here runs Python; the engine's own behaviour is the pytest suite's.
//
// The test kit answers `$.agent.spawn` with a model and no agent id (only the engine's own Agent
// tool sets one), so no test here starts a reviewer through the kit: the review flow is driven
// directly (`reviewWith`, `clarifyWith`) on a hand-made `$` (`fakeEngine`) with a stand-in
// reviewer, and the wait for a subagent's answer is tested on its own.

import type { EngineInterface, On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { type Completion, deliver, type Ran, type Task } from '../../hooks/reviewer'

// The test runtime has timers; the hooks' type library (ES only) does not declare them.
declare function setTimeout(callback: () => void, ms: number): unknown

export const PLUGIN = 'adversarial-review-loop'
export const HOME = '/home/me'
const ENV: Record<string, string | undefined> = { HOME, TMPDIR: '/run/user/tmp' }
export const LAUNCH = '/work'
export const tool = (name: string): string => `mcp__${PLUGIN}__${name}`

/** One engine command as the mod ran it, after `--dir <launch dir>`. */
export type Call = { args: string[]; stdin: string }

/** What a scripted engine answers, by command (`review record`, `status`): a value, or a function of the call. */
export type Script = Record<string, unknown>

/** A run summary as `RunState.summary` prints it. */
export function summary(fields: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run_id: 'r1',
    status: 'active',
    stage: 'phase',
    phase: 1,
    phase_count: 2,
    phases: ['one', 'two'],
    phases_revision: 1,
    approved_phases: 0,
    rounds: 0,
    final_required: false,
    pending_review: false,
    waiting: null,
    roots: [{ alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', proposed: '' }],
    ...fields,
  }
}

export const PREPARED = {
  ok: true,
  review_id: 'r1-1',
  label: 'phase1',
  round: 1,
  harness: 'subagent',
  model: "the session's default",
  reviewer_model: '',
  timeout_sec: 900,
  prompt_file: '/work/.arl/reviews/r1-001/prompt.md',
  bundle_dir: '/work/.arl/reviews/r1-001/bundle',
  roots: [LAUNCH, '/elsewhere/web'],
  answer_file: '/work/.arl/reviews/r1-001/answer.md',
}

export const RECORDED = {
  ok: true,
  status: 'recorded',
  verdict: 'APPROVED',
  label: 'phase1',
  report: '/r.md',
  message: 'The review of phase 1 approved the change.',
  run: summary(),
}

export const PROMPTS = { [PREPARED.prompt_file]: 'Review phase 1.', '/work/.arl/reviews/r1-001/repair-prompt.md': 'Repair it.' }

function commandOf(args: readonly string[]): string {
  return args[0] === 'review' || args[0] === 'clarify' ? `${args[0]} ${args[1]}` : (args[0] ?? '')
}

export type World = {
  /** Every engine command run, in order. */
  calls: Call[]
  /** Files the mod wrote. */
  written: Map<string, string>
  /** Prompts the mod submitted. */
  submitted: string[]
  /** Questions put to the user, with their options. */
  asked: { question: string; options: string[] }[]
  /** Reviewer spawns through the kit. */
  spawns: { prompt: string; type: string; model?: string }[]
  /** The engine commands run, by name. */
  commands: () => string[]
  /** Reviewer clocks that ran out. */
  expired: number
  /** Lines the mod logged for the person. */
  logged: string[]
  /** What the mod registered: tools and agent types, as specified. */
  registered: { tools: Record<string, unknown>[]; agents: Record<string, unknown>[] }
}

export type WorldOptions = {
  engine: Script
  /** Files the mod reads. */
  files?: Record<string, string>
  /** What the user picks at each question, in order; a missing answer dismisses it. */
  choices?: string[]
  /** Whether `<launch>/.arl/state.json` exists. */
  hasState?: boolean
  /** Paths that do not exist; every other path does. */
  missing?: string[]
  /** The state file's modification time, as `$.fs.stat` reads it. */
  stateMtime?: number
  /** The server name the host gives the plugin's tools (`mcp__<it>__<name>`); the plugin's own name by default. */
  toolServer?: string
  /** Make `$.env.get` reject, as a failing host would. */
  envFails?: boolean
  /** Make `$.fs.exists` reject. */
  existsFails?: boolean
  /**
   * How each subagent the hand-made `$` spawns ends, in order: delivered as its `turn.complete`
   * would be. `undefined` never answers; `'deny'` refuses the spawn; `'throw'` rejects it.
   */
  agents?: (Completion | undefined | 'deny' | 'throw')[]
  /** Called with each spawned agent's id before it answers, as its tool calls would come. */
  whileRunning?: (agentId: string) => Promise<void>
}

/** The scripted engine and the user, shared by the kit's stand-ins and the hand-made `$`. */
function scripted(options: WorldOptions): { state: World; answer: (argv: readonly string[], stdin: string) => string; choose: (question: string, labels: string[]) => string | undefined } {
  const state: World = {
    calls: [],
    written: new Map(),
    submitted: [],
    asked: [],
    spawns: [],
    commands: () => state.calls.map(call => commandOf(call.args)),
    expired: 0,
    logged: [],
    registered: { tools: [], agents: [] },
  }
  const answer = (argv: readonly string[], stdin: string): string => {
    if (argv[0] !== '--dir' || argv[1] !== LAUNCH) throw new Error(`the engine runs for ${LAUNCH}: ${argv.join(' ')}`)
    const call = { args: argv.slice(2), stdin }
    state.calls.push(call)
    const scripted = options.engine[commandOf(call.args)]
    const value = typeof scripted === 'function' ? (scripted as (call: Call) => unknown)(call) : (scripted ?? { ok: false, code: 'unscripted', error: commandOf(call.args) })
    return `${JSON.stringify(value)}\n`
  }
  const choices = [...(options.choices ?? [])]
  const choose = (question: string, labels: string[]): string | undefined => {
    state.asked.push({ question, options: labels })
    return choices.shift()
  }
  return { state, answer, choose }
}

function exists(options: WorldOptions, path: string): boolean {
  if (path === `${LAUNCH}/.arl/state.json`) return options.hasState ?? true
  return !(options.missing ?? []).includes(path)
}

function read(options: WorldOptions, state: World, path: string): string {
  const text = options.files?.[path] ?? state.written.get(path)
  if (text === undefined) throw new Error(`ENOENT: ${path}`)
  return text
}

/** Stand in, through the test kit, for everything beneath the plugin. */
export function world(on: On, options: WorldOptions): World {
  const { state, answer, choose } = scripted(options)
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'session-1' }))
  on('tool.register', (_, e) => {
    state.registered.tools.push(e as Record<string, unknown>)
    return { value: { tool: `mcp__${options.toolServer ?? PLUGIN}__${e.name}` } }
  })
  on('agent.register', (_, e) => {
    state.registered.agents.push(e as Record<string, unknown>)
    return { value: { agent: `${PLUGIN}:${e.name}` } }
  })
  on('agent.list', () => ({ value: [] }))
  on('fs.exists', (_, e) => {
    if (options.existsFails) throw new Error('fs unavailable')
    return { value: exists(options, e.path) }
  })
  on('fs.stat', () => ({ value: { mtimeMs: options.stateMtime ?? 1, size: 1, isFile: true, isDirectory: false, isSymbolicLink: false } as never }))
  on('env.get', (_, e) => {
    if (options.envFails) throw new Error('env unavailable')
    return { value: ENV[e.name] }
  })
  on('ui.log', (_, e) => {
    state.logged.push(e.text)
    return { value: undefined }
  })
  on('fs.read', (_, e) => ({ value: read(options, state, e.path) }))
  on('fs.write', (_, e) => {
    state.written.set(e.path, e.text)
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    if (!e.argv[0]?.endsWith('/scripts/arl.sh')) throw new Error(`unexpected process ${e.argv.join(' ')}`)
    return { value: { exitCode: 0, stdout: answer(e.argv.slice(1), e.init?.stdin ?? ''), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // A stand-in for a streaming call on `$` yields its pieces and returns `{ value }`, the exit.
  const exited = { value: { code: 0, signal: null } }
  on('process.spawn', async function* (_: unknown, e: { argv: readonly string[]; input?: string }) {
    if (e.argv[0] === 'sleep') {
      // The reviewer's clock: `sleep 1` expires at once; any other runs until the mod ends it.
      if (e.argv[1] === '1') return exited
      await new Promise(() => undefined)
    }
    yield { stream: 'stdout' as const, text: answer(e.argv.slice(1), e.input ?? '') }
    return exited
  } as never)
  on('agent.spawn', (_, e) => {
    // The kit hands the Agent tool's own spelling of the type.
    const fields = e as unknown as Record<string, string | undefined>
    state.spawns.push({ prompt: e.prompt, type: fields.subagent_type ?? e.subagentType, model: e.model })
    return { model: e.model ?? 'inherit' }
  })
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('agent.offer', () => ({ isOffered: true }))
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    const questions = e.questions as { question: string; options: { label: string }[] }[]
    const first = questions[0]
    const choice = choose(first?.question ?? '', first?.options.map(option => option.label) ?? [])
    if (choice === undefined) return { deny: 'The user dismissed the question.' }
    return { result: { questions, answers: { [first?.question ?? '']: choice } } } as never
  })
  on('ui.notify', () => ({ value: { isSent: false, reason: 'no-surface' } }))
  on('prompt.submit', (_, e) => {
    state.submitted.push(e.text)
    return { text: e.text }
  })
  on('classic.DirectoryAdded', () => ({}) as never)
  return state
}

/**
 * A hand-made engine interface over the same script, for driving the mod's own functions
 * directly: the kit's `$` is the engine's side of the hooks and has no `process` or `fs`.
 */
export function fakeEngine(options: WorldOptions): { $: EngineInterface; w: World } {
  const { state, answer, choose } = scripted(options)
  const agents = [...(options.agents ?? [])]
  const fake = {
    plugin: { name: PLUGIN, root: '/plugin' },
    process: {
      run: async (argv: readonly string[], init?: { stdin?: string }) => ({ exitCode: 0, stdout: answer(argv.slice(1), init?.stdin ?? ''), stderr: '' }),
      spawn: (request: { argv: readonly string[]; input?: string }) => {
        const pieces = async function* () {
          if (request.argv[0] === 'sleep') {
            // The reviewer's clock: `sleep 1` expires at once; any other runs until it is ended.
            if (request.argv[1] !== '1') await new Promise(() => undefined)
            state.expired += 1
            return { code: 0, signal: null }
          }
          yield { stream: 'stdout' as const, text: answer(request.argv.slice(1), request.input ?? '') }
          return { code: 0, signal: null }
        }
        const stream = pieces()
        return Object.assign(stream, { result: Promise.resolve({ code: 0, signal: null }) })
      },
    },
    fs: {
      exists: async (path: string) => exists(options, path),
      stat: async () => ({ mtimeMs: options.stateMtime ?? 1 }),
      read: async (path: string) => read(options, state, path),
      write: async (path: string, text: string) => {
        state.written.set(path, text)
      },
    },
    ui: {
      ask: async (question: string, labels: string[]) => {
        const choice = choose(question, labels)
        if (choice === undefined) throw new Error('$.ui.ask: no answer')
        return choice
      },
      notify: async () => ({ isSent: false, reason: 'no-surface' }),
    },
    prompt: {
      submit: async ({ text }: { text: string }) => {
        state.submitted.push(text)
        return {}
      },
    },
    env: { get: async (name: string) => ENV[name] },
    agent: {
      spawn: async (args: { prompt: string; subagentType?: string; model?: string }) => {
        state.spawns.push({ prompt: args.prompt, type: args.subagentType ?? '', model: args.model })
        const end = agents.shift()
        if (end === 'deny') return { deny: 'no agents here' }
        if (end === 'throw') throw new Error('the session is ending')
        const agentId = `agent-${state.spawns.length}`
        if (end !== undefined) {
          setTimeout(() => {
            void (options.whileRunning?.(agentId) ?? Promise.resolve()).then(() => deliver(agentId, end))
          }, 0)
        }
        return { model: args.model ?? 'inherit', agentId }
      },
      list: async () => [],
    },
  }
  return { $: fake as unknown as EngineInterface, w: state }
}

export async function start($: Engine, isInteractive = false): Promise<void> {
  await $.session.start({ cwd: LAUNCH, surface: isInteractive ? 'terminal' : null, isInteractive })
}

/** Call one of the mod's tools as Claude would, and answer its text. */
export async function callTool($: Engine, name: string, input: Record<string, unknown> = {}): Promise<string> {
  const result = await $.tool.call({ tool: tool(name), ...input } as never)
  if (result.deny !== undefined) return `DENIED ${result.deny}`
  return String(result.result)
}

/** A reviewer that answers each task in turn, recording what it was given. */
export function reviewer(runs: Ran[]): { tasks: Task[]; run: (task: Task) => Promise<Ran> } {
  const tasks: Task[] = []
  const queue = [...runs]
  return {
    tasks,
    run: async task => {
      tasks.push(task)
      return queue.shift() ?? { ok: false, kind: 'operational', error: 'no more scripted reviewer runs' }
    },
  }
}
