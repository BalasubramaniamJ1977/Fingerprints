"""Fidelity report: proves synthetic output is statistically faithful to the
learned source. Production replacement: SDMetrics quality reports."""

from collections import Counter

from .model import CATEGORICAL, NUMERIC, STATE_SEP


def _ks(real, synth):
    """Two-sample Kolmogorov–Smirnov statistic on empirical CDFs."""
    a, b = sorted(real), sorted(synth)
    i = j = 0
    d = 0.0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            i += 1
        else:
            j += 1
        d = max(d, abs(i / len(a) - j / len(b)))
    return round(d, 4)


def _tv(real_vals, synth_vals, top_k=20):
    """Total-variation distance over the top-k categories (rest bucketed as
    'other') so high-cardinality fields aren't dominated by sampling noise."""
    ra, sa = Counter(map(str, real_vals)), Counter(map(str, synth_vals))
    keep = {k for k, _ in ra.most_common(top_k)}
    def bucket(c):
        out = Counter()
        for k, n in c.items():
            out[k if k in keep else "<other>"] += n
        return out
    ra, sa = bucket(ra), bucket(sa)
    rt, st = sum(ra.values()), sum(sa.values())
    keys = set(ra) | set(sa)
    return round(0.5 * sum(abs(ra[k] / rt - sa[k] / st) for k in keys), 4)


def fidelity_report(fp, synthetic):
    """Compare synthetic records against the fingerprint's training data."""
    real = fp._train
    fields = {}
    for f in fp.fields:
        m = fp.models.get(f)
        if m is None or f in fp.state_fields:
            continue
        rv = [r.get(f) for r in real if r.get(f) not in (None, "")]
        sv = [r.get(f) for r in synthetic if r.get(f) not in (None, "")]
        if not rv or not sv:
            continue
        if m.kind == NUMERIC:
            fields[f] = {"metric": "ks", "value": _ks([float(v) for v in rv],
                                                      [float(v) for v in sv])}
        elif m.kind == CATEGORICAL:
            fields[f] = {"metric": "tv_distance", "value": _tv(rv, sv)}

    def path_shares(records):
        journeys = {}
        for r in records:
            journeys.setdefault(str(r.get(fp.entity_key)), []).append(r)
        c = Counter()
        for j in journeys.values():
            j.sort(key=lambda r: str(r.get(fp.timestamp_field)))
            c[tuple(STATE_SEP.join(str(r.get(s, "")) for s in fp.state_fields)
                    for r in j)] += 1
        total = sum(c.values())
        return {p: n / total for p, n in c.items()}, total

    # cross-field correlation: joint TV distance over pairs of categorical
    # journey-constant fields (independent sampling scores poorly here when
    # the source fields are correlated, e.g. currency <-> country)
    pair_distances = {}
    cats = [f for f in getattr(fp, "journey_constant", [])
            if fp.models.get(f) is not None and fp.models[f].kind == CATEGORICAL]
    cats = sorted(cats)[:4]
    for i in range(len(cats)):
        for j in range(i + 1, len(cats)):
            a, b = cats[i], cats[j]
            rv = [f"{r.get(a)}|{r.get(b)}" for r in real]
            sv = [f"{r.get(a)}|{r.get(b)}" for r in synthetic]
            pair_distances[f"{a}+{b}"] = _tv(rv, sv, top_k=30)

    rp, _ = path_shares(real)
    sp, n_synth_journeys = path_shares(synthetic)
    keys = set(rp) | set(sp)
    path_delta = round(0.5 * sum(abs(rp.get(k, 0) - sp.get(k, 0)) for k in keys), 4)
    top = [
        {"path": list(p), "real_share": round(rp.get(p, 0), 4),
         "synthetic_share": round(sp.get(p, 0), 4)}
        for p, _ in Counter({k: rp.get(k, 0) for k in keys}).most_common(5)
    ]
    worst = max((v["value"] for v in fields.values()), default=0.0)
    worst_pair = max(pair_distances.values(), default=0.0)
    return {
        "journeys_generated": n_synth_journeys,
        "events_generated": len(synthetic),
        "field_distances": fields,
        "worst_field_distance": worst,
        "pair_distances": pair_distances,
        "worst_pair_distance": worst_pair,
        "path_distribution_delta": path_delta,
        "top_paths": top,
        "verdict": ("PASS" if worst <= 0.15 and path_delta <= 0.1
                    and worst_pair <= 0.2 else "REVIEW"),
    }
