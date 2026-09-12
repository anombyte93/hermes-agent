/**
 * Workflow REST payload types for the kanban plugin.
 *
 * These are the React-side shapes for the read-only /evidence/* workflow
 * projections (attention queue, bounded changes, timeline) and the explicit
 * action /workflow/* writes (readiness, continuation draft, continue, hold)
 * defined by WORKFLOW-API-CONTRACT.md. Every route returns the standard
 * envelope (state + evidence + reason/remedy), so these types describe the
 * `evidence` payload; the fetchers in api.ts wrap them in EvidenceEnvelope<T>.
 *
 * Shapes mirror the supplied ADAPTER-LIVE.json / ADAPTER-SERVER.py samples;
 * nothing here invents an easier helper. Unavailable/partial reads arrive as a
 * FAIL/UNKNOWN envelope (state + reason/remedy), never as an empty success.
 */

/** One card row in the bounded attention queue. `operator_authority_needed` is
 *  null by default — the server never infers it from a generic blocked status. */
export interface AttentionCard {
  id: string
  title: string
  assignee?: null | string
  status: string
  block_reason?: null | string
  block_kind?: null | string
  current_run_id?: null | number
  created_at?: number
  started_at?: null | number
  completed_at?: null | number
  reasons?: string[]
  next_action?: null | string
  operator_authority_needed?: null | boolean
  running_total?: number
  running_truncated?: boolean
  process_state?: null | string
  process_present?: null | boolean
}

export interface AttentionData {
  board?: string
  read_at?: number
  observed_at?: number
  cards?: AttentionCard[]
  returned?: number
  has_more?: boolean
  next_cursor?: null | string
  omitted?: number
  remaining_after_page?: number
  high_water_rowid?: number
  incomplete?: boolean
}

export interface ChangeEvent {
  id?: number
  task_id?: string
  kind?: string
  created_at?: number
  run_id?: null | number
}

export interface ChangesData {
  board?: string
  read_at?: number
  observed_at?: number
  first_read_policy?: string
  events?: ChangeEvent[]
  returned?: number
  has_more?: boolean
  next_cursor?: null | string
  total_events?: number
  baseline_id?: number
  high_water_rowid?: number
  anchor_state?: string
  incomplete?: boolean
  empty_means_no_observed_changes?: boolean
}

export interface TimelineInterval {
  kind: 'execution' | 'blocked' | 'review' | 'unknown'
  start?: number
  end?: number
  duration_seconds?: number
  source_runs?: number[]
  source_events?: number[]
}

export interface TimelineData {
  board?: string
  card?: string
  card_status?: string
  read_at?: number
  observed_at?: number
  intervals?: TimelineInterval[]
  returned?: number
  has_more?: boolean
  next_cursor?: null | string
  totals?: {
    covered_window?: Record<string, number>
    page?: Record<string, number>
    all_time?: null
  }
  coverage?: { window_start?: number; window_end?: number; gaps?: unknown[]; note?: string }
  incomplete?: boolean
}

export interface ReadinessCheck {
  name: string
  state: 'PASS' | 'FAIL' | 'UNKNOWN'
  reason?: string
  mutation_authorized?: boolean
}

/** The root helper readiness receipt, forwarded as `evidence` on success. */
export interface ReadinessReceipt {
  state: 'PASS' | 'FAIL' | 'UNKNOWN'
  requested?: Record<string, unknown>
  observed_at?: number
  freshness?: { checked_at?: number; stale_after?: number; note?: string }
  ready_to_release?: boolean
  checks?: ReadinessCheck[]
  boundary?: string
  reason?: string
}

export interface RemainingCheck {
  check: string
  evidence: string
  acceptance: string
}

export interface ContinuationOriginal {
  status?: null | string
  assignee?: null | string
  result_excerpt?: null | string
  latest_run_summary_excerpt?: null | string
  source?: null | string
}

/** The root continuation draft receipt, forwarded as `evidence` on success. */
export interface ContinuationDraft {
  state: 'PASS' | 'FAIL' | 'UNKNOWN'
  board?: string
  card?: string
  fingerprint?: string
  original?: ContinuationOriginal
  worker?: { verdict?: string; reason?: string; aggregate?: Record<string, unknown> }
  passed_checks?: string[]
  remaining_checks?: RemainingCheck[]
  verification_note?: string
  commission?: Record<string, unknown>
  read_at?: { original?: number; worker?: number; note?: string }
  mutation_authorized?: boolean
  limitation?: string
  no_mutation_performed?: boolean
  reason?: string
}

/** The root continue receipt, forwarded as `evidence` on success. */
export interface ContinueReceipt {
  state: 'PASS' | 'FAIL' | 'UNKNOWN'
  board?: string
  original_card?: string
  new_card?: string
  new_card_status?: string
  new_card_assignee?: string
  held?: boolean
  released_after_previous_creation?: boolean
  reblocked_after_release?: boolean
  origin?: { board?: string; card?: string; original_status?: string; note?: string }
  worker?: { verdict?: string; reason?: string }
  no_original_mutation?: boolean
}

/** The compact hold receipt, forwarded as `evidence` on success. */
export interface HoldReceipt {
  state: 'PASS' | 'FAIL' | 'UNKNOWN'
  read_back?: Record<string, unknown>
  warning?: string
  reason?: string
}
