"""End-to-end payment flow simulator behind the Studio's "E2E Test" tab.

Models one payment traveling through a chain of systems as ISO 20022
messages, keeping an explicit field-level lineage at every hop so the UI
can show exactly how a message was transformed system-to-system. Four flow
scenarios, selected from one dropdown:

  outward         IBNK Channel --pain.001--> Payment Processing --pacs.008-->
                  CoreBanking --pacs.008--> Clearing & Settlement
                  (customer-initiated payment going out)
  inward          Regulator/FI --pacs.008--> Payment Processing --pacs.008-->
                  CoreBanking --pacs.008--> Realtime Notification (to channels)
                  (payment received from the regulator / another FI)
  outward_return  Regulator/FI --pacs.008--> Payment Processing --pacs.008-->
                  CoreBanking --pacs.008--> Realtime Notification (to channels)
                  (the beneficiary-side return of a previous outward payment)
  inward_return   IBNK Channel --pacs.004--> Payment Processing --pacs.004-->
                  CoreBanking --pacs.004--> Clearing & Settlement
                  (returning a previously credited inward payment)

Every scenario's forward chain ends with a pacs.002 status report generated
by the last system in the chain and relayed back, hop by hop, to the first.

Three ways to seed the run (`source`):
  synthetic  - every message is freshly generated (default)
  file       - one or more systems' forward message is a caller-supplied
               parsed ISO 20022 record (`overrides`); any system without an
               override still derives its message from the previous hop
  database   - the origin system's payment instruction (account / amount /
               currency) is pulled from a configured system database table
               instead of being randomly generated; the rest of the chain is
               still simulated, since only the origination system typically
               has a queryable instruction store in this kind of test
"""

import csv
import json
import random
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fingerprints.messages import BICS, FIRST, LAST

SYSTEMS = [
    {"id": "ibnk-channel", "name": "IBNK Channel", "role": "channel"},
    {"id": "payment-processing", "name": "Payment Processing System", "role": "processing"},
    {"id": "corebanking", "name": "CoreBanking", "role": "corebanking"},
    {"id": "clearing-settlement", "name": "Clearing & Settlement", "role": "clearing"},
    {"id": "regulator-fi", "name": "Regulator / FI", "role": "external"},
    {"id": "realtime-notification", "name": "Realtime Notification", "role": "notification"},
]
_SYS_BY_ID = {s["id"]: s for s in SYSTEMS}

ROOTS = {
    "pain.001.001.09": "CstmrCdtTrfInitn",
    "pacs.008.001.08": "FIToFICstmrCdtTrf",
    "pacs.002.001.10": "FIToFIPmtStsRpt",
    "pacs.004.001.09": "PmtRtr",
}
_NS = {mt: f"urn:iso:std:iso:20022:tech:xsd:{mt}" for mt in ROOTS}

SCENARIOS = {
    "outward": {
        "label": "Outward payment — customer-initiated (pain.001 → pacs.008)",
        "chain": ["ibnk-channel", "payment-processing", "corebanking", "clearing-settlement"],
        "origin_message_type": "pain.001.001.09",
    },
    "inward": {
        "label": "Inward payment — Regulator/FI → Payment Processing → CoreBanking → realtime notification (pacs.008)",
        "chain": ["regulator-fi", "payment-processing", "corebanking", "realtime-notification"],
        "origin_message_type": "pacs.008.001.08",
    },
    "outward_return": {
        "label": "Outward return — Regulator/FI → Payment Processing → CoreBanking → realtime notification (pacs.008)",
        "chain": ["regulator-fi", "payment-processing", "corebanking", "realtime-notification"],
        "origin_message_type": "pacs.008.001.08",
    },
    "inward_return": {
        "label": "Inward return — returning a previously credited payment (pacs.004)",
        "chain": ["ibnk-channel", "payment-processing", "corebanking", "clearing-settlement"],
        "origin_message_type": "pacs.004.001.09",
    },
}

# per-system supplementary field each hop stamps onto a relayed message, so
# clicking a system always shows at least one thing it added.
_ENRICH_SUFFIX = {
    "ibnk-channel": "ChannelRef",
    "payment-processing": "PmtProcRef",
    "corebanking": "CoreBankingTxnId",
    "regulator-fi": "RegulatorRef",
    "realtime-notification": "ChannelNotifRef",
    "clearing-settlement": "ClrSysRef",
}


# --------------------------------------------------------------- utilities

def _root(msg):
    """First dotted path segment of any non-metadata field = the message's
    root segment name (works for any ISO 20022 message, ours or uploaded)."""
    for k in msg:
        if not k.startswith("_"):
            return k.split(".")[0]
    return None


def _find(msg, *suffixes):
    for suf in suffixes:
        for k, v in msg.items():
            if k.endswith(suf):
                return k, v
    return None, None


def _seed_party(rng):
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
        "ts": datetime(2026, 7, 1) + timedelta(seconds=rng.uniform(0, 14 * 86400)),
        "ref": f"E2E{rng.randrange(10**9):09d}",
    }


def _apply_overrides(p, account, amount, ccy, our_side):
    if account:
        p[our_side] = account
    if amount is not None:
        p["amount"] = amount
    if ccy:
        p["ccy"] = ccy
    return p


# ------------------------------------------------------------- origination

def build_pain001(rng, account=None, amount=None, ccy=None):
    """IBNK Channel: customer-initiated outward payment instruction."""
    p = _apply_overrides(_seed_party(rng), account, amount, ccy, "dbtr_acct")
    root = ROOTS["pain.001.001.09"]
    ts = p["ts"].isoformat(timespec="seconds")
    msg = {
        f"{root}.GrpHdr.MsgId": f"PAIN{p['ref']}",
        f"{root}.GrpHdr.CreDtTm": ts,
        f"{root}.GrpHdr.NbOfTxs": "1",
        f"{root}.GrpHdr.InitgPty.Nm": p["dbtr_name"],
        f"{root}.PmtInf.PmtInfId": f"BATCH{rng.randrange(10**6):06d}",
        f"{root}.PmtInf.PmtMtd": "TRF",
        f"{root}.PmtInf.ReqdExctnDt.Dt": p["ts"].strftime("%Y-%m-%d"),
        f"{root}.PmtInf.Dbtr.Nm": p["dbtr_name"],
        f"{root}.PmtInf.DbtrAcct.Id.IBAN": p["dbtr_acct"],
        f"{root}.PmtInf.DbtrAgt.FinInstnId.BICFI": p["dbtr_bic"],
        f"{root}.PmtInf.CdtTrfTxInf.PmtId.EndToEndId": p["ref"],
        f"{root}.PmtInf.CdtTrfTxInf.Amt.InstdAmt": f"{p['amount']:.2f}",
        f"{root}.PmtInf.CdtTrfTxInf.Amt.InstdAmt@Ccy": p["ccy"],
        f"{root}.PmtInf.CdtTrfTxInf.Cdtr.Nm": p["cdtr_name"],
        f"{root}.PmtInf.CdtTrfTxInf.CdtrAcct.Id.IBAN": p["cdtr_acct"],
        f"{root}.PmtInf.CdtTrfTxInf.RmtInf.Ustrd": f"INVOICE {rng.randrange(99999):05d}",
        "_msg_type": "pain.001.001.09", "_ns": _NS["pain.001.001.09"],
        "_msg_id": f"PAIN{p['ref']}", "_ts": ts, "_format": "mx",
    }
    return msg


def build_pacs008_origin(rng, account=None, amount=None, ccy=None):
    """Regulator/FI: an inward credit transfer (or its return) received from
    an external regulator/institution, crediting our synthetic account."""
    p = _apply_overrides(_seed_party(rng), account, amount, ccy, "cdtr_acct")
    root = ROOTS["pacs.008.001.08"]
    ts = p["ts"].isoformat(timespec="seconds")
    uetr = str(uuid.UUID(int=rng.getrandbits(128)))
    msg = {
        f"{root}.GrpHdr.MsgId": f"MSG{p['ref']}",
        f"{root}.GrpHdr.CreDtTm": ts,
        f"{root}.GrpHdr.NbOfTxs": "1",
        f"{root}.GrpHdr.SttlmInf.SttlmMtd": "CLRG",
        f"{root}.CdtTrfTxInf.PmtId.EndToEndId": p["ref"],
        f"{root}.CdtTrfTxInf.PmtId.UETR": uetr,
        f"{root}.CdtTrfTxInf.IntrBkSttlmAmt": f"{p['amount']:.2f}",
        f"{root}.CdtTrfTxInf.IntrBkSttlmAmt@Ccy": p["ccy"],
        f"{root}.CdtTrfTxInf.ChrgBr": "SLEV",
        f"{root}.CdtTrfTxInf.Dbtr.Nm": p["dbtr_name"],
        f"{root}.CdtTrfTxInf.DbtrAcct.Id.Othr.Id": p["dbtr_acct"],
        f"{root}.CdtTrfTxInf.DbtrAgt.FinInstnId.BICFI": p["dbtr_bic"],
        f"{root}.CdtTrfTxInf.Cdtr.Nm": p["cdtr_name"],
        f"{root}.CdtTrfTxInf.CdtrAcct.Id.Othr.Id": p["cdtr_acct"],
        f"{root}.CdtTrfTxInf.CdtrAgt.FinInstnId.BICFI": p["cdtr_bic"],
        f"{root}.CdtTrfTxInf.RmtInf.Ustrd": f"INVOICE {rng.randrange(99999):05d}",
        "_msg_type": "pacs.008.001.08", "_ns": _NS["pacs.008.001.08"],
        "_msg_id": f"MSG{p['ref']}", "_ts": ts, "_format": "mx",
    }
    return msg


def build_pacs004_origin(rng, account=None, amount=None, ccy=None):
    """A payment return (either leg): funds are being sent back to the
    account named by `account` (the synthetic account for this scenario)."""
    p = _apply_overrides(_seed_party(rng), account, amount, ccy, "dbtr_acct")
    root = ROOTS["pacs.004.001.09"]
    ts = p["ts"].isoformat(timespec="seconds")
    orig_ref = f"REF{rng.randrange(10**8):08d}"
    orig_uetr = str(uuid.UUID(int=rng.getrandbits(128)))
    msg = {
        f"{root}.GrpHdr.MsgId": f"RTRN{p['ref']}",
        f"{root}.GrpHdr.CreDtTm": ts,
        f"{root}.TxInf.RtrId": f"RTR{rng.randrange(10**8):08d}",
        f"{root}.TxInf.OrgnlEndToEndId": orig_ref,
        f"{root}.TxInf.OrgnlUETR": orig_uetr,
        f"{root}.TxInf.OrgnlInstdAmt": f"{p['amount']:.2f}",
        f"{root}.TxInf.OrgnlInstdAmt@Ccy": p["ccy"],
        f"{root}.TxInf.RtrdInstdAmt": f"{p['amount']:.2f}",
        f"{root}.TxInf.RtrdInstdAmt@Ccy": p["ccy"],
        f"{root}.TxInf.RtrRsnInf.Rsn.Cd": rng.choice(["AC04", "AM04", "MS03"]),
        f"{root}.TxInf.RtrChain.Dbtr.Nm": p["dbtr_name"],
        f"{root}.TxInf.RtrChain.DbtrAcct.Id.Othr.Id": p["dbtr_acct"],
        "_msg_type": "pacs.004.001.09", "_ns": _NS["pacs.004.001.09"],
        "_msg_id": f"RTRN{p['ref']}", "_ts": ts, "_format": "mx",
    }
    return msg


_ORIGIN_BUILDERS = {
    "pain.001.001.09": build_pain001,
    "pacs.008.001.08": build_pacs008_origin,
    "pacs.004.001.09": build_pacs004_origin,
}


# --------------------------------------------------------------- transforms

def pain001_to_pacs008(prev, rng):
    """Payment Processing System: turns a customer initiation into an
    interbank credit transfer — the one hop with a real schema mapping."""
    root_from = _root(prev)
    root_to = ROOTS["pacs.008.001.08"]

    def g(suffix):
        return prev.get(f"{root_from}.{suffix}")

    ts = prev.get(f"{root_from}.GrpHdr.CreDtTm") or prev.get("_ts")
    uetr = str(uuid.UUID(int=rng.getrandbits(128)))
    msg_id = f"PPS{rng.randrange(10**10):010d}"

    field_map = [
        ("PmtInf.CdtTrfTxInf.PmtId.EndToEndId", "CdtTrfTxInf.PmtId.EndToEndId"),
        ("PmtInf.Dbtr.Nm", "CdtTrfTxInf.Dbtr.Nm"),
        ("PmtInf.DbtrAcct.Id.IBAN", "CdtTrfTxInf.DbtrAcct.Id.Othr.Id"),
        ("PmtInf.DbtrAgt.FinInstnId.BICFI", "CdtTrfTxInf.DbtrAgt.FinInstnId.BICFI"),
        ("PmtInf.CdtTrfTxInf.Cdtr.Nm", "CdtTrfTxInf.Cdtr.Nm"),
        ("PmtInf.CdtTrfTxInf.CdtrAcct.Id.IBAN", "CdtTrfTxInf.CdtrAcct.Id.Othr.Id"),
        ("PmtInf.CdtTrfTxInf.Amt.InstdAmt", "CdtTrfTxInf.IntrBkSttlmAmt"),
        ("PmtInf.CdtTrfTxInf.Amt.InstdAmt@Ccy", "CdtTrfTxInf.IntrBkSttlmAmt@Ccy"),
        ("PmtInf.CdtTrfTxInf.RmtInf.Ustrd", "CdtTrfTxInf.RmtInf.Ustrd"),
    ]

    new_msg = {
        f"{root_to}.GrpHdr.MsgId": msg_id,
        f"{root_to}.GrpHdr.CreDtTm": ts,
        f"{root_to}.GrpHdr.NbOfTxs": "1",
        f"{root_to}.GrpHdr.SttlmInf.SttlmMtd": "CLRG",
        f"{root_to}.CdtTrfTxInf.PmtId.UETR": uetr,
        f"{root_to}.CdtTrfTxInf.ChrgBr": "SLEV",
        "_msg_type": "pacs.008.001.08", "_ns": _NS["pacs.008.001.08"],
        "_msg_id": msg_id, "_ts": ts, "_format": "mx",
    }
    lineage = [
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.GrpHdr.MsgId", "to_value": msg_id, "kind": "generated"},
        {"from_field": f"{root_from}.GrpHdr.CreDtTm", "from_value": ts,
         "to_field": f"{root_to}.GrpHdr.CreDtTm", "to_value": ts, "kind": "mapped"},
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.CdtTrfTxInf.PmtId.UETR", "to_value": uetr, "kind": "generated"},
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.CdtTrfTxInf.ChrgBr", "to_value": "SLEV", "kind": "generated"},
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.GrpHdr.SttlmInf.SttlmMtd", "to_value": "CLRG", "kind": "generated"},
    ]
    for src_suf, dst_suf in field_map:
        v = g(src_suf)
        if v is not None:
            new_msg[f"{root_to}.{dst_suf}"] = v
        lineage.append({
            "from_field": f"{root_from}.{src_suf}" if v is not None else None,
            "from_value": v, "to_field": f"{root_to}.{dst_suf}", "to_value": v,
            "kind": "mapped" if v is not None else "missing",
        })
    return new_msg, lineage


def enrich_passthrough(prev, rng, system_id):
    """A system that relays a message unchanged except for its own message
    id and one supplementary reference it stamps on (CoreBanking, Clearing &
    Settlement, and the return leg all use this)."""
    root = _root(prev)
    msg_id_key = f"{root}.GrpHdr.MsgId"
    new_msg, lineage = {}, []
    for k, v in prev.items():
        if k.startswith("_") or k == msg_id_key:
            continue
        new_msg[k] = v
        lineage.append({"from_field": k, "from_value": v, "to_field": k,
                         "to_value": v, "kind": "passthrough"})

    old_msg_id = prev.get(msg_id_key)
    tag = "".join(ch for ch in system_id.upper() if ch.isalpha())[:4]
    new_msg_id = f"{tag}{rng.randrange(10**8):08d}"
    new_msg[msg_id_key] = new_msg_id
    lineage.append({"from_field": msg_id_key, "from_value": old_msg_id,
                     "to_field": msg_id_key, "to_value": new_msg_id, "kind": "regenerated"})

    suffix = _ENRICH_SUFFIX.get(system_id, "SysRef")
    enrich_key = f"{root}.SplmtryData.{suffix}"
    enrich_val = f"{tag}{rng.randrange(10**8):08d}"
    new_msg[enrich_key] = enrich_val
    lineage.append({"from_field": None, "from_value": None, "to_field": enrich_key,
                     "to_value": enrich_val, "kind": "enriched"})

    for meta in ("_msg_type", "_ns", "_format", "_ts"):
        new_msg[meta] = prev.get(meta)
    new_msg["_msg_id"] = new_msg_id
    return new_msg, lineage


def to_pacs002(prev, rng, verdict="ACSC"):
    """The last system in the chain reports back on the original message —
    the response leg then relays this status backward hop by hop."""
    root_from = _root(prev)
    root_to = ROOTS["pacs.002.001.10"]
    e2e_key, e2e_val = _find(prev, ".EndToEndId", ".OrgnlEndToEndId")
    uetr_key, uetr_val = _find(prev, ".UETR", ".OrgnlUETR")
    orig_msgid_key = f"{root_from}.GrpHdr.MsgId"
    orig_msgid = prev.get(orig_msgid_key)
    ts = prev.get("_ts") or datetime.utcnow().isoformat(timespec="seconds")
    msg_id = f"STS{rng.randrange(10**10):010d}"

    new_msg = {
        f"{root_to}.GrpHdr.MsgId": msg_id,
        f"{root_to}.GrpHdr.CreDtTm": ts,
        f"{root_to}.TxInfAndSts.OrgnlGrpInf.OrgnlMsgId": orig_msgid,
        f"{root_to}.TxInfAndSts.OrgnlEndToEndId": e2e_val,
        f"{root_to}.TxInfAndSts.OrgnlUETR": uetr_val,
        f"{root_to}.TxInfAndSts.TxSts": verdict,
        f"{root_to}.TxInfAndSts.AccptncDtTm": ts,
        "_msg_type": "pacs.002.001.10", "_ns": _NS["pacs.002.001.10"],
        "_msg_id": msg_id, "_ts": ts, "_format": "mx",
    }
    lineage = [
        {"from_field": orig_msgid_key, "from_value": orig_msgid,
         "to_field": f"{root_to}.TxInfAndSts.OrgnlGrpInf.OrgnlMsgId",
         "to_value": orig_msgid, "kind": "mapped"},
        {"from_field": e2e_key, "from_value": e2e_val,
         "to_field": f"{root_to}.TxInfAndSts.OrgnlEndToEndId", "to_value": e2e_val, "kind": "mapped"},
        {"from_field": uetr_key, "from_value": uetr_val,
         "to_field": f"{root_to}.TxInfAndSts.OrgnlUETR", "to_value": uetr_val, "kind": "mapped"},
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.TxInfAndSts.TxSts", "to_value": verdict, "kind": "generated"},
        {"from_field": None, "from_value": None,
         "to_field": f"{root_to}.GrpHdr.MsgId", "to_value": msg_id, "kind": "generated"},
    ]
    return new_msg, lineage


def generic_diff_lineage(prev, new):
    """Used when a hop's message was supplied directly (file upload /
    database) instead of derived: a plain field-by-field diff against the
    previous hop, since there is no schema mapping to trace."""
    keys = list(dict.fromkeys(
        [k for k in prev if not k.startswith("_")] +
        [k for k in new if not k.startswith("_")]))
    lineage = []
    for k in keys:
        a, b = prev.get(k), new.get(k)
        if a is None and b is not None:
            lineage.append({"from_field": None, "from_value": None,
                             "to_field": k, "to_value": b, "kind": "added"})
        elif a is not None and b is None:
            lineage.append({"from_field": k, "from_value": a,
                             "to_field": None, "to_value": None, "kind": "removed"})
        elif a != b:
            lineage.append({"from_field": k, "from_value": a,
                             "to_field": k, "to_value": b, "kind": "changed"})
        else:
            lineage.append({"from_field": k, "from_value": a,
                             "to_field": k, "to_value": b, "kind": "passthrough"})
    return lineage


# ------------------------------------------------------------------ runner

def seed_from_db(dsn, table, account=None, amount=None, ccy=None):
    """Pull one row from a configured system database table to parameterize
    the origination message instead of generating it at random. Best-effort
    column mapping: falls back to the synthetic/explicit values for any
    column the table doesn't have."""
    from fingerprints.connectors import read_table
    rows = read_table(dsn, table, limit=1)
    if not rows:
        raise ValueError(f"table '{table}' returned no rows")
    row = rows[0]
    lower = {str(k).lower(): v for k, v in row.items()}
    resolved_account = account or lower.get("account") or lower.get("iban") \
        or lower.get("debtor_account") or lower.get("creditor_account")
    resolved_amount = amount if amount is not None else lower.get("amount")
    resolved_ccy = ccy or lower.get("currency") or lower.get("ccy")
    return resolved_account, resolved_amount, resolved_ccy, row


def resolve_systems(chain, custom_systems=None):
    """Merge the built-in registry with any caller-supplied intermediary
    systems (id/name/role), returning one entry per id actually used in
    `chain`, in chain order. Unknown ids with no definition get a generic
    'intermediary' entry so a fully custom chain still works."""
    registry = dict(_SYS_BY_ID)
    for cs in (custom_systems or []):
        sid = cs["id"]
        registry[sid] = {"id": sid, "name": cs.get("name", sid),
                          "role": cs.get("role", "intermediary")}
    for sid in chain:
        registry.setdefault(sid, {"id": sid, "name": sid, "role": "intermediary"})
    return [registry[sid] for sid in chain]


def _apply_edits(msg, lineage, field_edits):
    """Merge caller-supplied field edits onto an already-built message,
    appending lineage entries for what changed. Downstream hops derive from
    the edited message, so an edit cascades forward through the rest of the
    chain exactly like a real correction would.

    A field_edits value of None means "remove this optional tag" (a no-op if
    the field wasn't present); any other value sets the field, added as a
    new tag if it wasn't already there ("added") or corrected if it was
    ("edited")."""
    if not field_edits:
        return msg, lineage
    new_msg = dict(msg)
    edit_lineage = []
    for k, v in field_edits.items():
        was_present = k in msg
        if v is None:
            if was_present:
                new_msg.pop(k, None)
                edit_lineage.append({"from_field": k, "from_value": msg.get(k),
                                      "to_field": None, "to_value": None, "kind": "removed"})
            continue
        new_msg[k] = v
        edit_lineage.append({
            "from_field": k if was_present else None,
            "from_value": msg.get(k) if was_present else None,
            "to_field": k, "to_value": v,
            "kind": "edited" if was_present else "added",
        })
    return new_msg, (lineage or []) + edit_lineage


def run_pipeline(scenario="outward", source="synthetic", seed=None, account=None,
                  amount=None, ccy=None, overrides=None, db=None, verdict="ACSC",
                  chain=None, systems=None, edits=None, scenario_def=None):
    """Run one scenario end to end.

    scenario_def: optional {"label", "chain", "origin_message_type"} —
               defines a caller-supplied flow *type* on the fly (e.g. "FAST",
               "RTGS", "Book Transfer", "Telegraphic Transfer") instead of
               looking `scenario` up in the built-in SCENARIOS. `scenario` is
               still used as the flow's key/label in the result. This is the
               general mechanism: the four built-in scenarios are just
               pre-registered instances of the same {chain,
               origin_message_type} shape — a custom flow type is not
               special-cased anywhere else in this function.
    chain:     optional ordered list of system ids overriding the scenario's
               default chain — insert any number of intermediary systems
               anywhere in the flow (N systems, not just 4).
    systems:   optional [{id, name, role}] definitions for any custom ids
               used in `chain` that aren't one of the built-in SYSTEMS.
    overrides: {system_id: parsed_record} — forward-leg messages supplied by
               the caller (file upload / one row of a test dataset) instead
               of being derived.
    db:        {"dsn": ..., "table": ...} — used only when source=="database"
               to seed the origination message's account/amount/currency.
    edits:     {system_id: {"forward": {field: value}, "response": {field:
               value}}} — field-level corrections applied on top of a hop
               after it is built (derived, overridden, or dataset-sourced).
               Every hop downstream of an edited one re-derives from the
               edited message, so review-then-edit-then-verify works the
               same way for a synthetic run, an uploaded message, or a
               database-seeded one.
    """
    if scenario_def:
        origin_type = scenario_def["origin_message_type"]
        if origin_type not in _ORIGIN_BUILDERS:
            raise ValueError(
                f"unsupported origin_message_type '{origin_type}' — must be one of "
                f"{sorted(_ORIGIN_BUILDERS)}")
        cfg = {"label": scenario_def.get("label", scenario),
               "chain": list(scenario_def["chain"]),
               "origin_message_type": origin_type}
    else:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario '{scenario}'")
        cfg = SCENARIOS[scenario]
    chain = list(chain) if chain else list(cfg["chain"])
    if len(chain) < 2:
        raise ValueError("chain needs at least 2 systems (an origin and one hop)")
    if len(set(chain)) != len(chain):
        raise ValueError("chain has a repeated system id")
    system_defs = resolve_systems(chain, systems)
    role_of = {s["id"]: s["role"] for s in system_defs}

    overrides = overrides or {}
    edits = edits or {}
    rng = random.Random(seed)
    source_context = {"type": source}

    if source == "database":
        if not db or not db.get("dsn") or not db.get("table"):
            raise ValueError("database source requires 'dsn' and 'table'")
        resolved_account, resolved_amount, resolved_ccy, row = seed_from_db(
            db["dsn"], db["table"], account, amount, ccy)
        account, amount, ccy = resolved_account, resolved_amount, resolved_ccy
        source_context.update({"dsn": db["dsn"], "table": db["table"], "sample_row": row})

    # pain.001 -> pacs.008 must happen once, at whichever system actually
    # does payment processing; any intermediary before it (compliance check,
    # sanctions screening, ...) just relays the pain.001 message untouched.
    # Keyed off the origin message type (not the scenario name) so a custom
    # flow type that also starts from a customer instruction gets the same
    # conversion behavior as the built-in "outward" scenario.
    convert_at = None
    if cfg["origin_message_type"] == "pain.001.001.09":
        convert_at = next((i for i in range(1, len(chain))
                            if role_of[chain[i]] == "processing"), 1)

    hops = []
    origin_id = chain[0]
    if origin_id in overrides:
        origin_msg = overrides[origin_id]
    else:
        builder = _ORIGIN_BUILDERS[cfg["origin_message_type"]]
        origin_msg = builder(rng, account=account, amount=amount, ccy=ccy)
    origin_msg, origin_lineage = _apply_edits(
        origin_msg, None, edits.get(origin_id, {}).get("forward"))
    hops.append({"seq": 1, "system": origin_id, "direction": "forward",
                 "message": origin_msg, "lineage": origin_lineage})

    prev = origin_msg
    for i in range(1, len(chain)):
        sys_id = chain[i]
        if sys_id in overrides:
            new_msg = overrides[sys_id]
            lineage = generic_diff_lineage(prev, new_msg)
        elif i == convert_at:
            new_msg, lineage = pain001_to_pacs008(prev, rng)
        else:
            new_msg, lineage = enrich_passthrough(prev, rng, sys_id)
        new_msg, lineage = _apply_edits(new_msg, lineage, edits.get(sys_id, {}).get("forward"))
        hops.append({"seq": i + 1, "system": sys_id, "direction": "forward",
                     "message": new_msg, "lineage": lineage})
        prev = new_msg

    resp_msg, resp_lineage = to_pacs002(prev, rng, verdict=verdict)
    resp_msg, resp_lineage = _apply_edits(
        resp_msg, resp_lineage, edits.get(chain[-1], {}).get("response"))
    hops.append({"seq": len(hops) + 1, "system": chain[-1], "direction": "response",
                 "message": resp_msg, "lineage": resp_lineage})
    prev = resp_msg
    for i in range(len(chain) - 2, -1, -1):
        sys_id = chain[i]
        new_msg, lineage = enrich_passthrough(prev, rng, sys_id)
        new_msg, lineage = _apply_edits(new_msg, lineage, edits.get(sys_id, {}).get("response"))
        hops.append({"seq": len(hops) + 1, "system": sys_id, "direction": "response",
                     "message": new_msg, "lineage": lineage})
        prev = new_msg

    return {"scenario": scenario, "label": cfg["label"], "chain": chain,
            "systems": system_defs, "seed": seed, "source": source_context,
            "hops": hops}


def hops_by_system(result):
    """{system_id: {"forward": hop, "response": hop}} for the click-to-inspect UI."""
    by_sys = {s["id"]: {"forward": None, "response": None} for s in result["systems"]}
    for h in result["hops"]:
        by_sys[h["system"]][h["direction"]] = h
    return by_sys


MAX_BATCH = 50          # interactive preview cap — kept small for the browser UI
MAX_EXPORT = 200_000    # bulk file export cap — a sanity ceiling, not a UI limit


def iter_batch(count, datasets=None, overrides=None, seed_base=None, **kwargs):
    """Generator over `count` independent, uniquely-seeded payment flows —
    "N unique payment flows for testing and verification". Yields one flow
    at a time instead of collecting them all in memory, so this is what
    bulk export (tens of thousands of flows) uses; run_batch (below) is the
    small, in-memory version for live preview in the UI.

    datasets:  {system_id: [record, record, ...]} — a per-system test
               dataset (e.g. parsed from a multi-message uploaded file).
               Flow i uses dataset[system_id][i % len(dataset[system_id])]
               as that system's forward-leg message instead of deriving it
               end-to-end; a system with no dataset still derives its
               message from the previous hop as usual, so one batch can mix
               "test dataset" systems and "end-to-end derived" systems.
    Remaining kwargs (scenario, source, account, chain, systems, db,
    verdict, edits, scenario_def, ...) are passed through to run_pipeline
    for every flow.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    datasets = datasets or {}
    base_overrides = dict(overrides or {})
    for i in range(count):
        per_flow_overrides = dict(base_overrides)
        for sys_id, records in datasets.items():
            if records:
                per_flow_overrides[sys_id] = records[i % len(records)]
        seed_i = (seed_base + i) if seed_base is not None else None
        result = run_pipeline(seed=seed_i, overrides=per_flow_overrides, **kwargs)
        result["flow_index"] = i
        yield result


def run_batch(count=1, datasets=None, overrides=None, seed_base=None, **kwargs):
    """In-memory batch of up to MAX_BATCH flows, for the Studio's live
    preview. For larger volumes (e.g. 50,000 messages for bulk testing and
    verification), use export_flows (below) instead."""
    if count < 1 or count > MAX_BATCH:
        raise ValueError(f"count must be between 1 and {MAX_BATCH}")
    flows = list(iter_batch(count, datasets=datasets, overrides=overrides,
                             seed_base=seed_base, **kwargs))
    return {"count": count, "flows": flows}


def export_flows(out_dir, count=1, datasets=None, overrides=None, seed_base=None,
                  run_id=None, **kwargs):
    """Stream `count` end-to-end flows to disk instead of holding them in
    memory — "50K end-to-end system messages for testing and verification"
    is this function, not run_batch. Writes, under out_dir:

      e2e-<run_id>-messages.ndjson   one JSON line per hop message, tagged
                                      with flow_index/seq/system/direction
      e2e-<run_id>-index.csv         one row per flow (account, origin msg
                                      id, verdict) for quick lookup
      e2e-<run_id>-manifest.json     run config + message-type/verdict
                                      counts

    Used by both fingerprints/e2e_cli.py and the Studio's
    /api/v1/e2e/export, so the bulk-generation logic lives in one place.
    Remaining kwargs (scenario, source, account, chain, systems, db,
    verdict, scenario_def, ...) are passed through to run_pipeline for
    every flow. Raises ValueError for a bad count or pipeline config; the
    partial messages file is removed if generation fails partway through.
    """
    if count < 1 or count > MAX_EXPORT:
        raise ValueError(f"count must be between 1 and {MAX_EXPORT}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    messages_path = out_dir / f"e2e-{run_id}-messages.ndjson"
    index_path = out_dir / f"e2e-{run_id}-index.csv"
    manifest_path = out_dir / f"e2e-{run_id}-manifest.json"

    scenario = kwargs.get("scenario", "outward")
    index_rows, msg_type_counts, verdict_counts, n_hops = [], {}, {}, 0
    try:
        with open(messages_path, "w", encoding="utf-8") as mf:
            for result in iter_batch(count=count, datasets=datasets, overrides=overrides,
                                      seed_base=seed_base, **kwargs):
                origin = result["hops"][0]["message"]
                response = result["hops"][-1]["message"]
                for h in result["hops"]:
                    rec = {"flow_index": result["flow_index"], "seq": h["seq"],
                           "system": h["system"], "direction": h["direction"],
                           **h["message"]}
                    mf.write(json.dumps(rec, default=str) + "\n")
                    n_hops += 1
                    mt = h["message"].get("_msg_type")
                    msg_type_counts[mt] = msg_type_counts.get(mt, 0) + 1
                acct_key = next((k for k in origin
                                  if k.endswith("IBAN") or k.endswith("Othr.Id")), None)
                verdict_val = next((v for k, v in response.items()
                                     if k.endswith("TxSts")), None)
                verdict_counts[verdict_val] = verdict_counts.get(verdict_val, 0) + 1
                index_rows.append({
                    "flow_index": result["flow_index"], "scenario": result["scenario"],
                    "account": origin.get(acct_key) if acct_key else None,
                    "origin_msg_id": origin.get("_msg_id"), "verdict": verdict_val,
                })
    except Exception:
        messages_path.unlink(missing_ok=True)
        raise

    if index_rows:
        with open(index_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(index_rows[0].keys()))
            w.writeheader()
            w.writerows(index_rows)

    manifest = {
        "run_id": run_id, "scenario": scenario, "source": kwargs.get("source", "synthetic"),
        "count": count,
        "chain": kwargs.get("chain") or (kwargs.get("scenario_def") or {}).get("chain")
                 or SCENARIOS.get(scenario, {}).get("chain"),
        "seed_base": seed_base, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_hop_messages": n_hops, "message_type_counts": msg_type_counts,
        "verdict_counts": verdict_counts,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["files"] = {"messages_ndjson": str(messages_path), "index_csv": str(index_path),
                          "manifest_json": str(manifest_path)}
    return manifest
