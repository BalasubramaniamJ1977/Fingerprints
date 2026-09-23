"""Fingerprint Studio engine API.

Run:  python -m uvicorn api.main:app --port 8600
Then open http://127.0.0.1:8600
"""

import json
import time
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fingerprints import e2e as e2e_engine
from fingerprints import simulate
from fingerprints.anonymize import Anonymizer
from fingerprints.binding import suggest_binding
from fingerprints.connectors import (describe_tables, read_csv, read_ndjson,
                                     read_table, write_csv)
from fingerprints.fidelity import fidelity_report
from fingerprints.messages import parse_mt, parse_mx, render_messages, render_mx
from fingerprints.model import Fingerprint
from fingerprints.pii import suggest_policies
from fingerprints.registry import Registry
from fingerprints.relational import RelationalFingerprint

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
BATCHES = DATA / "batches"
OUTPUTS = DATA / "outputs"
RECIPES = ROOT / "recipes"
E2E_UPLOADS = DATA / "e2e_uploads"
for d in (UPLOADS, BATCHES, OUTPUTS, RECIPES, E2E_UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

import logging
import os

logger = logging.getLogger("fingerprints")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Fingerprint Studio API")

API_TOKEN = os.environ.get("FP_API_TOKEN")


@app.middleware("http")
async def _auth(request: Request, call_next):
    # optional bearer auth: set FP_API_TOKEN to protect all /api routes
    if API_TOKEN and request.url.path.startswith("/api/"):
        if request.headers.get("Authorization") != f"Bearer {API_TOKEN}":
            return JSONResponse(status_code=401,
                                content={"detail": "missing or invalid token"})
    return await call_next(request)
registry = Registry(DATA / "registry")
_fp_cache = {}
_policy_store = DATA / "policies"
_policy_store.mkdir(exist_ok=True)


# ------------------------------------------------------------------ helpers

def _load_fp(name, version=None):
    key = (name, version)
    if key not in _fp_cache:
        _fp_cache[key] = registry.load(name, version)
    return _fp_cache[key]


def load_source(source):
    """Resolve a recipe 'source' block into records."""
    stype = source.get("type")
    if stype == "simulator":
        sim = simulate.SIMULATORS.get(source.get("name"))
        if not sim:
            raise HTTPException(400, f"unknown simulator '{source.get('name')}'")
        params = source.get("params", {})
        records, binding = sim(**params)
        return records, binding
    if stype == "file":
        path = UPLOADS / Path(source["name"]).name
        if not path.exists():
            raise HTTPException(404, f"upload '{source['name']}' not found")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return read_csv(str(path)), None
        if suffix in (".ndjson", ".json"):
            return read_ndjson(str(path)), None
        text_body = path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".xml" or "<Document" in text_body[:2000]:
            return parse_mx(text_body)
        if suffix in (".mt", ".fin", ".txt") or ":20:" in text_body[:2000]:
            return parse_mt(text_body)
        raise HTTPException(400, f"cannot detect format of '{path.name}'")
    if stype == "batches":
        sid = Path(source["source_id"]).name
        d = BATCHES / sid
        if not d.exists():
            raise HTTPException(404, f"no batches for source '{sid}'")
        records = []
        for p in sorted(d.glob("batch-*.ndjson")):
            records += read_ndjson(str(p))
        return records, None
    if stype == "postgres":
        try:
            records = read_table(source["dsn"], source["table"],
                                 source.get("limit", 500000))
        except Exception as e:  # driver/connection problems surface to the UI
            raise HTTPException(400, f"database error: {e}")
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
                 for k, v in r.items()} for r in records], None
    raise HTTPException(400, f"unknown source type '{stype}'")


def _policies_path(name):
    return _policy_store / f"{Path(name).name}.json"


def get_policies(name):
    p = _policies_path(name)
    return json.loads(p.read_text()) if p.exists() else {}


# ------------------------------------------------------------------ sources

@app.get("/api/v1/sources")
def sources():
    return {
        "simulators": list(simulate.SIMULATORS),
        "uploads": [p.name for p in sorted(UPLOADS.iterdir()) if p.is_file()],
        "batch_sources": [
            {"source_id": d.name,
             "batches": len(list(d.glob("batch-*.ndjson")))}
            for d in sorted(BATCHES.iterdir()) if d.is_dir()
        ],
    }


@app.get("/api/v1/sources/tables")
def db_tables(dsn: str):
    """List tables for a connected database, with row counts and FK links."""
    try:
        return describe_tables(dsn)
    except Exception as e:
        raise HTTPException(400, f"database error: {e}")


@app.post("/api/v1/sources/upload")
async def upload(file: UploadFile):
    name = Path(file.filename or "upload.csv").name
    if not name.endswith((".csv", ".ndjson", ".json", ".xml", ".mt", ".fin", ".txt")):
        raise HTTPException(
            400, "supported: .csv, .ndjson, .json, .xml (MX), .mt/.fin/.txt (MT)")
    dest = UPLOADS / name
    dest.write_bytes(await file.read())
    records, _ = load_source({"type": "file", "name": name})
    return {"name": name, "rows": len(records),
            "columns": list(records[0].keys()) if records else []}


@app.post("/api/v1/sources/{source_id}/batches")
async def bulk_batch(source_id: str, request: Request,
                     idempotency_key: str | None = Header(default=None)):
    """Bulk API ingestion: NDJSON body, idempotency-key header makes retries safe."""
    sid = Path(source_id).name
    d = BATCHES / sid
    d.mkdir(parents=True, exist_ok=True)
    keys_file = d / "idempotency.json"
    keys = json.loads(keys_file.read_text()) if keys_file.exists() else {}
    if idempotency_key and idempotency_key in keys:
        return JSONResponse(status_code=200, content=keys[idempotency_key])

    body = (await request.body()).decode("utf-8", errors="replace")
    accepted, rejects = [], []
    for i, line in enumerate(body.splitlines()):
        if not line.strip():
            continue
        try:
            accepted.append(json.loads(line))
        except json.JSONDecodeError as e:
            rejects.append({"line": i + 1, "error": str(e)})
    batch_id = f"batch-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    (d / f"{batch_id}.ndjson").write_text(
        "\n".join(json.dumps(r) for r in accepted), encoding="utf-8")
    result = {"job": batch_id, "status": "complete",
              "rows": len(accepted), "rejected": len(rejects),
              "rejects": rejects[:20]}
    if idempotency_key:
        keys[idempotency_key] = result
        keys_file.write_text(json.dumps(keys))
    return JSONResponse(status_code=202, content=result)


@app.post("/api/v1/sources/resolve")
def resolve(source: dict = Body(embed=True)):
    """Load a sample of the source, return columns, sample rows, and a
    suggested dataset binding for the user to confirm or override."""
    records, binding = load_source(source)
    suggestion = binding or suggest_binding(records)
    return {"rows": len(records),
            "columns": list(records[0].keys()) if records else [],
            "sample": records[:8],
            "suggested_binding": {k: v for k, v in suggestion.items()
                                  if k != "types"}}


# ------------------------------------------------------------- fingerprints

@app.get("/api/v1/fingerprints")
def fingerprints():
    return registry.list()


@app.post("/api/v1/fingerprints/{name}/fit")
def fit(name: str, source: dict = Body(...), dataset: dict = Body(default={})):
    if source.get("type") == "postgres" and source.get("include_children"):
        return _fit_relational(name, source)
    records, auto_binding = load_source(source)
    binding = {**(auto_binding or {}), **{k: v for k, v in dataset.items() if v}}
    for req in ("entity_key", "timestamp_field", "state_fields"):
        if not binding.get(req):
            raise HTTPException(400, f"dataset.{req} is required")
    fp = Fingerprint(name=name,
                     entity_key=binding["entity_key"],
                     timestamp_field=binding["timestamp_field"],
                     state_fields=binding["state_fields"]).fit(records)
    version = registry.save(name, fp)
    logger.info("fit %s v%s: %s journeys / %s events from %s",
                name, version, fp.n_journeys, fp.n_records, source.get("type"))
    _fp_cache[(name, None)] = fp
    _fp_cache[(name, version)] = fp
    pii = suggest_policies(records)
    # seed stored policies with PII suggestions for fields not yet configured
    policies = get_policies(name)
    for f, hit in pii.items():
        policies.setdefault(f, hit["suggested"])
    _policies_path(name).write_text(json.dumps(policies, indent=2))
    return {"version": version, "summary": fp.summary(),
            "pii_suggestions": pii, "policies": policies}


def _fit_relational(name, source):
    """Learn parent + FK-referencing child tables as one relational unit."""
    dsn, parent = source["dsn"], source["table"]
    try:
        info = describe_tables(dsn)
    except Exception as e:
        raise HTTPException(400, f"database error: {e}")
    meta = next((t for t in info if t["name"] == parent), None)
    if meta is None:
        raise HTTPException(404, f"table '{parent}' not found")
    if not meta["pk"]:
        raise HTTPException(400, f"table '{parent}' has no primary key")
    relations = [{"child": c["table"], "fk": c["fk"], "parent_pk": c["pk"]}
                 for c in meta["children"]]
    if not relations:
        raise HTTPException(
            400, f"table '{parent}' has no child references — untick "
                 "'learn parent + child tables' to fit it alone")
    tables = {parent: [dict(r) for r in _pg_rows(dsn, parent, source)]}
    for rel in relations:
        tables[rel["child"]] = [dict(r) for r in _pg_rows(dsn, rel["child"], source)]
    rfp = RelationalFingerprint(name, parent, meta["pk"]).fit(tables, relations)
    version = registry.save(name, rfp)
    _fp_cache[(name, None)] = rfp
    _fp_cache[(name, version)] = rfp
    pii = {}
    for recs in tables.values():
        pii.update(suggest_policies(recs))
    policies = get_policies(name)
    for f, hit in pii.items():
        policies.setdefault(f, hit["suggested"])
    _policies_path(name).write_text(json.dumps(policies, indent=2))
    return {"version": version, "summary": rfp.summary(),
            "pii_suggestions": pii, "policies": policies}


def _pg_rows(dsn, table, source):
    rows = read_table(dsn, table, source.get("limit", 500000))
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in r.items()} for r in rows]


@app.get("/api/v1/fingerprints/{name}")
def inspect(name: str, version: int | None = None):
    try:
        fp = _load_fp(name, version)
    except KeyError:
        raise HTTPException(404, f"no fingerprint '{name}'")
    sample = fp._train[:2000]
    all_fields = sorted({k for r in sample for k in r})
    return {"versions": registry.versions(name), "summary": fp.summary(),
            "policies": get_policies(name),
            "pii_suggestions": suggest_policies(sample, fields=all_fields)}


@app.put("/api/v1/fingerprints/{name}/policies")
def set_policies(name: str, policies: dict = Body(embed=True)):
    try:
        Anonymizer(policies)  # validates policy names (rejects fpe in Phase 1)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _policies_path(name).write_text(json.dumps(policies, indent=2))
    return {"ok": True, "policies": policies}


@app.post("/api/v1/fingerprints/{name}/policies/preview")
def policy_preview(name: str, policies: dict = Body(embed=True)):
    fp = _load_fp(name)
    try:
        anon = Anonymizer(policies, fingerprint=fp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return anon.preview(fp._train, n=4)


@app.post("/api/v1/fingerprints/{name}/generate")
def generate(name: str, body: dict = Body(...)):
    fp = _load_fp(name)
    mode = body.get("mode", "synthesize")
    volume = body.get("volume", {})
    seed = body.get("seed", 42)
    preview = bool(body.get("preview"))
    policies = body.get("policies") or get_policies(name)
    try:
        anon = Anonymizer(policies, fingerprint=fp)
    except ValueError as e:
        raise HTTPException(400, str(e))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = body.get("output") or {}

    if isinstance(fp, RelationalFingerprint):
        if mode == "anonymize":
            raise HTTPException(
                400, "relational fingerprints support synthesize mode in Phase 1")
        n = 5 if preview else volume.get("journeys")
        tables = fp.sample(n_parents=n, scale=volume.get("scale"), seed=seed)
        tables = {t: anon.apply(rows) for t, rows in tables.items()}
        if preview:
            return {"mode": mode,
                    "preview": tables[fp.parent_table][:10],
                    "preview_tables": {t: rows[:10] for t, rows in tables.items()}}
        all_rows = [r for rows in tables.values() for r in rows]
        result = {"mode": mode, "relational": True,
                  "rows": len(all_rows),
                  "tables": {t: len(rows) for t, rows in tables.items()},
                  "leak_check": anon.leak_check(all_rows),
                  "downloads": []}
        for t, rows in tables.items():
            fname = f"{name}-{t}-{stamp}.csv"
            write_csv(rows, str(OUTPUTS / fname))
            result["downloads"].append(f"/api/v1/outputs/{fname}")
            if output.get("type") == "table":
                from fingerprints.connectors import write_table
                try:
                    write_table(rows, output["dsn"], f"{t}_synth")
                    result.setdefault("tables_written", []).append(f"{t}_synth")
                except Exception as e:
                    result["table_error"] = str(e)
        return result

    backend = body.get("backend", "correlated")
    if mode == "anonymize":
        base = fp._train
    else:
        n = 12 if preview else volume.get("journeys")
        try:
            base = fp.sample(n_journeys=n, scale=volume.get("scale"),
                             seed=seed, backend=backend)
        except RuntimeError as e:      # e.g. sdv backend not installed
            raise HTTPException(400, str(e))
    out = anon.apply(base[:80] if (preview and mode == "anonymize") else base)

    if preview:
        resp = {"preview": out[:40], "mode": mode}
        _, rendered = render_messages(out[:6])
        if rendered:
            resp["message_preview"] = rendered
        return resp

    result = {"mode": mode, "rows": len(out),
              "leak_check": anon.leak_check(out)}
    if mode == "synthesize":
        result["fidelity"] = fidelity_report(fp, base)
    fname = f"{name}-{mode}-{stamp}.csv"
    write_csv(out, str(OUTPUTS / fname))
    result["download"] = f"/api/v1/outputs/{fname}"
    ext, rendered = render_messages(out)
    if rendered:
        mname = f"{name}-{mode}-{stamp}.{ext}"
        (OUTPUTS / mname).write_text(rendered, encoding="utf-8")
        result["message_download"] = f"/api/v1/outputs/{mname}"
    if output.get("type") == "table":
        from fingerprints.connectors import write_table
        try:
            n = write_table(out, output["dsn"], output["name"])
            result["table_written"] = {"table": output["name"], "rows": n}
        except Exception as e:
            result["table_error"] = str(e)
    return result


@app.get("/api/v1/outputs/{fname}")
def download(fname: str):
    path = OUTPUTS / Path(fname).name
    if not path.exists():
        raise HTTPException(404, "no such output")
    media = {".csv": "text/csv", ".xml": "application/xml",
             ".ndjson": "application/x-ndjson", ".json": "application/json"}.get(
        path.suffix, "text/plain")
    return FileResponse(path, filename=path.name, media_type=media)


@app.post("/api/v1/fingerprints/{name}/score")
def score(name: str, records: list = Body(embed=True)):
    fp = _load_fp(name)
    return fp.score_journey(records)


@app.get("/api/v1/fingerprints/{name}/diff")
def diff(name: str, against: int, version: int | None = None):
    fp_new = _load_fp(name, version)
    fp_old = _load_fp(name, against)
    return fp_new.diff(fp_old)


# -------------------------------------------------------------- recipes

@app.get("/api/v1/recipes")
def recipes():
    out = []
    for p in sorted(RECIPES.glob("*.json")):
        out.append({"name": p.stem, **json.loads(p.read_text())})
    return out


@app.post("/api/v1/recipes/{name}")
def save_recipe(name: str, recipe: dict = Body(embed=True)):
    (RECIPES / f"{Path(name).name}.json").write_text(
        json.dumps(recipe, indent=2), encoding="utf-8")
    return {"ok": True}


@app.post("/api/v1/recipes/{name}/run")
def run_recipe(name: str):
    p = RECIPES / f"{Path(name).name}.json"
    if not p.exists():
        raise HTTPException(404, f"no recipe '{name}'")
    recipe = json.loads(p.read_text())
    fitted = fit(name=recipe.get("fingerprint", name),
                 source=recipe["source"], dataset=recipe["dataset"])
    gen = generate(name=recipe.get("fingerprint", name), body={
        **recipe.get("generate", {}), "policies": recipe.get("policies"),
    })
    return {"fit": {"version": fitted["version"]}, "generate": gen}


# --------------------------------------------------------------------- e2e

@app.get("/api/v1/e2e/scenarios")
def e2e_scenarios():
    """Systems + flow scenarios for the E2E Test tab's dropdowns."""
    return {
        "systems": e2e_engine.SYSTEMS,
        "scenarios": [
            {"key": k, "label": v["label"], "chain": v["chain"],
             "origin_message_type": v["origin_message_type"]}
            for k, v in e2e_engine.SCENARIOS.items()
        ],
    }


@app.post("/api/v1/e2e/upload")
async def e2e_upload(file: UploadFile, system_id: str = Form(...)):
    """Upload a real message file to seed one system's leg of the flow
    (mode = file-upload by systems). Any system left without an upload
    still derives its message from the previous hop. system_id doesn't have
    to be one of the built-in systems — a custom intermediary system (added
    via "+ Add intermediary system") can have its own test dataset too."""
    if not system_id.strip():
        raise HTTPException(400, "system_id is required")
    name = Path(file.filename or "message.xml").name
    if not name.lower().endswith((".xml", ".txt")):
        raise HTTPException(400, "supported: .xml / .txt (MX / ISO 20022 message)")
    data = await file.read()
    records, _ = parse_mx(data.decode("utf-8", errors="replace"))
    if not records:
        raise HTTPException(400, f"no <Document> message found in '{name}'")
    dest = E2E_UPLOADS / f"{system_id}__{name}"
    dest.write_bytes(data)
    return {"system_id": system_id, "name": dest.name,
            "msg_type": records[0].get("_msg_type"), "messages_in_file": len(records)}


def _e2e_resolve_datasets(files):
    """{system_id: uploaded filename} -> {system_id: [record, ...]}. A file
    with several <Document> messages is that system's test dataset; one
    with a single message behaves like today's single-message override."""
    datasets = {}
    for system_id, fname in (files or {}).items():
        path = E2E_UPLOADS / Path(fname).name
        if not path.exists():
            raise HTTPException(404, f"e2e upload '{fname}' not found")
        text = path.read_text(encoding="utf-8", errors="replace")
        records, _ = parse_mx(text)
        if not records:
            raise HTTPException(400, f"could not parse message in '{fname}'")
        datasets[system_id] = records
    return datasets


def _e2e_resolve_overrides(files):
    return {sid: recs[0] for sid, recs in _e2e_resolve_datasets(files).items()}


def _e2e_render_xml(result):
    for h in result["hops"]:
        try:
            h["xml"] = render_mx([h["message"]])
        except Exception:
            h["xml"] = None
    return result


@app.post("/api/v1/e2e/run")
def e2e_run(spec: dict = Body(...)):
    """Run one E2E scenario. spec = {scenario, source, seed, account,
    amount, ccy, verdict, files: {system_id: uploaded_name}, database:
    {dsn, table}, chain: [system_id, ...], systems: [{id, name, role}],
    edits: {system_id: {"forward"|"response": {field: value}}}}.
    source is one of synthetic | file | database. chain/systems are
    optional — omit them to use the scenario's default 4-system chain, or
    supply chain to insert any number of intermediary systems anywhere in
    the flow (describe new ids via systems so the UI can label them).
    edits lets a reviewed message be corrected field by field — every hop
    downstream of the edit re-derives from the corrected message, for
    review-then-edit-then-verify testing. scenario_def = {label, chain,
    origin_message_type} defines a caller-supplied flow *type* (e.g. "FAST",
    "RTGS", "Book Transfer", "Telegraphic Transfer") instead of looking
    `scenario` up in the built-in four — `scenario` is still used as its
    key/label."""
    scenario = spec.get("scenario", "outward")
    source = spec.get("source", "synthetic")
    overrides = _e2e_resolve_overrides(spec.get("files")) if source == "file" else {}
    db = spec.get("database") if source == "database" else None
    try:
        result = e2e_engine.run_pipeline(
            scenario=scenario, source=source, seed=spec.get("seed"),
            account=spec.get("account") or None, amount=spec.get("amount"),
            ccy=spec.get("ccy") or None, overrides=overrides, db=db,
            verdict=spec.get("verdict", "ACSC"),
            chain=spec.get("chain") or None, systems=spec.get("systems"),
            edits=spec.get("edits"), scenario_def=spec.get("scenario_def"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"e2e run failed: {e}")
    return _e2e_render_xml(result)


@app.post("/api/v1/e2e/batch")
def e2e_batch(spec: dict = Body(...)):
    """Live-preview batch of up to fingerprints.e2e.MAX_BATCH flows (same
    spec as /e2e/run, plus count). For bulk volumes (thousands+), use
    /api/v1/e2e/export instead — this endpoint holds every flow in memory
    and in the browser, which doesn't scale past a small preview size."""
    scenario = spec.get("scenario", "outward")
    source = spec.get("source", "synthetic")
    count = int(spec.get("count", 1))
    datasets = _e2e_resolve_datasets(spec.get("files")) if source == "file" else {}
    db = spec.get("database") if source == "database" else None
    try:
        batch = e2e_engine.run_batch(
            count=count, datasets=datasets, seed_base=spec.get("seed"),
            scenario=scenario, source=source,
            account=spec.get("account") or None, amount=spec.get("amount"),
            ccy=spec.get("ccy") or None, db=db,
            verdict=spec.get("verdict", "ACSC"),
            chain=spec.get("chain") or None, systems=spec.get("systems"),
            edits=spec.get("edits"), scenario_def=spec.get("scenario_def"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"e2e batch run failed: {e}")
    batch["flows"] = [_e2e_render_xml(r) for r in batch["flows"]]
    return batch


@app.post("/api/v1/e2e/export")
def e2e_export(spec: dict = Body(...)):
    """Bulk-generate N end-to-end flows and write them to files under
    data/outputs instead of holding them in memory or the browser — for
    volumes too large to preview live (e.g. 50,000 messages for bulk
    testing and verification). Same spec as /e2e/run, plus count
    (<= fingerprints.e2e.MAX_EXPORT). Streams flow by flow to an NDJSON file
    (one hop message per line) so memory use stays flat regardless of
    count; also writes a per-flow summary CSV and a manifest. Returns
    download links (served by GET /api/v1/outputs/{name}). The generation
    logic itself lives in fingerprints.e2e.export_flows, shared with the
    command-line fingerprints.e2e_cli."""
    scenario = spec.get("scenario", "outward")
    source = spec.get("source", "synthetic")
    datasets = _e2e_resolve_datasets(spec.get("files")) if source == "file" else {}
    db = spec.get("database") if source == "database" else None
    try:
        manifest = e2e_engine.export_flows(
            OUTPUTS, count=int(spec.get("count", 1)), datasets=datasets,
            seed_base=spec.get("seed"), scenario=scenario, source=source,
            account=spec.get("account") or None, amount=spec.get("amount"),
            ccy=spec.get("ccy") or None, db=db,
            verdict=spec.get("verdict", "ACSC"),
            chain=spec.get("chain") or None, systems=spec.get("systems"),
            scenario_def=spec.get("scenario_def"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"e2e export failed: {e}")
    files = manifest.pop("files")
    manifest["downloads"] = {k: f"/api/v1/outputs/{Path(v).name}" for k, v in files.items()}
    return manifest


# ------------------------------------------------------------------ studio

app.mount("/", StaticFiles(directory=ROOT / "studio", html=True), name="studio")
