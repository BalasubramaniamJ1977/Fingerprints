import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints import e2e
from fingerprints.messages import render_mx


def test_scenarios_cover_all_four_flows():
    assert set(e2e.SCENARIOS) == {"outward", "inward", "outward_return", "inward_return"}
    for cfg in e2e.SCENARIOS.values():
        assert len(cfg["chain"]) == 4
        assert cfg["origin_message_type"] in e2e._ORIGIN_BUILDERS


def test_outward_default_chain_shape():
    r = e2e.run_pipeline(scenario="outward", seed=1)
    seqs = [(h["system"], h["direction"], h["message"]["_msg_type"]) for h in r["hops"]]
    assert seqs == [
        ("ibnk-channel", "forward", "pain.001.001.09"),
        ("payment-processing", "forward", "pacs.008.001.08"),
        ("corebanking", "forward", "pacs.008.001.08"),
        ("clearing-settlement", "forward", "pacs.008.001.08"),
        ("clearing-settlement", "response", "pacs.002.001.10"),
        ("corebanking", "response", "pacs.002.001.10"),
        ("payment-processing", "response", "pacs.002.001.10"),
        ("ibnk-channel", "response", "pacs.002.001.10"),
    ]


def test_outward_pain001_to_pacs008_field_mapping():
    r = e2e.run_pipeline(scenario="outward", seed=7, account="SG00MYACCT0000009")
    pain = r["hops"][0]["message"]
    pacs = r["hops"][1]["message"]
    assert pain["CstmrCdtTrfInitn.PmtInf.DbtrAcct.Id.IBAN"] == "SG00MYACCT0000009"
    assert pacs["FIToFICstmrCdtTrf.CdtTrfTxInf.DbtrAcct.Id.Othr.Id"] == "SG00MYACCT0000009"
    assert (pain["CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.Amt.InstdAmt"]
            == pacs["FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt"])
    assert (pain["CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.Amt.InstdAmt@Ccy"]
            == pacs["FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt@Ccy"])
    # a UETR is introduced at this hop — pain.001 doesn't carry one
    assert "FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR" in pacs
    assert not any(k.endswith("UETR") for k in pain if not k.startswith("_"))


def test_response_leg_references_original_uetr_and_end_to_end_id():
    r = e2e.run_pipeline(scenario="outward", seed=9)
    final_pacs = r["hops"][3]["message"]
    resp = r["hops"][4]["message"]
    assert resp["FIToFIPmtStsRpt.TxInfAndSts.OrgnlUETR"] == \
        final_pacs["FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR"]
    assert resp["FIToFIPmtStsRpt.TxInfAndSts.OrgnlEndToEndId"] == \
        final_pacs["FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.EndToEndId"]
    assert resp["FIToFIPmtStsRpt.TxInfAndSts.TxSts"] == "ACSC"


def test_verdict_is_configurable():
    r = e2e.run_pipeline(scenario="outward", seed=2, verdict="RJCT")
    assert r["hops"][4]["message"]["FIToFIPmtStsRpt.TxInfAndSts.TxSts"] == "RJCT"


def test_each_hop_after_origin_has_lineage_and_enrichment():
    r = e2e.run_pipeline(scenario="outward", seed=4)
    assert r["hops"][0]["lineage"] is None  # origination, nothing to trace
    for h in r["hops"][1:]:
        assert h["lineage"], f"hop {h['seq']} ({h['system']}) has no lineage"
    corebanking_hop = r["hops"][2]
    kinds = {entry["kind"] for entry in corebanking_hop["lineage"]}
    assert "enriched" in kinds and "passthrough" in kinds and "regenerated" in kinds


def test_all_four_scenarios_run_and_render_to_xml():
    for scenario in e2e.SCENARIOS:
        r = e2e.run_pipeline(scenario=scenario, seed=5, account="SG00DEMO0000001")
        assert len(r["hops"]) == 8
        for h in r["hops"]:
            xml = render_mx([h["message"]])
            assert xml.strip().startswith("<?xml")
            assert h["message"]["_msg_type"] in xml


def test_inward_scenario_credits_synthetic_account_as_creditor():
    r = e2e.run_pipeline(scenario="inward", seed=6, account="US00INWARD0000007")
    origin = r["hops"][0]["message"]
    assert origin["FIToFICstmrCdtTrf.CdtTrfTxInf.CdtrAcct.Id.Othr.Id"] == "US00INWARD0000007"


def test_inward_return_scenario_uses_pacs004():
    r = e2e.run_pipeline(scenario="inward_return", seed=8)
    assert r["hops"][0]["message"]["_msg_type"] == "pacs.004.001.09"
    assert r["hops"][3]["message"]["_msg_type"] == "pacs.004.001.09"
    assert r["chain"] == ["ibnk-channel", "payment-processing", "corebanking", "clearing-settlement"]


def test_inward_and_outward_return_flow_via_regulator_fi_to_realtime_notification():
    expected_chain = ["regulator-fi", "payment-processing", "corebanking", "realtime-notification"]
    for scenario in ("inward", "outward_return"):
        r = e2e.run_pipeline(scenario=scenario, seed=8)
        assert r["chain"] == expected_chain
        assert r["hops"][0]["system"] == "regulator-fi"
        assert r["hops"][0]["message"]["_msg_type"] == "pacs.008.001.08"
        assert r["hops"][1]["system"] == "payment-processing"
        assert r["hops"][2]["system"] == "corebanking"
        assert r["hops"][3]["system"] == "realtime-notification"
        assert r["hops"][3]["message"]["_msg_type"] == "pacs.008.001.08"
        # response relays back out through the same chain, in reverse
        response_systems = [h["system"] for h in r["hops"] if h["direction"] == "response"]
        assert response_systems == ["realtime-notification", "corebanking",
                                     "payment-processing", "regulator-fi"]


def test_hops_by_system_groups_forward_and_response():
    r = e2e.run_pipeline(scenario="outward", seed=10)
    by_sys = e2e.hops_by_system(r)
    assert set(by_sys) == set(e2e.SCENARIOS["outward"]["chain"])
    assert by_sys["ibnk-channel"]["forward"]["lineage"] is None
    assert by_sys["ibnk-channel"]["response"]["direction"] == "response"
    assert by_sys["clearing-settlement"]["response"]["message"]["_msg_type"] == "pacs.002.001.10"


def test_file_override_replaces_a_hop_and_uses_generic_diff():
    baseline = e2e.run_pipeline(scenario="outward", seed=11)
    real_pain001 = dict(baseline["hops"][0]["message"])
    real_pain001["CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.RmtInf.Ustrd"] = "REAL UPLOADED PAYMENT"

    r = e2e.run_pipeline(scenario="outward", seed=11,
                          overrides={"ibnk-channel": real_pain001})
    assert r["hops"][0]["message"]["CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.RmtInf.Ustrd"] \
        == "REAL UPLOADED PAYMENT"
    # downstream hops still derive normally from the (overridden) origin message
    assert r["hops"][1]["message"]["_msg_type"] == "pacs.008.001.08"


def test_custom_chain_inserts_intermediary_systems():
    chain = ["ibnk-channel", "fraud-check", "payment-processing",
             "sanctions-screening", "corebanking", "clearing-settlement"]
    systems = [{"id": "fraud-check", "name": "Fraud Check"},
               {"id": "sanctions-screening", "name": "Sanctions Screening"}]
    r = e2e.run_pipeline(scenario="outward", seed=12, chain=chain, systems=systems)
    assert [s["id"] for s in r["systems"]] == chain
    assert len(r["hops"]) == 2 * len(chain)

    # an intermediary inserted BEFORE payment-processing still relays pain.001
    fraud_hop = r["hops"][1]
    assert fraud_hop["system"] == "fraud-check"
    assert fraud_hop["message"]["_msg_type"] == "pain.001.001.09"
    assert all(entry["kind"] in ("passthrough", "regenerated", "enriched")
               for entry in fraud_hop["lineage"])

    # conversion happens exactly once, at payment-processing
    pps_hop = r["hops"][2]
    assert pps_hop["system"] == "payment-processing"
    assert pps_hop["message"]["_msg_type"] == "pacs.008.001.08"
    types = [h["message"]["_msg_type"] for h in r["hops"] if h["direction"] == "forward"]
    assert types.count("pain.001.001.09") == 2  # origin + fraud-check relay
    assert types.count("pacs.008.001.08") == 4


def test_custom_chain_without_processing_role_falls_back_to_first_hop():
    r = e2e.run_pipeline(scenario="outward", seed=13, chain=["A", "B", "C"],
                          systems=[{"id": "A"}, {"id": "B"}, {"id": "C"}])
    types = [h["message"]["_msg_type"] for h in r["hops"] if h["direction"] == "forward"]
    assert types == ["pain.001.001.09", "pacs.008.001.08", "pacs.008.001.08"]


def test_chain_validation_rejects_too_short_and_duplicate_ids():
    import pytest
    with pytest.raises(ValueError, match="at least 2 systems"):
        e2e.run_pipeline(chain=["only-one"])
    with pytest.raises(ValueError, match="repeated system id"):
        e2e.run_pipeline(chain=["x", "x", "y"])


def test_unknown_scenario_rejected():
    import pytest
    with pytest.raises(ValueError, match="unknown scenario"):
        e2e.run_pipeline(scenario="sideways")


def test_database_source_requires_dsn_and_table():
    import pytest
    with pytest.raises(ValueError, match="database source requires"):
        e2e.run_pipeline(scenario="outward", source="database", db=None)
    with pytest.raises(ValueError, match="database source requires"):
        e2e.run_pipeline(scenario="outward", source="database", db={"dsn": "sqlite:///x.db"})


def test_seeded_runs_are_deterministic():
    r1 = e2e.run_pipeline(scenario="outward", seed=99, account="SG00SEED0000001")
    r2 = e2e.run_pipeline(scenario="outward", seed=99, account="SG00SEED0000001")
    assert r1["hops"][0]["message"] == r2["hops"][0]["message"]
    assert r1["hops"][3]["message"] == r2["hops"][3]["message"]
