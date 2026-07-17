import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints import simulate
from fingerprints.fidelity import fidelity_report
from fingerprints.model import Fingerprint


def _payments_fp(n=400):
    records, binding = simulate.simulate_payments(n_journeys=n, seed=42)
    return Fingerprint(name="t", **binding).fit(records), records


def test_fit_learns_paths_and_schema():
    fp, _ = _payments_fp()
    s = fp.summary()
    assert s["journeys"] == 400
    assert s["distinct_paths"] == 3
    assert s["fields"]["debtor_account"]["type"] == "identifier"
    assert s["fields"]["amount"]["journey_constant"] is True


def test_sample_reproduces_shape_and_paths():
    fp, _ = _payments_fp()
    synth = fp.sample(n_journeys=200, seed=7)
    rep = fidelity_report(fp, synth)
    assert rep["verdict"] == "PASS", rep
    assert rep["path_distribution_delta"] < 0.1


def test_synthetic_entities_never_collide_with_training():
    fp, records = _payments_fp()
    real = {r["uetr"] for r in records}
    synth = fp.sample(n_journeys=300, seed=1)
    assert not ({r["uetr"] for r in synth} & real)


def test_scale_volume():
    fp, _ = _payments_fp(200)
    synth = fp.sample(scale=0.5, seed=3)
    assert len({r["uetr"] for r in synth}) == 100


def test_score_verdicts():
    fp, records = _payments_fp()
    normal = [r for r in records if r["uetr"] == records[0]["uetr"]]
    assert fp.score_journey(normal)["verdict"] in ("normal", "known-rare")
    novel = [dict(normal[0], service="gateway", status="OK"),
             dict(normal[0], service="settlement", status="FAILED")]
    res = fp.score_journey(novel)
    assert res["verdict"] == "novel" and res["unseen_transitions"]


def test_diff_detects_new_state():
    rel, binding = simulate.simulate_deployment()
    a = Fingerprint(name="a", **binding).fit(rel["v2.13"])
    b = Fingerprint(name="b", **binding).fit(rel["v2.14"])
    d = b.diff(a)
    assert "T06|ERROR" in d["states_added"]


def test_correlated_backend_beats_independent_on_pairs():
    fp, _ = _payments_fp(800)
    ind = fidelity_report(fp, fp.sample(n_journeys=400, seed=5,
                                        backend="independent"))
    cor = fidelity_report(fp, fp.sample(n_journeys=400, seed=5,
                                        backend="correlated"))
    key = "country+currency"
    assert cor["pair_distances"][key] < ind["pair_distances"][key]
    assert cor["pair_distances"][key] < 0.2


def test_mixed_types_keep_their_own_fields():
    r1, b1 = simulate.simulate_mt(n_messages=100, seed=1)
    r2, _ = simulate.simulate_mt202(n_messages=100, seed=2)
    fp = Fingerprint(name="mix", entity_key="_msg_id", timestamp_field="_ts",
                     state_fields=["_mt_type"]).fit(r1 + r2)
    synth = fp.sample(n_journeys=100, seed=9)
    for r in synth:
        if r["_mt_type"] == "202":
            assert r.get("23B") is None and r.get("58A")
        else:
            assert r.get("23B") and r.get("58A") is None
