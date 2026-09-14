import { useEffect, useState } from 'react'

import { injectScenario, listScenarios } from '../api.ts'
import type { Scenario } from '../types.ts'

interface Props {
  onStarted: (incidentId: string) => void
}

// "Inject Failure" demo control. For now it raises the scenario's alert only;
// breaking the demo stack for real is Phase 7 (DECISIONS.md D-048), and the
// control says so rather than implying otherwise.
export function InjectPanel({ onStarted }: Props) {
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listScenarios()
      .then((list) => {
        setScenarios(list)
        setSelectedId((current) => current || list[0]?.id || '')
      })
      .catch((err: Error) => setError(`Could not load scenarios: ${err.message}`))
  }, [])

  const selected = scenarios.find((scenario) => scenario.id === selectedId)

  const inject = async () => {
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      const incident = await injectScenario(selected)
      onStarted(incident.incident_id)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel inject" aria-labelledby="inject-title">
      <h2 id="inject-title">Inject Failure</h2>
      <label className="field">
        <span>Scenario</span>
        <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)} disabled={!scenarios.length}>
          {scenarios.map((scenario) => (
            <option key={scenario.id} value={scenario.id}>
              {scenario.label}
            </option>
          ))}
        </select>
      </label>

      {selected && (
        <dl className="scenario-facts">
          <dt>Service</dt>
          <dd><code>{selected.service_name}</code></dd>
          <dt>Alert</dt>
          <dd><code>{selected.alert_type}</code></dd>
          <dt>Expected cause</dt>
          <dd>{selected.expected_root_cause}</dd>
        </dl>
      )}

      <button type="button" className="button primary" onClick={inject} disabled={!selected || busy}>
        {busy ? 'Starting…' : 'Inject failure'}
      </button>
      <p className="note">
        Raises this scenario's alert and starts an investigation. It doesn't break the demo stack yet; that arrives
        in Phase 7.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}
