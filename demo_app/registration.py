"""AegisOps registration manifest for the synthetic demo-target app (used by the self-check).

Any application registers the same way: POST this JSON to
/api/v1/applications/register. Metric names map the app's own metrics onto
AegisOps' standard signals, so AegisOps needs no app-specific code.
"""


def registration(base_url: str = "http://localhost:9000", prometheus_url: str | None = None,
                 container_name: str | None = None) -> dict:
    b = base_url.rstrip("/")
    signals = {
        "cpu_percent": "shop_cpu_percent", "memory_percent": "shop_memory_percent",
        "error_rate": "shop_error_rate", "p95_latency_seconds": "shop_p95_latency_seconds",
        "request_rate": "shop_request_rate", "db_pool_in_use_ratio": "shop_db_pool_in_use_ratio",
        "seconds_since_deploy": "shop_seconds_since_deploy",
    }
    if prometheus_url:  # PromQL variants scoped to the demo job
        signals = {k: f'{v}{{job="demo-target"}}' for k, v in signals.items()}
    return {
        "name": "demo-target",
        "description": "Synthetic product API used by the AegisOps self-check (not ShopFront).",
        "base_url": b,
        "environment": "demo",
        "tech_stack": ["python", "fastapi", "postgresql"],
        "health_check_url": f"{b}/health",
        "services": [
            {"name": "demo-api", "kind": "http-api", "depends_on": ["demo-db"],
             "endpoints": ["/api/products"]},
            {"name": "demo-db", "kind": "postgresql", "depends_on": []},
        ],
        "deployment": {"platform": "docker", "strategy": "rolling"},
        "infrastructure": {"region": "local"},
        "monitoring": {
            "metrics_url": f"{b}/metrics",
            "prometheus_url": prometheus_url,
            "logs_url": f"{b}/logs/recent",
            "metric_queries": signals,
            "thresholds": {"request_rate": 60},
            "container_name": container_name,
        },
        "remediation_hooks": [
            {"name": "restart", "description": "Restart the demo API workers.", "url": f"{b}/admin/restart",
             "risk": "high", "addresses": ["cpu_saturation", "memory_exhaustion", "service_unavailable", "http_errors",
                                           "high_latency"], "rollback_hint": "Workers restart from the same image."},
            {"name": "rollback", "description": "Roll back to the previous release.", "url": f"{b}/admin/rollback",
             "risk": "high", "addresses": ["bad_deployment", "http_errors"],
             "rollback_hint": "Re-deploy the newer release once fixed."},
            {"name": "clear_cache", "description": "Clear the product-image cache.", "url": f"{b}/admin/clear_cache",
             "risk": "low", "addresses": ["memory_exhaustion"], "rollback_hint": "Cache warms up on demand."},
            {"name": "reset_db_pool", "description": "Recycle the database connection pool.",
             "url": f"{b}/admin/reset_db_pool", "risk": "low", "addresses": ["database_failure"],
             "rollback_hint": "Pool re-creates connections automatically."},
            {"name": "enable_rate_limit", "description": "Enable edge rate limiting.",
             "url": f"{b}/admin/enable_rate_limit", "risk": "low", "addresses": ["abnormal_traffic"],
             "rollback_hint": "Disable the rate limit rule."},
        ],
    }
