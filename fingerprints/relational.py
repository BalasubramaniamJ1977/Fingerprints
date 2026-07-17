"""Relational fingerprints: learn a parent table together with its child
tables (foreign-key references), then generate coherent synthetic datasets
where child rows point at freshly generated parent keys.

Model: the parent is a fingerprint whose entity is the primary key (one-row
journeys). Each child table is a fingerprint whose entity is the FK column —
so a "journey" is the set of child rows belonging to one parent, and the
sequence model learns how many and in what state mix children occur.
"""

from .binding import suggest_binding
from .model import Fingerprint, IdentifierModel


def _inject_defaults(records, binding, tag):
    """Ensure a usable timestamp and state field exist for plain tables."""
    binding = dict(binding)
    if not binding.get("timestamp_field"):
        for i, r in enumerate(records):
            r["_seq"] = f"{i:09d}"
        binding["timestamp_field"] = "_seq"
    if not binding.get("state_fields"):
        for r in records:
            r["_row"] = tag
        binding["state_fields"] = ["_row"]
    binding.pop("types", None)
    return records, binding


class RelationalFingerprint:
    """parent_table + relations [{child, fk, parent_pk}] learned as one unit."""

    def __init__(self, name, parent_table, pk_column):
        self.name = name
        self.parent_table = parent_table
        self.pk = pk_column
        self.children = {}          # table -> {"fp": Fingerprint, "fk": col}

    def fit(self, tables, relations):
        parent_records = tables[self.parent_table]
        binding = suggest_binding(parent_records)
        binding["entity_key"] = self.pk
        parent_records, binding = _inject_defaults(
            parent_records, binding, self.parent_table)
        self.parent = Fingerprint(name=self.parent_table, **binding).fit(parent_records)

        for rel in relations:
            child_records = tables[rel["child"]]
            cb = suggest_binding(child_records)
            cb["entity_key"] = rel["fk"]        # journeys = children per parent
            child_records, cb = _inject_defaults(child_records, cb, rel["child"])
            fp = Fingerprint(name=rel["child"], **cb).fit(child_records)
            self.children[rel["child"]] = {"fp": fp, "fk": rel["fk"]}
        return self

    # ---------------------------------------------------------------- sample

    def _fresh_keys(self, fp, n, rng_seed):
        import random
        rng = random.Random(rng_seed)
        model = fp.models.get(fp.entity_key)
        real = {str(r.get(fp.entity_key)) for r in fp._train}
        keys, taken = [], set()
        if isinstance(model, IdentifierModel):
            # attempt budget: the learned format space may be nearly (or fully)
            # exhausted by the real keys — never spin forever on it
            attempts = 0
            while len(keys) < n and attempts < 20 * n:
                attempts += 1
                k = model.sample(rng)
                if k not in taken and k not in real:
                    keys.append(k)
                    taken.add(k)
        i = 0
        while len(keys) < n:
            k = f"SYN{i:09d}"
            if k not in real:
                keys.append(k)
            i += 1
        return keys

    def sample(self, n_parents=None, scale=None, seed=None):
        if n_parents is None:
            n_parents = max(int(self.parent.n_journeys * (scale or 1.0)), 1)
        parents = self.parent.sample(n_journeys=n_parents, seed=seed)
        pks = self._fresh_keys(self.parent, len(parents), seed)
        for rec, pk in zip(parents, pks):
            rec[self.pk] = pk

        out = {self.parent_table: parents}
        for table, info in self.children.items():
            fp, fk = info["fp"], info["fk"]
            rows = []
            child_pk_counter = 1
            batch = fp.sample(n_journeys=len(pks), seed=seed)
            # regroup the sampled child journeys and reassign FKs to parents
            by_entity = {}
            for r in batch:
                by_entity.setdefault(r[fk], []).append(r)
            for pk, journey in zip(pks, by_entity.values()):
                for r in journey:
                    r[fk] = pk
                    rows.append(r)
            for r in rows:
                r.pop("_seq", None)
                r.pop("_row", None)
                child_pk_counter += 1
            out[table] = rows
        for r in parents:
            r.pop("_seq", None)
            r.pop("_row", None)
        return out

    # --------------------------------------------------------------- compat

    @property
    def _train(self):
        merged = list(self.parent._train)
        for info in self.children.values():
            merged += info["fp"]._train
        return merged

    @property
    def models(self):
        merged = {}
        for info in self.children.values():
            merged.update(info["fp"].models)
        merged.update(self.parent.models)
        return merged

    def summary(self, top_paths=6):
        ps = self.parent.summary(top_paths)
        fields = dict(ps["fields"])
        top = list(ps["top_paths"])
        children = {}
        total_events = ps["events"]
        for table, info in self.children.items():
            cs = info["fp"].summary(3)
            children[table] = {"fk": info["fk"], "rows": cs["events"],
                               "avg_per_parent": round(
                                   cs["events"] / max(ps["journeys"], 1), 2)}
            for f, v in cs["fields"].items():
                fields.setdefault(f, {**v, "from_table": table})
            top += cs["top_paths"][:2]
            total_events += cs["events"]
        return {
            "name": self.name, "relational": True,
            "parent_table": self.parent_table,
            "children": children,
            "journeys": ps["journeys"], "events": total_events,
            "entity_key": self.pk,
            "timestamp_field": ps["timestamp_field"],
            "state_fields": ps["state_fields"],
            "distinct_paths": ps["distinct_paths"],
            "top_paths": top[:8],
            "fields": fields,
        }
