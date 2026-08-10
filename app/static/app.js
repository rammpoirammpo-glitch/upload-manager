const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(2) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(1) + " KB";
  return n + " B";
}

let toastTimer;
function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let t = "";
    try { t = await res.text(); } catch (e) {}
    throw new Error(res.status + " " + t);
  }
  return res.json();
}

/* ---------------- Queue ---------------- */

const STATUS_CLASS = {
  pending: "st-pending", uploading: "st-uploading",
  completed: "st-completed", failed: "st-failed", skipped: "st-skipped",
};

async function refreshQueue() {
  let data;
  try { data = await api("/api/queue"); } catch (e) { return; }
  const tbody = $("#queueBody");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">Queue is empty</td></tr>';
    return;
  }
  tbody.innerHTML = data.items.map(item => {
    const chips = (item.tasks || []).map(t => {
      const cls = t.status === "completed" ? "chip-ok"
        : t.status === "failed" ? "chip-err"
        : t.status === "skipped" ? "chip-skip" : "chip-run";
      const pct = t.status === "completed" ? 100 : (t.progress || 0);
      const bar = (t.status === "uploading" || t.status === "pending") && pct > 0
        ? `<div class="mini-bar"><div style="width:${pct}%"></div></div>` : "";
      return `<div class="chip ${cls}" title="${esc(t.error || "")}">${esc(t.provider_name || t.provider_id)} ${pct}%${bar}</div>`;
    }).join("");
    let actions = "";
    if (item.status === "failed") {
      actions += `<button class="btn small" onclick="retryItem(${item.id})">Retry</button>`;
      actions += `<button class="btn small" onclick="skipItem(${item.id})">Skip</button>`;
    }
    actions += `<button class="btn small danger" onclick="deleteItem(${item.id})">Delete</button>`;
    return `<tr>
      <td>${item.id}</td>
      <td class="fname" title="${esc(item.path)}">${esc(item.filename)}</td>
      <td>${esc(item.folder)}</td>
      <td>${fmtSize(item.size)}</td>
      <td><span class="st-badge ${STATUS_CLASS[item.status] || ""}">${esc(item.status)}</span></td>
      <td>${chips || '<span class="muted">no providers</span>'}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
}

async function retryItem(id) { await api(`/api/queue/${id}/retry`, { method: "POST" }); refreshQueue(); }
async function skipItem(id) { await api(`/api/queue/${id}/skip`, { method: "POST" }); refreshQueue(); }
async function deleteItem(id) { if (!confirm("Delete this item?")) return; await api(`/api/queue/${id}`, { method: "DELETE" }); refreshQueue(); }

/* ---------------- Stats ---------------- */

async function refreshStats() {
  let s;
  try { s = await api("/api/stats"); } catch (e) { return; }
  const c = s.counts || {};
  $("#stPending").textContent = c.pending || 0;
  $("#stUploading").textContent = c.uploading || 0;
  $("#stCompleted").textContent = c.completed || 0;
  $("#stFailed").textContent = c.failed || 0;
  $("#stProviders").textContent = s.providers;
  $("#stPaths").textContent = s.watch_paths;
}

/* ---------------- Providers ---------------- */

let providerTypes = [];
let editingProviderId = null;
let currentProviderConfig = null;

async function loadProviderTypes() {
  const d = await api("/api/provider-types");
  providerTypes = d.types;
  const sel = $("#providerType");
  sel.innerHTML = providerTypes.map(t => `<option value="${t.type}">${esc(t.display_name)}</option>`).join("");
  sel.addEventListener("change", renderProviderFields);
  renderProviderFields();
}

function renderProviderFields() {
  const type = $("#providerType").value;
  const t = providerTypes.find(x => x.type === type);
  const box = $("#providerFields");
  if (!t) { box.innerHTML = ""; return; }
  box.innerHTML = t.fields.map(f => {
    const val = currentProviderConfig && currentProviderConfig[f.name] !== undefined
      ? currentProviderConfig[f.name] : (f.default ?? "");
    if (f.type === "bool") {
      return `<div class="field"><label class="chk">
        <input type="checkbox" data-field="${f.name}" ${val ? "checked" : ""}> ${esc(f.label)}
      </label>${f.help ? `<small>${esc(f.help)}</small>` : ""}</div>`;
    }
    if (f.type === "select") {
      const opts = (f.options || []).map(o =>
        `<option value="${esc(o)}"${String(val) === o ? " selected" : ""}>${esc(o || "(none)")}</option>`).join("");
      return `<div class="field"><label>${esc(f.label)}
        <select data-field="${f.name}" class="grow">${opts}</select>
      </label>${f.help ? `<small>${esc(f.help)}</small>` : ""}</div>`;
    }
    return `<div class="field"><label>${esc(f.label)}${f.required ? " *" : ""}
      <input type="${f.type === "password" ? "password" : f.type === "number" ? "number" : "text"}"
        data-field="${f.name}" value="${esc(val)}" ${f.required ? "required" : ""}>
    </label>${f.help ? `<small>${esc(f.help)}</small>` : ""}</div>`;
  }).join("");
  attachProviderPresets();
}

// For Xvids-style file_host providers, picking a platform preset auto-fills the
// API host, so adding a new similar host (StreamHG, EarnVids, ...) is one click.
const PRESET_HOSTS = {
  StreamHG: "https://streamhgapi.com",
  EarnVids: "https://earnvidsapi.com",
  Vidoza: "https://vidoza.net",
  LuluStream: "https://lulustream.com",
};

function attachProviderPresets() {
  if ($("#providerType").value !== "file_host") return;
  const platform = $('[data-field="platform"]');
  const host = $('[data-field="api_host"]');
  if (!platform || !host) return;
  const apply = () => {
    const preset = PRESET_HOSTS[platform.value];
    if (preset) host.value = preset;
  };
  platform.addEventListener("change", apply);
}

function collectProviderConfig() {
  const cfg = {};
  $$("#providerFields [data-field]").forEach(el => {
    if (el.type === "checkbox") cfg[el.dataset.field] = el.checked;
    else if (el.type === "number") cfg[el.dataset.field] = el.value === "" ? null : Number(el.value);
    else cfg[el.dataset.field] = el.value;
  });
  return cfg;
}

async function saveProvider() {
  const name = $("#providerName").value.trim();
  if (!name) return alert("Name is required");
  const body = {
    name,
    type: $("#providerType").value,
    config: collectProviderConfig(),
    enabled: $("#providerEnabled").checked,
  };
  try {
    if (editingProviderId) {
      await api("/api/providers/" + editingProviderId, { method: "PUT", body: JSON.stringify(body) });
    } else {
      await api("/api/providers", { method: "POST", body: JSON.stringify(body) });
    }
  } catch (e) { return alert("Save failed: " + e.message); }
  resetProviderForm();
  refreshProviders();
}

function resetProviderForm() {
  editingProviderId = null;
  currentProviderConfig = null;
  $("#providerName").value = "";
  $("#providerEnabled").checked = true;
  $("#providerFormTitle").textContent = "Add Provider";
  $("#providerSubmit").textContent = "Add";
  renderProviderFields();
}

async function refreshProviders() {
  let d;
  try { d = await api("/api/providers"); } catch (e) { return; }
  const box = $("#providerList");
  if (!d.providers.length) { box.innerHTML = '<p class="muted">No providers configured.</p>'; return; }
  box.innerHTML = d.providers.map(p => {
    const c = p.config || {};
    const summary = p.type === "webdav" ? c.url
      : p.type === "s3" ? (c.bucket || "")
      : p.type === "sftp" ? c.host
      : p.type === "file_host" ? c.api_host
      : c.target_dir;
    return `<div class="card provider-card">
      <div class="pc-head">
        <div><strong>${esc(p.name)}</strong> <span class="muted">(${esc(p.type)})</span>
          <span class="st-badge ${p.enabled ? "st-completed" : "st-skipped"}">${p.enabled ? "enabled" : "disabled"}</span></div>
        <div>
          <button class="btn small" onclick="testProvider(${p.id})">Test</button>
          <button class="btn small" onclick="editProvider(${p.id})">Edit</button>
          <button class="btn small" onclick="toggleProvider(${p.id})">${p.enabled ? "Disable" : "Enable"}</button>
          <button class="btn small danger" onclick="deleteProvider(${p.id})">Delete</button>
        </div>
      </div>
      <div class="muted">${esc(summary || "")}</div>
      <div id="testResult-${p.id}"></div>
    </div>`;
  }).join("");
}

async function testProvider(id) {
  const el = $("#testResult-" + id);
  el.innerHTML = "Testing...";
  try {
    const r = await api(`/api/providers/${id}/test`, { method: "POST" });
    el.innerHTML = r.ok
      ? `<span class="ok">OK &mdash; ${esc(r.message)}</span>`
      : `<span class="err">FAILED &mdash; ${esc(r.message)}</span>`;
  } catch (e) {
    el.innerHTML = `<span class="err">FAILED &mdash; ${esc(e.message)}</span>`;
  }
}

async function editProvider(id) {
  const d = await api("/api/providers");
  const p = d.providers.find(x => x.id === id);
  if (!p) return;
  editingProviderId = id;
  currentProviderConfig = p.config;
  $("#providerName").value = p.name;
  $("#providerType").value = p.type;
  $("#providerEnabled").checked = p.enabled;
  $("#providerFormTitle").textContent = "Edit Provider";
  $("#providerSubmit").textContent = "Update";
  renderProviderFields();
}

async function toggleProvider(id) {
  const d = await api("/api/providers");
  const p = d.providers.find(x => x.id === id);
  if (!p) return;
  await api("/api/providers/" + id, {
    method: "PUT",
    body: JSON.stringify({ name: p.name, type: p.type, config: p.config, enabled: !p.enabled }),
  });
  refreshProviders();
}

async function deleteProvider(id) {
  if (!confirm("Delete this provider?")) return;
  await api("/api/providers/" + id, { method: "DELETE" });
  refreshProviders();
}

/* ---------------- Watch paths ---------------- */

let _providersForSelect = [];

async function loadProvidersForSelect() {
  let d;
  try { d = await api("/api/providers"); } catch (e) { return; }
  _providersForSelect = (d.providers || []).map(p => ({ id: p.id, name: p.name, enabled: p.enabled }));
}

function providerOptions(selectedIds) {
  const sel = new Set(Array.isArray(selectedIds) ? selectedIds : String(selectedIds || "").split(",").filter(x => x));
  return _providersForSelect.map(p =>
    `<option value="${p.id}"${sel.has(String(p.id)) ? " selected" : ""}>${esc(p.name)}${p.enabled ? "" : " (off)"}</option>`
  ).join("");
}

async function refreshPaths() {
  let d;
  try { d = await api("/api/watchpaths"); } catch (e) { return; }
  const box = $("#pathList");
  if (!d.paths.length) { box.innerHTML = '<p class="muted">No watch paths configured.</p>'; return; }
  box.innerHTML = d.paths.map(p => {
    const existsBadge = p.exists
      ? '<span class="st-badge st-completed">exists</span>'
      : '<span class="st-badge st-failed">not found in container</span>';
    const remoteDir = p.effective_remote_dir || p.remote_dir || "";
    const remoteBadge = remoteDir
      ? `<span class="st-badge st-completed">cloud folder: ${esc(remoteDir)} (auto)</span>`
      : "";
    const provLabel = p.provider_ids
      ? `providers: ${esc(p.provider_ids)}`
      : "providers: all";
    return `<div class="card path-card">
      <div><code>${esc(p.path)}</code> ${existsBadge}
        <span class="st-badge ${p.enabled ? "st-completed" : "st-skipped"}">${p.enabled ? "enabled" : "disabled"}</span>
        ${remoteBadge} <span class="st-badge st-info">${provLabel}</span></div>
      <div class="row" style="margin-top:6px;align-items:flex-end">
        <label style="font-size:12px">Providers
          <select id="pp-${p.id}" multiple size="3" class="grow">${providerOptions(p.provider_ids)}</select>
        </label>
        <button class="btn small" onclick="savePathProviders(${p.id})">Save</button>
        <button class="btn small" onclick="togglePath(${p.id}, ${p.enabled ? 0 : 1})">${p.enabled ? "Disable" : "Enable"}</button>
        <button class="btn small danger" onclick="deletePath(${p.id})">Remove</button>
      </div>
    </div>`;
  }).join("");
}

async function addPath() {
  const input = $("#pathInput");
  const v = input.value.trim();
  if (!v) return alert("Path is required");
  const sel = Array.from($("#pathProviders").selectedOptions).map(o => o.value);
  const body = { path: v, enabled: true };
  if (sel.length) body.provider_ids = sel.join(",");
  try { await api("/api/watchpaths", { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { return alert(e.message); }
  input.value = "";
  const psel = $("#pathProviders");
  if (psel) Array.from(psel.options).forEach(o => o.selected = false);
  refreshPaths();
}

async function savePathProviders(id) {
  const el = $("#pp-" + id);
  if (!el) return;
  const sel = Array.from(el.selectedOptions).map(o => o.value).join(",");
  try { await api("/api/watchpaths/" + id, { method: "PUT", body: JSON.stringify({ enabled: true, provider_ids: sel }) }); }
  catch (e) { return alert(e.message); }
  refreshPaths();
}

async function togglePath(id, enabled) {
  try { await api("/api/watchpaths/" + id, { method: "PUT", body: JSON.stringify({ enabled: Boolean(enabled) }) }); }
  catch (e) { return alert(e.message); }
  refreshPaths();
}

async function deletePath(id) {
  if (!confirm("Remove this watch path?")) return;
  await api("/api/watchpaths/" + id, { method: "DELETE" });
  refreshPaths();
}

/* ---------------- Logs ---------------- */

async function refreshLogs() {
  let d;
  try { d = await api("/api/logs?lines=400"); } catch (e) { return; }
  const box = $("#logBox");
  box.textContent = d.logs;
  box.scrollTop = box.scrollHeight;
}

/* ---------------- Tabs / init ---------------- */

$$("[data-tab]").forEach(btn => btn.addEventListener("click", () => {
  $$("[data-tab]").forEach(b => b.classList.toggle("active", b === btn));
  $$("main section").forEach(s => s.classList.add("hidden"));
  $("#tab-" + btn.dataset.tab).classList.remove("hidden");
  if (btn.dataset.tab === "logs") refreshLogs();
}));

async function refreshAll() {
  await Promise.all([refreshStats(), refreshQueue(), refreshProviders(), refreshPaths(), refreshNotifyStatus()]);
}

$("#btnScan").onclick = async () => {
  try {
    const r = await api("/api/scan", { method: "POST" });
    toast(`Scan done: ${r.files} file(s) found, ${r.queued} newly queued`);
    refreshQueue(); refreshStats();
  } catch (e) {
    toast("Scan failed: " + e.message, true);
  }
};
$("#btnClearCompleted").onclick = async () => {
  await api("/api/queue/clear?status=completed", { method: "POST" });
  refreshQueue(); refreshStats();
};
$("#btnClearFailed").onclick = async () => {
  await api("/api/queue/clear?status=failed", { method: "POST" });
  refreshQueue(); refreshStats();
};
$("#btnTestNotify").onclick = async () => {
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    toast(r.ok ? "Telegram: " + r.message : "Telegram error: " + r.message, !r.ok);
  } catch (e) {
    toast("Telegram test failed: " + e.message, true);
  }
};
async function refreshNotifyStatus() {
  try {
    const r = await api("/api/notify/status");
    const el = $("#notifyStatus");
    if (!el) return;
    el.textContent = r.enabled ? "Telegram: on" : "Telegram: off";
    el.className = r.enabled ? "muted ok" : "muted";
  } catch (e) {}
}
/* ---------------- Settings (Telegram) ---------------- */

async function loadSettings() {
  try {
    const r = await api("/api/settings");
    $("#tgToken").value = r.telegram_bot_token || "";
    $("#tgChatId").value = r.telegram_chat_id || "";
  } catch (e) {}
}
async function saveSettings() {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        telegram_bot_token: $("#tgToken").value.trim(),
        telegram_chat_id: $("#tgChatId").value.trim(),
      }),
    });
    $("#settingsStatus").textContent = "Saved";
    refreshNotifyStatus();
  } catch (e) {
    $("#settingsStatus").textContent = "Save failed: " + e.message;
  }
}
$("#btnTestNotify2").onclick = async () => {
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    toast(r.ok ? "Telegram: " + r.message : "Telegram error: " + r.message, !r.ok);
  } catch (e) {
    toast("Telegram test failed: " + e.message, true);
  }
};

$("#btnPause").onclick = async () => {
  const paused = $("#btnPause").dataset.paused === "1";
  if (paused) {
    await api("/api/resume", { method: "POST" });
    $("#btnPause").dataset.paused = "0";
    $("#btnPause").textContent = "Pause";
  } else {
    await api("/api/pause", { method: "POST" });
    $("#btnPause").dataset.paused = "1";
    $("#btnPause").textContent = "Resume";
  }
};

async function init() {
  await loadProviderTypes();
  await loadProvidersForSelect();
  await loadSettings();
  await refreshAll();
  setInterval(() => { refreshStats(); refreshQueue(); }, 3000);
  setInterval(refreshLogs, 5000);
}

init();
