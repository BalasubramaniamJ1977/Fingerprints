"""Heuristic dataset-binding suggestion: which field groups rows into a
journey, which orders them, and which define behavioral state."""

from .model import CATEGORICAL, IDENTIFIER, infer_field_type


def suggest_binding(records):
    if not records:
        return {}
    fields = list(records[0].keys())
    sample = records[:5000]
    n = len(sample)
    types = {f: infer_field_type([r.get(f) for r in sample]) for f in fields}

    ts_field = next((f for f in fields if types[f] == "timestamp"), None)
    entity, best = None, 0
    for f in fields:
        if types[f] != IDENTIFIER:
            continue
        distinct = len(set(str(r.get(f)) for r in sample))
        if n / max(distinct, 1) >= 1.5 and distinct > best:
            entity, best = f, distinct
    if entity is None:
        entity = next((f for f in fields if types[f] == IDENTIFIER), fields[0])

    states = [f for f in fields
              if types[f] == CATEGORICAL
              and len(set(str(r.get(f)) for r in sample)) <= 30
              and f not in (entity, ts_field)]
    varying = []
    by_entity = {}
    for r in sample:
        by_entity.setdefault(str(r.get(entity)), []).append(r)
    for f in states:
        vary = sum(1 for j in by_entity.values()
                   if len({str(r.get(f)) for r in j}) > 1)
        if vary / max(len(by_entity), 1) > 0.05:
            varying.append(f)
    return {"entity_key": entity, "timestamp_field": ts_field,
            "state_fields": (varying or states)[:2],
            "types": types}
