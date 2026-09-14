import { diagnosisIsFinal, evidenceView, reasoningChain } from '../incidentModel.ts'
import type { IncidentState } from '../types.ts'

function time(timestamp: string | null): string {
  return timestamp ? new Date(timestamp).toLocaleTimeString() : ''
}

// The Evidence Engine panel, in exactly the layout EVIDENCE_ENGINE.md §4
// prescribes, and the reasoning chain from §5. Claims are shown as recorded,
// never reworded (§6).
export function EvidencePanel({ state }: { state: IncidentState | null }) {
  const view = evidenceView(state)
  const chain = reasoningChain(state)

  return (
    <section className="panel evidence" aria-labelledby="evidence-title">
      <h2 id="evidence-title">Evidence</h2>

      {view.rootCause ? (
        <>
          <div className="root-cause">
            <span className="eyebrow">Root Cause</span>
            <p>{view.rootCause}</p>
          </div>

          {chain.length > 1 && (
            <ol className="chain" aria-label="Reasoning chain">
              {chain.map((link, index) => (
                <li key={`${index}-${link.label}`} className={link.source === 'root cause' ? 'chain-end' : ''}>
                  <span className="chain-source">{link.source}{link.time ? ` · ${time(link.time)}` : ''}</span>
                  <span className="chain-label">{link.label}</span>
                </li>
              ))}
            </ol>
          )}

          <div className="evidence-columns">
            <div>
              <h3>Supporting Evidence</h3>
              <ul className="claims">
                {view.supporting.map((e) => (
                  <li key={e.id} className="supports">
                    <span aria-hidden="true">✓</span> {e.claim} <span className="source">{e.source}</span>
                  </li>
                ))}
                {view.similar.map((incident) => (
                  <li key={incident.incident_id} className="supports">
                    <span aria-hidden="true">✓</span> Similar to Incident {incident.incident_id} (
                    {Math.round(incident.similarity_score * 100)}% similar)
                    <span className="source">memory</span>
                  </li>
                ))}
                {!view.supporting.length && !view.similar.length && <li className="empty">None recorded</li>}
              </ul>
            </div>
            <div>
              <h3>Contradictory Evidence</h3>
              <ul className="claims">
                {view.contradicting.map((e) => (
                  <li key={e.id} className="contradicts">
                    <span aria-hidden="true">✗</span> {e.claim} <span className="source">{e.source}</span>
                  </li>
                ))}
                {!view.contradicting.length && <li className="empty">None recorded</li>}
              </ul>
            </div>
          </div>
        </>
      ) : (
        <>
          <p className="muted">
            {diagnosisIsFinal(state)
              ? 'No root cause was accepted. Evidence gathered:'
              : 'No root cause accepted yet. Evidence gathered so far:'}
          </p>
          <ul className="claims">
            {view.gathered.map((e) => (
              <li key={e.id}>
                <span className="source">{e.source}</span> {e.claim}
                <span className="time">{time(e.timestamp)}</span>
              </li>
            ))}
            {!view.gathered.length && (
              <li className="empty">{diagnosisIsFinal(state) ? 'No evidence was gathered' : 'No evidence yet'}</li>
            )}
          </ul>
        </>
      )}
    </section>
  )
}
