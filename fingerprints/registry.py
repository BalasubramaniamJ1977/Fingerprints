"""Versioned fingerprint registry: every re-learn adds a version so releases
and drift can be diffed. Pickle for the object, JSON sidecar for the summary."""

import json
import pickle
from pathlib import Path


class Registry:
    def __init__(self, root="data/registry"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def versions(self, name):
        d = self._dir(name)
        return sorted(int(p.stem[1:]) for p in d.glob("v*.pkl"))

    def save(self, name, fingerprint):
        d = self._dir(name)
        v = (self.versions(name)[-1] + 1) if self.versions(name) else 1
        with open(d / f"v{v}.pkl", "wb") as f:
            pickle.dump(fingerprint, f)
        with open(d / f"v{v}.json", "w", encoding="utf-8") as f:
            json.dump(fingerprint.summary(), f, indent=2, default=str)
        return v

    def load(self, name, version=None):
        vs = self.versions(name)
        if not vs:
            raise KeyError(f"no fingerprint named '{name}'")
        v = version or vs[-1]
        with open(self._dir(name) / f"v{v}.pkl", "rb") as f:
            return pickle.load(f)

    def list(self):
        out = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            vs = self.versions(d.name)
            if not vs:
                continue
            with open(d / f"v{vs[-1]}.json", encoding="utf-8") as f:
                s = json.load(f)
            out.append({"name": d.name, "versions": vs, "latest": vs[-1],
                        "journeys": s.get("journeys"), "events": s.get("events")})
        return out
