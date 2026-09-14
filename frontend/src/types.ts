// TypeScript mirror of the backend's typed models. STATE_SCHEMA.md is the
// source of truth; backend/app/models/state.py and events.py implement it.

export type EvidenceSource = 'logs' | 'metrics' | 'deploy' | 'test' | 'incident_memory'
export type HypothesisStatus = 'candidate' | 'accepted' | 'rejected'
export type TestResult = 'pending' | 'passed' | 'failed'
export type HumanDecision = 'pending' | 'approved' | 'rejected'
export type IncidentStatus =
  | 'investigating'
  | 'validating_hypothesis'
  | 'retrieving_memory'
  | 'planning_fix'
  | 'generating_patch'
  | 'testing_patch'
  | 'awaiting_human_approval'
  | 'creating_pr'
  | 'creating_issue'
  | 'writing_postmortem'
  | 'notifying'
  | 'resolved'

export interface Evidence {
  id: string
  source: EvidenceSource
  claim: string
  timestamp: string
  supports_hypothesis_id: string | null
  contradicts_hypothesis_id: string | null
}

export interface Hypothesis {
  id: string
  description: string
  status: HypothesisStatus
  supporting_evidence_ids: string[]
  contradicting_evidence_ids: string[]
  rejection_reason: string | null
}

export interface SimilarIncident {
  incident_id: string
  similarity_score: number
  summary: string
  fix_applied: string
  recovery_time_minutes: number
}

export interface FixStrategy {
  id: string
  description: string
  tradeoffs: string
  rank: number
}

export interface Patch {
  id: string
  diff: string
  attempt_number: number
  test_result: TestResult
  failure_traceback: string | null
}

export interface IncidentState {
  incident_id: string
  alert_payload: Record<string, unknown>
  service_name: string
  triggered_at: string
  agents_dispatched: string[]
  evidence: Evidence[]
  candidate_hypotheses: Hypothesis[]
  rejected_hypotheses: Hypothesis[]
  accepted_hypothesis: Hypothesis | null
  hypothesis_loop_iterations: number
  max_hypothesis_iterations: number
  similar_incidents: SimilarIncident[]
  candidate_fix_strategies: FixStrategy[]
  chosen_fix_strategy: FixStrategy | null
  patches: Patch[]
  patch_retry_count: number
  max_patch_retries: number
  human_decision: HumanDecision | null
  human_decision_by: string | null
  human_decision_at: string | null
  github_pr_url: string | null
  github_issue_url: string | null
  postmortem: string | null
  slack_notifications_sent: string[]
  status: IncidentStatus
}

export type ProgressEventType = 'node_completed' | 'state' | 'paused' | 'finished' | 'failed'

// DECISIONS.md D-046. Which optional fields are set depends on `type`.
export interface ProgressEvent {
  type: ProgressEventType
  incident_id: string
  seq: number
  at: string
  node: string | null
  state: IncidentState | null
  approval_allowed: boolean | null
  approval_reason: string | null
  message: string | null
}

export interface Scenario {
  id: string
  label: string
  service_name: string
  alert_type: string
  expected_root_cause: string
}

export interface ApprovalStatus {
  waiting_for_decision: boolean
  approval_allowed: boolean
  reason: string
}
