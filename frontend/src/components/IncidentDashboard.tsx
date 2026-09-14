import { useEffect, useState } from 'react'

import { ApiError, getIncident } from '../api.ts'
import { pipelineStages, statusLabel } from '../incidentModel.ts'
import { useIncidentStream } from '../useIncidentStream.ts'
import { DecisionPanel } from './DecisionPanel.tsx'
import { EvidencePanel } from './EvidencePanel.tsx'
import { FixPanel } from './FixPanel.tsx'
import { HypothesesPanel } from './HypothesesPanel.tsx'
import { OutcomePanel } from './OutcomePanel.tsx'
import { PipelineTracker } from './PipelineTracker.tsx'

interface Props {
  incidentId: string
  decidedBy: string
  onDecidedByChange: (name: string) => void
}

const CONNECTION_TEXT = { connecting: 'Connecting', open: 'Live', reconnecting: 'Reconnecting' }

// One incident, live. Mount with key={incidentId}.
export function IncidentDashboard({ incidentId, decidedBy, onDecidedByChange }: Props) {
  const { view, connection } = useIncidentStream(incidentId)
  const [missing, setMissing] = useState(false)
  const { state } = view

  // A reloaded page for an incident the backend no longer has (it keeps
  // incidents in memory, D-037) would otherwise wait for events forever.
  // Checked once per incident. A brand-new incident can briefly 404 before its
  // first checkpoint exists, so "missing" only shows while no events have arrived.
  useEffect(() => {
    getIncident(incidentId).catch((err) => {
      if (err instanceof ApiError && err.status === 404) setMissing(true)
    })
  }, [incidentId])

  if (missing && !state) {
    return (
      <section className="panel empty-state" role="status">
        <h2>Incident not found</h2>
        <p>
          The backend has no incident <code>{incidentId}</code>. Incidents are kept in memory, so a backend restart
          clears them. Inject a new failure to start again.
        </p>
      </section>
    )
  }

  return (
    <div className="dashboard">
      <header className="incident-header">
        <div>
          <span className="eyebrow">Incident <code>{incidentId}</code></span>
          <h1 aria-live="polite">{statusLabel(view)}</h1>
          {state && (
            <p className="muted">
              <code>{state.service_name}</code> · {String(state.alert_payload.alert_type ?? 'unknown alert')} · triggered{' '}
              {new Date(state.triggered_at).toLocaleTimeString()}
            </p>
          )}
        </div>
        <span className={`connection connection-${connection}`}>{CONNECTION_TEXT[connection]}</span>
      </header>

      {view.failure && (
        <p className="error banner" role="alert">
          The run stopped with an error: {view.failure}
        </p>
      )}

      <PipelineTracker stages={pipelineStages(view)} />

      {view.approval && (
        <DecisionPanel
          incidentId={incidentId}
          approval={view.approval}
          decidedBy={decidedBy}
          onDecidedByChange={onDecidedByChange}
        />
      )}

      <EvidencePanel state={state} />

      {state && (
        <div className="panel-grid">
          <HypothesesPanel state={state} />
          <FixPanel state={state} />
        </div>
      )}

      {state && <OutcomePanel state={state} />}
    </div>
  )
}
