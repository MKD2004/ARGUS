"""The demo fault scenarios from DEMO_ENVIRONMENT.md §3, defined once.

The dashboard's "Inject Failure" control lists these. Redis connection
exhaustion breaks the payments service for real (DECISIONS.md D-050, D-052);
the others only raise their alert (D-048).
"""

from __future__ import annotations

from pydantic import BaseModel


class Scenario(BaseModel):
    id: str
    label: str
    service_name: str
    alert_type: str
    expected_root_cause: str
    # True when injecting it breaks the demo stack for real, not just raises the alert.
    injects_fault: bool = False


# Service and alert type for database latency, bad deployment, and wrong env var
# are placeholders: DEMO_ENVIRONMENT.md doesn't pin those to one service (D-048).
SCENARIOS: list[Scenario] = [
    Scenario(
        id="redis_exhaustion",
        label="Redis connection exhaustion",
        service_name="payments",
        alert_type="high_latency",
        expected_root_cause="Redis connection pool exhausted",
        injects_fault=True,
    ),
    Scenario(
        id="database_latency",
        label="Database latency spike",
        service_name="inventory",
        alert_type="high_latency",
        expected_root_cause="Database query latency",
    ),
    Scenario(
        id="memory_leak",
        label="Memory leak",
        service_name="inventory",
        alert_type="memory_pressure",
        expected_root_cause="Gradual memory exhaustion",
    ),
    Scenario(
        id="cpu_spike",
        label="CPU spike",
        service_name="auth",
        alert_type="cpu_saturation",
        expected_root_cause="CPU saturation",
    ),
    Scenario(
        id="bad_deployment",
        label="Bad deployment",
        service_name="payments",
        alert_type="error_rate",
        expected_root_cause="Recent deployment correlated with failure onset",
    ),
    Scenario(
        id="expired_certificate",
        label="Expired certificate",
        service_name="api-gateway",
        alert_type="tls_errors",
        expected_root_cause="TLS handshake failures",
    ),
    Scenario(
        id="wrong_env_var",
        label="Wrong environment variable",
        service_name="payments",
        alert_type="error_rate",
        expected_root_cause="Config-driven failure, not code or infra",
    ),
]


def scenario_by_id(scenario_id: str) -> Scenario | None:
    return next((s for s in SCENARIOS if s.id == scenario_id), None)
