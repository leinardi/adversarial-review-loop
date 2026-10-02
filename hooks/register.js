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

// The review loop's display: a band above the prompt with this session's activation, and a
// transcript line when an alert appears. It decides nothing. The gate is the command hooks
// in hooks.json; a module that fails is skipped by the engine, which is why nothing here may
// ever matter to a verdict. See docs/design/mod.md before changing it.
//
// Everything it shows comes from `arl.sh status --json --session <id>`, which reads only and
// computes every alert. This module never re-derives one, and draws only enums and integers.
//
// Plain JavaScript that imports nothing; the JSDoc below is what `tsc --checkJs` reads, against
// the API types the engine lays in .claude-plugin/types/.

/** @typedef {import('claude-code').EngineInterface} Engine */

/**
 * One `status --json` answer, as this module has checked it.
 * @typedef {object} Answer
 * @property {'pending' | 'bound' | 'unbound' | 'unarmed' | 'unknown'} binding
 * @property {string[]} alerts
 * @property {string} [status]
 * @property {string} [cause]
 * @property {number} [phase]
 * @property {number} [phase_count]
 * @property {number} [rounds_this_phase]
 * @property {number} [failures]
 * @property {number} [max_failures]
 */

const BINDINGS = new Set(['bound', 'unbound', 'unarmed', 'unknown'])
const STATUSES = new Set(['ARMED', 'ACTIVE', 'RECONCILE', 'NEEDS_HUMAN', 'STALE', 'ARM_FAILED', 'COMPLETE', 'DISARMED', 'RESUMED'])
const CAUSES = new Set(['document_missing', 'document_unreadable', 'document_malformed', 'version_conflict', 'repo_unresolvable'])
const BOUND_COUNTS = ['phase', 'phase_count', 'rounds_this_phase', 'failures', 'max_failures']

// What each alert says in the transcript. The keys are the only alerts `status` emits.
/** @type {Record<string, string>} */
const ALERTS = {
  needs_human: 'the loop is NEEDS_HUMAN and only you can clear it: /adversarial-review-loop:status says why.',
  unreviewed_at_exit: 'the mode ended with work committed that no review approved: /adversarial-review-loop:status names the commit.',
  ended_record_malformed: "this activation's end record was not written by the gate: state.json was edited. Look at the history yourself.",
  ended_unverifiable: 'the repository could not be read when the mode ended, so nothing says whether that work was reviewed.',
  ended_unborn: 'HEAD no longer existed when the mode ended: the history this activation gated is gone.',
  state_unknown: "the loop's state could not be read. That does NOT mean it is off: /adversarial-review-loop:status says why.",
}

// The last answer, and the alerts already reported. Module state starts over on a reload,
// which only means the next refresh reports the current alerts once more.
/** @type {Answer} */
let last = { binding: 'pending', alerts: [] }
/** @type {Set<string>} */
let reported = new Set()

/**
 * @param {string} cause
 * @returns {Answer}
 */
function unknown(cause) {
  return { binding: 'unknown', cause, alerts: ['state_unknown'] }
}

/** @param {unknown} value */
function isCount(value) {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

// `status --json`'s answer, checked field by field. Anything this module does not recognise
// is an unknown state, never an unarmed one.
/**
 * @param {string} stdout
 * @returns {Answer}
 */
function parse(stdout) {
  /** @type {any} */
  let answer
  try {
    answer = JSON.parse(stdout)
  } catch {
    return unknown('unparseable')
  }
  if (answer === null || typeof answer !== 'object' || !BINDINGS.has(answer.binding)) {
    return unknown('unparseable')
  }
  // An alert this module has no line for is not dropped: a newer `status` raising one this
  // module predates must not leave the band drawing a quiet ACTIVE.
  if (!Array.isArray(answer.alerts) || !answer.alerts.every((/** @type {unknown} */ alert) => typeof alert === 'string' && Object.hasOwn(ALERTS, alert))) {
    return unknown('unparseable')
  }
  /** @type {string[]} */
  const alerts = answer.alerts
  if (answer.binding === 'unknown') {
    return unknown(CAUSES.has(answer.cause) ? answer.cause : 'unparseable')
  }
  if (answer.binding === 'unbound') {
    return STATUSES.has(answer.status) ? { binding: 'unbound', status: answer.status, alerts } : unknown('unparseable')
  }
  if (answer.binding === 'unarmed') {
    return { binding: 'unarmed', alerts: [] }
  }
  if (!STATUSES.has(answer.status) || !BOUND_COUNTS.every(key => isCount(answer[key]))) {
    return unknown('unparseable')
  }
  return {
    binding: 'bound',
    status: answer.status,
    alerts,
    phase: answer.phase,
    phase_count: answer.phase_count,
    rounds_this_phase: answer.rounds_this_phase,
    failures: answer.failures,
    max_failures: answer.max_failures,
  }
}

/**
 * @param {Engine} $
 * @returns {Promise<Answer>}
 */
async function ask($) {
  try {
    const session = await $.session.id()
    const cwd = await $.session.cwd()
    const ran = await $.process.run([$.plugin.root + '/scripts/arl.sh', 'status', '--json', '--session', session], { cwd, timeoutMs: 15000 })
    return ran.exitCode === 0 ? parse(ran.stdout) : unknown('status_failed')
  } catch {
    return unknown('status_failed')
  }
}

/** @param {Engine} $ */
async function refresh($) {
  const answer = await ask($)
  const current = new Set(answer.alerts)
  for (const alert of current) {
    if (!reported.has(alert)) {
      $.ui.log(ALERTS[alert] ?? alert)
    }
  }
  reported = current
  last = answer
  $.ui.invalidate('ui.render')
}

/**
 * The band's one line, or null to draw nothing.
 * @param {Answer} answer
 * @returns {{ text: string, color: string | undefined } | null}
 */
function bandLine(answer) {
  if (answer.binding === 'bound') {
    const alert = answer.alerts.length > 0 ? ' · alert: ' + answer.alerts.join(', ') : ''
    // Past the last phase, `phase` is total+1: what is left is the turn end, not a phase.
    const count = answer.phase_count ?? 0
    const phase = count > 0 && (answer.phase ?? 0) > count ? `all ${count} phases committed` : `phase ${answer.phase}/${count}`
    return {
      text: `ARL ${answer.status} · ${phase} · round ${answer.rounds_this_phase} · failures ${answer.failures}/${answer.max_failures}${alert}`,
      color: alert ? 'red' : undefined,
    }
  }
  if (answer.binding === 'unbound') {
    return { text: `ARL: this worktree is armed by another session (${answer.status}); /adversarial-review-loop:resume binds this one`, color: 'yellow' }
  }
  if (answer.binding === 'unknown') {
    return { text: `ARL: state unknown (${answer.cause})`, color: 'yellow' }
  }
  return null
}

// `Register` itself is not used as the type: it returns `unknown`, which a block body has to
// return explicitly. The parameter is what carries the types into every hook.
/** @param {import('claude-code').On} on */
export const register = on => {
  on('session.start', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  // /clear starts a new session id and fires no session.start (tests/STEP0.md item 22), so
  // every refresh reads the id afresh rather than keeping one from the start.
  on('turn.start', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    await refresh($)
    return result
  })

  // A commit lands inside a turn, so a Bash call is the one place the phase moves mid-turn.
  // Nothing is added to an unarmed session's tool calls.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    if (last.binding !== 'unarmed') {
      await refresh($)
    }
    return result
  })

  // The band is shared: whatever the engine and other plugins draw there stays, with this
  // line above it. A survey holds the band alone, so the line yields to it.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)
    const line = bandLine(last)
    if (line === null || e.props.hasSurvey) {
      return below
    }
    const { Box, Text } = $.ui.resolve(e)
    // JSX would type this as a RenderElement; a direct `h` call returns the looser RenderNode.
    return /** @type {import('claude-code').RenderElement} */ (
      h(Box, { flexDirection: 'column' }, h(Text, { color: line.color, dimColor: line.color === undefined, wrap: 'truncate-end' }, line.text), below)
    )
  })
}
