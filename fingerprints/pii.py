"""PII detection: rule-based recognizers over sampled values plus field-name
hints. Produces per-field suggestions the Studio surfaces for one-click
confirmation. Production replacement: Presidio recognizers."""

import re

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{8,30}$")
PAN_RE = re.compile(r"^\d{13,19}$")
PHONE_RE = re.compile(r"^\+?\d{8,15}$")

NAME_HINTS = ("name", "customer", "holder", "beneficiary")
ACCOUNT_HINTS = ("account", "acct", "iban")
ID_HINTS = ("nric", "ssn", "national", "passport", "tax_id")


def _luhn_ok(s):
    total, alt = 0, False
    for ch in reversed(s):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _match_rate(values, pred):
    hits = sum(1 for v in values if pred(str(v)))
    return hits / len(values)


def scan_field(name, values):
    """Return (pii_type, confidence, suggested_policy) or None."""
    vals = [v for v in values if v not in (None, "")][:500]
    if not vals:
        return None
    lname = name.lower()

    rate = _match_rate(vals, IBAN_RE.match)
    if rate > 0.8:
        return ("IBAN", rate, {"policy": "mask", "keep_last": 4})
    rate = _match_rate(vals, lambda s: PAN_RE.match(s) and _luhn_ok(s))
    if rate > 0.8:
        return ("PAN", rate, {"policy": "mask", "keep_last": 4})
    rate = _match_rate(vals, EMAIL_RE.match)
    if rate > 0.8:
        return ("EMAIL", rate, {"policy": "hash"})
    rate = _match_rate(vals, UUID_RE.match)
    if rate > 0.8:
        return ("UUID", rate, {"policy": "synthesize"})

    if any(h in lname for h in ID_HINTS):
        return ("NATIONAL_ID", 0.9, {"policy": "hash"})
    if any(h in lname for h in ACCOUNT_HINTS):
        # values must actually look like account numbers, so that fields like
        # acct_type ("SAVINGS") are not misflagged
        uniq = len(set(map(str, vals))) / len(vals)
        avg_len = sum(len(str(v)) for v in vals) / len(vals)
        if uniq > 0.5 and avg_len >= 8:
            return ("ACCOUNT", 0.85, {"policy": "mask", "keep_last": 4})
    if any(h in lname for h in NAME_HINTS) or lname.endswith(".nm") or lname.endswith("_name"):
        alpha = _match_rate(
            vals, lambda s: bool(re.match(r"^[A-Za-z][A-Za-z .'-]+$", s))
        )
        if alpha > 0.7:
            # message fields keep wire format: pseudonymize instead of drop
            in_message = lname.endswith(".nm") or bool(re.match(r"^\d+[a-z]?_name$", lname))
            policy = {"policy": "synthesize"} if in_message else {"policy": "drop"}
            return ("PERSON", round(alpha, 2), policy)
    rate = _match_rate(vals, PHONE_RE.match)
    if rate > 0.8 and any(h in lname for h in ("phone", "mobile", "msisdn")):
        return ("PHONE", rate, {"policy": "mask", "keep_last": 3})
    return None


def suggest_policies(records, fields=None):
    """Scan a record sample; return {field: {pii_type, confidence, suggested}}."""
    if not records:
        return {}
    fields = fields or list(records[0].keys())
    sample = records[:2000]
    out = {}
    for f in fields:
        hit = scan_field(f, [r.get(f) for r in sample])
        if hit:
            pii_type, conf, policy = hit
            out[f] = {
                "pii_type": pii_type,
                "confidence": round(conf, 2),
                "suggested": policy,
            }
    return out
