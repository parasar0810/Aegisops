import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import ActionRiskLevel, ActionStatus, IncidentInput, RecoveryStatus, SecurityAlert


def get_auth_token(client: TestClient, role: str = "admin", username: str = "analyst") -> str:
    res = client.post(
        "/api/auth/token",
        json={"username": username, "password": "analyst", "role": role},
    )
    return res.json()["access_token"]


def load_sample_incident() -> IncidentInput:
    payload = json.loads(Path("app/data/sample_alerts/payment_503_incident.json").read_text(encoding="utf-8"))
    return IncidentInput.model_validate(payload)


def load_sample_alert() -> SecurityAlert:
    payload = json.loads(Path("app/data/sample_alerts/login_anomaly.json").read_text(encoding="utf-8"))
    return SecurityAlert.model_validate(payload)


def test_remediation_created_in_pending_approval_state() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Investigate an incident
    incident = load_sample_incident()
    res = client.post("/api/incidents/investigate", json={"incident": incident.model_dump(mode="json")}, headers=headers)
    assert res.status_code == 200
    report = res.json()
    assert "remediation_action_id" in report
    assert report["remediation_action_id"] is not None

    action_id = report["remediation_action_id"]

    # Verify action in /api/remediations
    action_res = client.get(f"/api/remediations/{action_id}", headers=headers)
    assert action_res.status_code == 200
    action = action_res.json()
    assert action["id"] == action_id
    assert action["status"] == ActionStatus.pending_approval.value
    assert action["recovery_status"] == RecoveryStatus.pending.value
    assert action["is_simulated"] is True
    assert action["proposed_action"] != ""
    assert action["root_cause_hypothesis"] != ""
    assert action["risk_level"] in [r.value for r in ActionRiskLevel]


def test_approve_pending_action_executes_simulation_and_verifies_recovery() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Create incident
    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # Approve action
    approve_res = client.post(
        f"/api/remediations/{action_id}/approve",
        json={"comment": "Approved by SRE lead", "simulate_outcome": "success"},
        headers=headers,
    )
    assert approve_res.status_code == 200
    action = approve_res.json()
    assert action["status"] == ActionStatus.completed.value
    assert action["decided_by"] == "analyst"
    assert action["decision_comment"] == "Approved by SRE lead"
    assert action["recovery_status"] == RecoveryStatus.verified.value
    assert "Recovery verified" in action["recovery_details"]

    # Check simulated metrics comparison
    assert len(action["simulated_metrics"]) >= 2
    for metric in action["simulated_metrics"]:
        assert metric["passed"] is True
        assert "before_value" in metric
        assert "after_value" in metric
        assert "target_threshold" in metric

    # Check execution result
    assert action["execution_result"]["is_simulated"] is True
    assert action["execution_result"]["executed"] is True
    assert action["execution_result"]["status"] == "SIMULATED_EXECUTION_COMPLETED"


def test_reject_pending_action_prevents_execution() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Create incident
    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # Reject action
    reject_res = client.post(
        f"/api/remediations/{action_id}/reject",
        json={"comment": "Requires architecture change first"},
        headers=headers,
    )
    assert reject_res.status_code == 200
    action = reject_res.json()
    assert action["status"] == ActionStatus.rejected.value
    assert action["decided_by"] == "analyst"
    assert action["recovery_status"] == RecoveryStatus.pending.value
    assert action["execution_result"]["executed"] is False
    assert action["execution_result"]["status"] == "REJECTED_BY_OPERATOR"


def test_rejected_action_cannot_ever_execute() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # First reject
    client.post(f"/api/remediations/{action_id}/reject", json={"comment": "Rejecting"}, headers=headers)

    # Attempt to approve rejected action
    attempt_res = client.post(f"/api/remediations/{action_id}/approve", json={"comment": "Sneaky approve"}, headers=headers)
    assert attempt_res.status_code == 400
    assert "previously rejected" in attempt_res.json()["detail"].lower()

    # Verify action remains rejected and unexecuted
    action_res = client.get(f"/api/remediations/{action_id}", headers=headers)
    assert action_res.json()["status"] == ActionStatus.rejected.value
    assert action_res.json()["execution_result"]["executed"] is False


def test_duplicate_approval_prevention() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # First approve
    first_res = client.post(f"/api/remediations/{action_id}/approve", json={"comment": "First approval"}, headers=headers)
    assert first_res.status_code == 200

    # Second approve should return 409 Conflict
    second_res = client.post(f"/api/remediations/{action_id}/approve", json={"comment": "Duplicate approval"}, headers=headers)
    assert second_res.status_code == 409
    assert "already been approved" in second_res.json()["detail"].lower()


def test_duplicate_rejection_prevention() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    client.post(f"/api/remediations/{action_id}/reject", json={"comment": "First reject"}, headers=headers)

    second_reject = client.post(f"/api/remediations/{action_id}/reject", json={"comment": "Second reject"}, headers=headers)
    assert second_reject.status_code == 409


def test_cannot_reject_already_approved_action() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    client.post(f"/api/remediations/{action_id}/approve", json={"comment": "Approved"}, headers=headers)

    reject_res = client.post(f"/api/remediations/{action_id}/reject", json={"comment": "Rejecting after approve"}, headers=headers)
    assert reject_res.status_code == 400


def test_simulated_recovery_failed_outcome() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # Approve with simulated failure outcome
    approve_res = client.post(
        f"/api/remediations/{action_id}/approve",
        json={"comment": "Testing flawed remediation", "simulate_outcome": "failure"},
        headers=headers,
    )
    assert approve_res.status_code == 200
    action = approve_res.json()
    assert action["recovery_status"] == RecoveryStatus.failed.value
    assert "exceeded target" in action["recovery_details"].lower() or "failed" in action["recovery_details"].lower()

    # Assert at least one metric failed
    failed_metrics = [m for m in action["simulated_metrics"] if not m["passed"]]
    assert len(failed_metrics) > 0


def test_simulated_recovery_inconclusive_outcome() -> None:
    client = TestClient(app)
    token = get_auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post("/api/incidents/investigate", json={"incident": load_sample_incident().model_dump(mode="json")}, headers=headers)
    action_id = res.json()["remediation_action_id"]

    # Approve with simulated inconclusive outcome
    approve_res = client.post(
        f"/api/remediations/{action_id}/approve",
        json={"comment": "Testing telemetry drop", "simulate_outcome": "inconclusive"},
        headers=headers,
    )
    assert approve_res.status_code == 200
    action = approve_res.json()
    assert action["recovery_status"] == RecoveryStatus.inconclusive.value
    assert "inconclusive" in action["recovery_details"].lower()


def test_auth_and_rbac_on_remediation_endpoints() -> None:
    client = TestClient(app)

    # 1. Unauthenticated request to /api/remediations
    unauth_res = client.get("/api/remediations")
    assert unauth_res.status_code == 401

    # 2. Security analyst trying to access production remediation action
    admin_token = get_auth_token(client, role="admin")
    prod_res = client.post(
        "/api/incidents/investigate",
        json={"incident": load_sample_incident().model_dump(mode="json")},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    action_id = prod_res.json()["remediation_action_id"]

    sec_token = get_auth_token(client, role="security_analyst")
    sec_res = client.get(
        f"/api/remediations/{action_id}",
        headers={"Authorization": f"Bearer {sec_token}"},
    )
    assert sec_res.status_code == 403

    sec_approve = client.post(
        f"/api/remediations/{action_id}/approve",
        headers={"Authorization": f"Bearer {sec_token}"},
    )
    assert sec_approve.status_code == 403
