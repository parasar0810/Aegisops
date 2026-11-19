"""Evidence analysis: specialist analyzers, causal synthesis, remediation planning.

The deterministic analyzers are the no-credential fallback for the CrewAI
specialists (see ``a2a_specialists.py``). Synthesis is shared by both paths:
it correlates findings causally (causes explain symptoms), scores each
hypothesis by independent supporting evidence, and refuses to name a root
cause when evidence is thin.
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.sre.models import (
    Evidence,
    Hypothesis,
    IncidentState,
    RegisteredApplication,
    RemediationOption,
    RiskLevel,
    RootCauseAnalysis,
    SpecialistFinding,
)
from app.sre.models import (
    FailureCategory as C,
)

DEFAULT_THRESHOLDS = {
    "cpu_percent": 85.0,
    "memory_percent": 85.0,
    "error_rate": 0.05,
    "p95_latency_seconds": 2.0,
    "request_rate": 100.0,
    "db_pool_in_use_ratio": 0.9,
    "seconds_since_deploy": 900.0,  # deploy within 15 min is "recent"
}

# Root causes and the symptoms they commonly produce.
CAUSES: dict[C, set[C]] = {
    C.database_failure: {C.http_errors, C.high_latency, C.service_unavailable},
    C.bad_deployment: {C.http_errors, C.high_latency, C.service_unavailable},
    C.memory_exhaustion: {C.high_latency, C.http_errors, C.service_unavailable},
    C.cpu_saturation: {C.high_latency, C.http_errors},
    C.abnormal_traffic: {C.cpu_saturation, C.high_latency, C.http_errors},
    C.service_unavailable: {C.http_errors},
}

STATEMENTS = {
    C.cpu_saturation: "CPU saturation on the service is degrading request handling.",
    C.memory_exhaustion: "Memory exhaustion (leak or unbounded allocation) is degrading the service.",
    C.http_errors: "The application is returning elevated HTTP 5xx errors.",
    C.high_latency: "Request latency is elevated beyond the service objective.",
    C.database_failure: "Database connectivity failure / connection-pool exhaustion is causing request failures.",
    C.service_unavailable: "The service is unavailable (health endpoint unreachable or failing).",
    C.bad_deployment: "A recent deployment introduced a regression.",
    C.abnormal_traffic: "Abnormal request traffic volume is overloading the service.",
    C.unknown: "Root cause could not be determined from the available evidence.",
}

_LOG_PATTERNS: list[tuple[C, re.Pattern[str], str]] = [
    (C.database_failure, re.compile(r"connection pool exhausted|could not connect to (the )?database|"
                                    r"too many connections|database (is )?unavailable|db timeout|"
                                    r"connection to server .{0,80} failed|OperationalError", re.IGNORECASE), "database"),
    (C.memory_exhaustion, re.compile(r"out ?of ?memory|MemoryError|OOM|heap", re.IGNORECASE), "memory"),
    (C.cpu_saturation, re.compile(r"saturated|queue depth|cpu throttl", re.IGNORECASE), "cpu"),
    (C.http_errors, re.compile(r"Traceback|Exception|Internal Server Error|\b500\b", re.IGNORECASE), "exception"),
    (C.abnormal_traffic, re.compile(r"rate limit|too many requests|\b429\b|bot|flood", re.IGNORECASE), "traffic"),
    (C.bad_deployment, re.compile(r"deploy(ed|ment)|release v?\d|rollout", re.IGNORECASE), "deployment"),
]


def _thresholds(app: RegisteredApplication | None) -> dict[str, float]:
    return {**DEFAULT_THRESHOLDS, **(app.monitoring.thresholds if app else {})}


def _metric_evidence(state: IncidentState) -> list[Evidence]:
    return [e for e in state.evidence if e.kind == "metric"]


def _ids(evs: list[Evidence]) -> list[str]:
    return [e.evidence_id for e in evs]


# ── Deterministic specialist analyzers ──────────────────────────────────────


def analyze(specialty: str, state: IncidentState, app: RegisteredApplication | None) -> list[SpecialistFinding]:
    th = _thresholds(app)
    metrics = state.metric_snapshot()
    m_ids = _ids(_metric_evidence(state))
    health = [e for e in state.evidence if e.kind == "health" and not e.data.get("probe")]
    probes = [e for e in state.evidence if e.data.get("probe") and e.data.get("reachable")]
    logs = [e for e in state.evidence if e.kind == "log"]
    out: list[SpecialistFinding] = []

    def add(cat: C, obs: str, ids: list[str], w: float) -> None:
        out.append(SpecialistFinding(agent=specialty, category=cat, observation=obs,
                                     supporting_evidence=ids, weight=round(min(w, 1.0), 2)))

    def over(sig: str) -> float | None:
        v = metrics.get(sig)
        return None if v is None or v <= th[sig] else v

    def log_hits(cat: C) -> tuple[int, list[str]]:
        n, ids = 0, []
        for e in logs:
            hits = sum(1 for ln in e.data.get("logs", [])
                       for c, pat, _ in _LOG_PATTERNS if c == cat and pat.search(str(ln.get("message", ""))))
            if hits:
                n += hits
                ids.append(e.evidence_id)
        return n, ids

    if specialty == "sre_agent":
        if (v := over("cpu_percent")) is not None:
            add(C.cpu_saturation, f"CPU at {v:.0f}% (threshold {th['cpu_percent']:.0f}%).", m_ids, 0.5 + (v - th["cpu_percent"]) / 50)
        if (v := over("memory_percent")) is not None:
            add(C.memory_exhaustion, f"Memory at {v:.0f}% (threshold {th['memory_percent']:.0f}%).", m_ids, 0.5 + (v - th["memory_percent"]) / 50)
        n, ids = log_hits(C.memory_exhaustion)
        if n:
            add(C.memory_exhaustion, f"{n} out-of-memory / heap-pressure log lines.", ids, min(0.3 + n / 10, 0.7))
        n, ids = log_hits(C.cpu_saturation)
        if n:
            add(C.cpu_saturation, f"{n} worker/CPU saturation log lines.", ids, min(0.3 + n / 10, 0.6))
        if (v := over("p95_latency_seconds")) is not None:
            add(C.high_latency, f"p95 latency {v:.2f}s (threshold {th['p95_latency_seconds']:.1f}s).", m_ids, 0.6)
        slow = [p for p in probes if (p.data.get("latency_ms") or 0) > th["p95_latency_seconds"] * 1000 * 0.5]
        if slow:
            add(C.high_latency, f"{len(slow)} live endpoint probe(s) slow (max {max(p.data['latency_ms'] for p in slow):.0f} ms).",
                _ids(slow), 0.5)
        if metrics.get("up") == 0:  # Prometheus could not scrape the target at all
            add(C.service_unavailable, "Prometheus reports the scrape target down (up=0).", m_ids, 0.8)
        for h in health:
            code = h.data.get("status_code")
            if not h.data.get("reachable") or (code and code >= 500):
                add(C.service_unavailable, f"Health check failed (status={code}, reachable={h.data.get('reachable')}).",
                    [h.evidence_id], 0.8)
    elif specialty == "backend_agent":
        if (v := over("error_rate")) is not None:
            add(C.http_errors, f"HTTP 5xx error rate {v:.0%} (threshold {th['error_rate']:.0%}).", m_ids, 0.5 + v)
        n, ids = log_hits(C.http_errors)
        if n:
            add(C.http_errors, f"{n} exception/500 log lines in the incident window.", ids, min(0.3 + n / 20, 0.7))
        failing = [p for p in probes if (p.data.get("status_code") or 0) >= 500]
        if failing:
            add(C.http_errors, f"{len(failing)} live endpoint probe(s) returned 5xx.", _ids(failing), 0.4)
    elif specialty == "database_agent":
        n, ids = log_hits(C.database_failure)
        if n:
            add(C.database_failure, f"{n} database connectivity / pool-exhaustion log lines.", ids, min(0.5 + n / 10, 0.95))
        if (v := over("db_pool_in_use_ratio")) is not None:
            add(C.database_failure, f"DB connection pool {v:.0%} in use.", m_ids, 0.7)
        if metrics.get("db_up") == 0:  # app-reported database connectivity probe
            add(C.database_failure, "Application reports its database as unreachable (db_up=0).", m_ids, 0.8)
        for e in state.evidence:
            if e.kind == "database" and e.data.get("reachable") is False:
                add(C.database_failure, "Database endpoint unreachable over TCP.", [e.evidence_id], 0.8)
    elif specialty == "cloud_devops_agent":
        since = metrics.get("seconds_since_deploy")
        if since is not None and since < th["seconds_since_deploy"]:
            # Errors starting right after a release is the classic bad-deploy signature.
            erroring = over("error_rate") is not None or log_hits(C.http_errors)[0] > 0
            add(C.bad_deployment, f"Deployment {since / 60:.0f} min before the incident"
                + ("; error onset follows the release." if erroring else "."),
                m_ids + (log_hits(C.http_errors)[1] if erroring else []), 0.75 if erroring else 0.35)
        for e in state.evidence:
            if e.kind == "container" and e.data.get("available"):
                if e.data.get("oom_killed"):
                    add(C.memory_exhaustion, "Container was OOM-killed.", [e.evidence_id], 0.9)
                if e.data.get("restarting") or e.data.get("status") not in (None, "running"):
                    add(C.service_unavailable, f"Container status {e.data.get('status')}.", [e.evidence_id], 0.7)
    elif specialty == "security_agent":
        if (v := over("request_rate")) is not None:
            add(C.abnormal_traffic, f"Request rate {v:.0f}/s exceeds {th['request_rate']:.0f}/s.", m_ids, 0.5 + min(v / th["request_rate"] - 1, 0.45))
        n, ids = log_hits(C.abnormal_traffic)
        if n:
            add(C.abnormal_traffic, f"{n} rate-limit / flood log lines.", ids, min(0.3 + n / 20, 0.6))
        flagged = sum(e.data.get("injection_flagged", 0) for e in logs)
        if flagged:
            add(C.abnormal_traffic, f"{flagged} log lines contained prompt-injection or command patterns (filtered).", _ids(logs), 0.2)
    return out


# ── Synthesis ───────────────────────────────────────────────────────────────


def synthesize(state: IncidentState, past: list[IncidentState] | None = None) -> RootCauseAnalysis:
    by_cat: dict[C, list[SpecialistFinding]] = defaultdict(list)
    for f in state.findings:
        by_cat[f.category].append(f)

    ev_kind = {e.evidence_id: e.kind for e in state.evidence}

    def direct(cat: C) -> float:
        return sum(f.weight for f in by_cat.get(cat, []))

    scores: dict[C, float] = {}
    explains: dict[C, list[C]] = {}
    for cat in by_cat:
        s = direct(cat)
        explained = [sym for sym in CAUSES.get(cat, set()) if sym in by_cat]
        s += 0.3 * sum(direct(sym) for sym in explained)
        scores[cat], explains[cat] = s, explained
    # A symptom that a cause with evidence explains is not itself the root cause.
    for cause, syms in explains.items():
        for sym in syms:
            if direct(cause) >= 0.5 and sym in scores:
                scores[sym] *= 0.5

    confirmed = [f"[{f.agent}] {f.observation}" for f in state.findings]
    missing = [f"{e.source}: {e.data.get('error')}" for e in state.evidence if e.data.get("error")]
    missing += [f"{t} not configured or failed" for t in state.triage.get("unavailable_tools", [])]

    if not scores:
        return RootCauseAnalysis(
            primary=Hypothesis(category=C.unknown, statement=STATEMENTS[C.unknown], confidence=0.1),
            confirmed_observations=confirmed, missing_evidence=missing or ["No anomalous signals were found."],
            confidence_explanation="No specialist produced a finding; the alert may be a false positive or the "
                                   "relevant telemetry is not configured.",
            insufficient_evidence=True)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    def hyp(cat: C, score: float) -> Hypothesis:
        support = sorted({i for f in by_cat[cat] for i in f.supporting_evidence}
                         | {i for s in explains.get(cat, []) for f in by_cat[s] for i in f.supporting_evidence})
        kinds = {ev_kind.get(i) for i in support} - {None}
        # Confidence grows with score and with independent evidence kinds (metric, log, health...).
        conf = (score / (score + 0.8)) * min(1.0, 0.55 + 0.2 * len(kinds))
        stmt = STATEMENTS[cat]
        if explains.get(cat):
            stmt += " It explains observed " + ", ".join(s.value for s in explains[cat]) + "."
        return Hypothesis(category=cat, statement=stmt, confidence=round(min(conf, 0.95), 2),
                          supporting_evidence=support)

    # Rank by evidence-weighted confidence, not raw score: corroboration beats one loud signal.
    hyps = sorted((hyp(c, s) for c, s in ranked), key=lambda h: h.confidence, reverse=True)
    primary, alternatives = hyps[0], hyps[1:4]
    insufficient = primary.confidence < 0.45
    kinds = {ev_kind.get(i) for i in primary.supporting_evidence} - {None}
    explanation = (f"Score {scores[primary.category]:.2f} from {len(by_cat[primary.category])} direct finding(s) "
                   f"across {len(kinds)} evidence type(s) ({', '.join(sorted(kinds))}).")
    if alternatives and alternatives[0].confidence > primary.confidence - 0.1:
        explanation += f" Close alternative: {alternatives[0].category.value}; treat the primary as probable, not confirmed."
    if insufficient:
        explanation += " Evidence is insufficient for a confident root cause; investigate further before remediating."
        missing.append("Corroborating evidence from a second independent source (logs, traces or health).")

    similar = [f"{p.incident_id}: {p.rca.primary.category.value} on {p.application} -> "
               f"{', '.join(x.hook_name or 'manual' for x in p.executions if x.status == 'succeeded') or 'no action'}"
               f" ({p.phase.value})"
               for p in (past or []) if p.rca and p.rca.primary.category == primary.category
               and p.incident_id != state.incident_id][:3]

    return RootCauseAnalysis(primary=primary, alternatives=alternatives, confirmed_observations=confirmed,
                             missing_evidence=missing, confidence_explanation=explanation,
                             insufficient_evidence=insufficient, similar_past_incidents=similar)


# ── Remediation planning ────────────────────────────────────────────────────

_RISK_ORDER = {RiskLevel.read_only: 0, RiskLevel.low: 1, RiskLevel.high: 2}


def plan_remediation(rca: RootCauseAnalysis, app: RegisteredApplication | None,
                     past: list[IncidentState] | None = None) -> list[RemediationOption]:
    cat = rca.primary.category
    worked_before = {x.hook_name for p in (past or []) if p.phase.value == "resolved" and p.rca
                     and p.rca.primary.category == cat for x in p.executions if x.status == "succeeded"}
    hooks = [h for h in (app.remediation_hooks if app else []) if cat in h.addresses]
    # Prefer hooks that verifiably fixed this category before, then lower risk.
    hooks.sort(key=lambda h: (h.name not in worked_before, _RISK_ORDER[h.risk]))
    options: list[RemediationOption] = []
    if not rca.insufficient_evidence:
        for i, h in enumerate(hooks, start=1):
            options.append(RemediationOption(
                hook_name=h.name, description=h.description, risk=h.risk,
                expected_impact=f"Addresses {cat.value}" + (" (resolved a similar past incident)" if h.name in worked_before else ""),
                side_effects="Brief request failures possible during the action." if h.risk == RiskLevel.high
                else "Minimal; scoped to the affected component.",
                rollback=h.rollback_hint or "Re-run the previous known-good configuration.",
                rank=i, requires_approval=h.risk != RiskLevel.read_only))
    options.append(RemediationOption(
        hook_name=None, risk=RiskLevel.read_only, rank=len(options) + 1, requires_approval=False,
        description=("Continue investigation: " + "; ".join(rca.missing_evidence[:3]))
        if rca.insufficient_evidence or not hooks else "Escalate to on-call engineer with this report.",
        expected_impact="No system change.", rollback="Not applicable."))
    return options
