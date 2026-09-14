// Follows one incident's progress over WebSocket, reconnecting if the
// connection drops. The server replays the full history on every connection,
// and applyEvent skips events already seen, so a reconnect loses nothing (D-046).

import { useEffect, useState } from 'react'

import { eventsUrl } from './api.ts'
import { applyEvent, emptyView } from './incidentModel.ts'
import type { IncidentView } from './incidentModel.ts'
import type { ProgressEvent } from './types.ts'

export type ConnectionState = 'connecting' | 'open' | 'reconnecting'

const MAX_RETRY_DELAY_MS = 10_000

// Mount with `key={incidentId}` so a different incident starts from an empty view.
export function useIncidentStream(incidentId: string): { view: IncidentView; connection: ConnectionState } {
  const [view, setView] = useState<IncidentView>(() => emptyView(incidentId))
  const [connection, setConnection] = useState<ConnectionState>('connecting')

  useEffect(() => {
    let stopped = false
    let socket: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0

    const connect = () => {
      socket = new WebSocket(eventsUrl(incidentId))
      socket.onopen = () => {
        attempt = 0
        setConnection('open')
      }
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data as string) as ProgressEvent
        setView((current) => applyEvent(current, event))
      }
      socket.onclose = () => {
        if (stopped) return
        setConnection('reconnecting')
        const delay = Math.min(1000 * 2 ** attempt, MAX_RETRY_DELAY_MS)
        attempt += 1
        retryTimer = setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      stopped = true
      clearTimeout(retryTimer)
      socket?.close()
    }
  }, [incidentId])

  return { view, connection }
}
