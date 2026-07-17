"""Optional heavy synthesis backends.

Phase-1 default is the built-in "correlated" smoothed-bootstrap backend
(zero dependencies). When the `sdv` package is installed, backend="sdv"
fits an SDV GaussianCopulaSynthesizer over the journey-constant fields per
first-state and samples from it instead.

Install:  pip install "fingerprints-studio[sdv]"   (or: pip install sdv)
"""


class SDVConstants:
    """Lazy per-state SDV synthesizer pool over journey-constant fields."""

    def __init__(self, fp, seed=None, batch=500):
        try:
            import pandas  # noqa: F401
            from sdv.metadata import SingleTableMetadata  # noqa: F401
            from sdv.single_table import GaussianCopulaSynthesizer  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "backend='sdv' requires the sdv package (pip install sdv); "
                "use backend='correlated' for the built-in equivalent"
            ) from e
        self.fp = fp
        self.batch = batch
        self._pools = {}
        self._synths = {}

    def _synth_for(self, state):
        import pandas as pd
        from sdv.metadata import SingleTableMetadata
        from sdv.single_table import GaussianCopulaSynthesizer

        if state not in self._synths:
            rows = getattr(self.fp, "_const_rows", {}).get(state)
            if not rows or len(rows) < 10:
                self._synths[state] = None
            else:
                df = pd.DataFrame(rows)
                meta = SingleTableMetadata()
                meta.detect_from_dataframe(df)
                synth = GaussianCopulaSynthesizer(meta)
                synth.fit(df)
                self._synths[state] = synth
        return self._synths[state]

    def draw(self, state):
        synth = self._synth_for(state)
        if synth is None:
            return None                      # caller falls back
        pool = self._pools.get(state)
        if not pool:
            pool = self._pools[state] = synth.sample(self.batch).to_dict("records")
        return pool.pop()
