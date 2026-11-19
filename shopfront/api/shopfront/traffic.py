"""Gentle synthetic shoppers (~1.5 requests/s) so error-rate and latency metrics have data.

Browses the catalog, opens products, quotes carts and occasionally checks out, through the
public web tier exactly like a browser would. Not a load test: rate is fixed and small.
"""

import os
import random
import time

import httpx

BASE = os.environ.get("SHOPFRONT_URL", "http://shopfront-web").rstrip("/")
INTERVAL = float(os.environ.get("TRAFFIC_INTERVAL_S", "0.7"))


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=15)
    ids: list[int] = []
    while True:
        try:
            if not ids:
                ids = [p["id"] for p in client.get("/api/products").json()]
            roll = random.random()
            pid = random.choice(ids)
            if roll < 0.35:
                client.get("/api/products")
            elif roll < 0.75:
                client.get(f"/api/products/{pid}")
            elif roll < 0.92:
                client.post("/api/cart/quote", json={"items": [{"product_id": pid, "quantity": 1}]})
            else:
                client.post("/api/checkout", json={"email": f"shopper{random.randint(1, 50)}@example.com",
                                                    "items": [{"product_id": pid, "quantity": 1}]})
        except Exception:
            ids = []  # app down or database failing: keep trying gently
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
