// Tests for the dashboard's view logic (DECISIONS.md D-047). Run with `npm test`.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import type { Evidence, IncidentState, ProgressEvent } from './types.ts'
import {
  applyEvent,
  diagnosisIsFinal,
  diffLines,
  emptyView,
  evidenceView,
  pipelineStages,
  reasoningChain,
  statusLabel,
} from './incidentModel.ts'
import type { IncidentView, StageStatus } from './incidentModel.ts'

const ID = 'inc-1'

function evidence(id: string, source: Evidence['source'], claim: string, minute: number): Evidence {
  return {
    id,
    source,
    claim,
    timestamp: `2026-09-14T10:${String(minute).padStart(2, '0')}:00Z`,
    supports_hypothesis_id: null,
    contradicts_hypothesis_id: null,
  }
}

function state(overrides: Partial<IncidentState> = {}): IncidentState {
  return {
    incident_id: ID,
    alert_payload: {},
    service_name: 'payments',
    triggered_at: '2026-09-14T10:00:00Z',
    agents_dispatched: [],
    evidence: [],
    candidate_hypotheses: [],
    rejected_hypotheses: [],
    accepted_hypothesis: null,
    hypothesis_loop_iterations: 0,
    max_hypothesis_iterations: 3,
    similar_incidents: [],
    candidate_fix_strategies: [],
    chosen_fix_strategy: null,
    patches: [],
    patch_retry_count: 0,
    max_patch_retries: 3,
    human_decision: null,
    human_decision_by: null,
    human_decision_at: null,
    github_pr_url: null,
    github_issue_url: null,
    postmortem: null,
    slack_notifications_sent: [],
    status: 'investigating',
    ...overrides,
  }
}

let seq = 0
function event(type: ProgressEvent['type'], fields: Partial<ProgressEvent> = {}): ProgressEvent {
  seq += 1
  return {
    type,
    incident_id: ID,
    seq,
    at: '2026-09-14T10:00:00Z',
    node: null,
    state: null,
    approval_allowed: null,
    approval_reason: null,
    message: null,
    ...fields,
  }
}

function fold(events: ProgressEvent[]): IncidentView {
  return events.reduce(applyEvent, emptyView(ID))
}

function statusesOf(view: IncidentView): Record<string, StageStatus> {
  return Object.fromEntries(pipelineStages(view).map((s) => [s.id, s.status]))
}

// --- applyEvent -------------------------------------------------------------------

test('events fold into state, completed nodes, and approval', () => {
  seq = 0
  const view = fold([
    event('state', { state: state() }),
    event('node_completed', { node: 'supervisor' }),
    event('paused', { approval_allowed: false, approval_reason: 'No root cause was accepted.' }),
  ])
  assert.equal(view.lastSeq, 3)
  assert.equal(view.state?.status, 'investigating')
  assert.deepEqual(view.completedNodes, ['supervisor'])
  assert.deepEqual(view.approval, { allowed: false, reason: 'No root cause was accepted.' })
})

test('a replayed history after a reconnect changes nothing', () => {
  seq = 0
  const events = [event('state', { state: state() }), event('node_completed', { node: 'supervisor' })]
  const once = fold(events)
  const replayed = events.reduce(applyEvent, once)
  assert.deepEqual(replayed, once)
})

test('events for another incident are ignored', () => {
  seq = 0
  const view = applyEvent(emptyView(ID), { ...event('finished'), incident_id: 'inc-other' })
  assert.equal(view.finished, false)
})

test('a node finishing after a pause clears the approval offer', () => {
  seq = 0
  const view = fold([
    event('paused', { approval_allowed: true, approval_reason: 'passed' }),
    event('node_completed', { node: 'human_gate' }),
  ])
  assert.equal(view.approval, null)
})

test('a failed run is recorded and the approval offer withdrawn', () => {
  seq = 0
  const view = fold([
    event('paused', { approval_allowed: true, approval_reason: 'passed' }),
    event('failed', { message: 'GateViolation: no approval' }),
  ])
  assert.equal(view.failure, 'GateViolation: no approval')
  assert.equal(view.approval, null)
  assert.equal(statusLabel(view), 'Run failed')
})

// --- pipelineStages ----------------------------------------------------------------

test('before any state every stage is waiting', () => {
  assert.ok(pipelineStages(emptyView(ID)).every((s) => s.status === 'waiting'))
})

test('the supervisor is active first, then all dispatched agents at once', () => {
  seq = 0
  const starting = fold([event('state', { state: state() })])
  assert.equal(statusesOf(starting).supervisor, 'active')
  assert.equal(statusesOf(starting).log_agent, 'waiting')

  const dispatched = fold([
    event('state', { state: state() }),
    event('node_completed', { node: 'supervisor' }),
    event('state', { state: state({ agents_dispatched: ['log_agent', 'metrics_agent', 'deploy_agent'] }) }),
    event('node_completed', { node: 'metrics_agent' }),
  ])
  const s = statusesOf(dispatched)
  assert.equal(s.supervisor, 'done')
  assert.equal(s.metrics_agent, 'done')
  assert.equal(s.log_agent, 'active')
  assert.equal(s.deploy_agent, 'active')
  assert.equal(s.evidence_collector, 'waiting')
})

test('agents the supervisor did not dispatch are skipped', () => {
  seq = 0
  const view = fold([
    event('node_completed', { node: 'supervisor' }),
    event('state', { state: state({ agents_dispatched: ['log_agent'] }) }),
  ])
  const s = statusesOf(view)
  assert.equal(s.log_agent, 'active')
  assert.equal(s.metrics_agent, 'skipped')
  assert.equal(s.deploy_agent, 'skipped')
})

test('the hypothesis loop stays active while it loops, with its round shown', () => {
  seq = 0
  const view = fold([
    ...['supervisor', 'log_agent', 'metrics_agent', 'deploy_agent', 'evidence_collector', 'hypothesis_generator', 'hypothesis_validator']
      .map((node) => event('node_completed', { node })),
    event('state', {
      state: state({
        status: 'validating_hypothesis',
        agents_dispatched: ['log_agent', 'metrics_agent', 'deploy_agent'],
        hypothesis_loop_iterations: 1,
        rejected_hypotheses: [
          { id: 'h', description: 'CPU', status: 'rejected', supporting_evidence_ids: [], contradicting_evidence_ids: [], rejection_reason: 'flat' },
        ],
      }),
    }),
  ])
  const hypotheses = pipelineStages(view).find((stage) => stage.id === 'hypotheses')
  assert.equal(hypotheses?.status, 'active')
  assert.equal(hypotheses?.detail, 'Round 1/3 · 1 ruled out')
})

test('an escalation straight to the gate marks the fix stages skipped and the gate active', () => {
  seq = 0
  const view = fold([
    ...['supervisor', 'log_agent', 'evidence_collector', 'hypothesis_generator'].map((node) => event('node_completed', { node })),
    event('state', { state: state({ status: 'awaiting_human_approval', agents_dispatched: ['log_agent'] }) }),
    event('paused', { approval_allowed: false, approval_reason: 'No root cause was accepted.' }),
  ])
  const s = statusesOf(view)
  assert.equal(s.hypotheses, 'done')
  assert.equal(s.incident_memory, 'skipped')
  assert.equal(s.fix_planner, 'skipped')
  assert.equal(s.patch_loop, 'skipped')
  assert.equal(s.human_gate, 'active')
  assert.equal(s.output, 'waiting')
})

test('after a rejection the output stage is the issue, and a finished run has nothing active', () => {
  seq = 0
  const nodes = ['supervisor', 'log_agent', 'metrics_agent', 'deploy_agent', 'evidence_collector', 'hypothesis_generator',
    'hypothesis_validator', 'incident_memory', 'fix_planner', 'patch_generator', 'test_execution', 'human_gate',
    'issue_creator', 'postmortem_writer', 'slack_notifier', 'incident_memory_update']
  const view = fold([
    ...nodes.map((node) => event('node_completed', { node })),
    event('state', {
      state: state({
        status: 'resolved',
        agents_dispatched: ['log_agent', 'metrics_agent', 'deploy_agent'],
        human_decision: 'rejected',
        human_decision_by: 'oncall',
        github_issue_url: 'https://github.com/org/repo/issues/10',
      }),
    }),
    event('finished'),
  ])
  const stages = pipelineStages(view)
  const output = stages.find((stage) => stage.id === 'output')
  assert.equal(output?.label, 'GitHub Issue')
  assert.equal(output?.detail, 'Issue opened')
  assert.ok(stages.every((stage) => stage.status === 'done'), JSON.stringify(stages.map((x) => [x.id, x.status])))
})

test('steps that ran without doing anything say so instead of a bare "Done"', () => {
  // The browser check on 2026-09-14 showed Slack, the issue, and the memory
  // update as "Done" on a run where none of them actually happened.
  seq = 0
  const nodes = ['supervisor', 'log_agent', 'evidence_collector', 'hypothesis_generator', 'human_gate',
    'issue_creator', 'postmortem_writer', 'slack_notifier', 'incident_memory_update']
  const view = fold([
    ...nodes.map((node) => event('node_completed', { node })),
    event('state', {
      state: state({ status: 'resolved', agents_dispatched: ['log_agent'], human_decision: 'rejected', human_decision_by: 'oncall' }),
    }),
    event('finished'),
  ])
  const details = Object.fromEntries(pipelineStages(view).map((s) => [s.id, s.detail]))
  assert.equal(details.hypotheses, 'Nothing to test')
  assert.equal(details.output, 'Nothing opened')
  assert.equal(details.slack_notifier, 'Not sent')
  assert.equal(details.incident_memory_update, 'Not stored (no diagnosis)')
  assert.equal(details.human_gate, 'rejected by oncall')
})

test('the fix planner count reads naturally for one strategy', () => {
  seq = 0
  const strategy = { id: 'f', description: 'bigger pool', tradeoffs: 'load', rank: 1 }
  const one = fold([event('node_completed', { node: 'fix_planner' }), event('state', { state: state({ status: 'generating_patch', candidate_fix_strategies: [strategy] }) })])
  const two = fold([event('node_completed', { node: 'fix_planner' }), event('state', { state: state({ status: 'generating_patch', candidate_fix_strategies: [strategy, { ...strategy, id: 'g' }] }) })])
  assert.equal(pipelineStages(one).find((s) => s.id === 'fix_planner')?.detail, '1 strategy')
  assert.equal(pipelineStages(two).find((s) => s.id === 'fix_planner')?.detail, '2 strategies')
})

test('the gate says it is waiting for you while paused', () => {
  seq = 0
  const view = fold([
    event('node_completed', { node: 'hypothesis_generator' }),
    event('state', { state: state({ status: 'awaiting_human_approval' }) }),
  ])
  assert.equal(pipelineStages(view).find((s) => s.id === 'human_gate')?.detail, 'Waiting for you')
})

test('the diagnosis is final from the human gate onwards', () => {
  assert.equal(diagnosisIsFinal(null), false)
  assert.equal(diagnosisIsFinal(state({ status: 'validating_hypothesis' })), false)
  assert.equal(diagnosisIsFinal(state({ status: 'awaiting_human_approval' })), true)
  assert.equal(diagnosisIsFinal(state({ status: 'resolved' })), true)
})

test('slack is active before the memory update, which follows it', () => {
  seq = 0
  const base = ['supervisor', 'log_agent', 'evidence_collector', 'hypothesis_generator', 'human_gate', 'issue_creator', 'postmortem_writer']
  const notifying = state({ status: 'notifying', agents_dispatched: ['log_agent'], human_decision: 'rejected' })
  const before = fold([...base.map((node) => event('node_completed', { node })), event('state', { state: notifying })])
  assert.equal(statusesOf(before).slack_notifier, 'active')
  assert.equal(statusesOf(before).incident_memory_update, 'waiting')

  const after = applyEvent(before, event('node_completed', { node: 'slack_notifier' }))
  assert.equal(statusesOf(after).slack_notifier, 'done')
  assert.equal(statusesOf(after).incident_memory_update, 'active')
})

test('a run that fails at the gate marks the gate as failed', () => {
  // The realistic case: resuming without a valid decision raises GateViolation (D-038).
  seq = 0
  const view = fold([
    ...['supervisor', 'log_agent', 'evidence_collector', 'hypothesis_generator', 'hypothesis_validator',
      'incident_memory', 'fix_planner', 'patch_generator', 'test_execution'].map((node) => event('node_completed', { node })),
    event('state', { state: state({ status: 'awaiting_human_approval', agents_dispatched: ['log_agent'] }) }),
    event('failed', { message: 'GateViolation: no approval' }),
  ])
  const s = statusesOf(view)
  assert.equal(s.patch_loop, 'done')
  assert.equal(s.human_gate, 'failed')
  assert.equal(s.output, 'waiting')
})

// --- Evidence Engine and reasoning chain --------------------------------------------

const withDiagnosis = state({
  evidence: [
    evidence('ev-3', 'logs', 'pool timeout errors', 3),
    evidence('ev-1', 'deploy', 'deploy abc123', 1),
    evidence('ev-2', 'metrics', 'Redis latency +340%', 2),
    evidence('ev-4', 'metrics', 'CPU stable', 4),
  ],
  accepted_hypothesis: {
    id: 'hyp-1',
    description: 'Redis connection pool exhausted',
    status: 'accepted',
    supporting_evidence_ids: ['ev-3', 'ev-1', 'ev-missing', 'ev-2'],
    contradicting_evidence_ids: ['ev-4'],
    rejection_reason: null,
  },
  similar_incidents: [
    { incident_id: 'inc-14', similarity_score: 0.92, summary: 'Redis pool', fix_applied: 'bigger pool', recovery_time_minutes: 18 },
  ],
})

test('evidence view splits supporting and contradicting, ignoring unknown ids', () => {
  const view = evidenceView(withDiagnosis)
  assert.equal(view.rootCause, 'Redis connection pool exhausted')
  assert.deepEqual(view.supporting.map((e) => e.id), ['ev-3', 'ev-1', 'ev-2'])
  assert.deepEqual(view.contradicting.map((e) => e.id), ['ev-4'])
  assert.equal(view.similar[0].incident_id, 'inc-14')
  assert.deepEqual(view.gathered.map((e) => e.id), ['ev-1', 'ev-2', 'ev-3', 'ev-4'])
})

test('without a diagnosis the evidence view only has what was gathered', () => {
  const view = evidenceView(state({ evidence: [evidence('ev-1', 'logs', 'x', 1)] }))
  assert.equal(view.rootCause, null)
  assert.deepEqual(view.supporting, [])
  assert.deepEqual(view.similar, [])
  assert.equal(view.gathered.length, 1)
})

test('the reasoning chain is supporting evidence in time order, ending at the root cause', () => {
  const chain = reasoningChain(withDiagnosis)
  assert.deepEqual(chain.map((link) => link.label), [
    'deploy abc123',
    'Redis latency +340%',
    'pool timeout errors',
    'Redis connection pool exhausted',
  ])
  assert.equal(chain[chain.length - 1].source, 'root cause')
  assert.deepEqual(reasoningChain(state()), [])
})

// --- Diffs -------------------------------------------------------------------------------

test('diff lines are classified for colouring', () => {
  const lines = diffLines('--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n same\n\\ No newline at end of file\n')
  assert.deepEqual(lines.map((line) => line.kind), ['meta', 'meta', 'hunk', 'del', 'add', 'context', 'meta'])
  assert.deepEqual(diffLines(''), [])
})
