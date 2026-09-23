import json
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


# ------------------------------------------------------------- edit & re-verify

def test_edit_on_a_hop_cascades_downstream():
    field = "FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt"
    r = e2e.run_pipeline(scenario="outward", seed=1,
                          edits={"payment-processing": {"forward": {field: "999999.99"}}})
    assert r["hops"][1]["message"][field] == "999999.99"
    assert r["hops"][2]["message"][field] == "999999.99"  # CoreBanking derived from the edit
    assert r["hops"][3]["message"][field] == "999999.99"  # Clearing derived from the edit
    kinds = {e["kind"] for e in r["hops"][1]["lineage"]}
    assert "edited" in kinds


def test_edit_does_not_affect_unrelated_flow():
    field = "FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt"
    baseline = e2e.run_pipeline(scenario="outward", seed=1)
    edited = e2e.run_pipeline(scenario="outward", seed=1,
                               edits={"payment-processing": {"forward": {field: "1.00"}}})
    assert baseline["hops"][1]["message"][field] != edited["hops"][1]["message"][field]
    # everything else on that hop is untouched
    other_field = "FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.Nm"
    assert baseline["hops"][1]["message"][other_field] == edited["hops"][1]["message"][other_field]


def test_edit_on_origin_hop_gets_lineage_instead_of_none():
    field = "CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.RmtInf.Ustrd"
    r = e2e.run_pipeline(scenario="outward", seed=2,
                          edits={"ibnk-channel": {"forward": {field: "EDITED"}}})
    assert r["hops"][0]["message"][field] == "EDITED"
    assert r["hops"][0]["lineage"] is not None
    assert r["hops"][0]["lineage"][0]["kind"] == "edited"


def test_edit_on_response_leg_only_affects_response():
    field = "FIToFIPmtStsRpt.TxInfAndSts.TxSts"
    r = e2e.run_pipeline(scenario="outward", seed=3, verdict="ACSC",
                          edits={"clearing-settlement": {"response": {field: "RJCT"}}})
    assert r["hops"][4]["message"][field] == "RJCT"
    # forward leg for the same system is untouched
    assert r["hops"][3]["direction"] == "forward"
    assert "TxSts" not in "".join(r["hops"][3]["message"].keys())


# --------------------------------------------------------- optional tags

def test_edit_can_add_an_optional_tag_and_it_propagates_downstream():
    new_field = "FIToFICstmrCdtTrf.CdtTrfTxInf.Purp.Cd"
    r = e2e.run_pipeline(scenario="outward", seed=1,
                          edits={"payment-processing": {"forward": {new_field: "SALA"}}})
    assert r["hops"][1]["message"][new_field] == "SALA"
    assert r["hops"][2]["message"][new_field] == "SALA"  # CoreBanking still carries it
    assert r["hops"][3]["message"][new_field] == "SALA"  # Clearing still carries it
    kinds = {e["kind"] for e in r["hops"][1]["lineage"] if e["to_field"] == new_field}
    assert kinds == {"added"}


def test_edit_can_remove_an_optional_tag_and_it_stays_gone_downstream():
    field = "FIToFICstmrCdtTrf.CdtTrfTxInf.RmtInf.Ustrd"
    r = e2e.run_pipeline(scenario="outward", seed=1,
                          edits={"payment-processing": {"forward": {field: None}}})
    assert field not in r["hops"][1]["message"]
    assert field not in r["hops"][2]["message"]
    assert field not in r["hops"][3]["message"]
    removed_entries = [e for e in r["hops"][1]["lineage"] if e["kind"] == "removed"]
    assert len(removed_entries) == 1 and removed_entries[0]["from_field"] == field


def test_edit_removing_a_field_that_is_not_present_is_a_no_op():
    r = e2e.run_pipeline(scenario="outward", seed=1,
                          edits={"ibnk-channel": {"forward": {"CstmrCdtTrfInitn.Not.There": None}}})
    assert r["hops"][0]["lineage"] == []


def test_edit_can_mix_add_remove_and_correct_in_one_call():
    add_field = "FIToFICstmrCdtTrf.CdtTrfTxInf.Purp.Cd"
    remove_field = "FIToFICstmrCdtTrf.CdtTrfTxInf.RmtInf.Ustrd"
    correct_field = "FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt"
    r = e2e.run_pipeline(scenario="outward", seed=1, edits={"payment-processing": {"forward": {
        add_field: "SALA", remove_field: None, correct_field: "1.23",
    }}})
    msg = r["hops"][1]["message"]
    assert msg[add_field] == "SALA"
    assert remove_field not in msg
    assert msg[correct_field] == "1.23"
    kinds_by_field = {e["to_field"] or e["from_field"]: e["kind"] for e in r["hops"][1]["lineage"]
                       if e["to_field"] in (add_field, correct_field) or e["from_field"] == remove_field}
    assert kinds_by_field[add_field] == "added"
    assert kinds_by_field[correct_field] == "edited"
    assert kinds_by_field[remove_field] == "removed"


# ------------------------------------------------------------------- batch

def test_run_batch_produces_n_unique_flows():
    batch = e2e.run_batch(count=5, scenario="outward", seed_base=100)
    assert batch["count"] == 5 and len(batch["flows"]) == 5
    refs = [f["hops"][0]["message"]["CstmrCdtTrfInitn.PmtInf.CdtTrfTxInf.PmtId.EndToEndId"]
            for f in batch["flows"]]
    assert len(set(refs)) == 5
    assert [f["flow_index"] for f in batch["flows"]] == [0, 1, 2, 3, 4]


def test_run_batch_per_system_dataset_cycles_and_others_still_derive():
    ds_msg_a = e2e.build_pain001(__import__("random").Random(1), account="SG00DATASET0001")
    ds_msg_b = e2e.build_pain001(__import__("random").Random(2), account="SG00DATASET0002")
    batch = e2e.run_batch(count=4, scenario="outward",
                           datasets={"ibnk-channel": [ds_msg_a, ds_msg_b]}, seed_base=1)
    accounts = [f["hops"][0]["message"]["CstmrCdtTrfInitn.PmtInf.DbtrAcct.Id.IBAN"]
                for f in batch["flows"]]
    assert accounts == ["SG00DATASET0001", "SG00DATASET0002",
                         "SG00DATASET0001", "SG00DATASET0002"]
    # payment-processing has no dataset -> still derives end-to-end from the origin
    for f in batch["flows"]:
        assert f["hops"][1]["message"]["_msg_type"] == "pacs.008.001.08"
        assert f["hops"][1]["lineage"]


def test_run_batch_rejects_out_of_range_count():
    import pytest
    with pytest.raises(ValueError, match="count must be between"):
        e2e.run_batch(count=0, scenario="outward")
    with pytest.raises(ValueError, match="count must be between"):
        e2e.run_batch(count=e2e.MAX_BATCH + 1, scenario="outward")


# ------------------------------------------------------------- custom flow types

def test_scenario_def_defines_a_custom_flow_type():
    rtgs_def = {"label": "RTGS", "origin_message_type": "pain.001.001.09",
                "chain": ["ibnk-channel", "payment-processing", "clearing-settlement"]}
    r = e2e.run_pipeline(scenario="rtgs", scenario_def=rtgs_def, seed=1,
                          account="SG00RTGS0000001")
    assert r["scenario"] == "rtgs"
    assert r["chain"] == rtgs_def["chain"]
    forward_types = [h["message"]["_msg_type"] for h in r["hops"] if h["direction"] == "forward"]
    assert forward_types == ["pain.001.001.09", "pacs.008.001.08", "pacs.008.001.08"]


def test_scenario_def_can_skip_systems_entirely():
    book_def = {"label": "Book Transfer", "origin_message_type": "pain.001.001.09",
                "chain": ["ibnk-channel", "payment-processing", "corebanking"]}
    r = e2e.run_pipeline(scenario="book-transfer", scenario_def=book_def, seed=2)
    assert "clearing-settlement" not in r["chain"]
    assert len(r["hops"]) == 6  # 3 forward + 3 response, no clearing leg


def test_scenario_def_inward_style_stays_pacs008_throughout():
    tt_def = {"label": "Telegraphic Transfer", "origin_message_type": "pacs.008.001.08",
              "chain": ["regulator-fi", "payment-processing", "corebanking",
                        "realtime-notification"]}
    r = e2e.run_pipeline(scenario="tt", scenario_def=tt_def, seed=3)
    forward_types = [h["message"]["_msg_type"] for h in r["hops"] if h["direction"] == "forward"]
    assert forward_types == ["pacs.008.001.08"] * 4


def test_scenario_def_rejects_unsupported_origin_message_type():
    import pytest
    with pytest.raises(ValueError, match="unsupported origin_message_type"):
        e2e.run_pipeline(scenario="x", scenario_def={
            "label": "x", "origin_message_type": "camt.053.001.08",
            "chain": ["a", "b"]})


# --------------------------------------------------------------- bulk export

def test_iter_batch_is_a_generator_and_matches_run_batch():
    generated = list(e2e.iter_batch(5, scenario="outward", seed_base=1))
    batched = e2e.run_batch(count=5, scenario="outward", seed_base=1)["flows"]
    assert [f["hops"][0]["message"] for f in generated] == \
           [f["hops"][0]["message"] for f in batched]


def test_export_flows_writes_ndjson_csv_and_manifest(tmp_path):
    manifest = e2e.export_flows(tmp_path, count=10, scenario="outward", seed_base=1)
    files = manifest.pop("files")
    assert manifest["count"] == 10
    assert manifest["total_hop_messages"] == 80  # 10 flows x 8 hops
    assert manifest["message_type_counts"]["pain.001.001.09"] == 10
    assert manifest["verdict_counts"] == {"ACSC": 10}

    messages_path = Path(files["messages_ndjson"])
    lines = messages_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 80
    first = json.loads(lines[0])
    assert first["flow_index"] == 0 and first["system"] == "ibnk-channel"

    index_path = Path(files["index_csv"])
    assert index_path.exists() and index_path.stat().st_size > 0

    manifest_path = Path(files["manifest_json"])
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["total_hop_messages"] == 80


def test_export_flows_supports_scenario_def_and_custom_chain(tmp_path):
    rtgs_def = {"label": "RTGS", "origin_message_type": "pain.001.001.09",
                "chain": ["ibnk-channel", "payment-processing", "clearing-settlement"]}
    manifest = e2e.export_flows(tmp_path, count=3, scenario="rtgs",
                                 scenario_def=rtgs_def, seed_base=1)
    assert manifest["chain"] == rtgs_def["chain"]
    assert manifest["total_hop_messages"] == 3 * 6  # 3-system chain -> 6 hops/flow


def test_export_flows_cleans_up_partial_file_on_error(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        e2e.export_flows(tmp_path, count=3, scenario="does-not-exist")
    assert list(tmp_path.glob("*.ndjson")) == []


def test_export_flows_rejects_out_of_range_count(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="count must be between"):
        e2e.export_flows(tmp_path, count=0, scenario="outward")
    with pytest.raises(ValueError, match="count must be between"):
        e2e.export_flows(tmp_path, count=e2e.MAX_EXPORT + 1, scenario="outward")
