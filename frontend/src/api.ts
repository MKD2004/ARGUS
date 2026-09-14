// HTTP and WebSocket access to the Argus backend. In development Vite proxies
// /api and /ws to the backend (vite.config.ts), so no CORS setup is needed (D-047).

import type { ApprovalStatus, IncidentState, Scenario } from './types.ts'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      // Not JSON; keep the status text.
    }
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

export function listScenarios(): Promise<Scenario[]> {
  return request('/scenarios')
}

export function injectScenario(scenario: Scenario): Promise<IncidentState> {
  return request('/incidents', {
    method: 'POST',
    body: JSON.stringify({
      service_name: scenario.service_name,
      alert_type: scenario.alert_type,
      extra: { scenario: scenario.id },
    }),
  })
}

export function getIncident(incidentId: string): Promise<IncidentState> {
  return request(`/incidents/${encodeURIComponent(incidentId)}`)
}

export function getApproval(incidentId: string): Promise<ApprovalStatus> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/approval`)
}

export function decide(incidentId: string, decision: 'approved' | 'rejected', decidedBy: string): Promise<IncidentState> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/decision`, {
    method: 'POST',
    body: JSON.stringify({ decision, decided_by: decidedBy }),
  })
}

export function eventsUrl(incidentId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${protocol}://${window.location.host}/ws/incidents/${encodeURIComponent(incidentId)}`
}
