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

Work in few messages. Every message you send re-reads your whole context (the bundle and every file you opened), so the cost of a review is its context times its number of messages, not its number of tool calls.

- First message: read \`range.txt\` and every other bundle file your task message says to read in full, together, as parallel tool calls in that one message.
- A read can stop short of a file's end (a \`changes.NN.diff\` often does). When it does, read on from the line it stopped at, in the next message, until you have read every such file to its end. What your task message says to read in full is the evidence: no budget below excuses leaving part of it unread.
- Then decide everything you need to check, and issue it as one batch: several tool calls in one message, or several commands in one Bash call (\`grep -n -C3 ... ; sed -n 10,60p ...\`). Chain a call on an earlier result only when it truly depends on it.
- Locate with grep (\`-n\`, \`-C\`), then read only the region it points at. Never read a large file whole to find one symbol, and never read again a range already in your context.
- Budget: a small diff needs about 3 messages, a large one about 8. If you are past 12, stop investigating outside the bundle and write the review from what you have, once you have read what your task message says to read in full.
- Thoroughness is judged by the findings, not by the number of calls.

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
      "Ask for the adversarial review of the current phase (or of the final review stage) once the phase is implemented. First run the project's checks yourself and pass what they reported as verification; the reviewer does not run them. After a review that asked for changes, pass dispositions too: for each finding of that review (by its id, r<round>.<n>), whether you fixed it or reject it, with a note citing the file and line that shows it. A finding you reject with no such citation stays open. Returns the findings to fix, or the approval: on approval, commit each changed git repository by its own conventions, then do what the answer says next (go on with the next phase, unless it says to stop or end your turn). The review sees every change since the last approval, in every root.",
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
        dispositions: {
          type: 'array',
          description: "What you did about the previous review's findings, one entry per finding. The next reviewer reads them as your claim, to check against the diff.",
          items: {
            type: 'object',
            properties: {
              id: { type: 'string', description: 'The finding id the previous review gave it, such as r1.3.' },
              status: { type: 'string', enum: ['fixed', 'rejected'] },
              note: { type: 'string', description: 'Why: for a rejection, the file and line that show the finding is wrong.' },
            },
            required: ['id', 'status'],
          },
        },
      },
    },
  },
} as const

export type ToolName = keyof typeof TOOLS

/** The three answers to "the loop needs you", compared with the reply exactly. */
export const ACCEPT = 'Accept'
export const KEEP_GOING = 'Keep going'
export const STOP = 'Stop'
/** A fourth answer when the reviewer's model cannot be used: this run goes back to the built-in models. */
export const USE_DEFAULT_MODEL = 'Use the default model'
/** The fourth answer when the default is the refused model itself: another tier, by its word. */
export const useModel = (word: string): string => `Use the ${word} model`
