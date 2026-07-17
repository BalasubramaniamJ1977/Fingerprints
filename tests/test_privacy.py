import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints import simulate
from fingerprints.anonymize import Anonymizer
from fingerprints.model import Fingerprint
from fingerprints.pii import suggest_policies


@pytest.fixture(scope="module")
def fp_and_records():
    records, binding = simulate.simulate_payments(n_journeys=300, seed=42)
    return Fingerprint(name="t", **binding).fit(records), records


def test_pii_scan_finds_expected_fields(fp_and_records):
    _, records = fp_and_records
    pii = suggest_policies(records)
    assert pii["uetr"]["pii_type"] == "UUID"
    assert pii["debtor_account"]["pii_type"] == "IBAN"
    assert pii["debtor_name"]["pii_type"] == "PERSON"
    assert "status" not in pii and "currency" not in pii


def test_policies_apply(fp_and_records):
    fp, records = fp_and_records
    anon = Anonymizer({
        "uetr": {"policy": "synthesize"},
        "debtor_name": {"policy": "drop"},
        "debtor_account": {"policy": "mask", "keep_last": 4},
        "amount": {"policy": "generalize", "bucket": 500},
        "country": {"policy": "hash", "length": 12},
    }, fingerprint=fp)
    out = anon.apply(records[:50])
    r = out[0]
    assert "debtor_name" not in r
    assert r["debtor_account"].startswith("*") and len(r["debtor_account"]) == 18
    assert "-" in r["amount"]
    assert len(r["country"]) == 12
    # consistent pseudonyms: same original -> same output
    again = anon.apply(records[:50])
    assert [x["uetr"] for x in out] == [x["uetr"] for x in again]


def test_fpe_rejected_in_phase1():
    with pytest.raises(ValueError):
        Anonymizer({"pan": {"policy": "fpe"}})


def test_leak_check_zero_on_policied_output(fp_and_records):
    fp, _ = fp_and_records
    anon = Anonymizer({
        "uetr": {"policy": "synthesize"},
        "debtor_name": {"policy": "drop"},
        "debtor_account": {"policy": "mask", "keep_last": 4},
    }, fingerprint=fp)
    out = anon.apply(fp.sample(n_journeys=200, seed=3))
    assert anon.leak_check(out)["leaks"] == 0


def test_leak_check_catches_a_planted_leak(fp_and_records):
    fp, records = fp_and_records
    anon = Anonymizer({"debtor_account": {"policy": "mask"}}, fingerprint=fp)
    out = anon.apply(fp.sample(n_journeys=10, seed=3))
    out[0]["note"] = records[0]["debtor_account"]      # plant a real value
    assert anon.leak_check(out)["leaks"] == 1
