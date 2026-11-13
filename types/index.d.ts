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

// The values the mod keeps in the session's state, which its band and pane draw. They outlive a
// reload of the mod; a change to a value's fields changes its shape tag (hooks/ui.tsx).

/** A run summary, as the engine prints it (`RunState.summary`). */
export type RunView = {
  run_id: string
  status: string
  stage: string
  phase: number
  phase_count: number
  phases: string[]
  approved_phases: number
  rounds: number
  final_required: boolean
  pending_review: boolean
  waiting: { kind: string; detail?: string } | null
  roots: { alias: string; path: string; kind: string; baseline: string; proposed: string }[]
}

/** The latest review this session recorded, as the engine answered it. */
export type LastReview = { label: string; verdict: string; message: string; report: string }

declare module 'claude-code' {
  interface PluginState {
    'adversarial-review-loop': {
      run: Shaped<RunView | null>
      last: Shaped<LastReview | null>
      /** What the loop is doing now (`reviewing phase 2, round 1`), empty when idle. */
      activity: string
      /** Whether Claude's main turn is running. */
      busy: boolean
      /** The stored report the person asked to see (`/arl-report`), for the report pane. */
      report: string
    }
  }
}
