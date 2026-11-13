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
// registration before a file is written, and the run's continuation at the end of a turn.
// Every decision about the run is the engine's (`scripts/arl/`); see flow.ts.

import type { EngineInterface, Register } from 'claude-code'
import { addRoot, clarify, continueRun, ensureRoot, parentOf, review, session, setPhases, unregistered } from './flow'
import { deliver, registerReviewer, REVIEWER, reviewerDecision } from './reviewer'
import { TOOLS, type ToolName } from './text'

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
    await registerTools($)
    await registerReviewer($)
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
    // A registration that fails is said, never dropped: the write would land unreviewed.
    const note = await ensureRoot($, parentOf(file)).catch((error: unknown) => unregistered(parentOf(file), String(error)))
    const result = await next(e)
    return note === undefined || result.deny !== undefined ? result : { ...result, context: [...(result.context ?? []), note] }
  })

  on('tool.check', async ($, e, next) => {
    // The plugin's own tools: insurance against a host that would prompt for them.
    if (tools.has(e.tool) && e.agentId === undefined) return { decision: 'allow', reason: 'adversarial-review-loop tool' }
    const verdict = await reviewerDecision($, e)
    if (verdict === undefined || verdict === 'theirs') return next(e)
    return verdict
  }).catch(($, e, next) => (e.agentId === undefined ? next(e) : { decision: 'deny', reason: 'the reviewer permission check failed' }))

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) deliver(e.agentId, { answer: e.answer, reason: e.reason, usage: e.usage, durationMs: e.durationMs })
    const done = await next(e)
    // An interrupted or failed turn hands control back to the person.
    if (e.agentId === undefined && e.reason === 'answer') await continueRun($)
    return done
  })

  // A directory added to the session mid-run (`/add-dir`) joins the run as a root.
  on('classic.DirectoryAdded', async ($, e, next) => {
    // Nothing here reaches Claude, so the person is told what Claude would have been.
    const note = await ensureRoot($, e.directory, { explicit: true }).catch((error: unknown) => unregistered(e.directory, String(error)))
    if (note !== undefined) $.ui.log(note)
    return next(e)
  })
}
