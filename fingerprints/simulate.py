"""Simulators for the five fingerprint domains. Phase 1 ships these so the
Studio works end to end on day one; real sources plug in whenever ready.

Each simulator returns (records, binding) where binding is the dataset config
{entity_key, timestamp_field, state_fields} a recipe would carry.
"""

import random
import uuid
from datetime import datetime, timedelta

BASE = datetime(2026, 7, 1, 8, 0, 0)

FIRST = ["Wei", "Aisha", "Rajesh", "Mei Ling", "Daniel", "Fatimah", "Arun",
         "Grace", "Hakim", "Priya", "Marcus", "Siti", "Kumar", "Elaine"]
LAST = ["Tan", "Lim", "Sharma", "Ng", "Abdullah", "Lee", "Krishnan", "Wong",
        "Iyer", "Chua", "Hassan", "Goh", "Pillai", "Ong"]


# ---------------------------------------------------------------- payments

PAY_NORMAL = [("gateway", "OK"), ("auth", "OK"), ("validation", "OK"),
              ("aml", "OK"), ("payment-hub", "OK"), ("core-banking", "OK"),
              ("settlement", "SETTLED")]
PAY_AML_TIMEOUT = [("gateway", "OK"), ("auth", "OK"), ("validation", "OK"),
                   ("aml", "TIMEOUT"), ("payment-hub", "FAILED")]
PAY_REJECT = [("gateway", "OK"), ("auth", "OK"), ("validation", "REJECTED")]

LATENCY = {"gateway": (15, 60), "auth": (20, 90), "validation": (25, 120),
           "aml": (40, 300), "payment-hub": (30, 150), "core-banking": (50, 250),
           "settlement": (60, 400)}


def simulate_payments(n_journeys=2000, seed=42):
    rng = random.Random(seed)
    records = []
    for _ in range(n_journeys):
        roll = rng.random()
        path = PAY_NORMAL if roll < 0.85 else PAY_AML_TIMEOUT if roll < 0.95 else PAY_REJECT
        uetr = str(uuid.UUID(int=rng.getrandbits(128)))
        msg_type = rng.choices(["pacs.008", "MT103", "0200"], weights=[6, 3, 1])[0]
        rail = {"pacs.008": "FAST", "MT103": "SWIFT", "0200": "CARDS"}[msg_type]
        # currency correlates strongly with country, as in real corridors
        country = rng.choices(["SG", "MY", "IN", "US", "GB", "HK"],
                              weights=[50, 15, 12, 10, 7, 6])[0]
        home_ccy = {"SG": "SGD", "MY": "MYR", "IN": "INR",
                    "US": "USD", "GB": "GBP", "HK": "USD"}[country]
        currency = home_ccy if rng.random() < 0.8 else rng.choice(
            ["SGD", "USD", "EUR", "MYR", "INR", "GBP"])
        amount = round(rng.lognormvariate(6.5, 1.1), 2)
        debtor_account = "SG%02d%s%010d" % (
            rng.randrange(100), rng.choice(["OCBC", "DBSS", "UOBV"]), rng.randrange(10**10))
        debtor_name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        t = BASE + timedelta(seconds=rng.uniform(0, 14 * 86400))
        for service, status in path:
            lo, hi = LATENCY[service]
            latency = 30000 if status == "TIMEOUT" else rng.randint(lo, hi)
            records.append({
                "uetr": uetr,
                "ts": t.isoformat(timespec="milliseconds"),
                "service": service, "status": status,
                "msg_type": msg_type, "rail": rail,
                "amount": amount, "currency": currency,
                "debtor_account": debtor_account, "debtor_name": debtor_name,
                "country": country, "latency_ms": latency,
            })
            t += timedelta(milliseconds=latency + rng.randint(5, 40))
    binding = {"entity_key": "uetr", "timestamp_field": "ts",
               "state_fields": ["service", "status"]}
    return records, binding


# ------------------------------------------------------------ service logs

SVC_TEMPLATES = [
    ("T01", "INFO", "payment validated", 0.62, (20, 80)),
    ("T02", "INFO", "schema check passed", 0.20, (5, 25)),
    ("T03", "WARN", "db slow query", 0.10, (400, 1500)),
    ("T04", "ERROR", "validation timeout", 0.05, (5000, 9000)),
    ("T05", "ERROR", "iso parse failure", 0.03, (10, 50)),
]


def simulate_service_logs(n_traces=1500, seed=7, error_rate_shift=0.0,
                          extra_template=None):
    """Service fingerprint source. error_rate_shift / extra_template let the
    deployment simulator derive a 'new release' variant of the same service."""
    rng = random.Random(seed)
    templates = [list(t) for t in SVC_TEMPLATES]
    if error_rate_shift:
        templates[3][3] += error_rate_shift   # more T04 timeouts
        templates[0][3] -= error_rate_shift
    if extra_template:
        templates.append(list(extra_template))
    weights = [t[3] for t in templates]
    records = []
    for _ in range(n_traces):
        trace = "%032x" % rng.getrandbits(128)
        t = BASE + timedelta(seconds=rng.uniform(0, 7 * 86400))
        for _ in range(rng.randint(2, 5)):
            tid, level, msg, _w, (lo, hi) = rng.choices(templates, weights=weights)[0]
            records.append({
                "trace_id": trace,
                "ts": t.isoformat(timespec="milliseconds"),
                "template_id": tid, "level": level, "message": msg,
                "latency_ms": rng.randint(int(lo), int(hi)),
                "pod": f"payment-validation-{rng.randint(0, 5)}",
            })
            t += timedelta(milliseconds=rng.randint(2, 200))
    binding = {"entity_key": "trace_id", "timestamp_field": "ts",
               "state_fields": ["template_id", "level"]}
    return records, binding


# -------------------------------------------------------------- deployment

def simulate_deployment(seed=11):
    """Two releases of the same service. v2.14 introduces a SCHEMA_ERROR
    template and shifts timeout rates — a deployment fingerprint is the diff."""
    v1, binding = simulate_service_logs(n_traces=1200, seed=seed)
    v2, _ = simulate_service_logs(
        n_traces=1200, seed=seed + 1, error_rate_shift=0.04,
        extra_template=("T06", "ERROR", "schema error on new field", 0.031, (10, 60)),
    )
    return {"v2.13": v1, "v2.14": v2}, binding


# ---------------------------------------------------------------- incident

INCIDENTS = {
    "redis-saturation": [
        ("redis", "SATURATED"), ("validation", "TIMEOUT"),
        ("aml", "TIMEOUT"), ("payment-hub", "DEGRADED"),
    ],
    "kafka-rebalance": [
        ("kafka", "REBALANCE"), ("consumer-lag", "HIGH"),
        ("payment-hub", "STALLED"), ("settlement", "DELAYED"),
    ],
}


def simulate_incident(kind="redis-saturation", n_windows=200, seed=23):
    rng = random.Random(seed)
    pattern = INCIDENTS[kind]
    records = []
    for i in range(n_windows):
        wid = f"{kind}-{i:04d}"
        t = BASE + timedelta(seconds=rng.uniform(0, 30 * 86400))
        for component, condition in pattern:
            if rng.random() < 0.06:      # occasional missing signal
                continue
            records.append({
                "window_id": wid,
                "ts": t.isoformat(timespec="milliseconds"),
                "component": component, "condition": condition,
                "affected_txns": rng.randint(500, 30000),
                "duration_s": rng.randint(30, 900),
            })
            t += timedelta(seconds=rng.uniform(5, 120))
    binding = {"entity_key": "window_id", "timestamp_field": "ts",
               "state_fields": ["component", "condition"]}
    return records, binding


# ----------------------------------------------------------- infrastructure

def simulate_infra(n_hours=400, seed=31):
    """Kafka broker health: metric-derived states per broker-hour sequence."""
    rng = random.Random(seed)
    records = []
    for i in range(n_hours):
        broker = f"broker-{rng.randint(1, 6)}"
        eid = f"{broker}-h{i:05d}"
        t = BASE + timedelta(hours=i % 300)
        imbalanced = rng.random() < 0.12
        seq = (["BALANCED", "SKEWING", "IMBALANCED", "REBALANCING", "BALANCED"]
               if imbalanced else ["BALANCED"] * rng.randint(3, 5))
        for stage in seq:
            lag = {"BALANCED": rng.randint(0, 500),
                   "SKEWING": rng.randint(2000, 20000),
                   "IMBALANCED": rng.randint(50000, 400000),
                   "REBALANCING": rng.randint(10000, 80000)}[stage]
            records.append({
                "obs_id": eid,
                "ts": t.isoformat(timespec="milliseconds"),
                "broker": broker, "partition_state": stage,
                "max_lag": lag,
                "cpu_pct": min(round(20 + lag / 6000 + rng.uniform(-5, 5), 1), 99.0),
            })
            t += timedelta(minutes=10)
    binding = {"entity_key": "obs_id", "timestamp_field": "ts",
               "state_fields": ["broker", "partition_state"]}
    return records, binding


from .messages import (simulate_camt052, simulate_camt053,  # noqa: E402
                       simulate_camt054, simulate_camt998, simulate_mt,
                       simulate_mt101, simulate_mt199, simulate_mt202,
                       simulate_mt202cov, simulate_mt900, simulate_mt910,
                       simulate_mt940, simulate_mt942, simulate_mx,
                       simulate_pacs009, simulate_pacs009cov, simulate_pain001)

SIMULATORS = {
    "payments": simulate_payments,
    "mx-pacs008-messages": simulate_mx,
    "mx-pacs009-messages": simulate_pacs009,
    "mx-pacs009cov-messages": simulate_pacs009cov,
    "mx-pain001-messages": simulate_pain001,
    "mx-camt052-messages": simulate_camt052,
    "mx-camt053-messages": simulate_camt053,
    "mx-camt054-messages": simulate_camt054,
    "mx-camt998-messages": simulate_camt998,
    "mt101-messages": simulate_mt101,
    "mt103-messages": simulate_mt,
    "mt199-messages": simulate_mt199,
    "mt202-messages": simulate_mt202,
    "mt202cov-messages": simulate_mt202cov,
    "mt900-messages": simulate_mt900,
    "mt910-messages": simulate_mt910,
    "mt940-messages": simulate_mt940,
    "mt942-messages": simulate_mt942,
    "service-logs": simulate_service_logs,
    "incident-redis": lambda **kw: simulate_incident("redis-saturation", **kw),
    "incident-kafka": lambda **kw: simulate_incident("kafka-rebalance", **kw),
    "infra-kafka": simulate_infra,
}
