"""Generic fingerprint model.

A Fingerprint is a compact, generative statistical signature of an event
dataset: schema profile + field marginals + per-state conditionals + a Markov
sequence model over behavioral states + a temporal model. One object supports
learn (fit), reproduce (sample), recognize (score_journey), and compare (diff).
The engine is domain-agnostic: payment, service, deployment, incident, and
infrastructure fingerprints are all configurations of this class.
"""

import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

CATEGORICAL = "categorical"
NUMERIC = "numeric"
IDENTIFIER = "identifier"
TIMESTAMP = "timestamp"
TEXT = "text"

START = "<start>"
END = "<end>"
STATE_SEP = "|"


def _is_number(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _parse_ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _is_timestamp(v):
    if _is_number(v):
        return False
    try:
        _parse_ts(v)
        return True
    except (TypeError, ValueError):
        return False


def infer_field_type(values):
    """Infer a field type from (entity-deduplicated) sample values."""
    sample = [v for v in values if v not in (None, "")][:2000]
    if not sample:
        return TEXT
    if all(_is_timestamp(v) for v in sample):
        return TIMESTAMP
    if all(_is_number(v) for v in sample):
        as_str = [str(v) for v in sample]
        digits_only = all("." not in s and "e" not in s.lower() for s in as_str)
        long_ids = statistics.mean(len(s) for s in as_str) >= 10
        near_unique = len(set(as_str)) / len(as_str) > 0.9
        if digits_only and long_ids and near_unique:
            return IDENTIFIER
        return NUMERIC
    uniq = len(set(str(v) for v in sample)) / len(sample)
    if uniq > 0.9:
        return IDENTIFIER
    if uniq < 0.5:
        return CATEGORICAL
    return TEXT


class CategoricalModel:
    kind = CATEGORICAL

    def fit(self, values):
        self.freq = Counter(str(v) for v in values)
        self.total = sum(self.freq.values())
        return self

    def sample(self, rng):
        return rng.choices(list(self.freq), weights=list(self.freq.values()))[0]

    def top(self, n=5):
        return [
            {"value": v, "share": round(c / self.total, 4)}
            for v, c in self.freq.most_common(n)
        ]


class NumericModel:
    kind = NUMERIC

    def fit(self, values):
        self.values = sorted(float(v) for v in values)
        self.is_int = all(v.is_integer() for v in self.values)
        return self

    def sample(self, rng):
        # empirical sampling: uniform between two adjacent observed values
        i = rng.randrange(len(self.values))
        j = min(i + 1, len(self.values) - 1)
        v = rng.uniform(self.values[i], self.values[j])
        return int(round(v)) if self.is_int else round(v, 2)

    def stats(self):
        vs = self.values
        return {
            "min": vs[0],
            "p50": vs[len(vs) // 2],
            "p95": vs[int(len(vs) * 0.95) - 1] if len(vs) > 1 else vs[0],
            "max": vs[-1],
            "mean": round(statistics.mean(vs), 2),
        }


class IdentifierModel:
    """Learns a char-class format pattern so fresh, format-valid values can be
    generated (the basis of format-preserving pseudonyms)."""

    kind = IDENTIFIER
    _CLASSES = {
        "9": "0123456789",
        "A": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "a": "abcdefghijklmnopqrstuvwxyz",
    }

    def fit(self, values):
        patterns = Counter(self._pattern(str(v)) for v in values)
        self.pattern = patterns.most_common(1)[0][0]
        # per-position alphabets from all values of the modal length, so hex
        # stays hex and fixed prefixes stay fixed. Modal length (not modal
        # pattern) keeps the alphabet pool large enough to never collapse to
        # a single real value.
        modal_len = Counter(len(str(v)) for v in values).most_common(1)[0][0]
        matching = [str(v) for v in values if len(str(v)) == modal_len][:5000]
        self.position_chars = [
            "".join(sorted({s[i] for s in matching})) for i in range(modal_len)
        ]
        return self

    @staticmethod
    def _pattern(s):
        out = []
        for ch in s:
            if ch.isdigit():
                out.append("9")
            elif ch.isupper():
                out.append("A")
            elif ch.islower():
                out.append("a")
            else:
                out.append(ch)
        return "".join(out)

    def sample(self, rng):
        if getattr(self, "position_chars", None):
            return "".join(rng.choice(chars) for chars in self.position_chars)
        return self.sample_wide(rng)

    def sample_wide(self, rng):
        """Sample from full char classes — a much larger space than the
        observed per-position alphabets, for collision-free fresh IDs."""
        return "".join(
            rng.choice(self._CLASSES[c]) if c in self._CLASSES else c
            for c in self.pattern
        )


def _build_model(ftype, values):
    if ftype == NUMERIC:
        return NumericModel().fit(values)
    if ftype == IDENTIFIER:
        return IdentifierModel().fit(values)
    return CategoricalModel().fit(values)  # categorical/text/timestamp fallback


class SequenceModel:
    """Markov chain over behavioral states, one chain per fingerprint."""

    def __init__(self):
        self.trans = defaultdict(Counter)

    def fit(self, paths):
        for path in paths:
            prev = START
            for state in path:
                self.trans[prev][state] += 1
                prev = state
            self.trans[prev][END] += 1
        return self

    def sample_path(self, rng, max_len=60):
        path, cur = [], START
        while len(path) < max_len:
            nxt = self._draw(rng, cur)
            if nxt == END:
                break
            path.append(nxt)
            cur = nxt
        return path

    def _draw(self, rng, cur):
        c = self.trans[cur]
        return rng.choices(list(c), weights=list(c.values()))[0]

    def transition_prob(self, a, b):
        c = self.trans.get(a)
        if not c:
            return 0.0
        return c.get(b, 0) / sum(c.values())

    def score_path(self, path):
        """Per-transition probabilities including START/END edges."""
        steps, prev = [], START
        for s in list(path) + [END]:
            steps.append((prev, s, self.transition_prob(prev, s)))
            prev = s
        return steps

    def transition_table(self):
        out = {}
        for a, c in self.trans.items():
            total = sum(c.values())
            out[a] = {b: round(n / total, 4) for b, n in c.most_common()}
        return out


class Fingerprint:
    """One generative fingerprint over canonical records.

    records: list[dict] with entity_key, timestamp_field, state_fields present.
    """

    def __init__(self, entity_key, timestamp_field, state_fields, name="fingerprint"):
        self.name = name
        self.entity_key = entity_key
        self.timestamp_field = timestamp_field
        self.state_fields = list(state_fields)

    # ------------------------------------------------------------------ fit

    def _state(self, r):
        return STATE_SEP.join(str(r.get(f, "")) for f in self.state_fields)

    def fit(self, records):
        if not records:
            raise ValueError("no records to fit")
        # union of fields across all records, first-seen order — records of
        # different shapes (e.g. mixed MT message types) may carry different tags
        seen = {}
        for r in records:
            for k in r:
                if k not in (self.entity_key, self.timestamp_field):
                    seen.setdefault(k)
        self.fields = list(seen)
        self.n_records = len(records)

        journeys = defaultdict(list)
        for r in records:
            journeys[str(r.get(self.entity_key))].append(r)
        for j in journeys.values():
            j.sort(key=lambda r: str(r.get(self.timestamp_field)))
        self.n_journeys = len(journeys)

        # fields constant within a journey are modeled once per journey
        self.journey_constant = set()
        for f in self.fields:
            if f in self.state_fields:
                continue
            const = sum(
                1 for j in journeys.values()
                if len({str(r.get(f)) for r in j}) == 1
            )
            if const / self.n_journeys >= 0.9:
                self.journey_constant.add(f)

        # execution-path fingerprints
        paths = [tuple(self._state(r) for r in j) for j in journeys.values()]
        self.path_counts = Counter(paths)
        self.sequence = SequenceModel().fit(paths)

        # schema profile + global marginals (dedupe per journey for constants,
        # so an account repeated across a journey's events still reads unique)
        self.types, self.models, self.null_pct, self.distinct = {}, {}, {}, {}
        for f in self.fields + [self.entity_key]:
            if f in self.journey_constant or f == self.entity_key:
                vals = [j[0].get(f) for j in journeys.values()]
            else:
                vals = [r.get(f) for r in records]
            nonnull = [v for v in vals if v not in (None, "")]
            self.null_pct[f] = round(1 - len(nonnull) / max(len(vals), 1), 4)
            self.distinct[f] = len(set(str(v) for v in nonnull))
            ftype = CATEGORICAL if f in self.state_fields else infer_field_type(nonnull)
            self.types[f] = ftype
            self.models[f] = _build_model(ftype, nonnull) if nonnull else None

        # per-state conditionals for event-varying fields; every observed
        # state gets an entry so absence of a field in a state is learnable
        self.conditional = defaultdict(dict)
        by_state = defaultdict(list)
        for r in records:
            by_state[self._state(r)].append(r)
        for state, rs in by_state.items():
            _ = self.conditional[state]
            for f in self.fields:
                if f in self.state_fields or f in self.journey_constant:
                    continue
                vals = [r.get(f) for r in rs if r.get(f) not in (None, "")]
                if vals:
                    self.conditional[state][f] = _build_model(self.types[f], vals)

        # journey-constant fields conditioned on the journey's first state:
        # for message data (one record per journey) this is what keeps each
        # message type carrying only its own fields
        self.constant_conditional = defaultdict(dict)
        self.constant_presence = defaultdict(dict)   # state -> field -> ratio
        by_first = defaultdict(list)
        for j in journeys.values():
            by_first[self._state(j[0])].append(j[0])
        for state, rows in by_first.items():
            _ = self.constant_conditional[state]
            for f in self.journey_constant:
                vals = [r.get(f) for r in rows if r.get(f) not in (None, "")]
                if vals:
                    self.constant_conditional[state][f] = _build_model(self.types[f], vals)
                    self.constant_presence[state][f] = len(vals) / len(rows)

        # raw constant rows per first-state: the correlated backend bootstraps
        # these to preserve cross-field dependencies (currency<->country etc.)
        self._const_rows = {
            state: [{f: r.get(f) for f in self.journey_constant} for r in rows]
            for state, rows in by_first.items()
        }

        # temporal model
        self.inter_arrivals = []
        all_ts = []
        for j in journeys.values():
            try:
                ts = [_parse_ts(r[self.timestamp_field]) for r in j]
            except (KeyError, ValueError, TypeError):
                continue
            all_ts += [ts[0], ts[-1]]
            self.inter_arrivals += [
                max((b - a).total_seconds(), 0.0) for a, b in zip(ts, ts[1:])
            ]
        if not self.inter_arrivals:
            self.inter_arrivals = [0.1]
        self.window = (min(all_ts), max(all_ts)) if all_ts else (
            datetime(2026, 1, 1), datetime(2026, 1, 2)
        )

        self._train = records  # kept for previews and fidelity checks
        return self

    # --------------------------------------------------------------- sample

    def sample(self, n_journeys=None, scale=None, seed=None, backend="correlated"):
        """Generate synthetic records. Volume = absolute journeys or a scale
        factor against the training set.

        backend="correlated" (default): journey constants come from a smoothed
        bootstrap of real constant rows — categorical combinations are
        preserved jointly, numerics are jittered, identifiers are ALWAYS
        regenerated fresh. backend="independent": per-field marginal sampling
        (no cross-field correlation). backend="sdv": SDV GaussianCopula if the
        sdv package is installed.
        """
        if n_journeys is None:
            n_journeys = max(int(self.n_journeys * (scale or 1.0)), 1)
        sdv = None
        if backend == "sdv":
            from .backends import SDVConstants
            sdv = SDVConstants(self, seed)
        rng = random.Random(seed)
        t0, t1 = self.window
        span = max((t1 - t0).total_seconds(), 1.0)
        real_entities = {str(r.get(self.entity_key)) for r in getattr(self, "_train", [])}
        emitted = set()
        out = []
        for _ in range(n_journeys):
            path = self.sequence.sample_path(rng)
            if not path:
                continue
            entity = self._fresh_entity(rng, real_entities | emitted)
            emitted.add(entity)
            # constants drawn per first-state so each message/journey type
            # only carries the fields it actually had in training
            constants = self._draw_constants(rng, path[0], backend, real_entities, sdv)
            t = t0 + timedelta(seconds=rng.uniform(0, span))
            for state in path:
                rec = {
                    self.entity_key: entity,
                    self.timestamp_field: t.isoformat(timespec="milliseconds"),
                }
                for f, v in zip(self.state_fields, state.split(STATE_SEP)):
                    rec[f] = v
                cond = self.conditional.get(state)
                for f in self.fields:
                    if f in rec:
                        continue
                    if f in self.journey_constant:
                        rec[f] = constants[f]
                        continue
                    if cond is not None:
                        m = cond.get(f)          # None = absent in this state
                    else:
                        m = self.models.get(f)
                    rec[f] = m.sample(rng) if m else None
                out.append(rec)
                t += timedelta(seconds=rng.choice(self.inter_arrivals))
        return out

    def _draw_constants(self, rng, first_state, backend, real_entities, sdv=None):
        const_rows = getattr(self, "_const_rows", {})
        if backend == "sdv" and sdv is not None:
            row = sdv.draw(first_state)
            if row is not None:
                return self._privatize_constants(rng, dict(row))
        if backend == "correlated" and const_rows.get(first_state):
            # smoothed bootstrap: keep the categorical combination of a real
            # journey jointly, jitter numerics, regenerate identifiers fresh
            row = dict(rng.choice(const_rows[first_state]))
            return self._privatize_constants(rng, row, jitter=True)
        # independent per-field marginals (with per-state presence)
        first_cc = getattr(self, "constant_conditional", {}).get(first_state)
        presence = getattr(self, "constant_presence", {}).get(first_state, {})
        constants, group_draws = {}, {}
        for f in self.journey_constant:
            if first_cc is not None:
                m = first_cc.get(f)
                p = presence.get(f, 1.0)
                if m is not None and p < 1.0:
                    rep = re.search(r"#(\d+)", f)
                    key = rep.group(1) if rep else f
                    if group_draws.setdefault(key, rng.random()) > p:
                        constants[f] = None
                        continue
            else:
                m = self.models.get(f)
            constants[f] = m.sample(rng) if m else None
        return constants

    def _privatize_constants(self, rng, row, jitter=False):
        """A bootstrapped/SDV row must never re-emit sensitive singletons:
        identifiers are regenerated fresh (avoiding real values) and numerics
        are jittered so no exact real row appears in output."""
        for f, v in row.items():
            if v in (None, ""):
                row[f] = None
                continue
            m = self.models.get(f)
            if m is None:
                continue
            if m.kind == IDENTIFIER:
                real = self._real_field_values(f)
                cand = m.sample(rng)
                for _ in range(20):
                    if cand not in real:
                        break
                    cand = m.sample(rng)
                if cand in real and isinstance(m, IdentifierModel):
                    cand = m.sample_wide(rng)
                row[f] = cand
            elif jitter and m.kind == NUMERIC:
                x = float(v) * rng.uniform(0.92, 1.08)
                row[f] = int(round(x)) if m.is_int else round(x, 2)
        return row

    def _real_field_values(self, f):
        cache = getattr(self, "_real_vals_cache", None)
        if cache is None:
            cache = self._real_vals_cache = {}
        if f not in cache:
            cache[f] = {str(r.get(f)) for r in getattr(self, "_train", [])}
        return cache[f]

    def _fresh_entity(self, rng, taken):
        """Fresh journey key that never collides with training or already
        emitted entities — the learned per-position format space may be small,
        so widen to full char classes before falling back to a suffix."""
        model = self.models.get(self.entity_key)
        if model is None:
            return str(rng.randrange(10**12))
        for _ in range(20):
            e = str(model.sample(rng))
            if e not in taken:
                return e
        if isinstance(model, IdentifierModel):
            for _ in range(100):
                e = model.sample_wide(rng)
                if e not in taken:
                    return e
        return f"{model.sample(rng)}-{len(taken)}"

    # ---------------------------------------------------------------- score

    def score_journey(self, journey_records):
        journey = sorted(journey_records, key=lambda r: str(r.get(self.timestamp_field)))
        path = [self._state(r) for r in journey]
        steps = self.sequence.score_path(path)
        min_p = min(p for _, _, p in steps)
        unseen = [(a, b) for a, b, p in steps if p == 0.0]
        if unseen:
            verdict = "novel"
        elif min_p < 0.2:
            verdict = "known-rare"
        else:
            verdict = "normal"
        return {
            "verdict": verdict,
            "min_transition_prob": round(min_p, 4),
            "unseen_transitions": [f"{a} -> {b}" for a, b in unseen],
            "path": path,
            "path_share": round(
                self.path_counts.get(tuple(path), 0) / self.n_journeys, 4
            ),
        }

    # ----------------------------------------------------------------- diff

    def diff(self, other):
        """Compare against another fingerprint of the same shape — the basis of
        deployment fingerprints and drift detection."""
        mine = set(self.sequence.trans) | {
            s for c in self.sequence.trans.values() for s in c
        }
        theirs = set(other.sequence.trans) | {
            s for c in other.sequence.trans.values() for s in c
        }
        specials = {START, END}
        added = sorted((mine - theirs) - specials)
        removed = sorted((theirs - mine) - specials)

        shifts = []
        edges = set()
        for a, c in list(self.sequence.trans.items()) + list(other.sequence.trans.items()):
            for b in c:
                edges.add((a, b))
        for a, b in edges:
            p1, p0 = self.sequence.transition_prob(a, b), other.sequence.transition_prob(a, b)
            if abs(p1 - p0) >= 0.02:
                shifts.append(
                    {"transition": f"{a} -> {b}", "before": round(p0, 4), "after": round(p1, 4)}
                )
        shifts.sort(key=lambda s: -abs(s["after"] - s["before"]))

        field_drift = []
        for f in self.fields:
            m1, m0 = self.models.get(f), other.models.get(f)
            if not m1 or not m0 or m1.kind != m0.kind:
                continue
            if m1.kind == CATEGORICAL:
                keys = set(m1.freq) | set(m0.freq)
                tv = 0.5 * sum(
                    abs(m1.freq.get(k, 0) / m1.total - m0.freq.get(k, 0) / m0.total)
                    for k in keys
                )
                if tv >= 0.01:
                    field_drift.append({"field": f, "tv_distance": round(tv, 4)})
            elif m1.kind == NUMERIC:
                a, b = statistics.mean(m1.values), statistics.mean(m0.values)
                if b and abs(a - b) / max(abs(b), 1e-9) >= 0.05:
                    field_drift.append(
                        {"field": f, "mean_before": round(b, 2), "mean_after": round(a, 2)}
                    )
        field_drift.sort(
            key=lambda d: -(d.get("tv_distance") or abs(d["mean_after"] - d["mean_before"]))
        )
        return {
            "states_added": added,
            "states_removed": removed,
            "transition_shifts": shifts[:15],
            "field_drift": field_drift[:15],
        }

    # -------------------------------------------------------------- summary

    def summary(self, top_paths=6):
        fields = {}
        for f in self.fields:
            info = {
                "type": self.types[f],
                "distinct": self.distinct[f],
                "null_pct": self.null_pct[f],
                "journey_constant": f in self.journey_constant,
                "is_state_field": f in self.state_fields,
            }
            m = self.models.get(f)
            if m is not None:
                if m.kind == CATEGORICAL:
                    info["top_values"] = m.top(5)
                elif m.kind == NUMERIC:
                    info["stats"] = m.stats()
                elif m.kind == IDENTIFIER:
                    info["format_pattern"] = m.pattern
            fields[f] = info
        paths = [
            {
                "share": round(c / self.n_journeys, 4),
                "count": c,
                "path": list(p),
            }
            for p, c in self.path_counts.most_common(top_paths)
        ]
        return {
            "name": self.name,
            "journeys": self.n_journeys,
            "events": self.n_records,
            "entity_key": self.entity_key,
            "timestamp_field": self.timestamp_field,
            "state_fields": self.state_fields,
            "distinct_paths": len(self.path_counts),
            "top_paths": paths,
            "fields": fields,
        }
