"""ShopFront API: catalog, cart, checkout and orders, with health, metrics and logs.

Public routes live under /api (proxied by the web tier). Operational routes live under
/internal and /health and are only reachable on the private Docker network.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from shopfront import faults
from shopfront.db import Order, OrderItem, Product, db
from shopfront.telemetry import (DB_ERRORS, DB_POOL, DB_UP, LATENCY, ORDERS, RECENT_LOGS, REQUESTS, event,
                                 refresh_resource_gauges)

FAULT_TOKEN = os.environ.get("FAULT_TOKEN", "")
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")
VERSION = os.environ.get("APP_VERSION", "2.4.0")
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in range(30):  # wait for PostgreSQL on cold start
        try:
            db.init()
            break
        except SQLAlchemyError:
            await asyncio.sleep(2)
    threading.Thread(target=faults.watchdog, daemon=True).start()
    event("info", f"shopfront-api {VERSION} started", faults_enabled=faults.ENABLED)
    yield


app = FastAPI(title="ShopFront API", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def telemetry(request: Request, call_next):
    started = time.perf_counter()
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        raise
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route.startswith("/api"):
            elapsed = time.perf_counter() - started
            REQUESTS.labels(request.method, route, str(status)).inc()
            LATENCY.labels(route).observe(elapsed)
            event("error" if status >= 500 else "info", f"{request.method} {request.url.path} {status}",
                  request_id=rid, route=route, status=status, duration_ms=round(elapsed * 1000, 1))
    response.headers["x-request-id"] = rid
    return response


@app.exception_handler(SQLAlchemyError)
async def db_error(_: Request, exc: SQLAlchemyError):
    DB_ERRORS.inc()
    event("error", f"OperationalError: {str(exc).splitlines()[0][:300]}", service="shopfront-db-client")
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


# ── Catalog, cart, checkout, orders ─────────────────────────────────────────

def _product(p: Product) -> dict:
    return {"id": p.id, "sku": p.sku, "name": p.name, "category": p.category, "description": p.description,
            "price_cents": p.price_cents, "stock": p.stock, "image": p.image}


@app.get("/api/products")
def list_products(category: str | None = None) -> list[dict]:
    with db.session() as s:
        q = select(Product).order_by(Product.id)
        if category:
            q = q.where(Product.category == category)
        return [_product(p) for p in s.scalars(q)]


@app.get("/api/categories")
def categories() -> list[str]:
    with db.session() as s:
        return sorted({c for c in s.scalars(select(Product.category))})


@app.get("/api/products/{product_id}")
async def product_detail(product_id: int) -> dict:
    with db.session() as s:
        p = s.get(Product, product_id)
        if p is None:
            raise HTTPException(404, "product not found")
        others = s.scalars(select(Product).where(Product.category == p.category, Product.id != p.id).limit(3))
        recs = [_product(o) for o in others]
        item = _product(p)
    if faults.state.recommendations_delay_ms:  # slow recommendations dependency
        await asyncio.sleep(faults.state.recommendations_delay_ms / 1000)
    return {**item, "recommendations": recs}


class CartLine(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=20)


class Cart(BaseModel):
    items: list[CartLine] = Field(min_length=1, max_length=50)


class Checkout(Cart):
    email: str = Field(pattern=EMAIL, max_length=200)


def _price(s, items: list[CartLine]) -> tuple[list[tuple[Product, int]], int]:
    lines, total = [], 0
    for line in items:
        p = s.get(Product, line.product_id)
        if p is None:
            raise HTTPException(422, f"unknown product {line.product_id}")
        if p.stock < line.quantity:
            raise HTTPException(409, f"only {p.stock} of {p.name} left")
        lines.append((p, line.quantity))
        total += p.price_cents * line.quantity
    return lines, total


@app.post("/api/cart/quote")
def quote(cart: Cart) -> dict:
    with db.session() as s:
        lines, total = _price(s, cart.items)
        shipping = 0 if total >= 5000 else 499
        return {"subtotal_cents": total, "shipping_cents": shipping, "total_cents": total + shipping,
                "lines": [{"product_id": p.id, "name": p.name, "quantity": q, "unit_cents": p.price_cents}
                          for p, q in lines]}


@app.post("/api/checkout", status_code=201)
def checkout(body: Checkout) -> dict:
    with db.session() as s:
        lines, total = _price(s, body.items)
        if faults.state.checkout_v2:  # release 2.4.0 regression behind the checkout_v2 flag
            ORDERS.labels("error").inc()
            event("error", "Traceback (most recent call last): KeyError: 'tax_region' in checkout_v2.calculate_tax")
            raise HTTPException(500, "internal server error")
        order = Order(email=body.email, total_cents=total + (0 if total >= 5000 else 499))
        for p, q in lines:
            p.stock -= q
            order.items.append(OrderItem(product_id=p.id, quantity=q, unit_cents=p.price_cents))
        s.add(order)
        s.commit()
        ORDERS.labels("placed").inc()
        event("info", f"Order {order.id} placed", order_id=order.id, total_cents=order.total_cents)
        return {"order_id": order.id, "status": order.status, "total_cents": order.total_cents}


def _order(o: Order) -> dict:
    return {"id": o.id, "email": o.email, "status": o.status, "total_cents": o.total_cents,
            "created_at": o.created_at,
            "items": [{"product_id": i.product_id, "quantity": i.quantity, "unit_cents": i.unit_cents} for i in o.items]}


@app.get("/api/orders")
def orders(email: str = Query(pattern=EMAIL, max_length=200)) -> list[dict]:
    with db.session() as s:
        return [_order(o) for o in s.scalars(select(Order).where(Order.email == email).order_by(Order.id.desc()).limit(20))]


@app.get("/api/orders/{order_id}")
def order(order_id: int) -> dict:
    with db.session() as s:
        o = s.get(Order, order_id)
        if o is None:
            raise HTTPException(404, "order not found")
        return _order(o)


# ── Operational endpoints (private network only) ────────────────────────────

@app.get("/health")
def health() -> Response:
    ok = db.ping()
    DB_UP.set(1 if ok else 0)
    if not ok:
        event("error", "Health check: database connection to server failed", service="shopfront-db-client")
    body = {"status": "ok" if ok else "degraded", "database": "up" if ok else "unreachable", "version": VERSION}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/internal/metrics")
def metrics() -> Response:
    DB_UP.set(1 if db.ping() else 0)
    DB_POOL.set(db.pool_in_use_ratio())
    refresh_resource_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/internal/logs")
def logs(limit: int = 300) -> list[dict]:
    return list(RECENT_LOGS)[-min(max(limit, 1), 2000):]


def _check(token: str | None, expected: str) -> None:
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "invalid token")


class FaultRequest(BaseModel):
    duration_s: float = Field(default=600, ge=10, le=faults.MAX_DURATION)
    delay_ms: int = Field(default=2500, ge=100, le=8000)
    target_percent: float = Field(default=90, ge=10, le=92)


@app.get("/internal/faults")
def list_faults(x_fault_token: str | None = Header(default=None)) -> dict:
    _check(x_fault_token, FAULT_TOKEN)
    return {"enabled": faults.ENABLED, "active": faults.active()}


@app.post("/internal/faults/{name}")
def inject(name: str, body: FaultRequest | None = None, x_fault_token: str | None = Header(default=None)) -> dict:
    if not faults.ENABLED:
        raise HTTPException(403, "fault injection is disabled (FAULT_INJECTION_ENABLED=false)")
    _check(x_fault_token, FAULT_TOKEN)
    if name not in faults.ENABLE:
        raise HTTPException(404, f"unknown fault; choose from {sorted(faults.ENABLE)} (downtime: supervisor port 8081)")
    b = body or FaultRequest()
    kwargs = {"latency": {"delay_ms": b.delay_ms}, "memory": {"target_percent": b.target_percent}}.get(name, {})
    faults.ENABLE[name](duration=b.duration_s, **kwargs)
    return {"injected": name, "active": faults.active()}


@app.delete("/internal/faults/{name}")
def clear(name: str, x_fault_token: str | None = Header(default=None)) -> dict:
    _check(x_fault_token, FAULT_TOKEN)
    if name not in faults.DISABLE:
        raise HTTPException(404, "unknown fault")
    return {"cleared": name, "result": faults.DISABLE[name](), "active": faults.active()}


@app.post("/internal/ops/{action}")
def ops(action: str, authorization: str | None = Header(default=None)) -> dict:
    """Allowlisted remediation actions, called by AegisOps after human approval."""
    _check((authorization or "").removeprefix("Bearer "), OPS_TOKEN)
    if action not in faults.OPS:
        raise HTTPException(404, f"action not allowlisted; allowed: {sorted(faults.OPS)}")
    result = faults.OPS[action]()
    event("info", f"Remediation '{action}' executed: {result}", service="shopfront-ops")
    return {"action": action, "result": result}
