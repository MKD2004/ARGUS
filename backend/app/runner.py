"""Runs and resumes incident graphs on background workers, publishing progress
to the EventBus as it happens (DECISIONS.md D-045, D-046).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor

from app.agents.human_gate import approval_allowed
from app.events import EventBus
from app.graph import HUMAN_GATE, run_config
from app.models.events import ProgressEvent
from app.models.state import IncidentState

logger = logging.getLogger(__name__)


class IncidentRunner:
    def __init__(self, graph, bus: EventBus, executor: Executor | None = None) -> None:
        self.graph = graph
        self.bus = bus
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="argus-run")
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def is_running(self, incident_id: str) -> bool:
        with self._lock:
            return incident_id in self._running

    def start(self, state: IncidentState, delay_seconds: float = 0) -> None:
        """Run a new incident until it pauses at the human gate.

        `delay_seconds` lets an injected fault show up in logs and metrics
        before the agents look for it (D-052). The incident counts as running
        during the delay, so it can't be started or decided twice.
        """
        self._submit(state.incident_id, state, state, delay_seconds)

    def resume(self, state: IncidentState) -> None:
        """Continue a paused incident after its decision has been recorded."""
        self._submit(state.incident_id, None, state)

    def _submit(
        self, incident_id: str, graph_input: IncidentState | None, state: IncidentState, delay_seconds: float = 0
    ) -> None:
        with self._lock:
            if incident_id in self._running:
                raise RuntimeError(f"incident {incident_id} is already running")
            self._running.add(incident_id)
        try:
            self._executor.submit(self._run, incident_id, graph_input, run_config(state), delay_seconds)
        except Exception:
            with self._lock:
                self._running.discard(incident_id)
            raise

    def _publish(self, incident_id: str, **fields) -> None:
        self.bus.publish(ProgressEvent(incident_id=incident_id, **fields))

    def _run(self, incident_id: str, graph_input: IncidentState | None, config: dict, delay_seconds: float = 0) -> None:
        try:
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            # "updates" names each node as it finishes (the parallel agents one
            # at a time); "values" is the correct full state after each step.
            # get_state() lags mid-run, so it isn't used for live state (D-046).
            for mode, chunk in self.graph.stream(graph_input, config=config, stream_mode=["updates", "values"]):
                if mode == "updates":
                    for node in chunk:
                        if not node.startswith("__"):
                            self._publish(incident_id, type="node_completed", node=node)
                else:
                    self._publish(incident_id, type="state", state=IncidentState(**chunk))

            snapshot = self.graph.get_state({"configurable": {"thread_id": incident_id}})
            if tuple(snapshot.next) == (HUMAN_GATE,):
                allowed, reason = approval_allowed(IncidentState(**snapshot.values))
                self._publish(incident_id, type="paused", approval_allowed=allowed, approval_reason=reason)
            elif not snapshot.next:
                self._publish(incident_id, type="finished")
        except Exception as exc:
            logger.exception("Runner: incident %s failed", incident_id)
            self._publish(incident_id, type="failed", message=f"{exc.__class__.__name__}: {exc}")
        finally:
            with self._lock:
                self._running.discard(incident_id)
