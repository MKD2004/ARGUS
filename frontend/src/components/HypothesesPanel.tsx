import { diagnosisIsFinal } from '../incidentModel.ts'
import type { IncidentState } from '../types.ts'

// Every hypothesis considered and why it was ruled out (D-007, D-036), so the
// reasoning is inspectable, not just the answer (PRD.md §7).
export function HypothesesPanel({ state }: { state: IncidentState }) {
  const { accepted_hypothesis: accepted, rejected_hypotheses: rejected, candidate_hypotheses: candidates } = state
  const nothingYet = !accepted && !rejected.length && !candidates.length

  return (
    <section className="panel hypotheses" aria-labelledby="hypotheses-title">
      <h2 id="hypotheses-title">
        Hypotheses <span className="count">round {state.hypothesis_loop_iterations}/{state.max_hypothesis_iterations}</span>
      </h2>
      {nothingYet && <p className="muted">{diagnosisIsFinal(state) ? 'None were proposed.' : 'None proposed yet.'}</p>}
      <ul className="hypothesis-list">
        {accepted && (
          <li className="hypothesis accepted">
            <span className="badge">Accepted</span>
            <span>{accepted.description}</span>
          </li>
        )}
        {candidates.map((h) => (
          <li key={h.id} className="hypothesis candidate">
            <span className="badge">Checking</span>
            <span>{h.description}</span>
          </li>
        ))}
        {rejected.map((h) => (
          <li key={h.id} className="hypothesis rejected">
            <span className="badge">Ruled out</span>
            <span>
              {h.description}
              <span className="reason">{h.rejection_reason}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
