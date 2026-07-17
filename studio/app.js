/* Fingerprint Studio - talks only to the engine API; every screen state can
   be captured as a recipe and re-run. */

const S = {
  source: null, binding: null, columns: [], name: null,
  summary: null, policies: {}, pii: {},
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch("/api/v1" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body && typeof opts.body !== "string"
      ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

/* ------------------------------------------------------------- tabs */

document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "registry") loadRegistry();
  }));

function goTab(name) {
  document.querySelector(`nav button[data-tab=${name}]`).click();
}

/* ----------------------------------------------------------- connect */

document.querySelectorAll("input[name=stype]").forEach((r) =>
  r.addEventListener("change", () => {
    ["simulator", "file", "postgres", "batches"].forEach((t) =>
      $("src-" + t).classList.toggle("hidden", t !== r.value));
  }));

async function loadSources() {
  const s = await api("/sources");
  $("simName").innerHTML = s.simulators
    .map((n) => `<option>${esc(n)}</option>`).join("");
  $("uploadSelect").innerHTML = "<option value=''>-</option>" + s.uploads
    .map((n) => `<option>${esc(n)}</option>`).join("");
  $("batchSelect").innerHTML = s.batch_sources.length
    ? s.batch_sources.map((b) =>
        `<option value="${esc(b.source_id)}">${esc(b.source_id)} (${b.batches} batches)</option>`).join("")
    : "<option value=''>no batch sources yet - push one with curl below</option>";
}

$("fileInput").addEventListener("change", async () => {
  const f = $("fileInput").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  $("fileStatus").textContent = "uploading...";
  const res = await fetch("/api/v1/sources/upload", { method: "POST", body: fd });
  const j = await res.json();
  if (!res.ok) { $("fileStatus").textContent = "error: " + (j.detail || res.statusText); return; }
  $("fileStatus").textContent = `uploaded ${j.name}: ${j.rows} rows, ${j.columns.length} columns`;
  await loadSources();
  $("uploadSelect").value = j.name;
});

function currentSource() {
  const t = document.querySelector("input[name=stype]:checked").value;
  if (t === "simulator") return { type: "simulator", name: $("simName").value };
  if (t === "file") {
    const name = $("uploadSelect").value;
    if (!name) throw new Error("upload or pick a file first");
    return { type: "file", name };
  }
  if (t === "postgres") {
    const table = $("pgTable").classList.contains("hidden")
      ? $("pgTableText").value.trim() : $("pgTable").value;
    if (!$("pgDsn").value || !table) throw new Error("DSN and table are required");
    return { type: "postgres", dsn: $("pgDsn").value, table,
             include_children: $("includeChildren").checked };
  }
  const sid = $("batchSelect").value;
  if (!sid) throw new Error("no batch source available yet");
  return { type: "batches", source_id: sid };
}

let TABLEMETA = {};

$("btnListTables").addEventListener("click", async () => {
  const btn = $("btnListTables"); btn.disabled = true;
  try {
    const tables = await api("/sources/tables?dsn=" + encodeURIComponent($("pgDsn").value));
    TABLEMETA = Object.fromEntries(tables.map((t) => [t.name, t]));
    $("pgTable").innerHTML = tables.map((t) =>
      `<option value="${esc(t.name)}">${esc(t.name)} (${(t.rows ?? 0).toLocaleString()} rows)</option>`).join("");
    $("pgTable").classList.remove("hidden");
    $("pgTableText").classList.add("hidden");
    $("pgTable").onchange = showTableInfo;
    showTableInfo();
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

function showTableInfo() {
  const m = TABLEMETA[$("pgTable").value];
  if (!m) return;
  $("tableInfo").innerHTML = m.children.length
    ? "child references: <b>" +
      m.children.map((c) => `${esc(c.table)} (fk ${esc(c.fk)})`).join(", ") + "</b>"
    : "no child references";
  $("childOptWrap").classList.toggle("hidden", !m.children.length);
  if (!m.children.length) $("includeChildren").checked = false;
}

$("btnResolve").addEventListener("click", async () => {
  const btn = $("btnResolve");
  btn.disabled = true;
  try {
    S.source = currentSource();
    const r = await api("/sources/resolve", { method: "POST", body: { source: S.source } });
    S.columns = r.columns;
    S.binding = r.suggested_binding || {};
    renderBinding();
    renderSample(r);
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

function renderBinding() {
  $("bindingArea").classList.add("hidden");
  $("bindingForm").classList.remove("hidden");
  const opts = S.columns.map((c) => `<option>${esc(c)}</option>`).join("");
  $("bindEntity").innerHTML = opts;
  $("bindTs").innerHTML = opts;
  if (S.binding.entity_key) $("bindEntity").value = S.binding.entity_key;
  if (S.binding.timestamp_field) $("bindTs").value = S.binding.timestamp_field;
  $("bindStates").innerHTML = S.columns.map((c) =>
    `<label><input type="checkbox" value="${esc(c)}"
       ${(S.binding.state_fields || []).includes(c) ? "checked" : ""}> ${esc(c)}</label>`).join("");
  if (!$("fpName").value) {
    $("fpName").value = (S.source.name || S.source.table || S.source.source_id || "fingerprint")
      .replace(/[^a-zA-Z0-9_-]/g, "-");
  }
}

function renderSample(r) {
  $("sampleCard").style.display = "";
  const cols = r.columns;
  $("sampleTable").innerHTML =
    `<div class="hint">${r.rows} rows loaded</div>
     <table><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>` +
    r.sample.map((row) =>
      `<tr>${cols.map((c) => `<td class="mono">${esc(row[c])}</td>`).join("")}</tr>`).join("") +
    "</table>";
}

$("btnLearn").addEventListener("click", async () => {
  const btn = $("btnLearn");
  const name = $("fpName").value.trim();
  if (!name) return alert("give the fingerprint a name");
  try { S.source = currentSource(); } catch (e) { return alert(e.message); }
  let dataset = {
    entity_key: $("bindEntity").value,
    timestamp_field: $("bindTs").value,
    state_fields: [...$("bindStates").querySelectorAll("input:checked")].map((i) => i.value),
  };
  if (S.source?.include_children) {
    dataset = {};   // relational: bindings derived per table server-side
  } else if (!dataset.state_fields.length) {
    return alert("pick at least one state field");
  }
  btn.disabled = true; btn.textContent = "learning...";
  try {
    const r = await api(`/fingerprints/${encodeURIComponent(name)}/fit`,
      { method: "POST", body: { source: S.source, dataset } });
    S.name = name; S.summary = r.summary; S.policies = r.policies; S.pii = r.pii_suggestions;
    S.binding = dataset;
    await loadFpList();
    $("fpSelect").value = name;
    renderInspect(); renderPolicies(); renderGenerate();
    goTab("inspect");
  } catch (e) { alert(e.message); }
  btn.disabled = false; btn.textContent = "Learn fingerprint";
});

/* ----------------------------------------------------------- inspect */

function statChip(v, label, cls = "") {
  return `<div class="stat ${cls}"><b>${esc(v)}</b><span>${esc(label)}</span></div>`;
}

function renderInspect() {
  const s = S.summary;
  if (!s) return;
  $("inspectEmpty").classList.add("hidden");
  $("inspectBody").classList.remove("hidden");
  $("statRow").innerHTML =
    statChip(s.journeys.toLocaleString(), "journeys") +
    statChip(s.events.toLocaleString(), "events") +
    statChip(s.distinct_paths, "distinct paths") +
    statChip(s.entity_key, "entity key") +
    statChip(s.state_fields.join(" + "), "state fields");
  $("pathList").innerHTML = s.top_paths.map((p, i) => {
    const warn = i > 0;
    return `<div class="pathrow">
      <div class="pathfreq ${warn ? "warn" : "ok"}">${(p.share * 100).toFixed(1)}%</div>
      <div class="pathbar ${warn ? "warn" : ""}"><i style="width:${Math.max(p.share * 100, 2)}%"></i></div>
      <div class="pathnodes">${p.path.map(esc).join('<span class="sep">&rarr;</span>')}</div>
    </div>`;
  }).join("");
  const rows = Object.entries(s.fields).map(([f, info]) => {
    const pii = S.pii[f];
    const extra = info.format_pattern
      ? `pattern ${esc(info.format_pattern)}`
      : info.stats
        ? `p50 ${info.stats.p50} &middot; p95 ${info.stats.p95}`
        : (info.top_values || []).slice(0, 3).map((t) =>
            `${esc(t.value)} ${(t.share * 100).toFixed(0)}%`).join(", ");
    return `<tr>
      <td class="mono"><b>${esc(f)}</b></td>
      <td><span class="badge ${info.is_state_field ? "acc" : ""}">${info.is_state_field ? "state" : esc(info.type)}</span></td>
      <td>${info.distinct.toLocaleString()}</td>
      <td>${(info.null_pct * 100).toFixed(1)}%</td>
      <td>${info.journey_constant ? '<span class="badge">journey-constant</span>' : ""}</td>
      <td>${pii ? `<span class="badge pii">${esc(pii.pii_type)} &middot; ${(pii.confidence * 100).toFixed(0)}%</span>` : "-"}</td>
      <td class="mono">${extra}</td>
    </tr>`;
  }).join("");
  $("fieldTable").innerHTML =
    `<table><tr><th>Field</th><th>Type</th><th>Distinct</th><th>Null</th><th></th>
     <th>PII scan</th><th>Learned shape</th></tr>${rows}</table>`;
}

/* ---------------------------------------------------------- policies */

const POLICY_KINDS = ["keep", "drop", "hash", "mask", "generalize", "synthesize"];

function renderPolicies() {
  if (!S.summary) return;
  $("polEmpty").classList.add("hidden");
  $("polBody").classList.remove("hidden");
  const fields = Object.keys(S.summary.fields).concat([S.summary.entity_key]);
  $("policyTable").innerHTML =
    `<table><tr><th>Field</th><th>PII scan</th><th>Policy</th><th>Param</th></tr>` +
    fields.map((f) => {
      const p = S.policies[f] || { policy: "keep" };
      const pii = S.pii[f];
      const param = p.policy === "mask"
        ? `<input class="polparam" data-f="${esc(f)}" data-k="keep_last" type="number" value="${p.keep_last ?? 4}"> keep last`
        : p.policy === "generalize"
          ? `<input class="polparam" data-f="${esc(f)}" data-k="bucket" type="number" value="${p.bucket ?? 500}"> bucket`
          : p.policy === "hash"
            ? `<input class="polparam" data-f="${esc(f)}" data-k="length" type="number" value="${p.length ?? 16}"> chars`
            : "";
      return `<tr>
        <td class="mono"><b>${esc(f)}</b></td>
        <td>${pii ? `<span class="badge pii">${esc(pii.pii_type)}</span>` : "-"}</td>
        <td><select class="polsel" data-f="${esc(f)}">${POLICY_KINDS.map((k) =>
          `<option ${k === p.policy ? "selected" : ""}>${k}</option>`).join("")}</select></td>
        <td>${param}</td>
      </tr>`;
    }).join("") + "</table>";
  $("policyTable").querySelectorAll(".polsel").forEach((sel) =>
    sel.addEventListener("change", () => {
      S.policies[sel.dataset.f] = { policy: sel.value };
      if (sel.value === "mask") S.policies[sel.dataset.f].keep_last = 4;
      if (sel.value === "generalize") S.policies[sel.dataset.f].bucket = 500;
      renderPolicies();
    }));
  $("policyTable").querySelectorAll(".polparam").forEach((inp) =>
    inp.addEventListener("change", () => {
      S.policies[inp.dataset.f][inp.dataset.k] = Number(inp.value);
    }));
}

$("btnSavePolicies").addEventListener("click", async () => {
  try {
    await api(`/fingerprints/${encodeURIComponent(S.name)}/policies`,
      { method: "PUT", body: { policies: S.policies } });
    $("polStatus").textContent = "saved OK";
    setTimeout(() => ($("polStatus").textContent = ""), 2500);
  } catch (e) { alert(e.message); }
});

$("btnPreviewPolicies").addEventListener("click", async () => {
  try {
    const prev = await api(`/fingerprints/${encodeURIComponent(S.name)}/policies/preview`,
      { method: "POST", body: { policies: S.policies } });
    $("policyPreview").innerHTML = Object.entries(prev).map(([f, d]) =>
      `<div class="prevpair"><span class="field">${esc(f)} &middot; ${esc(d.policy.policy)}</span><br>` +
      d.samples.map((s) =>
        `${esc(s.before)} &rarr; <span class="after">${esc(s.after)}</span>`).join("<br>") +
      "</div>").join("") || '<div class="hint">no anonymizing policies set</div>';
  } catch (e) { alert(e.message); }
});

/* ---------------------------------------------------------- generate */

function renderGenerate() {
  if (!S.summary) return;
  $("genEmpty").classList.add("hidden");
  $("genBody").classList.remove("hidden");
  $("genJourneys").value = S.summary.journeys;
  if (!$("recipeName").value) $("recipeName").value = S.name + "-recipe";
}

function genVolume() {
  const scale = $("genScale").value.trim();
  if (scale) return { scale: Number(scale) };
  return { journeys: Number($("genJourneys").value) || 1000 };
}

$("btnPreviewGen").addEventListener("click", async () => {
  const btn = $("btnPreviewGen"); btn.disabled = true;
  try {
    const r = await api(`/fingerprints/${encodeURIComponent(S.name)}/generate`, {
      method: "POST",
      body: { mode: $("genMode").value, preview: true, seed: Number($("genSeed").value),
              backend: $("genBackend").value, volume: genVolume(), policies: S.policies },
    });
    const rows = r.preview;
    if (!rows.length) { $("genPreview").innerHTML = "<div class='hint'>empty</div>"; return; }
    const cols = Object.keys(rows[0]);
    $("genPreview").innerHTML =
      `<table><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>` +
      rows.map((row) => `<tr>${cols.map((c) => `<td class="mono">${esc(row[c])}</td>`).join("")}</tr>`).join("") +
      "</table>";
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

$("btnGenerate").addEventListener("click", async () => {
  const btn = $("btnGenerate"); btn.disabled = true;
  $("genStatus").textContent = "generating...";
  try {
    const r = await api(`/fingerprints/${encodeURIComponent(S.name)}/generate`, {
      method: "POST",
      body: { mode: $("genMode").value, seed: Number($("genSeed").value),
              backend: $("genBackend").value, volume: genVolume(), policies: S.policies },
    });
    let chips = statChip(r.rows.toLocaleString(), "rows produced");
    if (r.tables) {
      for (const [t, n] of Object.entries(r.tables)) chips += statChip(n.toLocaleString(), t);
    }
    if (r.fidelity) {
      chips += statChip(r.fidelity.journeys_generated.toLocaleString(), "journeys");
      chips += statChip(r.fidelity.worst_field_distance, "worst field distance",
        r.fidelity.worst_field_distance > 0.15 ? "warn" : "");
      chips += statChip(r.fidelity.path_distribution_delta, "path delta");
      chips += statChip(r.fidelity.verdict, "fidelity",
        r.fidelity.verdict === "PASS" ? "" : "warn");
    }
    chips += statChip(r.leak_check.leaks, "PII leaks", r.leak_check.leaks ? "bad" : "");
    $("genMetrics").innerHTML = chips;
    const links = (r.downloads || (r.download ? [r.download] : []))
      .concat(r.message_download ? [r.message_download] : []);
    $("genDownload").innerHTML = links.map((u) =>
      `<a class="dl" href="${u}" style="margin-right:14px">Download ${esc(u.split("/").pop())}</a>`).join("");
    $("genStatus").textContent = "done OK";
  } catch (e) { $("genStatus").textContent = ""; alert(e.message); }
  btn.disabled = false;
});

$("btnSaveRecipe").addEventListener("click", async () => {
  const name = $("recipeName").value.trim();
  if (!name || !S.name) return alert("recipe name and a learned fingerprint required");
  if (!S.source || !S.binding) return alert(
    "recipes capture the source + binding: re-learn this fingerprint from the Connect tab once, then save");
  const recipe = {
    fingerprint: S.name, source: S.source,
    dataset: S.binding, policies: S.policies,
    generate: { mode: $("genMode").value, volume: genVolume(),
                seed: Number($("genSeed").value) },
  };
  await api(`/recipes/${encodeURIComponent(name)}`, { method: "POST", body: { recipe } });
  $("recipeStatus").textContent = "saved OK";
  setTimeout(() => ($("recipeStatus").textContent = ""), 2500);
});

/* ---------------------------------------------------------- registry */

async function loadRegistry() {
  const list = await api("/fingerprints");
  $("registryTable").innerHTML = list.length
    ? `<table><tr><th>Name</th><th>Versions</th><th>Journeys</th><th>Events</th><th></th></tr>` +
      list.map((e) =>
        `<tr><td class="mono"><b>${esc(e.name)}</b></td>
         <td>${e.versions.join(", ")}</td>
         <td>${(e.journeys ?? 0).toLocaleString()}</td>
         <td>${(e.events ?? 0).toLocaleString()}</td>
         <td><button class="btn" data-open="${esc(e.name)}">open</button></td></tr>`).join("") +
      "</table>"
    : '<div class="hint">nothing learned yet</div>';
  $("registryTable").querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => openFingerprint(b.dataset.open)));
  $("diffName").innerHTML = list.map((e) => `<option>${esc(e.name)}</option>`).join("");
  updateDiffVersions(list);
  $("diffName").onchange = () => updateDiffVersions(list);

  const recipes = await api("/recipes");
  $("recipeList").innerHTML = recipes.length
    ? recipes.map((r) =>
        `<div class="pathrow"><div class="pathnodes"><b>${esc(r.name)}</b>
         <span class="sep">&middot;</span>${esc(r.source?.type)} &rarr; ${esc(r.generate?.mode)}
         </div><button class="btn" data-run="${esc(r.name)}">Re-run</button></div>`).join("")
    : "No recipes saved yet.";
  $("recipeList").querySelectorAll("[data-run]").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "running...";
      try {
        const res = await api(`/recipes/${encodeURIComponent(b.dataset.run)}/run`, { method: "POST" });
        alert(`re-run complete: fit v${res.fit.version}, ${res.generate.rows} rows -> ${res.generate.download}`);
        loadRegistry(); loadFpList();
      } catch (e) { alert(e.message); }
      b.disabled = false; b.textContent = "Re-run";
    }));
}

function updateDiffVersions(list) {
  const e = list.find((x) => x.name === $("diffName").value);
  const opts = (e?.versions || []).map((v) => `<option>${v}</option>`).join("");
  $("diffA").innerHTML = opts; $("diffB").innerHTML = opts;
  if (e?.versions.length > 1) $("diffB").value = e.versions[e.versions.length - 2];
}

$("btnDiff").addEventListener("click", async () => {
  try {
    const d = await api(`/fingerprints/${encodeURIComponent($("diffName").value)}/diff` +
      `?version=${$("diffA").value}&against=${$("diffB").value}`);
    $("diffOut").classList.remove("hidden");
    $("diffOut").textContent = JSON.stringify(d, null, 2);
  } catch (e) { alert(e.message); }
});

/* ---------------------------------------------------------- messages */

let MSG = { name: null, uploadName: null };

$("msgSource").addEventListener("change", () =>
  $("msgUploadWrap").classList.toggle("hidden", $("msgSource").value !== "upload"));

$("msgFile").addEventListener("change", async () => {
  const f = $("msgFile").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  $("msgFileStatus").textContent = "uploading...";
  const res = await fetch("/api/v1/sources/upload", { method: "POST", body: fd });
  const j = await res.json();
  if (!res.ok) { $("msgFileStatus").textContent = "error: " + (j.detail || res.statusText); return; }
  MSG.uploadName = j.name;
  $("msgFileStatus").textContent = `parsed ${j.name}: ${j.rows} messages`;
});

function msgSourceSpec() {
  if ($("msgSource").value === "upload") {
    if (!MSG.uploadName) throw new Error("upload a message file first");
    return { type: "file", name: MSG.uploadName };
  }
  return { type: "simulator", name: $("msgFormat").value,
           params: { seed: Number($("msgSeed").value) } };
}

$("btnMsgLearn").addEventListener("click", async () => {
  const btn = $("btnMsgLearn"); btn.disabled = true;
  $("msgStatus").textContent = "learning...";
  try {
    const src = msgSourceSpec();
    const name = $("msgSource").value === "upload"
      ? "msg-" + MSG.uploadName.replace(/[^a-zA-Z0-9_-]/g, "-")
      : $("msgFormat").value;
    const r = await api(`/fingerprints/${encodeURIComponent(name)}/fit`,
      { method: "POST", body: { source: src, dataset: {} } });
    MSG.name = name;
    S.name = name; S.summary = r.summary; S.policies = r.policies;
    S.pii = r.pii_suggestions; S.source = src;
    S.binding = { entity_key: r.summary.entity_key,
                  timestamp_field: r.summary.timestamp_field,
                  state_fields: r.summary.state_fields };
    renderInspect(); renderPolicies(); renderGenerate();
    await loadFpList();
    $("fpSelect").value = name;
    const piiN = Object.keys(r.pii_suggestions).length;
    $("msgStatus").textContent =
      `learned ${r.summary.journeys.toLocaleString()} messages (v${r.version}) Â· ` +
      `${piiN} PII fields auto-policied OK`;
  } catch (e) { $("msgStatus").textContent = ""; alert(e.message); }
  btn.disabled = false;
});

$("btnMsgPreview").addEventListener("click", async () => {
  if (!MSG.name) return alert("learn first");
  const btn = $("btnMsgPreview"); btn.disabled = true;
  try {
    const r = await api(`/fingerprints/${encodeURIComponent(MSG.name)}/generate`, {
      method: "POST",
      body: { mode: "synthesize", preview: true, seed: Number($("msgSeed").value),
              volume: { journeys: 6 }, policies: S.policies },
    });
    $("msgPreview").textContent =
      r.message_preview || JSON.stringify(r.preview, null, 2);
  } catch (e) { alert(e.message); }
  btn.disabled = false;
});

$("btnMsgGenerate").addEventListener("click", async () => {
  if (!MSG.name) return alert("learn first");
  const btn = $("btnMsgGenerate"); btn.disabled = true;
  $("msgStatus").textContent = "generating...";
  try {
    const r = await api(`/fingerprints/${encodeURIComponent(MSG.name)}/generate`, {
      method: "POST",
      body: { mode: "synthesize", seed: Number($("msgSeed").value),
              volume: { journeys: Number($("msgCount").value) || 500 },
              policies: S.policies },
    });
    const links = [r.message_download, r.download].filter(Boolean);
    $("msgDownloads").innerHTML = links.map((u) =>
      `<a class="dl" href="${u}" style="display:block;margin-top:6px">Download ${esc(u.split("/").pop())}</a>`).join("");
    $("msgStatus").textContent =
      `${r.rows.toLocaleString()} messages Â· leaks=${r.leak_check.leaks} Â· ` +
      `fidelity ${r.fidelity ? r.fidelity.verdict : "-"} OK`;
  } catch (e) { $("msgStatus").textContent = ""; alert(e.message); }
  btn.disabled = false;
});

/* ------------------------------------------------------------ header */

async function loadFpList() {
  const list = await api("/fingerprints");
  $("fpSelect").innerHTML = "<option value=''>-</option>" +
    list.map((e) => `<option>${esc(e.name)}</option>`).join("");
  if (S.name) $("fpSelect").value = S.name;
}

async function openFingerprint(name) {
  const r = await api(`/fingerprints/${encodeURIComponent(name)}`);
  S.name = name; S.summary = r.summary; S.policies = r.policies; S.pii = r.pii_suggestions;
  $("fpSelect").value = name;
  renderInspect(); renderPolicies(); renderGenerate();
  goTab("inspect");
}

$("fpSelect").addEventListener("change", () => {
  if ($("fpSelect").value) openFingerprint($("fpSelect").value);
});

loadSources();
loadFpList();
