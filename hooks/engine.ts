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

// Running the Python engine: `scripts/arl.sh --dir <launch dir> <command>`, one JSON answer on
// the last line of stdout. The launcher finds Python 3.12+ and runs the bootstrap isolated
// (`python3 -I`), so nothing in the launch directory can shadow the engine's modules.

import type { EngineInterface } from 'claude-code'

/** One engine answer: `ok` with the command's fields, or a refusal with its `code`. */
export type Answer = {
  ok: boolean
  code?: string
  error?: string
  [field: string]: unknown
}

/** A refusal the mod makes up itself when the engine gave no answer it can read. */
function broken(what: string, detail: string): Answer {
  return { ok: false, code: 'engine', error: `the engine ${what}${detail ? `: ${detail}` : ''}` }
}

/** The last stdout line that is a JSON object: the engine prints exactly one. */
export function parseAnswer(stdout: string, stderr: string, exit: string): Answer {
  const lines = stdout.split('\n').map(line => line.trim()).filter(line => line !== '')
  const last = lines.at(-1)
  if (last !== undefined) {
    try {
      const value: unknown = JSON.parse(last)
      if (typeof value === 'object' && value !== null && !Array.isArray(value) && typeof (value as Answer).ok === 'boolean') {
        return value as Answer
      }
    } catch {
      // Falls through to the refusal below.
    }
  }
  return broken(`gave no answer (${exit})`, stderr.trim().split('\n').slice(-5).join('\n'))
}

function argv($: EngineInterface, dir: string, args: readonly string[]): string[] {
  return [`${$.plugin.root}/scripts/arl.sh`, '--dir', dir, ...args]
}

/** Engine commands that take a lock and a snapshot: minutes on a huge root, never hours. */
export const ENGINE_TIMEOUT_MS = 600_000

/**
 * Run one engine command and answer what it printed.
 *
 * `stdin` is always written and closed: `set-phases`, `review prepare` and `clarify prepare`
 * read their input to end of file, and an inherited stdin would hang them.
 */
export async function engine($: EngineInterface, dir: string, args: readonly string[], stdin = ''): Promise<Answer> {
  try {
    const ran = await $.process.run(argv($, dir, args), { cwd: dir, stdin, timeoutMs: ENGINE_TIMEOUT_MS })
    return parseAnswer(ran.stdout, ran.stderr, `exit ${ran.exitCode}`)
  } catch (error) {
    return broken('could not run', String(error))
  }
}

/**
 * Run an engine command that may outlast `$.process.run`'s ten minutes (an external reviewer),
 * streaming its output until it exits. Leaving the loop, an interrupt included, kills it; the
 * engine then kills its reviewer's process group.
 */
export async function engineStream($: EngineInterface, dir: string, args: readonly string[], input = ''): Promise<Answer> {
  let stdout = ''
  let stderr = ''
  try {
    const child = $.process.spawn({ argv: argv($, dir, args), cwd: dir, input })
    for await (const piece of child) {
      if (piece.stream === 'stdout') stdout += piece.text
      else stderr = (stderr + piece.text).slice(-8192)
    }
    const ended = await child.result
    return parseAnswer(stdout, stderr, ended.signal ? `signal ${ended.signal}` : `exit ${ended.code}`)
  } catch (error) {
    return broken('was stopped', String(error))
  }
}
