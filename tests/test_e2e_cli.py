import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints.e2e_cli import main


def test_list_scenarios(capsys):
    rc = main(["list-scenarios"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(out["scenarios"]) == 4
    assert len(out["systems"]) == 6


def test_export_writes_files_and_prints_summary(tmp_path, capsys):
    rc = main(["export", "--scenario", "outward", "--count", "10", "--seed", "1",
               "--out-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "10 flows" in out
    assert "80 hop messages" in out
    ndjson_files = list(tmp_path.glob("*-messages.ndjson"))
    assert len(ndjson_files) == 1
    lines = ndjson_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 80


def test_export_json_format(tmp_path, capsys):
    rc = main(["export", "--scenario", "outward", "--count", "5", "--seed", "2",
               "--out-dir", str(tmp_path), "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert manifest["count"] == 5
    assert manifest["total_hop_messages"] == 40


def test_export_custom_chain_and_systems(tmp_path, capsys):
    rc = main([
        "export", "--scenario", "outward", "--count", "3", "--seed", "1",
        "--out-dir", str(tmp_path), "--format", "json",
        "--chain", "ibnk-channel,fraud-check,payment-processing,corebanking,clearing-settlement",
        "--system", "fraud-check:Fraud Check",
    ])
    manifest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert manifest["chain"] == ["ibnk-channel", "fraud-check", "payment-processing",
                                  "corebanking", "clearing-settlement"]
    assert manifest["total_hop_messages"] == 3 * 10  # 5-system chain -> 10 hops/flow


def test_export_scenario_def_inline_json(tmp_path, capsys):
    scenario_def = json.dumps({
        "label": "RTGS", "origin_message_type": "pain.001.001.09",
        "chain": ["ibnk-channel", "payment-processing", "clearing-settlement"],
    })
    rc = main(["export", "--scenario", "rtgs", "--scenario-def", scenario_def,
               "--count", "4", "--seed", "1", "--out-dir", str(tmp_path),
               "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert manifest["scenario"] == "rtgs"
    assert manifest["chain"] == ["ibnk-channel", "payment-processing", "clearing-settlement"]


def test_export_scenario_def_from_file(tmp_path, capsys):
    def_path = tmp_path / "book_transfer.json"
    def_path.write_text(json.dumps({
        "label": "Book Transfer", "origin_message_type": "pain.001.001.09",
        "chain": ["ibnk-channel", "payment-processing", "corebanking"],
    }), encoding="utf-8")
    rc = main(["export", "--scenario", "book-transfer", "--scenario-def-file", str(def_path),
               "--count", "2", "--seed", "1", "--out-dir", str(tmp_path), "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert manifest["total_hop_messages"] == 2 * 6  # 3-system chain -> 6 hops/flow


def test_export_file_source_reads_local_dataset_file(tmp_path, capsys):
    # first, generate one flow to get a real pain.001 message to reuse as a dataset
    rc = main(["export", "--scenario", "outward", "--count", "1", "--seed", "9",
               "--out-dir", str(tmp_path), "--format", "json"])
    manifest = json.loads(capsys.readouterr().out)
    ndjson_path = Path(manifest["files"]["messages_ndjson"])
    first_hop = json.loads(ndjson_path.read_text(encoding="utf-8").splitlines()[0])
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">\n'
        '<CstmrCdtTrfInitn><GrpHdr><MsgId>{}</MsgId></GrpHdr></CstmrCdtTrfInitn>\n'
        '</Document>\n'
    ).format(first_hop["CstmrCdtTrfInitn.GrpHdr.MsgId"])
    dataset_path = tmp_path / "channel_dataset.xml"
    dataset_path.write_text(xml, encoding="utf-8")

    rc = main(["export", "--scenario", "outward", "--count", "1", "--seed", "1",
               "--out-dir", str(tmp_path), "--format", "json", "--source", "file",
               "--file", f"ibnk-channel:{dataset_path}"])
    manifest2 = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert manifest2["total_hop_messages"] == 8


def test_export_rejects_bad_count(tmp_path, capsys):
    rc = main(["export", "--scenario", "outward", "--count", "0", "--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "count must be between" in err


def test_export_rejects_unknown_scenario(tmp_path, capsys):
    rc = main(["export", "--scenario", "nope", "--count", "5", "--out-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown scenario" in err
