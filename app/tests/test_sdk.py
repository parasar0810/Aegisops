"""SDK -> AegisOps webhook contract test (real FastAPI app, in-process)."""

import sys
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))
from aegisops_sdk import AegisOpsClient, AegisOpsMiddleware

from app.core.security import create_access_token
from app.main import app as aegis_app
from app.models.schemas import UserRole
from app.sre import routes as sre_routes
from app.sre.pipeline import SREOrchestrator
from app.sre.store import StateStore
from demo_app.registration import registration


def test_sdk_registers_and_reports_exceptions(tmp_path):
    store = StateStore(tmp_path)
    sre_routes.ctx.store = store
    sre_routes.ctx.orch = SREOrchestrator(store, "http://aegis.test", httpx.ASGITransport(app=aegis_app))
    with TestClient(aegis_app) as aegis:
        client = AegisOpsClient("http://testserver", "demo-target",
                                admin_token=create_access_token("ops", UserRole.admin), transport=aegis._transport)
        assert client.register(registration("http://shopfront.test")).startswith("aeg_")

        user_app = FastAPI()
        user_app.add_middleware(AegisOpsMiddleware, client=client, service="checkout")

        @user_app.get("/boom")
        def boom():
            raise RuntimeError("payment provider timeout")

        with TestClient(user_app, raise_server_exceptions=False) as uc:
            r = uc.get("/boom", headers={"X-Correlation-ID": "corr-123"})
            assert r.status_code == 500
        logs = store.read_logs("demo-target")
        assert any("payment provider timeout" in ln["message"] and ln["correlation_id"] == "corr-123" for ln in logs)
        # Wrong key is rejected and never raises into the caller.
        assert AegisOpsClient("http://testserver", "demo-target", ingest_key="bad",
                              transport=aegis._transport).send_event("x") is None
