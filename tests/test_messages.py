import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints import simulate
from fingerprints.messages import parse_mt, parse_mx, render_mt, render_mx
from fingerprints.model import Fingerprint

MT_SIMS = ["mt101-messages", "mt103-messages", "mt199-messages",
           "mt202-messages", "mt202cov-messages", "mt900-messages",
           "mt910-messages", "mt940-messages", "mt942-messages"]
MX_SIMS = ["mx-pacs008-messages", "mx-pacs009-messages",
           "mx-pacs009cov-messages", "mx-pain001-messages",
           "mx-camt052-messages", "mx-camt053-messages",
           "mx-camt054-messages", "mx-camt998-messages"]


@pytest.mark.parametrize("sim", MT_SIMS)
def test_mt_round_trip(sim):
    records, binding = simulate.SIMULATORS[sim](n_messages=40, seed=3)
    text = render_mt(records)
    reparsed, _ = parse_mt(text)
    assert len(reparsed) == 40
    assert {r["_mt_type"] for r in reparsed} == {records[0]["_mt_type"]}
    fp = Fingerprint(name=sim, **binding).fit(records)
    synth = fp.sample(n_journeys=20, seed=5)
    text2 = render_mt(synth)
    assert len(parse_mt(text2)[0]) == 20


@pytest.mark.parametrize("sim", MX_SIMS)
def test_mx_round_trip(sim):
    records, binding = simulate.SIMULATORS[sim](n_messages=40, seed=3)
    xml = render_mx(records)
    reparsed, _ = parse_mx(xml)
    assert len(reparsed) == 40
    assert reparsed[0]["_msg_type"] == records[0]["_msg_type"]
    fp = Fingerprint(name=sim, **binding).fit(records)
    synth = fp.sample(n_journeys=20, seed=5)
    assert len(parse_mx(render_mx(synth))[0]) == 20


def test_mt940_statement_lines_interleave():
    records, _ = simulate.SIMULATORS["mt940-messages"](n_messages=10, seed=3)
    text = render_mt(records)
    block = text.split("-}")[0]
    tags = [line.split(":")[1] for line in block.splitlines()
            if line.startswith(":")]
    i61 = [i for i, t in enumerate(tags) if t == "61"]
    i86 = [i for i, t in enumerate(tags) if t == "86"]
    assert i61 and i86 and len(i61) == len(i86)
    assert all(b == a + 1 for a, b in zip(i61, i86))     # 61 then its 86
    assert tags.index("62F") > max(i86)                  # closing after lines


def test_cov_marker_round_trip():
    records, _ = simulate.SIMULATORS["mt202cov-messages"](n_messages=5, seed=3)
    text = render_mt(records)
    assert "{3:{119:COV}}" in text
    reparsed, _ = parse_mt(text)
    assert all(r.get("119") == "COV" for r in reparsed)


def test_camt_repeated_entries_render_as_repeated_elements():
    records, _ = simulate.SIMULATORS["mx-camt053-messages"](n_messages=5, seed=3)
    xml = render_mx(records)
    first_doc = xml.split("</Document>")[0]
    assert first_doc.count("<Ntry>") >= 2
    assert first_doc.count("<Bal>") == 2
    assert "#" not in first_doc                          # suffixes never leak
