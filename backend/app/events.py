"""In-memory event bus: graph runs publish from worker threads, WebSocket
handlers subscribe from the event loop (DECISIONS.md D-046).

Every event is kept per incident, so a subscriber that connects late, or
reconnects, gets the full history first and then live events. History is held
in this process only and is lost on restart, like the checkpointer (D-037).
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict

from app.models.events import ProgressEvent


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, list[ProgressEvent]] = defaultdict(list)
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(list)

    def publish(self, event: ProgressEvent) -> ProgressEvent:
        """Stamps the event with the next sequence number, stores it, and hands
        it to every subscriber. Safe to call from any thread.
        """
        with self._lock:
            history = self._history[event.incident_id]
            stamped = event.model_copy(update={"seq": len(history) + 1})
            history.append(stamped)
            subscribers = list(self._subscribers[event.incident_id])
        for loop, queue in subscribers:
            # Queues belong to their event loop; only that loop may touch them.
            loop.call_soon_threadsafe(queue.put_nowait, stamped)
        return stamped

    def subscribe(self, incident_id: str) -> tuple[list[ProgressEvent], asyncio.Queue]:
        """Returns (history so far, queue of events published after it).

        Taken under the same lock as `publish`, so no event lands in both or in
        neither. Must be called from the event loop that will read the queue.
        """
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers[incident_id].append((asyncio.get_running_loop(), queue))
            return list(self._history.get(incident_id, [])), queue

    def unsubscribe(self, incident_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers[incident_id] = [
                (loop, q) for loop, q in self._subscribers.get(incident_id, []) if q is not queue
            ]

    def history(self, incident_id: str) -> list[ProgressEvent]:
        with self._lock:
            return list(self._history.get(incident_id, []))

    def has_incident(self, incident_id: str) -> bool:
        with self._lock:
            return bool(self._history.get(incident_id))
