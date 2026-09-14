// Everything the dashboard shows is derived here, from the event stream, by
// pure functions (DECISIONS.md D-047). Components only render the results,
// so this file is where the "what does the screen say" logic is tested.

import type { Evidence, IncidentState, IncidentStatus, ProgressEvent, SimilarIncident } from './types.ts'

// --- Folding events into a view ----------------------------------------------

export interface IncidentView {
  incidentId: string
  lastSeq: number
  state: IncidentState | null
  // Nodes that have finished at least once, in the order they first finished.
  completedNodes: string[]
  // Set while the run waits at the human gate (D-038); cleared once it resumes.
  approval: { allowed: boolean; reason: string } | null
  finished: boolean
  failure: string | null
}

export function emptyView(incidentId: string): IncidentView {
  return { incidentId, lastSeq: 0, state: null, completedNodes: [], approval: null, finished: false, failure: null }
}

export function applyEvent(view: IncidentView, event: ProgressEvent): IncidentView {
  // A reconnect replays the whole history (D-046); skip anything already seen.
  if (event.incident_id !== view.incidentId || event.seq <= view.lastSeq) {
    return view
  }
  const next: IncidentView = { ...view, lastSeq: event.seq }
  switch (event.type) {
    case 'node_completed':
      if (event.node && !view.completedNodes.includes(event.node)) {
        next.completedNodes = [...view.completedNodes, event.node]
      }
      // Any node finishing after a pause means the decision was made and the run resumed.
      next.approval = null
      next.failure = null
      return next
    case 'state':
      next.state = event.state
      return next
    case 'paused':
      next.approval = { allowed: event.approval_allowed === true, reason: event.approval_reason ?? '' }
      return next
    case 'finished':
      next.finished = true
      next.approval = null
      return next
    case 'failed':
      next.failure = event.message ?? 'The run failed.'
      next.approval = null
      return next
  }
}

// --- Pipeline stages ----------------------------------------------------------

export type StageStatus = 'done' | 'active' | 'waiting' | 'skipped' | 'failed'

export interface Stage {
  id: string
  label: string
  status: StageStatus
  detail: string
}

// How far along the pipeline a status is. Both loop statuses share a rank, and
// so do the two output statuses.
const STATUS_RANK: Record<IncidentStatus, number> = {
  investigating: 0,
  validating_hypothesis: 1,
  retrieving_memory: 2,
  planning_fix: 3,
  generating_patch: 4,
  testing_patch: 4,
  awaiting_human_approval: 5,
  creating_pr: 6,
  creating_issue: 6,
  writing_postmortem: 7,
  notifying: 8,
  resolved: 9,
}

interface StageDef {
  id: string
  label: string
  nodes: string[]
  rank: number
  // A loop stage keeps running while the status stays at its rank.
  loop?: boolean
  parallelGroup?: string
}

const AGENT_IDS = ['log_agent', 'metrics_agent', 'deploy_agent']

const STAGES: StageDef[] = [
  { id: 'supervisor', label: 'Supervisor', nodes: ['supervisor'], rank: 0 },
  { id: 'log_agent', label: 'Log Agent', nodes: ['log_agent'], rank: 0, parallelGroup: 'agents' },
  { id: 'metrics_agent', label: 'Metrics Agent', nodes: ['metrics_agent'], rank: 0, parallelGroup: 'agents' },
  { id: 'deploy_agent', label: 'Deploy Agent', nodes: ['deploy_agent'], rank: 0, parallelGroup: 'agents' },
  { id: 'evidence_collector', label: 'Evidence Collector', nodes: ['evidence_collector'], rank: 0 },
  { id: 'hypotheses', label: 'Hypotheses', nodes: ['hypothesis_generator', 'hypothesis_validator'], rank: 1, loop: true },
  { id: 'incident_memory', label: 'Incident Memory', nodes: ['incident_memory'], rank: 2 },
  { id: 'fix_planner', label: 'Fix Planner', nodes: ['fix_planner'], rank: 3 },
  { id: 'patch_loop', label: 'Patch & Test', nodes: ['patch_generator', 'test_execution'], rank: 4, loop: true },
  { id: 'human_gate', label: 'Human Decision', nodes: ['human_gate'], rank: 5 },
  { id: 'output', label: 'PR or Issue', nodes: ['pr_creator', 'issue_creator'], rank: 6 },
  { id: 'postmortem_writer', label: 'Postmortem', nodes: ['postmortem_writer'], rank: 7 },
  { id: 'slack_notifier', label: 'Slack', nodes: ['slack_notifier'], rank: 8 },
  { id: 'incident_memory_update', label: 'Memory Update', nodes: ['incident_memory_update'], rank: 8 },
]

// A stage's short status line. "Done" alone would overstate what happened when
// a step ran but had nothing to do (Slack not configured, nothing stored), so
// those say so plainly.
function stageDetail(def: StageDef, state: IncidentState, ran: boolean): string {
  const evidenceFrom = (source: string) => state.evidence.filter((e) => e.source === source).length
  const rank = STATUS_RANK[state.status]
  switch (def.id) {
    case 'log_agent':
      return `${evidenceFrom('logs')} evidence`
    case 'metrics_agent':
      return `${evidenceFrom('metrics')} evidence`
    case 'deploy_agent':
      return `${evidenceFrom('deploy')} evidence`
    case 'evidence_collector':
      return `${state.evidence.length} total`
    case 'hypotheses':
      // The generator can end the loop before any round runs, e.g. with no evidence (D-034).
      if (ran && rank > 1 && state.hypothesis_loop_iterations === 0 && !state.accepted_hypothesis) {
        return 'Nothing to test'
      }
      return `Round ${state.hypothesis_loop_iterations}/${state.max_hypothesis_iterations}` +
        (state.accepted_hypothesis ? ' · accepted' : state.rejected_hypotheses.length ? ` · ${state.rejected_hypotheses.length} ruled out` : '')
    case 'incident_memory':
      return `${state.similar_incidents.length} similar`
    case 'fix_planner': {
      const count = state.candidate_fix_strategies.length
      return `${count} ${count === 1 ? 'strategy' : 'strategies'}`
    }
    case 'patch_loop': {
      const latest = state.patches[state.patches.length - 1]
      return `Attempt ${state.patches.length}/${state.max_patch_retries}` + (latest ? ` · ${latest.test_result}` : '')
    }
    case 'human_gate':
      if (state.human_decision && state.human_decision_by) return `${state.human_decision} by ${state.human_decision_by}`
      return state.status === 'awaiting_human_approval' ? 'Waiting for you' : ''
    case 'output':
      if (state.github_pr_url) return 'PR opened'
      if (state.github_issue_url) return 'Issue opened'
      return ran ? 'Nothing opened' : ''
    case 'slack_notifier':
      if (state.slack_notifications_sent.length) return 'Summary sent'
      return ran ? 'Not sent' : ''
    case 'incident_memory_update':
      if (!ran) return ''
      return state.accepted_hypothesis ? 'Stored' : 'Not stored (no diagnosis)'
    default:
      return ''
  }
}

// True once the run can no longer find a root cause: it's at the human gate or past it.
export function diagnosisIsFinal(state: IncidentState | null): boolean {
  return state !== null && STATUS_RANK[state.status] >= STATUS_RANK.awaiting_human_approval
}

function stageLabel(def: StageDef, state: IncidentState | null): string {
  if (def.id !== 'output' || !state) return def.label
  if (state.human_decision === 'approved') return 'Pull Request'
  if (state.human_decision === 'rejected') return 'GitHub Issue'
  return def.label
}

export function pipelineStages(view: IncidentView): Stage[] {
  const { state } = view
  const completed = new Set(view.completedNodes)

  if (!state) {
    return STAGES.map((def) => ({ id: def.id, label: def.label, status: 'waiting', detail: '' }))
  }

  const rank = STATUS_RANK[state.status]

  // Pass 1: what each stage's own history says.
  const statuses: StageStatus[] = STAGES.map((def) => {
    const ran = def.nodes.some((node) => completed.has(node))
    const notDispatched =
      AGENT_IDS.includes(def.id) && state.agents_dispatched.length > 0 && !state.agents_dispatched.includes(def.id)
    if (notDispatched) return 'skipped'
    if (def.loop) {
      if (ran && rank > def.rank) return 'done'
    } else if (ran) {
      return 'done'
    }
    // The run moved past this stage without it: an escalation, or the other output path.
    if (rank > def.rank || view.finished) return 'skipped'
    return 'waiting'
  })

  // Pass 2: the first unfinished stage the run has reached is the one working
  // now (all dispatched agents at once, since they run in parallel).
  if (!view.finished) {
    const first = STAGES.findIndex((def, i) => statuses[i] === 'waiting' && def.rank <= rank)
    if (first !== -1) {
      const group = STAGES[first].parallelGroup
      STAGES.forEach((def, i) => {
        const inPlay = i === first || (group !== undefined && def.parallelGroup === group)
        if (inPlay && statuses[i] === 'waiting') {
          statuses[i] = view.failure ? 'failed' : 'active'
        }
      })
    }
  }

  return STAGES.map((def, i) => ({
    id: def.id,
    label: stageLabel(def, state),
    status: statuses[i],
    detail: stageDetail(def, state, def.nodes.some((node) => completed.has(node))),
  }))
}

// --- Evidence Engine (EVIDENCE_ENGINE.md §4) -----------------------------------

export interface EvidenceView {
  rootCause: string | null
  supporting: Evidence[]
  similar: SimilarIncident[]
  contradicting: Evidence[]
  // Before a hypothesis is accepted, everything gathered so far.
  gathered: Evidence[]
}

function byTime(a: Evidence, b: Evidence): number {
  return Date.parse(a.timestamp) - Date.parse(b.timestamp)
}

function pick(state: IncidentState, ids: string[]): Evidence[] {
  const byId = new Map(state.evidence.map((e) => [e.id, e]))
  return ids.map((id) => byId.get(id)).filter((e): e is Evidence => e !== undefined)
}

export function evidenceView(state: IncidentState | null): EvidenceView {
  if (!state) {
    return { rootCause: null, supporting: [], similar: [], contradicting: [], gathered: [] }
  }
  const accepted = state.accepted_hypothesis
  return {
    rootCause: accepted?.description ?? null,
    supporting: accepted ? pick(state, accepted.supporting_evidence_ids) : [],
    similar: accepted ? state.similar_incidents : [],
    contradicting: accepted ? pick(state, accepted.contradicting_evidence_ids) : [],
    gathered: [...state.evidence].sort(byTime),
  }
}

// --- Reasoning chain (EVIDENCE_ENGINE.md §5) -----------------------------------

export interface ChainLink {
  label: string
  source: string
  time: string | null
}

// The accepted hypothesis's supporting evidence in time order, ending at the
// root cause. Built from the evidence list, never hand-authored.
export function reasoningChain(state: IncidentState | null): ChainLink[] {
  const accepted = state?.accepted_hypothesis
  if (!state || !accepted) return []
  const links = pick(state, accepted.supporting_evidence_ids)
    .sort(byTime)
    .map((e) => ({ label: e.claim, source: e.source, time: e.timestamp }))
  return [...links, { label: accepted.description, source: 'root cause', time: null }]
}

// --- Diffs ----------------------------------------------------------------------

export type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'context'

export function diffLines(diff: string): { kind: DiffLineKind; text: string }[] {
  if (!diff) return []
  return diff.replace(/\n$/, '').split('\n').map((text) => {
    if (text.startsWith('+++') || text.startsWith('---') || text.startsWith('\\')) return { kind: 'meta', text }
    if (text.startsWith('@@')) return { kind: 'hunk', text }
    if (text.startsWith('+')) return { kind: 'add', text }
    if (text.startsWith('-')) return { kind: 'del', text }
    return { kind: 'context', text }
  })
}

// --- Labels -----------------------------------------------------------------------

const STATUS_LABELS: Record<IncidentStatus, string> = {
  investigating: 'Investigating',
  validating_hypothesis: 'Testing hypotheses',
  retrieving_memory: 'Checking past incidents',
  planning_fix: 'Planning a fix',
  generating_patch: 'Writing a patch',
  testing_patch: 'Testing the patch',
  awaiting_human_approval: 'Waiting for your decision',
  creating_pr: 'Opening a pull request',
  creating_issue: 'Opening an issue',
  writing_postmortem: 'Writing the postmortem',
  notifying: 'Notifying',
  resolved: 'Resolved',
}

export function statusLabel(view: IncidentView): string {
  if (view.failure) return 'Run failed'
  if (!view.state) return 'Starting'
  return STATUS_LABELS[view.state.status]
}
