# Fingerprints — generic fingerprint modeling platform (Phase 1)

One generative fingerprint model that learns any event dataset (payments,
service logs, deployments, incidents, infrastructure), reproduces statistically
faithful synthetic data, and anonymizes with per-field policies — controlled
from a web workbench, **Fingerprint Studio**.

Design: see `DESIGN.md` (full illustrated version published as an artifact).

## Production notes

- **Synthesis backends** (Generate tab / `backend` in the API): `correlated`
  (default — smoothed bootstrap preserving cross-field dependencies; numerics
  jittered, identifiers always regenerated fresh), `independent` (per-field
  marginals), `sdv` (SDV GaussianCopula, `pip install ".[sdv]"`). Measured on
  the demo data: currency↔country joint TV distance 0.54 (independent) →
  0.06 (correlated).
- **Fidelity report** now includes `pair_distances` (joint distribution error
  for categorical field pairs) and gates the PASS verdict on it.
- **Secrets**: set `FP_SALT` (anonymization salt) in the environment; set
  `FP_API_TOKEN` to require `Authorization: Bearer <token>` on all /api routes.
- **Tests**: `python -m pytest tests` — engine, privacy (PII scan, policies,
  leak checks), and 17-format MT/MX round-trips.
- **Deploy**: `docker build -t fingerprints-studio . && docker run -p 8600:8600
  -e FP_SALT=... -e FP_API_TOKEN=... fingerprints-studio`
- **Guarantees enforced by tests**: synthetic entity IDs never equal training
  IDs; a bootstrapped row never re-emits a real identifier; masked/dropped
  values never appear in output (mechanical leak check).

## Quick start

```powershell
# no install needed if fastapi/uvicorn/sqlalchemy are present
python demo.py                                   # CLI: all five fingerprint types
python -m uvicorn api.main:app --port 8600       # Studio + API
# then open http://127.0.0.1:8600
```

## Studio workflow

1. **Connect** — pick a source: simulator (5 demo datasets), file upload
   (CSV/NDJSON), PostgreSQL table (read-only DSN), or Bulk API batches.
   "Load & suggest binding" proposes entity key / timestamp / state fields.
2. **Learn** — fits the fingerprint, versions it in the registry, and runs the
   PII scan (UUID, IBAN, PAN+Luhn, email, person-name, phone recognizers).
3. **Inspect** — journeys/events/paths stats, execution-path fingerprints with
   shares, per-field learned shapes.
4. **Mark PII / Policies** — per-field policy (keep / drop / hash / mask /
   generalize / synthesize; all irreversible by design in Phase 1), PII
   suggestions pre-applied, live before→after preview on real sampled values.
5. **Generate** — mode synthesize (new journeys) or anonymize (real rows
   through policies); volume as journeys or scale factor; seed; "Preview
   sample" before the full run; fidelity report (KS / TV distance / path
   delta) + mechanical zero-leak check; CSV download.
6. **Registry & Recipes** — fingerprint versions, deployment-style version
   diff, saved recipes with one-click re-run.

## E2E Test (tab 7)

Runs a simulated payment across a chain of systems as ISO 20022 messages
(default: IBNK Channel `pain.001` &rarr; Payment Processing &rarr; CoreBanking
&rarr; Clearing & Settlement, all `pacs.008`, with a `pacs.002` response
relayed back), so a tester can click any system and see exactly how it
transformed the message (mapped / generated / enriched / passed through
field by field). Pick the flow from a dropdown — **Outward**, **Inward**,
**Outward Return**, **Inward Return**. Inward and Outward Return share a
second chain: `Regulator/FI` &rarr; Payment Processing &rarr; CoreBanking
&rarr; `Realtime Notification` (to channels), both carrying `pacs.008`;
Inward Return mirrors Outward's chain with `pacs.004`. Pick a source — **complete
synthetic**, **file upload by system**, or **connect to a system database**
(read-only). The chain isn't fixed at four systems: **+ Add intermediary
system** inserts any number of extra hops (fraud check, sanctions
screening, …) anywhere in the flow. See `fingerprints/e2e.py`, tests in
`tests/test_e2e.py`, and `docs/documentation.html` &rarr; "Enhancement: E2E
Test tab" for the full design, a UI test walkthrough, and the roadmap.

## MT/MX messages (tab 6)

The adapters are **format-generic**:

- **MX**: any ISO 20022 definition (pacs.*, pain.*, camt.*, …) — the parser
  flattens whatever XML it sees, the message type and namespace come from the
  document itself and are carried per record, so files may mix definitions and
  rendering restores each one's namespace.
- **MT**: any tag-block type — the type comes from the block-2 header,
  repeated tags get #n suffixes, party tags (50x/59x) and 32A are decomposed
  for modeling and reassembled on render; other tags learn verbatim, rendered
  in SWIFT numeric tag order.
- **Mixed files learn as one fingerprint**: the engine conditions field
  presence on message type, so a synthetic MT202 never grows MT103-only tags.

Built-in standards (no upload needed) — the full MT↔MX equivalence set:

| Business function | MT | MX |
|---|---|---|
| Customer credit transfer | MT103 | pacs.008 |
| FI transfer | MT202 | pacs.009 |
| Cover payment | MT202 COV (`{119:COV}`) | pacs.009 COV (UndrlygCstmrCdtTrf) |
| Statement | MT940 (61/86 lines) | camt.053 (Bal + Ntry blocks) |
| Interim report | MT942 | camt.052 |
| Debit/credit confirmation | MT900 / MT910 | camt.054 (DBIT/CRDT) |
| Payment initiation | MT101 | pain.001 |
| Free format | MT199 | camt.998 |

Repeated elements are first-class: camt `Bal`/`Ntry` blocks flatten to `#n`
paths and render back as repeated XML elements; MT statement lines interleave
61/86 pairs by sequence with 62F after them, and pairs appear or vanish
together in synthesis (learned presence ratios). Balance composites
(60F/62F/64/65) decompose like 32A so currencies stay real codes.
Any other format: upload a sample file. PII policies apply in the message body
(accounts masked, names pseudonymized so format survives). Round-trip
verified: generated files parse back and re-learn.

Known Phase-1 limits: MX element order inside deep blocks is not
XSD-validated; single transaction per MX document; MT940-style interleaved
statement lines (61/86 pairs) render approximately.

## Relational tables (parent + child)

On the Connect tab, "List tables" shows every table with row counts and
foreign-key children. Ticking "learn parent + child tables" fits the parent
(entity = primary key) and each child (entity = FK, so a journey is the set of
child rows per parent). Generation emits FK-consistent datasets: fresh parent
keys, child rows pointing at them, child-count distribution preserved —
written back as `<table>_synth` tables or CSVs.

## Bulk API

```bash
curl -X POST http://127.0.0.1:8600/api/v1/sources/my-team/batches \
  -H "Idempotency-Key: batch-001" --data-binary @events.ndjson
# -> 202 {"job":"batch-...","rows":N,"rejected":M}
```

Batches accumulate under the source id; select "Bulk API batches" in the
Studio to learn from them. Idempotency keys make retries safe.

## Registry CLI — see what changed, from the command line

The versioned registry (`fingerprints/registry.py`) already backs the
Studio's Registry tab; `fingerprints/registry_cli.py` exposes the same
list/diff capability as a standalone command line tool, so drift between
fingerprint versions can be inspected or gated on without the web UI:

```powershell
python -m fingerprints.registry_cli list
python -m fingerprints.registry_cli versions payments
python -m fingerprints.registry_cli show payments --version 2
python -m fingerprints.registry_cli changes payments                 # last two versions
python -m fingerprints.registry_cli changes payments --from 1 --to 3
python -m fingerprints.registry_cli changes --all --format json
python -m fingerprints.registry_cli changes payments --fail-on-drift # exit 1 if changed
```

`pip install -e .` also installs it as `fp-registry` (same subcommands).
Tests: `python -m pytest tests/test_registry_cli.py`. A ready-to-use Azure
DevOps pipeline that runs the suite and then gates a build on registry drift
is in `azure-pipelines.yml`. Full write-up (design, features, testing, ADO
integration): `docs/documentation.html`.

## Layout

```
fingerprints/model.py       schema induction, field models, Markov sequences,
                            Fingerprint fit/sample/score/diff/summary
fingerprints/anonymize.py   policy engine + preview + leak check
fingerprints/pii.py         rule-based PII recognizers -> policy suggestions
fingerprints/fidelity.py    KS / TV-distance / path-delta fidelity report
fingerprints/simulate.py    simulators for the five fingerprint domains
fingerprints/connectors.py  CSV/NDJSON files, SQLAlchemy tables
fingerprints/registry.py    versioned fingerprint store
api/main.py                 FastAPI engine API + static Studio hosting
studio/                     Fingerprint Studio SPA (no build step)
recipes/                    saved, re-runnable configurations
data/                       registry, uploads, batches, outputs (generated)
demo.py                     CLI demo of all five fingerprint types
```

## The five fingerprint types (one model, five configurations)

| Type | entity_key | state_fields | Demo |
|---|---|---|---|
| Payment | uetr | service+status | `simulate_payments` — 85% settle / 10% AML timeout / 5% reject |
| Service | trace_id | template_id+level | `simulate_service_logs` |
| Deployment | service+version | same as service | `simulate_deployment` → `fp.diff()` |
| Incident | window_id | component+condition | redis-saturation vs kafka-rebalance recognition |
| Infrastructure | broker-hour | broker+partition_state | Kafka imbalance shapes |

Phase-1 components are stdlib-only in the engine; production replacements
(Drain3, SDV/CTGAN, PAR, PatchTST, Presidio, SDMetrics, contrastive encoder)
are mapped in `DESIGN.md` §7.
