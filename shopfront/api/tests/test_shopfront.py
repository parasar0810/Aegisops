"""ShopFront API tests (SQLite; faults enabled with test tokens)."""

import os
import sys
import time
from pathlib import Path

os.environ.update(DATABASE_URL=f"sqlite:///{Path(__file__).parent / 'test.db'}", FAULT_INJECTION_ENABLED="true",
                  FAULT_TOKEN="ft", OPS_TOKEN="ot")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
Path(__file__).with_name("test.db").unlink(missing_ok=True)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from shopfront import faults  # noqa: E402
from shopfront.main import app  # noqa: E402

F = {"X-Fault-Token": "ft"}
OPS = {"Authorization": "Bearer ot"}


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        yield client


def test_catalog_and_details(c):
    products = c.get("/api/products").json()
    assert len(products) == 12
    d = c.get(f"/api/products/{products[0]['id']}").json()
    assert d["name"] and isinstance(d["recommendations"], list)
    assert "Audio" in c.get("/api/categories").json()
    assert c.get("/api/products/9999").status_code == 404


def test_cart_checkout_and_orders(c):
    q = c.post("/api/cart/quote", json={"items": [{"product_id": 1, "quantity": 2}]}).json()
    assert q["subtotal_cents"] == 2 * 12999 and q["shipping_cents"] == 0
    r = c.post("/api/checkout", json={"email": "ann@example.com", "items": [{"product_id": 1, "quantity": 1}]})
    assert r.status_code == 201, r.text
    oid = r.json()["order_id"]
    assert c.get(f"/api/orders/{oid}").json()["email"] == "ann@example.com"
    assert any(o["id"] == oid for o in c.get("/api/orders", params={"email": "ann@example.com"}).json())
    assert c.post("/api/checkout", json={"email": "bad", "items": [{"product_id": 1, "quantity": 1}]}).status_code == 422


def test_health_metrics_logs(c):
    assert c.get("/health").json()["database"] == "up"
    m = c.get("/internal/metrics").text
    for name in ("shopfront_http_requests_total", "shopfront_http_request_duration_seconds_bucket",
                 "shopfront_db_up 1.0", "shopfront_cpu_percent", "shopfront_memory_percent",
                 "shopfront_deploy_timestamp_seconds"):
        assert name in m, name
    logs = c.get("/internal/logs").json()
    assert logs and {"timestamp", "level", "service", "message"} <= set(logs[-1])


def test_fault_api_requires_token(c):
    assert c.post("/internal/faults/http500").status_code == 401
    assert c.post("/internal/faults/http500", headers={"X-Fault-Token": "nope"}).status_code == 401
    assert c.post("/internal/ops/reset_db_pool").status_code == 401
    assert c.post("/internal/ops/rm_rf", headers=OPS).status_code == 404  # not allowlisted


def test_faults_disabled_by_default(c, monkeypatch):
    monkeypatch.setattr(faults, "ENABLED", False)
    assert c.post("/internal/faults/http500", headers=F).status_code == 403


def test_http500_and_rollback(c):
    body = {"email": "bob@example.com", "items": [{"product_id": 2, "quantity": 1}]}
    assert c.post("/internal/faults/http500", headers=F, json={"duration_s": 60}).status_code == 200
    assert c.post("/api/checkout", json=body).status_code == 500
    assert any("KeyError" in x["message"] for x in c.get("/internal/logs").json())
    assert c.post("/internal/ops/rollback_feature_flag", headers=OPS).status_code == 200
    assert c.post("/api/checkout", json=body).status_code == 201


def test_latency_and_disable_recommendations(c):
    c.post("/internal/faults/latency", headers=F, json={"duration_s": 60, "delay_ms": 400})
    t = time.perf_counter()
    c.get("/api/products/1")
    assert time.perf_counter() - t >= 0.4
    c.post("/internal/ops/disable_recommendations", headers=OPS)
    t = time.perf_counter()
    c.get("/api/products/1")
    assert time.perf_counter() - t < 0.4


def test_db_failure_and_reset(c):
    c.post("/internal/faults/db", headers=F, json={"duration_s": 60})
    h = c.get("/health")
    assert h.status_code == 503 and h.json()["database"] == "unreachable"
    assert c.get("/api/products").status_code == 503
    assert "shopfront_db_up 0.0" in c.get("/internal/metrics").text
    assert any("OperationalError" in x["message"] for x in c.get("/internal/logs").json())
    r = c.post("/internal/ops/reset_db_pool", headers=OPS)
    assert "reachable=True" in r.json()["result"]
    assert c.get("/health").status_code == 200


def test_cpu_job_bounded_and_stoppable(c):
    c.post("/internal/faults/cpu", headers=F, json={"duration_s": 30})
    assert faults.state.cpu_stop is not None and "cpu" in faults.active()
    c.post("/internal/ops/stop_background_jobs", headers=OPS)
    assert faults.state.cpu_stop is None and "cpu" not in faults.active()


def test_memory_ballast_released(c):
    faults.enable_memory(duration=30, max_mb=16)  # outside a cgroup: bounded by max_mb
    assert sum(len(b) for b in faults.state.ballast) >= 16 * 2**20
    assert c.post("/internal/ops/release_memory", headers=OPS).json()["result"].endswith("released")
    assert not faults.state.ballast


def test_faults_expire_automatically(c):
    faults.enable_http500(duration=10)
    faults.state.expires["http500"] = time.time() - 1  # pretend time passed
    deadline = time.time() + 8
    while faults.state.checkout_v2 and time.time() < deadline:
        time.sleep(0.5)
    assert not faults.state.checkout_v2
