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

// The loop around the reviews: the tools Claude is given, phases and roots, a root registered
// before a file is written, and the run continued when a turn ends.

import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { expect, test } from 'claude-code/testing'
import { callTool, HOME, LAUNCH, start, summary, world } from './world'

const STATUS = { ok: true, run: summary() }

/** A main-loop turn ending, as the engine raises it. */
function turnEnds($: Engine, fields: Record<string, unknown> = {}): Promise<unknown> {
  return $.turn.complete({ answer: 'done', reason: 'answer', durationMs: 5, isAborted: false, turnId: 't1', ...fields } as never)
}

/** Stand in for the file tools themselves, beneath the mod. */
function fileTools(on: On): void {
  on('tool.call', { tool: 'Edit' }, () => ({ result: { type: 'update' } }) as never)
  on('tool.call', { tool: 'Write' }, () => ({ result: { type: 'create' } }) as never)
}

// --------------------------------------------------------------------------
// The tools
// --------------------------------------------------------------------------

test('every session gets the four tools, each with a schema', async ($, on) => {
  const w = world(on, { engine: {} })
  await start($)
  expect(w.registered.tools.map(spec => spec.name)).toEqual(['set_phases', 'add_root', 'review'])
  for (const spec of w.registered.tools) {
    expect(typeof spec.description).toBe('string')
    expect(spec.inputSchema).toEqual(expect.objectContaining({ type: 'object' }))
  }
})

test('each description says its rule within what tool search shows of it', async ($, on) => {
  // Code Mode's tool search shows about the first 160 characters of a description.
  const w = world(on, { engine: {} })
  await start($)
  const shown = new Map(w.registered.tools.map(spec => [spec.name, String(spec.description).slice(0, 160)]))
  expect(shown.get('set_phases')).toContain('before changing any file')
  expect(shown.get('add_root')).toContain('never a scratch')
  expect(shown.get('review')).toContain('commit each changed git repository')
})

test('set_phases hands the engine the list on stdin', async ($, on) => {
  const w = world(on, { engine: { 'set-phases': { ok: true, run: summary({ phases: ['Engine', 'Mod'], phases_revision: 2 }) } } })
  await start($)
  const text = await callTool($, 'set_phases', { phases: ['Engine', 'Mod'] })
  expect(w.calls).toEqual([{ args: ['set-phases'], stdin: '["Engine","Mod"]' }])
  expect(text).toContain('revision 2')
  expect(text).toContain('1. Engine; 2. Mod')
})

test('set_phases with replan says so, and a refusal is passed on', async ($, on) => {
  const w = world(on, { engine: { 'set-phases': { ok: false, code: 'replan-required', error: 'phase 1 is approved' } } })
  await start($)
  expect(await callTool($, 'set_phases', { phases: ['x'], replan: true })).toContain('(replan-required): phase 1 is approved')
  expect(w.calls[0]?.args).toEqual(['set-phases', '--replan'])
})

test('add_root registers a directory and reports its baseline', async ($, on) => {
  const w = world(on, { engine: { 'root-add': { ok: true, status: 'added', alias: 'lib', baseline: 'set', proposed: '' } } })
  await start($)
  expect(await callTool($, 'add_root', { path: '../lib' })).toContain('Registered root lib')
  expect(w.calls[0]?.args).toEqual(['root-add', '../lib', '--not-scratch'])
})

test('add_root on a directory that already changed asks the user for its baseline', async ($, on) => {
  const unknown = summary({ roots: [{ alias: 'lib', path: '/lib', kind: 'dir', baseline: 'unknown', proposed: '' }] })
  const w = world(on, {
    engine: {
      'root-add': { ok: true, status: 'added', alias: 'lib', baseline: 'unknown', proposed: '' },
      status: { ok: true, run: unknown },
      'root-baseline': { ok: true, run: summary() },
    },
    choices: ['Everything here is new'],
  })
  await start($, true)
  expect(await callTool($, 'add_root', { path: '/lib' })).toContain('with the baseline the user chose')
  expect(w.asked[0]?.options).toEqual(['Everything here is new', 'The current content is the baseline'])
  expect(w.calls.at(-1)?.args).toEqual(['root-baseline', 'lib', '--empty'])
})

test('headless, a root with an unknown baseline is reported, not guessed', async ($, on) => {
  const unknown = summary({ roots: [{ alias: 'lib', path: '/lib', kind: 'dir', baseline: 'unknown', proposed: '' }] })
  const w = world(on, {
    engine: { 'root-add': { ok: true, status: 'added', alias: 'lib', baseline: 'unknown', proposed: '' }, status: { ok: true, run: unknown } },
  })
  await start($)
  expect(await callTool($, 'add_root', { path: '/lib' })).toContain('its baseline is unknown')
  expect(w.commands()).not.toContain('root-baseline')
})

// --------------------------------------------------------------------------
// A root before the first write
// --------------------------------------------------------------------------

test('a file written outside every root registers its directory before the write runs', async ($, on) => {
  const order: string[] = []
  const w = world(on, {
    engine: {
      status: () => {
        order.push('status')
        return STATUS
      },
      'root-add': () => {
        order.push('root-add')
        return { ok: true, status: 'added', alias: 'web', baseline: 'set', proposed: '' }
      },
    },
  })
  on('tool.call', { tool: 'Write' }, () => {
    order.push('write')
    return { result: { type: 'create' } } as never
  })
  await start($)

  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/web/new/a.ts', content: 'x' })

  expect(w.calls.find(call => call.args[0] === 'root-add')?.args).toEqual(['root-add', '/elsewhere/web/new', '--not-scratch'])
  expect(order.indexOf('root-add')).toBeLessThan(order.indexOf('write'))
  expect(result.deny === undefined && result.context?.join('\n')).toContain('registered /elsewhere/web/new as root web')
})

test('a file inside a known root costs no registration', async ($, on) => {
  const w = world(on, { engine: { status: STATUS } })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Edit', file_path: `${LAUNCH}/src/a.ts`, old_string: 'a', new_string: 'b' })
  await $.tool.call({ tool: 'Edit', file_path: `${LAUNCH}/src/b.ts`, old_string: 'a', new_string: 'b' })
  expect(w.commands()).toEqual(['status'])
})

test('with no run here, a write asks the engine nothing', async ($, on) => {
  const w = world(on, { engine: {}, hasState: false })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/anywhere/a.ts', content: 'x' })
  expect(w.calls).toEqual([])
})

test('a stopped run registers nothing', async ($, on) => {
  const w = world(on, { engine: { status: { ok: true, run: summary({ status: 'stopped' }) } } })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/anywhere/a.ts', content: 'x' })
  expect(w.commands()).toEqual(['status'])
})

test('a directory added to the session joins the run as a root', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: true, status: 'added', alias: 'extra', baseline: 'set', proposed: '' } } })
  await start($)
  await $.classic.DirectoryAdded({ directory: '/extra', source: 'slash_command' })
  expect(w.calls.find(call => call.args[0] === 'root-add')?.args).toEqual(['root-add', '/extra'])
})

// --------------------------------------------------------------------------
// The turn's end
// --------------------------------------------------------------------------

const CONTINUE = { ok: true, action: 'continue', stage: 'phase', phase: 2, prompt: 'Phase 1 is approved. Implement phase 2: Mod.', token: 'r1:2:0:1' }

test('a turn that ends with work left continues the run with the engine prompt', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'next-action': CONTINUE } })
  await start($)
  await turnEnds($)
  expect(w.submitted).toEqual([CONTINUE.prompt])
})

test('a turn the engine says to yield on is not continued', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'next-action': { ok: true, action: 'yield', reason: 'waiting' } } })
  await start($)
  await turnEnds($)
  expect(w.commands()).toContain('next-action')
  expect(w.submitted).toEqual([])
})

test('an interrupted or failed turn hands control back without asking the engine', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'next-action': CONTINUE } })
  await start($)
  await turnEnds($, { reason: 'aborted', isAborted: true })
  await turnEnds($, { reason: 'error' })
  expect(w.commands()).not.toContain('next-action')
  expect(w.submitted).toEqual([])
})

test("a subagent's turn never continues the run", async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'next-action': CONTINUE } })
  await start($)
  await turnEnds($, { agentId: 'agent-1' })
  expect(w.submitted).toEqual([])
})

test('with no run here, a turn end asks the engine nothing', async ($, on) => {
  const w = world(on, { engine: {}, hasState: false })
  await start($)
  await turnEnds($)
  expect(w.calls).toEqual([])
})

test('a file in a folder being created registers the folder being created, not its deepest subfolder', async ($, on) => {
  const w = world(on, {
    engine: { status: STATUS, 'root-add': { ok: true, status: 'added', alias: 'svc', baseline: 'set', proposed: '' } },
    missing: ['/x/svc/src/deep', '/x/svc/src', '/x/svc'],
  })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/x/svc/src/deep/a.py', content: 'x' })
  expect(w.calls.find(call => call.args[0] === 'root-add')?.args).toEqual(['root-add', '/x/svc', '--not-scratch'])
})

test('a folder that could not become a root is said, never silently left unreviewed', async ($, on) => {
  world(on, { engine: { status: STATUS, 'root-add': { ok: false, code: 'oversized', error: 'big.bin is 20 MB' } } })
  fileTools(on)
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/a.ts', content: 'x' })
  const note = result.deny === undefined ? (result.context ?? []).join('\n') : ''
  expect(note).toContain('could not register /elsewhere as a root')
  expect(note).toContain('big.bin is 20 MB')
  expect(note).toContain('Nothing written there is reviewed')
})

test('a run the engine cannot read is said too, rather than taken as no run', async ($, on) => {
  world(on, { engine: { status: { ok: false, code: 'state-corrupt', error: 'state.json does not load' } } })
  fileTools(on)
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/a.ts', content: 'x' })
  expect(result.deny === undefined ? (result.context ?? []).join('\n') : '').toContain('state.json does not load')
})

test('add_root refreshes what the mod knows, so a write under the new root asks nothing more', async ($, on) => {
  const withLib = summary({ roots: [{ alias: 'work', path: LAUNCH, kind: 'git', baseline: 'set', proposed: '' }, { alias: 'lib', path: '/lib', kind: 'dir', baseline: 'set', proposed: '' }] })
  const w = world(on, { engine: { 'root-add': { ok: true, status: 'added', alias: 'lib', baseline: 'set', proposed: '' }, status: { ok: true, run: withLib } } })
  fileTools(on)
  await start($)
  await callTool($, 'add_root', { path: '/lib' })
  const before = w.calls.length
  await $.tool.call({ tool: 'Write', file_path: '/lib/a.md', content: 'x' })
  expect(w.calls.length).toBe(before)
})

test('set_phases names the phase to implement, which after a replan need not be the first', async ($, on) => {
  world(on, { engine: { 'set-phases': { ok: true, run: summary({ phases: ['a', 'b', 'c'], phase: 3, phases_revision: 4 }) } } })
  await start($)
  expect(await callTool($, 'set_phases', { phases: ['a', 'b', 'c'], replan: true })).toContain('Implement phase 3')
})

test('a write through a symlink in a root to a folder outside every root registers where it lands', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: true, status: 'added', alias: 'lib', baseline: 'set', proposed: '' } }, links: { [`${LAUNCH}/link`]: '/elsewhere/lib' } })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: `${LAUNCH}/link/src/a.ts`, content: 'x' })
  expect(w.calls.filter(call => call.args[0] === 'root-add').map(call => call.args)).toEqual([['root-add', `${LAUNCH}/link/src`, '--not-scratch']])
  // A folder that really is in a root still skips the engine.
  const before = w.calls.length
  await $.tool.call({ tool: 'Write', file_path: `${LAUNCH}/src/b.ts`, content: 'x' })
  expect(w.calls.length).toBe(before)
})

test('a write the engine finds in a scratch place makes no root and tells Claude nothing', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: false, code: 'scratch', error: '/tmp/x is a scratch directory' } } })
  fileTools(on)
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: `${HOME}/.claude/plans/plan.md`, content: 'x' })
  expect(w.calls.filter(call => call.args[0] === 'root-add').map(call => call.args)).toEqual([['root-add', `${HOME}/.claude/plans`, '--not-scratch']])
  // No refresh after a refusal, and nothing said: the write goes on, outside the run.
  expect(w.commands()).toEqual(['status', 'root-add'])
  expect(result.deny).toBeUndefined()
  expect((result.context ?? []).join('\n')).not.toContain('adversarial-review-loop')
})

test('add_root refuses a scratch folder and points to /add-dir', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: false, code: 'scratch', error: '/tmp/proto is a scratch directory' } } })
  await start($)
  const text = await callTool($, 'add_root', { path: '/tmp/proto' })
  expect(w.calls[0]?.args).toEqual(['root-add', '/tmp/proto', '--not-scratch'])
  expect(text).toContain('not registered')
  expect(text).toContain('/add-dir')
  expect(text).not.toContain('Registered')
})

test('a path that climbs out of a root with .. is not taken to be inside it', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: true, status: 'added', alias: 'other', baseline: 'set', proposed: '' } } })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: `${LAUNCH}/../other/a.ts`, content: 'x' })
  expect(w.commands()).toContain('root-add')
})

test('a stopped run is asked about once, not at every write, until its state changes', async ($, on) => {
  const w = world(on, { engine: { status: { ok: true, run: summary({ status: 'stopped' }) } } })
  fileTools(on)
  await start($)
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/a.ts', content: 'x' })
  await $.tool.call({ tool: 'Write', file_path: '/elsewhere/b.ts', content: 'x' })
  await turnEnds($)
  expect(w.commands()).toEqual(['status'])
})

test('a directory added to the session that cannot become a root is shown to the person', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: false, code: 'oversized', error: 'big.bin is 20 MB' } } })
  await start($)
  await $.classic.DirectoryAdded({ directory: '/extra', source: 'slash_command' })
  expect(w.logged.join('\n')).toContain('could not register /extra')
})

test('set_phases in the final stage asks for the final review, not a phase', async ($, on) => {
  world(on, { engine: { 'set-phases': { ok: true, run: summary({ phases: ['a'], phase: 0, stage: 'final' }) } } })
  await start($)
  const text = await callTool($, 'set_phases', { phases: ['a'] })
  expect(text).toContain('final review')
  expect(text).not.toContain('phase 0')
})

test('a registration that throws is said to Claude, never dropped', async ($, on) => {
  world(on, { engine: { status: STATUS }, envFails: true })
  fileTools(on)
  await start($)
  const result = await $.tool.call({ tool: 'Write', file_path: '/elsewhere/a.ts', content: 'x' })
  expect(result.deny === undefined ? (result.context ?? []).join('\n') : '').toContain('could not register /elsewhere')
})

test('the tools are served under the names the host registered them by', async ($, on) => {
  const w = world(on, { engine: { 'set-phases': { ok: true, run: summary() } }, toolServer: 'renamed' })
  await start($)
  const result = await $.tool.call({ tool: 'mcp__renamed__set_phases', phases: ['a'] } as never)
  expect(result.deny).toBeUndefined()
  expect(w.commands()).toEqual(['set-phases'])
})

test('a directory the person adds under a temp folder still joins the run', async ($, on) => {
  const w = world(on, { engine: { status: STATUS, 'root-add': { ok: true, status: 'added', alias: 'extra', baseline: 'set', proposed: '' } } })
  await start($)
  await $.classic.DirectoryAdded({ directory: '/tmp/extra', source: 'slash_command' })
  expect(w.calls.find(call => call.args[0] === 'root-add')?.args).toEqual(['root-add', '/tmp/extra'])
})

test('a directory added to the session whose registration throws is shown, and the event goes on', async ($, on) => {
  const w = world(on, { engine: { status: STATUS }, existsFails: true })
  await start($)
  await $.classic.DirectoryAdded({ directory: '/extra', source: 'slash_command' })
  expect(w.logged.join('\n')).toContain('could not register /extra as a root of the run (')
})

test('add_root on a root that has no baseline yet settles it with the user', async ($, on) => {
  const unknown = summary({ roots: [{ alias: 'lib', path: '/lib', kind: 'dir', baseline: 'unknown', proposed: '' }] })
  const w = world(on, {
    engine: {
      'root-add': { ok: true, status: 'known', alias: 'lib', baseline: 'unknown' },
      status: { ok: true, run: unknown },
      'root-baseline': { ok: true, run: summary() },
    },
    choices: ['The current content is the baseline'],
  })
  await start($, true)
  expect(await callTool($, 'add_root', { path: '/lib' })).toContain('whose baseline the user just chose')
  expect(w.calls.at(-1)?.args).toEqual(['root-baseline', 'lib', '--current'])
})

test('the band says working while Claude\'s main turn runs, and idle once it ended, whatever ended it', async ($, on) => {
  world(on, { engine: { attach: { ok: true, run: summary(), foreign_review: false, effort: '' }, status: STATUS, 'next-action': { ok: true, action: 'yield', reason: 'unchanged' } } })
  on('turn.start', (_, e) => e as never)
  await start($)
  const band = async (): Promise<string | undefined> => {
    const ui = await $.ui.mount({ plugin: 'adversarial-review-loop', component: 'AbovePrompt', surface: 'terminal', props: { hasSurvey: false, isCollapsed: false, isWorking: true, maxRows: 10 } } as never)
    const text = (await ui.find({ type: 'Text', text: /phase 1/ }))?.text
    await ui.unmount()
    return text
  }
  await $.turn.start({ text: 'go', turnId: 't1' } as never)
  expect(await band()).toContain('· working')
  await turnEnds($, { reason: 'aborted', isAborted: true })
  expect(await band()).toContain('· idle')
})
