"""End-to-end demo of all five fingerprint types on simulated data.

Run: python demo.py
"""

import json

from fingerprints.anonymize import Anonymizer
from fingerprints.fidelity import fidelity_report
from fingerprints.model import Fingerprint
from fingerprints.pii import suggest_policies
from fingerprints.registry import Registry
from fingerprints import simulate


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show_paths(summary):
    for p in summary["top_paths"]:
        print(f"  {p['share']:6.1%}  {' -> '.join(p['path'])}")


registry = Registry()

# ----------------------------------------------------------- 1. payment
hr("1. PAYMENT FINGERPRINT  (MX/MT/ISO8583 execution paths)")
records, binding = simulate.simulate_payments(n_journeys=2000, seed=42)
fp = Fingerprint(name="payments", **binding).fit(records)
v = registry.save("payments", fp)
s = fp.summary()
print(f"learned {s['journeys']} journeys / {s['events']} events "
      f"/ {s['distinct_paths']} distinct paths (registry v{v})")
print("\nExecution-path fingerprints:")
show_paths(s)

print("\nField schema (inferred):")
for f, info in s["fields"].items():
    extra = info.get("format_pattern") or info.get("stats") or ""
    print(f"  {f:16} {info['type']:12} distinct={info['distinct']:<7} "
          f"{'journey-constant' if info['journey_constant'] else '':17} {extra}")

print("\nPII scan suggestions:")
pii = suggest_policies(records)
for f, hit in pii.items():
    print(f"  {f:16} {hit['pii_type']:12} conf={hit['confidence']:<5} "
          f"suggested={hit['suggested']}")

policies = {f: hit["suggested"] for f, hit in pii.items()}
policies["debtor_name"] = {"policy": "drop"}
policies["amount"] = {"policy": "generalize", "bucket": 500}
anon = Anonymizer(policies, fingerprint=fp)

print("\nAnonymization preview (before -> after):")
for f, prev in anon.preview(records, n=2).items():
    for pair in prev["samples"]:
        print(f"  {f:16} {str(pair['before'])[:36]:38} -> {pair['after']}")

synthetic = fp.sample(n_journeys=500, seed=7)
synthetic_anon = anon.apply(synthetic)
rep = fidelity_report(fp, synthetic)
leaks = anon.leak_check(synthetic_anon)
print(f"\nSynthetic: {rep['journeys_generated']} journeys / "
      f"{rep['events_generated']} events")
print(f"Fidelity: worst field distance={rep['worst_field_distance']}, "
      f"path delta={rep['path_distribution_delta']}  -> {rep['verdict']}")
print(f"Leak check on anonymized output: {leaks['leaks']} leaks "
      f"across fields {leaks['checked_fields']}")
print("\nSample synthetic journey (anonymized):")
first_entity = synthetic_anon[0]["uetr"]
for r in [r for r in synthetic_anon if r["uetr"] == first_entity][:7]:
    print("  " + json.dumps(r, default=str))

print("\nScoring three journeys against the fingerprint:")
normal = [r for r in records if r["uetr"] == records[0]["uetr"]]
print(f"  seen-normal    -> {fp.score_journey(normal)['verdict']}")
timeout_uetr = next(r["uetr"] for r in records if r["status"] == "TIMEOUT")
rare = [r for r in records if r["uetr"] == timeout_uetr]
print(f"  known-rare     -> {fp.score_journey(rare)['verdict']} "
      f"(aml timeout, share={fp.score_journey(rare)['path_share']:.1%})")
novel = [dict(normal[0], service="gateway", status="OK"),
         dict(normal[0], service="settlement", status="FAILED")]
res = fp.score_journey(novel)
print(f"  never-seen     -> {res['verdict']}  unseen={res['unseen_transitions']}")

# ----------------------------------------------------------- 2. service
hr("2. SERVICE FINGERPRINT  (log-template behavior of one microservice)")
svc_records, svc_binding = simulate.simulate_service_logs(seed=7)
svc = Fingerprint(name="payment-validation", **svc_binding).fit(svc_records)
registry.save("payment-validation", svc)
ss = svc.summary()
print(f"learned {ss['journeys']} traces / {ss['events']} events")
print("Template mix (state marginals):")
tmpl = svc.models["template_id"]
for t in tmpl.top(6):
    print(f"  {t['value']:6} {t['share']:6.1%}")

# -------------------------------------------------------- 3. deployment
hr("3. DEPLOYMENT FINGERPRINT  (diff of service fingerprints across releases)")
releases, dep_binding = simulate.simulate_deployment()
fp_v13 = Fingerprint(name="pv-v2.13", **dep_binding).fit(releases["v2.13"])
fp_v14 = Fingerprint(name="pv-v2.14", **dep_binding).fit(releases["v2.14"])
d = fp_v14.diff(fp_v13)
print("release v2.14 vs v2.13:")
print(f"  states added:   {d['states_added']}")
print(f"  states removed: {d['states_removed']}")
print("  top transition shifts:")
for shift in d["transition_shifts"][:5]:
    print(f"    {shift['transition']}: {shift['before']} -> {shift['after']}")

# ----------------------------------------------------------- 4. incident
hr("4. INCIDENT FINGERPRINT  (recognize a live window against known patterns)")
known = {}
for kind in ("incident-redis", "incident-kafka"):
    inc_records, inc_binding = simulate.SIMULATORS[kind](seed=23)
    known[kind] = Fingerprint(name=kind, **inc_binding).fit(inc_records)
    registry.save(kind, known[kind])
live_records, _ = simulate.simulate_incident("redis-saturation", n_windows=1, seed=99)
print("live window states:", [f"{r['component']}|{r['condition']}" for r in live_records])
for kind, kfp in known.items():
    res = kfp.score_journey(live_records)
    print(f"  vs {kind:16} -> {res['verdict']:11} "
          f"min_p={res['min_transition_prob']}")

# ------------------------------------------------------ 5. infrastructure
hr("5. INFRASTRUCTURE FINGERPRINT  (Kafka partition-imbalance shapes)")
infra_records, infra_binding = simulate.simulate_infra(seed=31)
infra = Fingerprint(name="kafka-brokers", **infra_binding).fit(infra_records)
registry.save("kafka-brokers", infra)
si = infra.summary()
print(f"learned {si['journeys']} broker-hours / {si['events']} observations")
print("Characteristic state sequences:")
for p in si["top_paths"][:4]:
    stages = [st.split("|")[-1] for st in p["path"]]
    print(f"  {p['share']:6.1%}  {' -> '.join(stages)}")

print("\nRegistry contents:")
for e in registry.list():
    print(f"  {e['name']:20} versions={e['versions']} "
          f"journeys={e['journeys']} events={e['events']}")

hr("DONE -- all five fingerprint types learned, sampled, scored, and diffed")
