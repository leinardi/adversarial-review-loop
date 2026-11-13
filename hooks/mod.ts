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

// The adversarial-review-loop mod: the tools Claude calls, the reviewer subagent, root
// registration before a file is written, the run's continuation at the end of a turn, the
// person's slash commands, and the band and pane that show the run.
// Every decision about the run is the engine's (`scripts/arl/`); see flow.ts.

import type { EngineInterface, Register } from 'claude-code'
import { COMMANDS, openPane, registerCommands, runCommand } from './commands'
import { engine } from './engine'
import { addRoot, clarify, continueRun, ensureRoot, parentOf, type Registration, review, session, resetWrites, setPhases, sweepRoots, track, unregistered, writeIn } from './flow'
import { deliver, registerReviewer, REVIEWER, reviewerDecision } from './reviewer'
import { TOOLS, type ToolName } from './text'
import { pane, registerUi, showBusy } from './ui'

/** Serve one of the plugin's tools; `undefined` for a name it does not register. */
async function serve($: EngineInterface, name: string, input: Record<string, unknown>, signal: AbortSignal): Promise<string | undefined> {
  switch (name as ToolName) {
    case 'set_phases':
      return setPhases($, input)
    case 'add_root':
      return addRoot($, input)
    case 'review':
      return review($, input, signal)
    case 'clarify':
      return clarify($, input, signal)
    default:
      return undefined
  }
}

/** This plugin's tools by the full name the host gave each (`mcp__<plugin>__<name>`), to its own name. */
const tools = new Map<string, string>()

async function registerTools($: EngineInterface): Promise<void> {
  for (const [name, spec] of Object.entries(TOOLS)) {
    const registered = await $.tool.register({ name, description: spec.description, inputSchema: spec.inputSchema })
    tools.set(registered.tool, name)
  }
}

/** The tool's arguments, without the envelope `tool.call` adds. */
function argumentsOf(e: Record<string, unknown>): Record<string, unknown> {
  const rest = { ...e }
  for (const key of ['tool', 'tool_use_id', 'agentId', 'requestMeta']) delete rest[key]
  return rest
}

/**
 * A session starts here: find the run and show it, and say when another session's review is
 * pending. Answers the reviewer effort the configuration names.
 */
async function attach($: EngineInterface): Promise<string> {
  const answer = await engine($, session.dir, ['attach', '--session', session.id])
  if (!answer.ok) {
    $.ui.log(`Could not read the run here: ${answer.error ?? 'no reason given'}`)
    return ''
  }
  await track($, answer)
  const found = answer.run as { status?: string } | null | undefined
  // A session that starts on a run shows it: nothing else would say a run is here.
  if (found?.status === 'active' || found?.status === 'paused') await openPane($)
  if (answer.foreign_review === true) {
    $.ui.log('A review another session started is pending. If that session is gone, /arl-resume abandons it and continues the run here.')
  }
  if (typeof answer.effort_note === 'string') $.ui.log(answer.effort_note)
  return typeof answer.effort === 'string' ? answer.effort : ''
}

/** The file a writing tool is about to change, if `e` is one. */
function writtenFile(e: { tool: string } & Record<string, unknown>): string | undefined {
  if (e.tool === 'Edit' || e.tool === 'Write') return typeof e.file_path === 'string' ? e.file_path : undefined
  if (e.tool === 'NotebookEdit') return typeof e.notebook_path === 'string' ? e.notebook_path : undefined
  return undefined
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    session.dir = e.cwd
    session.isInteractive = e.isInteractive
    session.id = await $.session.id()
    // A reload starts the session's hooks again: what an earlier session wrote is not this one's.
    resetWrites()
    // A pane the person closed stays closed for their session, not for the next one (`/clear`).
    pane.dismissed = false
    // A session that ended mid-turn left the flag set; this one has run no turn yet.
    await showBusy($, false).catch(() => undefined)
    await registerTools($)
    const effort = await attach($).catch(() => '')
    // The reviewer before anything optional: without it every review fails. An effort the host
    // refuses falls back to the default.
    await registerReviewer($, effort).catch(async (error: unknown) => {
      $.ui.log(`The reviewer could not be registered with effort ${effort} (${String(error)}); using the default.`)
      await registerReviewer($)
    })
    await registerCommands($)
    return started
  })

  // The reviewer is the plugin's alone: Claude never sees it in its list of agents.
  on('agent.offer', ($, e, next) => (e.agent === `${$.plugin.name}:${REVIEWER}` ? { isOffered: false } : next(e)))

  on('tool.call', async ($, e, next) => {
    const call = e as unknown as { tool: string } & Record<string, unknown>
    const own = tools.get(call.tool)
    if (own !== undefined) {
      const answer = await serve($, own, argumentsOf(call), next.signal)
      return answer === undefined ? next(e) : { result: answer }
    }
    const file = writtenFile(call)
    if (file === undefined) return next(e)
    // A write the session's rules deny makes no root. One that may run (allowed, or put to the
    // person) registers its folder first, so the baseline is the content before the change.
    const check = await $.tool.check({ tool: call.tool, input: argumentsOf(call) } as never).catch(() => undefined)
    if (check?.decision === 'deny') return next(e)
    const folder = parentOf(file)
    const { result, registration, dropped } = await writeIn(
      $,
      folder,
      // A registration that fails is said, never dropped: the write would land unreviewed.
      () => ensureRoot($, folder).catch((error: unknown): Registration => ({ note: unregistered(folder, String(error)) })),
      () => next(e),
      written => written.deny === undefined && written.isError !== true,
    )
    if (dropped) return result
    if (result.deny !== undefined || result.isError === true) return result
    return registration.note === undefined ? result : { ...result, context: [...(result.context ?? []), registration.note] }
  })

  on('tool.check', async ($, e, next) => {
    // The plugin's own tools: insurance against a host that would prompt for them.
    if (tools.has(e.tool) && e.agentId === undefined) return { decision: 'allow', reason: 'adversarial-review-loop tool' }
    const verdict = await reviewerDecision($, e)
    if (verdict === undefined || verdict === 'theirs') return next(e)
    return verdict
  }).catch(($, e, next) => (e.agentId === undefined ? next(e) : { decision: 'deny', reason: 'the reviewer permission check failed' }))

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) await showBusy($, true).catch(() => undefined)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) deliver(e.agentId, { answer: e.answer, reason: e.reason, usage: e.usage, durationMs: e.durationMs })
    // Whatever ended it (an answer, an interrupt, an error), the main turn is over.
    if (e.agentId === undefined) await showBusy($, false).catch(() => undefined)
    if (e.agentId === undefined) await sweepRoots($)
    const done = await next(e)
    // An interrupted or failed turn hands control back to the person.
    if (e.agentId === undefined && e.reason === 'answer') await continueRun($)
    return done
  })

  // A directory added to the session mid-run (`/add-dir`) joins the run as a root.
  on('classic.DirectoryAdded', async ($, e, next) => {
    // Nothing here reaches Claude, so the person is told what Claude would have been.
    const { note } = await ensureRoot($, e.directory, { explicit: true }).catch((error: unknown) => ({ note: unregistered(e.directory, String(error)) }))
    if (note !== undefined) $.ui.log(note)
    return next(e)
  })

  for (const spec of COMMANDS) {
    on('command.run', { command: spec.name }, async ($, e) => {
      const text = await runCommand($, e.command, e.args)
      return { text: text ?? `No command ${e.command} here.` }
    }).catch(($, e) => ({ text: `/${e.command} failed.` }))
  }

  registerUi(on)
}
