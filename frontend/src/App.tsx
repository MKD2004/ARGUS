import { useEffect, useState } from 'react'

import { IncidentDashboard } from './components/IncidentDashboard.tsx'
import { InjectPanel } from './components/InjectPanel.tsx'

const NAME_KEY = 'argus.decidedBy'

function incidentFromHash(): string {
  return decodeURIComponent(window.location.hash.replace(/^#/, ''))
}

function storedName(): string {
  try {
    return window.localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return ''
  }
}

// The Argus dashboard (Phase 6). The incident being followed lives in the URL
// hash, so a reload or a shared link reopens the same incident.
function App() {
  const [incidentId, setIncidentId] = useState(incidentFromHash)
  const [decidedBy, setDecidedBy] = useState(storedName)

  useEffect(() => {
    const onHashChange = () => setIncidentId(incidentFromHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  const followIncident = (id: string) => {
    window.location.hash = encodeURIComponent(id)
    setIncidentId(id)
  }

  const rememberName = (name: string) => {
    setDecidedBy(name)
    try {
      window.localStorage.setItem(NAME_KEY, name)
    } catch {
      // Storage unavailable (private mode); the name just isn't remembered.
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <span className="brand">Argus</span>
        <span className="tagline">Incident investigation, with a human making the final call</span>
      </header>

      <div className="layout">
        <aside className="sidebar">
          <InjectPanel onStarted={followIncident} />
        </aside>

        <main className="main">
          {incidentId ? (
            <IncidentDashboard
              key={incidentId}
              incidentId={incidentId}
              decidedBy={decidedBy}
              onDecidedByChange={rememberName}
            />
          ) : (
            <section className="panel empty-state">
              <h1>No incident selected</h1>
              <p>
                Pick a scenario and inject a failure. Argus will investigate it live here, then wait for your decision
                before anything reaches GitHub.
              </p>
            </section>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
