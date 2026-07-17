"""Source connectors and sinks. Phase 1: CSV/NDJSON files and PostgreSQL (any
SQLAlchemy URL works; PostgreSQL is the tested target)."""

import csv
import io
import json


# ------------------------------------------------------------------- files

def read_csv(path_or_text, is_text=False):
    if is_text:
        f = io.StringIO(path_or_text)
        return _read_csv_stream(f)
    with open(path_or_text, newline="", encoding="utf-8-sig") as f:
        return _read_csv_stream(f)


def _read_csv_stream(f):
    out = []
    for row in csv.DictReader(f):
        out.append({k: _coerce(v) for k, v in row.items()})
    return out


def read_ndjson(path_or_text, is_text=False):
    text = path_or_text if is_text else open(path_or_text, encoding="utf-8").read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def write_csv(records, path):
    if not records:
        return path
    cols = {}
    for r in records:
        for k in r:
            cols.setdefault(k)
    cols = list(cols)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    return path


def _coerce(v):
    if v is None or v == "":
        return v
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    except ValueError:
        return v


# --------------------------------------------------------------------- sql

def read_table(dsn, table, limit=500000):
    from sqlalchemy import create_engine, text
    engine = create_engine(dsn)
    safe_table = "".join(c for c in table if c.isalnum() or c in "._")
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT * FROM {safe_table} LIMIT :n'), {"n": int(limit)}
        )
        cols = list(rows.keys())
        return [dict(zip(cols, r)) for r in rows]


def list_tables(dsn):
    from sqlalchemy import create_engine, inspect
    engine = create_engine(dsn)
    return inspect(engine).get_table_names()


def describe_tables(dsn):
    """Tables with row counts plus parent/child foreign-key relationships."""
    from sqlalchemy import create_engine, inspect, text
    engine = create_engine(dsn)
    insp = inspect(engine)
    names = insp.get_table_names()
    tables = {n: {"name": n, "rows": 0, "pk": None,
                  "parents": [], "children": []} for n in names}
    with engine.connect() as conn:
        for n in names:
            safe = "".join(c for c in n if c.isalnum() or c in "._")
            try:
                tables[n]["rows"] = conn.execute(
                    text(f"SELECT count(*) FROM {safe}")).scalar()
            except Exception:
                pass
            pk = insp.get_pk_constraint(n)
            cols = pk.get("constrained_columns") or []
            tables[n]["pk"] = cols[0] if cols else None
    for n in names:
        for fk in insp.get_foreign_keys(n):
            ref = fk.get("referred_table")
            col = (fk.get("constrained_columns") or [None])[0]
            refcol = (fk.get("referred_columns") or [None])[0]
            if ref in tables and col:
                tables[n]["parents"].append(
                    {"table": ref, "fk": col, "pk": refcol})
                tables[ref]["children"].append(
                    {"table": n, "fk": col, "pk": refcol})
    return list(tables.values())


def write_table(records, dsn, table):
    from sqlalchemy import Column, MetaData, String, Table, create_engine
    if not records:
        return 0
    engine = create_engine(dsn)
    meta = MetaData()
    cols = [Column(k, String) for k in records[0].keys()]
    t = Table("".join(c for c in table if c.isalnum() or c == "_"), meta, *cols)
    meta.drop_all(engine, [t], checkfirst=True)
    meta.create_all(engine, [t])
    with engine.begin() as conn:
        conn.execute(t.insert(), [{k: str(v) for k, v in r.items()} for r in records])
    return len(records)
