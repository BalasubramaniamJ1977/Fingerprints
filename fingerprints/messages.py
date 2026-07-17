"""MX (ISO 20022 XML) and MT (SWIFT tag-block) message adapters.

Parsing produces canonical records (one message = one record) with a hidden
`_format` field so synthetic output can be rendered back to wire format.
Fields:
  MX: dotted leaf paths, attributes as `path@attr`  (e.g. ...IntrBkSttlmAmt@Ccy)
  MT: tag names, with composite tags decomposed     (32A -> _ts + ccy + amount)
"""

import random
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

FIRST = ["Wei", "Aisha", "Rajesh", "Mei Ling", "Daniel", "Fatimah", "Arun",
         "Grace", "Hakim", "Priya", "Marcus", "Siti", "Kumar", "Elaine"]
LAST = ["Tan", "Lim", "Sharma", "Ng", "Abdullah", "Lee", "Krishnan", "Wong",
        "Iyer", "Chua", "Hassan", "Goh", "Pillai", "Ong"]
BICS = ["OCBCSGSG", "DBSSSGSG", "UOVBSGSG", "CITIUS33", "HSBCGB2L", "MAYBMYKL"]

MX_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
P = "FIToFICstmrCdtTrf"


# ------------------------------------------------------------------ MX parse

def _strip(tag):
    return tag.split("}")[-1]


def _flatten(elem, prefix, out, seg=None):
    from collections import Counter as _C
    children = list(elem)
    path = f"{prefix}.{seg or _strip(elem.tag)}" if prefix else (seg or _strip(elem.tag))
    for attr, val in elem.attrib.items():
        out[f"{path}@{attr}"] = val
    if children:
        counts = _C(_strip(c.tag) for c in children)
        idx = {}
        for c in children:
            t = _strip(c.tag)
            if counts[t] > 1:            # repeated siblings: Bal#1, Bal#2, Ntry#1...
                idx[t] = idx.get(t, 0) + 1
                _flatten(c, path, out, seg=f"{t}#{idx[t]}")
            else:
                _flatten(c, path, out)
    elif elem.text and elem.text.strip():
        out[path] = elem.text.strip()


def _find_suffix(rec, *suffixes):
    for suf in suffixes:
        for k, v in rec.items():
            if k.endswith(suf):
                return v
    return None


def parse_mx(text):
    """Parse one or more <Document> messages of ANY ISO 20022 definition
    (pacs.*, pain.*, camt.*, ...); returns (records, binding). Message type and
    namespace are taken from the document itself and carried per record, so a
    file may mix definitions and rendering restores each one's namespace."""
    chunks = re.split(r"(?=<Document\b)", text)
    records = []
    for chunk in chunks:
        if "<Document" not in chunk:
            continue
        chunk = chunk[chunk.index("<Document"):]
        end = chunk.rindex("</Document>") + len("</Document>")
        root = ET.fromstring(chunk[:end])
        ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
        rec = {}
        for c in root:
            _flatten(c, "", rec)
        rec["_ns"] = ns or MX_NS
        rec["_msg_type"] = ns.rsplit(":", 1)[-1] if ns else "unknown"
        rec["_msg_id"] = (_find_suffix(rec, ".GrpHdr.MsgId", ".MsgId")
                          or uuid.uuid4().hex[:16])
        rec["_ts"] = (_find_suffix(rec, ".GrpHdr.CreDtTm", ".CreDtTm")
                      or "2026-01-01T00:00:00")
        rec["_format"] = "mx"
        records.append(rec)
    states = ["_msg_type"]
    if records:
        ccy = next((k for k in records[0] if k.endswith("@Ccy")), None)
        if ccy:
            states.append(ccy)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": states}
    return records, binding


# ----------------------------------------------------------------- MX render

def _nest(record):
    root = {}
    # pacs.008 schema order: GrpHdr before CdtTrfTxInf (stable within groups)
    ordered = sorted(record.items(), key=lambda kv: 0 if ".GrpHdr" in kv[0] else 1)
    for k, v in ordered:
        if k.startswith("_") or v in (None, ""):
            continue
        path, attr = (k.split("@", 1) + [None])[:2]
        parts = path.split(".")
        node = root
        for p in parts[:-1]:
            nxt = node.setdefault(p, {})
            if not isinstance(nxt, dict):
                nxt = node[p] = {"#text": nxt}
            node = nxt
        leaf = parts[-1]
        if attr:
            cur = node.get(leaf)
            if not isinstance(cur, dict):
                cur = node[leaf] = ({"#text": cur} if cur is not None else {})
            cur["@" + attr] = v
        else:
            cur = node.get(leaf)
            if isinstance(cur, dict):
                cur["#text"] = str(v)
            else:
                node[leaf] = str(v)
    return root


def _xml(name, val, indent):
    name = name.split("#")[0]           # Bal#2 renders as a repeated <Bal>
    if isinstance(val, dict):
        attrs = "".join(f' {k[1:]}="{escape(str(v))}"'
                        for k, v in val.items() if k.startswith("@"))
        text = val.get("#text")
        kids = {k: v for k, v in val.items()
                if not k.startswith("@") and k != "#text"}
        if kids:
            inner = "\n".join(_xml(k, v, indent + "  ") for k, v in kids.items())
            return f"{indent}<{name}{attrs}>\n{inner}\n{indent}</{name}>"
        return f"{indent}<{name}{attrs}>{escape(str(text or ''))}</{name}>"
    return f"{indent}<{name}>{escape(str(val))}</{name}>"


def render_mx(records):
    docs = []
    for r in records:
        ns = r.get("_ns") or MX_NS
        body = "\n".join(_xml(k, v, "  ") for k, v in _nest(r).items())
        docs.append(f'<Document xmlns="{ns}">\n{body}\n</Document>')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(docs) + "\n"


# ------------------------------------------------------------------ MT parse

MT_TAG = re.compile(r"^:(\w{2,3}):(.*)$")
MT_HDR = re.compile(r"\{1:.{3}(\w{8,12})\w*\}\{2:.(\d{3})(\w{8,12})")
MT_B3 = re.compile(r"\{3:.*?\{119:(\w+)\}")
MT_BAL = re.compile(r"^([DC])(\d{6})([A-Z]{3})([\d,\.]+)$")
MT_PARTY = re.compile(r"^(50|59)[A-Z]?$")      # ordering/beneficiary customer
MT_TAGNUM = re.compile(r"^(\d{2})([A-Z]?)(?:#(\d+))?$")

# canonical SWIFT tag order; 61/86 statement lines interleave by sequence and
# sit between the opening (60) and closing (62) balances
MT_CANON = ["20", "21", "21R", "25", "25P", "28", "28C", "28D", "13C", "13D",
            "23B", "23E", "26T", "30", "32A", "32B", "33B", "36", "50A", "50F",
            "50G", "50H", "50K", "51A", "52A", "52D", "53A", "53B", "53D",
            "54A", "56A", "56D", "57A", "57D", "58A", "58D", "59", "59A",
            "59F", "60F", "60M", "61", "86", "62F", "62M", "64", "65", "90C",
            "90D", "70", "71A", "71F", "71G", "72", "77B", "77T", "79"]


def parse_mt(text):
    """Parse any MT tag-block messages (103, 202, 940, ... — separated by '-}').
    The message type comes from the block-2 header and is carried per record,
    so a file may mix types. Repeated tags (e.g. statement lines) get #n
    suffixes. Party tags (50x/59x) and 32A are decomposed for modeling and
    reassembled on render; every other tag is learned verbatim."""
    records = []
    for raw in text.split("-}"):
        if ":20:" not in raw:
            continue
        rec = {"_format": "mt"}
        hdr = MT_HDR.search(raw)
        rec["_sender_bic"] = (hdr.group(1)[:8] if hdr else "BANKSGSG")
        rec["_mt_type"] = (hdr.group(2) if hdr else "103")
        rec["_receiver_bic"] = (hdr.group(3)[:8] if hdr else "BANKUS33")
        b3 = MT_B3.search(raw)
        if b3:
            rec["119"] = b3.group(1)     # e.g. COV on MT202 COV
        block4 = raw.split("{4:", 1)[-1]
        tags = {}
        cur = None
        for line in block4.splitlines():
            m = MT_TAG.match(line.strip())
            if m:
                cur = m.group(1)
                if cur in tags:                     # repeated tag -> suffix
                    n = 2
                    while f"{cur}#{n}" in tags:
                        n += 1
                    cur = f"{cur}#{n}"
                tags[cur] = m.group(2)
            elif cur and line.strip():
                tags[cur] += "\n" + line.strip()
        rec["_msg_id"] = tags.pop("20", uuid.uuid4().hex[:12].upper())
        v32 = tags.pop("32A", None)
        if v32:
            rec["_ts"] = f"20{v32[0:2]}-{v32[2:4]}-{v32[4:6]}T00:00:00"
            rec["32A_ccy"] = v32[6:9]
            rec["32A_amount"] = v32[9:].replace(",", ".") or "0"
        else:
            rec["_ts"] = "2026-01-01T00:00:00"
        for tag in [t for t in list(tags) if MT_PARTY.match(t)]:
            v = tags.pop(tag)
            lines = v.splitlines()
            rec[f"{tag}_acct"] = (lines[0].lstrip("/") if lines else "")
            rec[f"{tag}_name"] = " ".join(lines[1:]) if len(lines) > 1 else ""
        for tag in list(tags):                     # balance composites
            if tag.split("#")[0] in ("60F", "60M", "62F", "62M", "64", "65"):
                m = MT_BAL.match(tags[tag])
                if m:
                    tags.pop(tag)
                    rec[f"{tag}_dc"] = m.group(1)
                    rec[f"{tag}_ccy"] = m.group(3)
                    rec[f"{tag}_amount"] = m.group(4).replace(",", ".")
        for tag, v in tags.items():
            rec[tag] = v.replace("\n", " ")
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type"]}
    return records, binding


# ----------------------------------------------------------------- MT render

def _mt_sort_key(tag):
    """Canonical SWIFT ordering. Statement lines (61/86) share a slot and
    interleave by their #n sequence, so 62F correctly follows all of them."""
    base = tag.split("#")[0]
    seq = int(tag.split("#")[1]) if "#" in tag else 0
    if base in ("61", "86"):
        return (MT_CANON.index("61"), seq, 0 if base == "61" else 1, tag)
    if base in MT_CANON:
        return (MT_CANON.index(base), seq, 0, tag)
    m = MT_TAGNUM.match(tag)
    num = int(m.group(1)) if m else 99
    return (100 + num, seq, 0, tag)


def render_mt(records):
    out = []
    for r in records:
        mt_type = str(r.get("_mt_type", "103"))
        entries = {}  # tag -> text (party/amount composites reassembled below)
        date = str(r.get("_ts", "2026-01-01"))[2:10].replace("-", "")
        for k, v in r.items():
            if k.startswith("_") or v in (None, ""):
                continue
            if k.endswith("_acct"):
                tag = k[:-5]
                name = r.get(f"{tag}_name") or ""
                entries[tag] = f"/{v}" + (f"\n{name}" if name else "")
            elif k.endswith("_ccy"):
                tag = k[:-4]
                try:
                    amount = f"{float(r.get(f'{tag}_amount', 0)):.2f}".replace(".", ",")
                except (TypeError, ValueError):
                    amount = "0,00"
                entries[tag] = f"{r.get(f'{tag}_dc', '')}{date}{v}{amount}"
            elif k.endswith("_name") or k.endswith("_amount") or k.endswith("_dc"):
                continue
            else:
                entries[k] = str(v)
        cov = entries.pop("119", None)
        hdr = ("{1:F01" + str(r.get("_sender_bic", "BANKSGSG")) + "AXXX0000000000}"
               "{2:I" + mt_type + str(r.get("_receiver_bic", "BANKUS33")) + "XXXXN}")
        if cov:
            hdr += "{3:{119:" + str(cov) + "}}"
        lines = [hdr + "{4:", f":20:{r.get('_msg_id', '')}"]
        for tag in sorted(entries, key=_mt_sort_key):
            lines.append(f":{tag.split('#')[0]}:{entries[tag]}")
        lines.append("-}")
        out.append("\n".join(lines))
    return "\n".join(out) + "\n"


RENDERERS = {"mx": ("xml", render_mx), "mt": ("txt", render_mt),
             "mt103": ("txt", render_mt)}


def render_messages(records):
    """Render records to (extension, text) if they carry a message format."""
    fmt = records[0].get("_format") if records else None
    if fmt in RENDERERS:
        ext, fn = RENDERERS[fmt]
        return ext, fn(records)
    return None, None


# --------------------------------------------------------------- simulators

def _base_tx(rng, i):
    return {
        "amount": round(rng.lognormvariate(6.8, 1.0), 2),
        "ccy": rng.choices(["SGD", "USD", "EUR", "MYR"], weights=[5, 3, 1, 1])[0],
        "dbtr_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}".upper(),
        "cdtr_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}".upper(),
        "dbtr_acct": "SG%02d%s%010d" % (rng.randrange(100),
                                        rng.choice(["OCBC", "DBSS", "UOBV"]),
                                        rng.randrange(10**10)),
        "cdtr_acct": "US%02d%s%010d" % (rng.randrange(100),
                                        rng.choice(["CITI", "CHAS", "BOFA"]),
                                        rng.randrange(10**10)),
        "dbtr_bic": rng.choice(BICS[:3]),
        "cdtr_bic": rng.choice(BICS[3:]),
        "ts": (datetime(2026, 7, 1) +
               timedelta(seconds=rng.uniform(0, 14 * 86400))),
        "ref": f"REF{i:08d}{rng.randrange(1000):03d}",
    }


def simulate_mx(n_messages=800, seed=42):
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        rec = {
            f"{P}.GrpHdr.MsgId": f"MSG{t['ref'][3:]}",
            f"{P}.GrpHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{P}.GrpHdr.NbOfTxs": "1",
            f"{P}.GrpHdr.SttlmInf.SttlmMtd": "CLRG",
            f"{P}.CdtTrfTxInf.PmtId.EndToEndId": t["ref"],
            f"{P}.CdtTrfTxInf.PmtId.UETR": str(uuid.UUID(int=rng.getrandbits(128))),
            f"{P}.CdtTrfTxInf.IntrBkSttlmAmt": f"{t['amount']:.2f}",
            f"{P}.CdtTrfTxInf.IntrBkSttlmAmt@Ccy": t["ccy"],
            f"{P}.CdtTrfTxInf.ChrgBr": rng.choices(["SLEV", "SHAR"], weights=[8, 2])[0],
            f"{P}.CdtTrfTxInf.Dbtr.Nm": t["dbtr_name"],
            f"{P}.CdtTrfTxInf.DbtrAcct.Id.Othr.Id": t["dbtr_acct"],
            f"{P}.CdtTrfTxInf.DbtrAgt.FinInstnId.BICFI": t["dbtr_bic"],
            f"{P}.CdtTrfTxInf.Cdtr.Nm": t["cdtr_name"],
            f"{P}.CdtTrfTxInf.CdtrAcct.Id.Othr.Id": t["cdtr_acct"],
            f"{P}.CdtTrfTxInf.CdtrAgt.FinInstnId.BICFI": t["cdtr_bic"],
            f"{P}.CdtTrfTxInf.RmtInf.Ustrd": f"INVOICE {rng.randrange(99999):05d}",
            "_msg_type": "pacs.008.001.08",
            "_ns": MX_NS,
            "_msg_id": f"MSG{t['ref'][3:]}",
            "_ts": t["ts"].isoformat(timespec="seconds"),
            "_format": "mx",
        }
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type",
                                f"{P}.CdtTrfTxInf.IntrBkSttlmAmt@Ccy"]}
    return records, binding


def simulate_mt(n_messages=800, seed=42):
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            "_msg_id": t["ref"],
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"],
            "_receiver_bic": t["cdtr_bic"],
            "23B": "CRED",
            "32A_ccy": t["ccy"],
            "32A_amount": f"{t['amount']:.2f}",
            "50K_acct": t["dbtr_acct"],
            "50K_name": t["dbtr_name"],
            "59_acct": t["cdtr_acct"],
            "59_name": t["cdtr_name"],
            "70": f"INVOICE {rng.randrange(99999):05d}",
            "71A": rng.choices(["OUR", "SHA", "BEN"], weights=[6, 3, 1])[0],
            "_mt_type": "103",
            "_format": "mt",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["23B", "71A"]}
    return records, binding


def simulate_mt202(n_messages=800, seed=42):
    """MT202 — general financial institution transfer (bank-to-bank)."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            "_msg_id": t["ref"],
            "21": f"REL{rng.randrange(10**8):08d}",
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"],
            "_receiver_bic": t["cdtr_bic"],
            "32A_ccy": t["ccy"],
            "32A_amount": f"{t['amount'] * 100:.2f}",     # interbank sizes
            "52A": t["dbtr_bic"],
            "57A": rng.choice(BICS),
            "58A": t["cdtr_bic"],
            "_mt_type": "202",
            "_format": "mt",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type", "32A_ccy"]}
    return records, binding


def _mtamt(v):
    return f"{v:.2f}".replace(".", ",")


def _d6(ts):
    return ts.strftime("%y%m%d")


def simulate_mt202cov(n_messages=800, seed=42):
    """MT202 COV — cover payment: MT202 plus underlying customer data."""
    records, binding = simulate_mt202(n_messages, seed)
    rng = random.Random(seed + 1)
    for r in records:
        t = _base_tx(rng, 0)
        r["119"] = "COV"
        r["50K_acct"] = t["dbtr_acct"]
        r["50K_name"] = t["dbtr_name"]
        r["59_acct"] = t["cdtr_acct"]
        r["59_name"] = t["cdtr_name"]
    binding = dict(binding, state_fields=["_mt_type", "119"])
    return records, binding


def simulate_mt940(n_messages=400, seed=42, mt_type="940"):
    """MT940 — customer statement with interleaved 61/86 entry lines."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        opening = round(rng.lognormvariate(10, 1), 2)
        rec = {
            "_msg_id": f"STMT{i:08d}",
            "21": f"REL{rng.randrange(10**8):08d}",
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"], "_receiver_bic": t["cdtr_bic"],
            "25": t["dbtr_acct"],
            "28C": f"{rng.randint(1, 366)}/1",
            "_mt_type": mt_type, "_format": "mt",
        }
        if mt_type == "942":
            rec["13D"] = t["ts"].strftime("%y%m%d%H%M+0800")
            rec["34F"] = f"{t['ccy']}0,"
        else:
            rec["60F_dc"] = "C"
            rec["60F_ccy"] = t["ccy"]
            rec["60F_amount"] = f"{opening:.2f}"
        bal = opening
        for e in range(1, rng.randint(2, 5)):
            amt = round(rng.lognormvariate(6, 1.2), 2)
            dc = rng.choices(["D", "C"], weights=[6, 4])[0]
            bal += amt if dc == "C" else -amt
            suffix = "" if e == 1 else f"#{e}"
            rec[f"61{suffix}"] = (f"{_d6(t['ts'])}{_d6(t['ts'])[2:]}{dc}"
                                  f"{_mtamt(amt)}NTRFNONREF")
            rec[f"86{suffix}"] = (f"/REF/E2E{rng.randrange(10**8):08d}/ "
                                  f"{rng.choice(['SALARY', 'INVOICE', 'RENT', 'SUPPLIER', 'FEES'])}")
        if mt_type == "942":
            rec["90D"] = f"{rng.randint(1, 9)}{t['ccy']}{_mtamt(abs(bal - opening))}"
        else:
            rec["62F_dc"] = "C"
            rec["62F_ccy"] = t["ccy"]
            rec["62F_amount"] = f"{max(bal, 0):.2f}"
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type"]}
    return records, binding


def simulate_mt942(n_messages=400, seed=42):
    """MT942 — interim transaction report."""
    return simulate_mt940(n_messages, seed, mt_type="942")


def simulate_mt900(n_messages=800, seed=42, mt_type="900"):
    """MT900 debit confirmation / MT910 credit confirmation."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        rec = {
            "_msg_id": t["ref"],
            "21": f"REL{rng.randrange(10**8):08d}",
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"], "_receiver_bic": t["cdtr_bic"],
            "25": t["dbtr_acct"],
            "32A_ccy": t["ccy"], "32A_amount": f"{t['amount']:.2f}",
            "72": f"/BNF/{t['cdtr_name']}",
            "_mt_type": mt_type, "_format": "mt",
        }
        if mt_type == "910":
            rec["50K_acct"] = t["dbtr_acct"]
            rec["50K_name"] = t["dbtr_name"]
            rec["52A"] = t["dbtr_bic"]
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type"]}
    return records, binding


def simulate_mt910(n_messages=800, seed=42):
    return simulate_mt900(n_messages, seed, mt_type="910")


def simulate_mt101(n_messages=800, seed=42):
    """MT101 — request for transfer (payment initiation)."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            "_msg_id": t["ref"],
            "21": f"TXN{rng.randrange(10**8):08d}",
            "28D": "1/1",
            "50H_acct": t["dbtr_acct"], "50H_name": t["dbtr_name"],
            "30": _d6(t["ts"]),
            "32B": f"{t['ccy']}{_mtamt(t['amount'])}",
            "57A": t["cdtr_bic"],
            "59_acct": t["cdtr_acct"], "59_name": t["cdtr_name"],
            "70": f"INVOICE {rng.randrange(99999):05d}",
            "71A": rng.choices(["OUR", "SHA", "BEN"], weights=[6, 3, 1])[0],
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"], "_receiver_bic": t["cdtr_bic"],
            "_mt_type": "101", "_format": "mt",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type", "71A"]}
    return records, binding


def simulate_mt199(n_messages=600, seed=42):
    """MT199 — free-format message (queries, cancellations, gpi updates)."""
    rng = random.Random(seed)
    topics = ["/QRY/ STATUS OF PAYMENT UNDER REF",
              "/CANC/ REQUEST CANCELLATION OF",
              "/RETN/ FUNDS RETURNED FOR",
              "/UPDT/ GPI TRACKER UPDATE FOR",
              "/INQR/ BENEFICIARY DETAILS REQUIRED FOR"]
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            "_msg_id": t["ref"],
            "21": f"REL{rng.randrange(10**8):08d}",
            "79": f"{rng.choice(topics)} REF{rng.randrange(10**8):08d}",
            "_ts": t["ts"].strftime("%Y-%m-%dT00:00:00"),
            "_sender_bic": t["dbtr_bic"], "_receiver_bic": t["cdtr_bic"],
            "_mt_type": "199", "_format": "mt",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_mt_type"]}
    return records, binding


PACS9_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08"
P9 = "FICdtTrf"


def simulate_pacs009(n_messages=800, seed=42, cover=False):
    """pacs.009 — FI credit transfer; cover=True adds underlying customer data."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        rec = {
            f"{P9}.GrpHdr.MsgId": f"MSG{t['ref'][3:]}",
            f"{P9}.GrpHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{P9}.GrpHdr.NbOfTxs": "1",
            f"{P9}.GrpHdr.SttlmInf.SttlmMtd": "INDA",
            f"{P9}.CdtTrfTxInf.PmtId.EndToEndId": t["ref"],
            f"{P9}.CdtTrfTxInf.PmtId.UETR": str(uuid.UUID(int=rng.getrandbits(128))),
            f"{P9}.CdtTrfTxInf.IntrBkSttlmAmt": f"{t['amount'] * 100:.2f}",
            f"{P9}.CdtTrfTxInf.IntrBkSttlmAmt@Ccy": t["ccy"],
            f"{P9}.CdtTrfTxInf.Dbtr.FinInstnId.BICFI": t["dbtr_bic"],
            f"{P9}.CdtTrfTxInf.Cdtr.FinInstnId.BICFI": t["cdtr_bic"],
            "_msg_type": "pacs.009.001.08", "_ns": PACS9_NS,
            "_msg_id": f"MSG{t['ref'][3:]}",
            "_ts": t["ts"].isoformat(timespec="seconds"), "_format": "mx",
        }
        if cover:
            u = f"{P9}.CdtTrfTxInf.UndrlygCstmrCdtTrf"
            rec[f"{u}.Dbtr.Nm"] = t["dbtr_name"]
            rec[f"{u}.DbtrAcct.Id.Othr.Id"] = t["dbtr_acct"]
            rec[f"{u}.Cdtr.Nm"] = t["cdtr_name"]
            rec[f"{u}.CdtrAcct.Id.Othr.Id"] = t["cdtr_acct"]
            rec[f"{u}.RmtInf.Ustrd"] = f"INVOICE {rng.randrange(99999):05d}"
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type", f"{P9}.CdtTrfTxInf.IntrBkSttlmAmt@Ccy"]}
    return records, binding


def simulate_pacs009cov(n_messages=800, seed=42):
    return simulate_pacs009(n_messages, seed, cover=True)


def _camt_entry(rng, prefix, t):
    return {
        f"{prefix}.Amt": f"{round(rng.lognormvariate(6, 1.2), 2):.2f}",
        f"{prefix}.Amt@Ccy": t["ccy"],
        f"{prefix}.CdtDbtInd": rng.choices(["DBIT", "CRDT"], weights=[6, 4])[0],
        f"{prefix}.Sts.Cd": "BOOK",
        f"{prefix}.BookgDt.Dt": t["ts"].strftime("%Y-%m-%d"),
        f"{prefix}.AcctSvcrRef": f"ASR{rng.randrange(10**9):09d}",
        f"{prefix}.AddtlNtryInf": rng.choice(
            ["SALARY", "INVOICE", "RENT", "SUPPLIER", "FEES"]),
    }


def simulate_camt053(n_messages=400, seed=42, root="BkToCstmrStmt",
                     block="Stmt", msg_type="camt.053.001.08"):
    """camt.053 statement (also parameterized for camt.052 interim report)."""
    rng = random.Random(seed)
    ns = f"urn:iso:std:iso:20022:tech:xsd:{msg_type}"
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        opening = round(rng.lognormvariate(10, 1), 2)
        rec = {
            f"{root}.GrpHdr.MsgId": f"STMT{i:08d}",
            f"{root}.GrpHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{root}.{block}.Id": f"{block.upper()}{i:08d}",
            f"{root}.{block}.Acct.Id.Othr.Id": t["dbtr_acct"],
            f"{root}.{block}.Bal#1.Tp.CdOrPrtry.Cd": "OPBD" if block == "Stmt" else "ITBD",
            f"{root}.{block}.Bal#1.Amt": f"{opening:.2f}",
            f"{root}.{block}.Bal#1.Amt@Ccy": t["ccy"],
            f"{root}.{block}.Bal#1.CdtDbtInd": "CRDT",
            f"{root}.{block}.Bal#2.Tp.CdOrPrtry.Cd": "CLBD" if block == "Stmt" else "ITBD",
            f"{root}.{block}.Bal#2.Amt": f"{round(opening * rng.uniform(0.8, 1.2), 2):.2f}",
            f"{root}.{block}.Bal#2.Amt@Ccy": t["ccy"],
            f"{root}.{block}.Bal#2.CdtDbtInd": "CRDT",
            "_msg_type": msg_type, "_ns": ns,
            "_msg_id": f"STMT{i:08d}",
            "_ts": t["ts"].isoformat(timespec="seconds"), "_format": "mx",
        }
        for e in range(1, rng.randint(3, 5)):
            rec.update(_camt_entry(rng, f"{root}.{block}.Ntry#{e}", t))
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type"]}
    return records, binding


def simulate_camt052(n_messages=400, seed=42):
    return simulate_camt053(n_messages, seed, root="BkToCstmrAcctRpt",
                            block="Rpt", msg_type="camt.052.001.08")


def simulate_camt054(n_messages=800, seed=42):
    """camt.054 — debit/credit notification (MT900/MT910 equivalent)."""
    rng = random.Random(seed)
    root, msg_type = "BkToCstmrDbtCdtNtfctn", "camt.054.001.08"
    ns = f"urn:iso:std:iso:20022:tech:xsd:{msg_type}"
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        rec = {
            f"{root}.GrpHdr.MsgId": f"NTFN{i:08d}",
            f"{root}.GrpHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{root}.Ntfctn.Id": f"NTF{i:08d}",
            f"{root}.Ntfctn.Acct.Id.Othr.Id": t["dbtr_acct"],
            f"{root}.Ntfctn.Ntry.Amt": f"{t['amount']:.2f}",
            f"{root}.Ntfctn.Ntry.Amt@Ccy": t["ccy"],
            f"{root}.Ntfctn.Ntry.CdtDbtInd": rng.choices(["DBIT", "CRDT"],
                                                         weights=[5, 5])[0],
            f"{root}.Ntfctn.Ntry.Sts.Cd": "BOOK",
            f"{root}.Ntfctn.Ntry.BookgDt.Dt": t["ts"].strftime("%Y-%m-%d"),
            f"{root}.Ntfctn.Ntry.NtryDtls.TxDtls.Refs.EndToEndId": t["ref"],
            "_msg_type": msg_type, "_ns": ns,
            "_msg_id": f"NTFN{i:08d}",
            "_ts": t["ts"].isoformat(timespec="seconds"), "_format": "mx",
        }
        records.append(rec)
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type", f"{root}.Ntfctn.Ntry.CdtDbtInd"]}
    return records, binding


def simulate_camt998(n_messages=600, seed=42):
    """camt.998 — proprietary/free-format message (MT199 equivalent)."""
    rng = random.Random(seed)
    root, msg_type = "PrtryMsg", "camt.998.001.03"
    ns = f"urn:iso:std:iso:20022:tech:xsd:{msg_type}"
    topics = ["PAYMENT STATUS QUERY", "CANCELLATION REQUEST",
              "FUNDS RETURN ADVICE", "GPI TRACKER UPDATE",
              "BENEFICIARY DETAILS REQUEST"]
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            f"{root}.MsgHdr.MsgId": f"FREE{i:08d}",
            f"{root}.MsgHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{root}.Rltd.Ref": t["ref"],
            f"{root}.PrtryData.Tp": "FREE_FORMAT",
            f"{root}.PrtryData.Data.Nrrtv": f"{rng.choice(topics)} "
                                            f"REF{rng.randrange(10**8):08d}",
            "_msg_type": msg_type, "_ns": ns,
            "_msg_id": f"FREE{i:08d}",
            "_ts": t["ts"].isoformat(timespec="seconds"), "_format": "mx",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type"]}
    return records, binding


PAIN_NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
Q = "CstmrCdtTrfInitn"


def simulate_pain001(n_messages=800, seed=42):
    """pain.001 — customer credit transfer initiation."""
    rng = random.Random(seed)
    records = []
    for i in range(n_messages):
        t = _base_tx(rng, i)
        records.append({
            f"{Q}.GrpHdr.MsgId": f"PAIN{t['ref'][3:]}",
            f"{Q}.GrpHdr.CreDtTm": t["ts"].isoformat(timespec="seconds"),
            f"{Q}.GrpHdr.NbOfTxs": "1",
            f"{Q}.GrpHdr.InitgPty.Nm": t["dbtr_name"],
            f"{Q}.PmtInf.PmtInfId": f"BATCH{rng.randrange(10**6):06d}",
            f"{Q}.PmtInf.PmtMtd": "TRF",
            f"{Q}.PmtInf.ReqdExctnDt.Dt": t["ts"].strftime("%Y-%m-%d"),
            f"{Q}.PmtInf.Dbtr.Nm": t["dbtr_name"],
            f"{Q}.PmtInf.DbtrAcct.Id.IBAN": t["dbtr_acct"],
            f"{Q}.PmtInf.DbtrAgt.FinInstnId.BICFI": t["dbtr_bic"],
            f"{Q}.PmtInf.CdtTrfTxInf.PmtId.EndToEndId": t["ref"],
            f"{Q}.PmtInf.CdtTrfTxInf.Amt.InstdAmt": f"{t['amount']:.2f}",
            f"{Q}.PmtInf.CdtTrfTxInf.Amt.InstdAmt@Ccy": t["ccy"],
            f"{Q}.PmtInf.CdtTrfTxInf.Cdtr.Nm": t["cdtr_name"],
            f"{Q}.PmtInf.CdtTrfTxInf.CdtrAcct.Id.IBAN": t["cdtr_acct"],
            f"{Q}.PmtInf.CdtTrfTxInf.RmtInf.Ustrd": f"INVOICE {rng.randrange(99999):05d}",
            "_msg_type": "pain.001.001.09",
            "_ns": PAIN_NS,
            "_msg_id": f"PAIN{t['ref'][3:]}",
            "_ts": t["ts"].isoformat(timespec="seconds"),
            "_format": "mx",
        })
    binding = {"entity_key": "_msg_id", "timestamp_field": "_ts",
               "state_fields": ["_msg_type", f"{Q}.PmtInf.CdtTrfTxInf.Amt.InstdAmt@Ccy"]}
    return records, binding
