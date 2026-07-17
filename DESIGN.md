# Generic Fingerprint Modeling — Design

## 1. Core principle

A **fingerprint is a compact generative model of an event dataset**, not a hash or a label.
If a fingerprint can *generate* data statistically indistinguishable from what it learned, then
one abstraction gives you every capability the platform needs:

| Capability | How the generative fingerprint provides it |
|---|---|
| **Learn** any dataset | `fit(records)` — schema induction + distribution learning, no per-dataset code |
| **Represent** behavior compactly | 100M log lines → one fingerprint object (KBs) |
| **Recognize** known patterns | `score(journey)` — likelihood under the fingerprint; low likelihood = novel/anomalous |
| **Reproduce** synthetic data | `sample(n)` — draw new records from the learned distributions |
| **Anonymize** | policy layer swaps sensitive fields for synthesized/masked equivalents; the statistical shape survives, the PII does not |
| **Diff** (deployment fingerprints) | compare two fingerprints of the same entity across time/versions |

## 2. Canonical event model

Every source is normalized by a thin **adapter** into the same shape before modeling:

```
Record = {
  entity_key   : groups records into a journey/session (UETR, trace_id, pod, txn STAN…)
  timestamp    : ordering + temporal model
  state_fields : the fields whose combination defines a behavioral "state"
                 (service+status, log_template_id, k8s condition, ISO8583 MTI+RC…)
  attributes   : everything else — typed automatically
}
```

Adapters (parsing only, no modeling logic): ISO 20022 MX (XML→path/value), SWIFT MT
(tag blocks), ISO 8583 (bitmap fields), OTEL logs/traces, Kafka/Redis/DB/K8s metrics,
CI/CD release events, incident tickets. **The model never knows which domain it is in.**

## 3. Anatomy of a fingerprint (five learned layers)

1. **Schema profile** — inferred field types: `categorical | numeric | identifier | timestamp | text`.
   Uniqueness measured *per entity* (an account number repeated across a journey's 7 events is
   still an identifier). Identifier fields get a *format pattern* (char-class template) so fresh,
   format-valid values can be generated.
2. **Field marginals** — frequency tables (categorical), empirical distributions (numeric),
   format patterns (identifiers). Fields that are constant within a journey (amount, currency,
   debtor) are detected and modeled once per journey.
3. **Conditional dependencies** — per-state distributions (e.g. `latency_ms | state=aml-TIMEOUT`
   differs from `latency_ms | state=aml-OK`). Production upgrade: Bayesian network / copula / CTGAN.
4. **Sequence model** — Markov chain over states per journey (production upgrade: small
   transformer / SDV-PAR). This is the "execution path" — the heart of payment fingerprints.
5. **Temporal model** — start-time window + empirical inter-arrival distribution
   (production upgrade: PatchTST/TFT for rate forecasting and drift).

Plus a derived **embedding signature** (vector of path/field statistics, or a contrastive
encoder in production) so fingerprints themselves can be compared, clustered, and searched.

## 4. The five platform fingerprints = five configurations of the same model

| Fingerprint | entity_key | state_fields | What the layers capture |
|---|---|---|---|
| **Payment** (MX/MT/ISO8583) | UETR / MsgId / STAN | service + status (or MTI + response code) | Sequence model = normal & abnormal execution paths; conditionals = amounts/codes per state |
| **Service** | trace_id or time-bucket per service | log template id + level | Marginals = template mix; conditionals = latency/error per template; temporal = rate/seasonality |
| **Deployment** | service + version | same as service | **A diff of two service fingerprints** (v_n vs v_n+1): new states, shifted distributions, changed transitions |
| **Incident** | incident window | deviating states across entities | A labeled snapshot of *which* fingerprints deviated and how — matched by embedding similarity for instant recognition |
| **Infrastructure** | broker / node / pool | metric-derived states (lag-skew bucket, saturation level, wait-class) | Sequence + temporal layers capture the characteristic *shape* of Kafka imbalance, Redis saturation, DB contention, node pressure |

Normal vs abnormal payment paths fall out naturally: frequent paths are the normal
fingerprint; rare-but-seen paths are *known failure fingerprints* (recognized instantly with
their historical frequency); zero-probability transitions are *novel* anomalies.

## 5. Synthetic data with configurable anonymization

Two modes, both driven by one config file (see `config/payment_fingerprint.json`):

- **Synthesize mode** — `fingerprint.sample(n)` produces entirely new journeys. No record maps
  to a real one; identifiers are freshly generated from learned formats. Safest for sharing
  test data with vendors / lower environments.
- **Anonymize mode** — real records pass through per-field policies:

| Policy | Effect | Typical fields |
|---|---|---|
| `drop` | field removed | names, addresses, free-text remittance info |
| `hash` | salted SHA-256 (irreversible, join-preserving) | customer IDs used only for joins |
| `mask` | `********1234` | PAN, IBAN, account numbers |
| `generalize` | bucket numerics / truncate timestamps or geo | amounts, birthdates |
| `synthesize` | format-preserving pseudonym from the learned identifier pattern; **consistent** (same real value → same pseudonym) so journeys still correlate | UETR, MsgId, STAN |
| `keep` | untouched | currency, country, rail, status |

Production hardening: format-preserving encryption (FF3-1) instead of random pseudonyms where
reversibility under key custody is required; differential-privacy noise on learned marginals
before sampling; PII auto-detection (e.g. Presidio) to propose the policy file instead of
hand-writing it.

## 5b. Fingerprint Studio — the UI

A web workbench over the engine API (`/connect /fit /inspect /policies /generate /score /diff`).
Every screen state serializes into a **recipe** (JSON) making any run repeatable and schedulable.

Workflow: **Connect → Learn → Inspect → Mark PII → Generate → Validate → Re-run**

- **Connect — five source integrations**, each configured/tested/saved once in the
  Integrations screen, then reused by any recipe:
  - *File upload*: drag-and-drop CSV/NDJSON/Parquet (gzip ok); auto-detects compression,
    delimiter, encoding, header row, with manual overrides.
  - *Database table*: read-only SQLAlchemy (PostgreSQL, MySQL, SQL Server, Oracle);
    sampling by rows or time window.
  - *Kafka topic*: brokers + SASL/SSL or mTLS (vault-referenced credentials), own consumer
    group, Schema Registry (JSON/Avro/Protobuf); sample N messages / time window, or
    **continuous mode** where the fingerprint keeps learning and flags drift live.
  - *Bulk API upload*: REST ingestion for teams without DB/broker access —
    `POST /api/v1/sources/{id}/batches` (NDJSON/CSV/Parquet, gzip, chunked multipart),
    async validate→learn jobs with idempotency keys and per-batch reject reports; scoped
    tokens issued from the UI; synthetic data retrievable from the same API
    (`POST /fingerprints/{id}/generate` → download).
  - *OTEL/log stream*: OTLP receiver, continuous.
  Auto-suggested dataset binding (entity_key / timestamp / state_fields), user-confirmable.
- **Inspect**: field grid (inferred type, cardinality, null rate, distribution shape),
  execution-path viewer, per-state distributions — "what it learned".
- **Mark PII by selection**: PII scan (Presidio-class recognizers: PERSON, IBAN, PAN, email,
  national IDs) proposes policies; click field(s) → pick method
  (drop / HMAC-SHA-256 / mask / k-anonymity generalize / FF3-1 FPE / consistent pseudonym);
  live before/after preview on real sampled values.
- **Generate**: mode (synthesize | anonymize), **volume as absolute journeys or scale factor
  (0.1× / 1× / 10× of source)**, seed, output to table or CSV/Parquet; **"Preview 100" renders a
  seed-stable sample outcome table before the full run**.
- **Validate**: fidelity report — per-field KS distance, correlation-matrix delta, path-frequency
  comparison, and a mechanical zero-leak check that no masked/dropped original values appear.
- **Re-run**: saved recipes re-run on demand or on schedule; each re-learn versions the
  fingerprint for diffing.

Architecture: React Studio UI → FastAPI engine API → fingerprint engine → connectors/sinks.

## 6. Matching, drift, and the registry

- **Registry**: fingerprints are versioned artifacts (per rail, per service, per release, per
  incident). Small enough to keep thousands in memory.
- **Recognition**: an incoming journey is scored against relevant fingerprints; best match with
  likelihood above threshold = "this is known pattern #4832 (Redis saturation, seen 12k/day)".
- **Drift / deployment diff**: KL-divergence per field + transition-matrix delta between the
  pre-release and post-release fingerprint → "release 2.14 introduced state
  `validation|SCHEMA_ERROR` (0% → 3.1%)".

## 7. Scale-up path (prototype → production)

| Prototype component (this repo, stdlib-only) | Production replacement |
|---|---|
| Char-class pattern induction | Drain3 template mining + FF3-1 FPE |
| Frequency/empirical marginals | SDV (GaussianCopula / CTGAN / TVAE) with DP option |
| Per-state conditionals | Bayesian network or CTGAN conditional vectors |
| Markov sequence model | SDV-PAR or small transformer over state tokens |
| Inter-arrival sampling | PatchTST / TFT temporal models |
| Statistics-vector similarity | Contrastive (Siamese) fingerprint encoder + vector DB |

## 8. Repository layout

```
fingerprints/model.py      # schema induction, field models, sequence model, Fingerprint
fingerprints/anonymize.py  # config-driven anonymization policies
config/payment_fingerprint.json  # dataset binding + per-field anonymization policy
demo.py                    # learn → summarize → synthesize → anonymize → recognize
```

Run: `python demo.py`
