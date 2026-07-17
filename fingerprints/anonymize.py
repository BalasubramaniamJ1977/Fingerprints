"""Config-driven, irreversible anonymization applied to real or synthetic records.

Policies (Phase 1 — irreversible only):
  keep        untouched
  drop        suppression: field removed
  hash        HMAC-SHA-256 with salt, truncated — irreversible, join-preserving
  mask        partial masking, keep last N chars (PCI-DSS style)
  generalize  k-anonymity binning: numeric buckets, or prefix truncation
  synthesize  consistent format-preserving pseudonym from the learned
              identifier pattern (same real value -> same pseudonym)
"""

import hashlib
import hmac
import os
import random
from collections import defaultdict

from .model import IdentifierModel

IRREVERSIBLE_POLICIES = {"keep", "drop", "hash", "mask", "generalize", "synthesize"}


class Anonymizer:
    def __init__(self, policies, salt=None, fingerprint=None, seed=1234):
        # production: set FP_SALT in the environment; never rely on the default
        salt = salt or os.environ.get("FP_SALT", "fingerprints-default-salt")
        for field, p in policies.items():
            kind = p.get("policy", "keep")
            if kind == "fpe":
                raise ValueError(
                    "fpe (FF3-1) is not enabled in Phase 1 — irreversible policies only"
                )
            if kind not in IRREVERSIBLE_POLICIES:
                raise ValueError(f"unknown policy '{kind}' for field '{field}'")
        self.policies = policies
        self.salt = salt.encode()
        self.fp = fingerprint
        self._pseudo = defaultdict(dict)   # field -> original -> pseudonym
        self._rng = random.Random(seed)
        self._pattern_models = {}

    def apply(self, records):
        return [self._one(r) for r in records]

    def _one(self, rec):
        out = {}
        for k, v in rec.items():
            p = self.policies.get(k, {"policy": "keep"})
            kind = p.get("policy", "keep")
            if kind == "drop":
                continue
            if kind == "keep" or v in (None, ""):
                out[k] = v
            elif kind == "hash":
                digest = hmac.new(self.salt, str(v).encode(), hashlib.sha256).hexdigest()
                out[k] = digest[: p.get("length", 16)]
            elif kind == "mask":
                s = str(v)
                keep = p.get("keep_last", 4)
                out[k] = "*" * max(len(s) - keep, 0) + s[-keep:] if len(s) > keep else "*" * len(s)
            elif kind == "generalize":
                if "bucket" in p:
                    b = float(p["bucket"])
                    lo = int(float(v) // b * b)
                    out[k] = f"{lo}-{lo + int(b)}"
                else:
                    s = str(v)
                    n = p.get("prefix", 2)
                    out[k] = s[:n] + "*" * max(len(s) - n, 0)
            elif kind == "synthesize":
                out[k] = self._pseudonym(k, str(v))
            else:
                out[k] = v
        return out

    def _pseudonym(self, field, value):
        cache = self._pseudo[field]
        if value not in cache:
            model = None
            if self.fp is not None:
                m = self.fp.models.get(field)
                if m is not None and m.kind == "identifier":
                    model = m
            if model is None:
                model = self._pattern_models.get(field)
                if model is None:
                    model = IdentifierModel()
                    model.pattern = IdentifierModel._pattern(value)
                    self._pattern_models[field] = model
            # never emit an existing pseudonym, nor any real value seen in
            # training data for this field (bounded: format space may be small)
            taken = set(cache.values()) | self._real_values(field)
            candidate = model.sample(self._rng)
            attempts = 0
            while candidate in taken and attempts < 1000:
                candidate = model.sample(self._rng)
                attempts += 1
            if candidate in taken:
                candidate = f"{candidate}-{len(cache)}"
            cache[value] = candidate
        return cache[value]

    def _real_values(self, field):
        if not hasattr(self, "_real_cache"):
            self._real_cache = {}
        if field not in self._real_cache:
            self._real_cache[field] = (
                {str(r.get(field)) for r in self.fp._train}
                if self.fp is not None else set()
            )
        return self._real_cache[field]

    def preview(self, records, n=5):
        """Before/after pairs per anonymized field, for the Studio live preview."""
        out = {}
        for field, p in self.policies.items():
            if p.get("policy", "keep") == "keep":
                continue
            pairs = []
            seen = set()
            for r in records:
                v = r.get(field)
                if v in (None, "") or str(v) in seen:
                    continue
                seen.add(str(v))
                after = self._one({field: v})
                pairs.append({"before": v, "after": after.get(field, "(dropped)")})
                if len(pairs) >= n:
                    break
            out[field] = {"policy": p, "samples": pairs}
        return out

    def leak_check(self, output_records):
        """Mechanical safety check: no masked/dropped/hashed/synthesized original
        value may appear verbatim in the output. Returns count of leaks."""
        sensitive_fields = [
            f for f, p in self.policies.items()
            if p.get("policy") in ("drop", "hash", "mask", "synthesize")
        ]
        if not sensitive_fields or self.fp is None:
            return {"checked_fields": sensitive_fields, "leaks": 0}
        originals = set()
        for r in self.fp._train:
            for f in sensitive_fields:
                v = r.get(f)
                if v not in (None, ""):
                    originals.add(str(v))
        leaks = 0
        for r in output_records:
            for v in r.values():
                if str(v) in originals:
                    leaks += 1
        return {"checked_fields": sensitive_fields, "leaks": leaks}
