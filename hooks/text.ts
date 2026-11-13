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

// What Claude and the reviewer read from the mod: tool descriptions and the reviewer's system
// prompt. The review task itself is the engine's composed prompt (`prompts/*.md`).

export const REVIEWER_DESCRIPTION = 'Adversarial reviewer of one phase of an adversarial-review-loop run. Started by the plugin only.'

export const REVIEWER_SYSTEM_PROMPT = `You are an adversarial code reviewer, run by the adversarial-review-loop plugin to review one phase of a plan another agent implemented.

Your task message is the review: what to look at, where the review bundle is, and the output contract your answer must follow.

You only read. Use Read, and read-only shell commands (grep, find, ls, cat, head, git log, git show, git diff) to inspect the bundle and the workspace roots it names. Never change a file, and never run a build, a test or anything else with side effects: the implementer already ran the project's checks, and a command that is not read-only is refused.

Your answer is your final message (or the report you hand back, where your session asks for that), in the format your task message gives, and nothing else.`

export const TOOLS = {
  set_phases: {
    description:
      'Record the phases of the plan you are implementing under adversarial-review-loop, in order, one short title per phase. Call it once after reading the plan, before changing any file. While no phase is approved you may call it again to revise the list; once a phase is approved, revising needs replan: true.',
    inputSchema: {
      type: 'object',
      properties: {
        phases: { type: 'array', items: { type: 'string' }, minItems: 1, description: 'Phase titles, in order.' },
        replan: { type: 'boolean', description: 'Revise the phases after one was approved.' },
      },
      required: ['phases'],
    },
  },
  add_root: {
    description:
      'Register a directory as a workspace root of the adversarial-review-loop run before you change anything in it through Bash, including a directory you are about to create. Edit and Write register their own files. A change made through Bash in a directory outside every root is never reviewed.',
    inputSchema: {
      type: 'object',
      properties: { path: { type: 'string', description: 'The directory, absolute or relative to the launch directory.' } },
      required: ['path'],
    },
  },
  review: {
    description:
      "Ask for the adversarial review of the current phase (or of the final review stage) once the phase is implemented. First run the project's checks yourself and pass what they reported as verification; the reviewer does not run them. Returns the findings to fix, or the approval: on approval, commit each changed git repository by its own conventions, then do what the answer says next (go on with the next phase, unless it says to stop or end your turn). The review sees every change since the last approval, in every root.",
    inputSchema: {
      type: 'object',
      properties: {
        verification: {
          type: 'array',
          description: 'What the checks you ran reported, one entry per command.',
          items: {
            type: 'object',
            properties: {
              root: { type: 'string', description: 'The root the command ran for (its alias or path).' },
              command: { type: 'string' },
              cwd: { type: 'string' },
              status: { type: 'integer', description: 'Exit status.' },
              output: { type: 'string', description: 'The tail of its output.' },
            },
            required: ['command', 'status'],
          },
        },
      },
    },
  },
  clarify: {
    description:
      "Ask the reviewer of the latest review one question about its findings, when a finding is unclear or you believe it is wrong. A few questions per review at most. The reply may retract a finding for the next round; it never changes this review's verdict, so fix or explain, then call review again.",
    inputSchema: {
      type: 'object',
      properties: { question: { type: 'string' } },
      required: ['question'],
    },
  },
} as const

export type ToolName = keyof typeof TOOLS

/** The three answers to "the loop needs you", compared with the reply exactly. */
export const ACCEPT = 'Accept'
export const KEEP_GOING = 'Keep going'
export const STOP = 'Stop'
