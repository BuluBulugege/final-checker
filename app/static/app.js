/* final-checker UI controller: submit job, stream SSE, render live results. */
"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  keys: $("keysInput"),
  keyCount: $("keyCount"),
  conc: $("concurrency"),
  concVal: $("concVal"),
  fullLoad: $("fullLoad"),
  run: $("runBtn"),
  cancel: $("cancelBtn"),
  warn: $("warnBox"),
  body: $("resultsBody"),
  jobMeta: $("jobMeta"),
  overallBar: $("overallBar"),
  overallFill: $("overallFill"),
  overallText: $("overallText"),
  pills: $("pluginPills"),
};

let mode = "grade";
let evtSource = null;
let currentJob = null;
const rows = new Map(); // index -> <tr>

// ---------- init ----------
init();
async function init() {
  countKeys();
  els.keys.addEventListener("input", countKeys);
  els.conc.addEventListener("input", () => (els.concVal.value = els.conc.value));
  document.querySelectorAll(".mode-btn").forEach((btn) =>
    btn.addEventListener("click", () => selectMode(btn))
  );
  els.run.addEventListener("click", run);
  els.cancel.addEventListener("click", cancel);
  loadPlugins();
}

function countKeys() {
  els.keyCount.textContent = countCredentials(els.keys.value);
}

// Mirror of the server's parse_credentials: each balanced top-level {...} JSON
// block counts as one credential; remaining non-empty lines count one each.
function countCredentials(raw) {
  let count = 0;
  let i = 0;
  const n = raw.length;
  let lineBuf = "";
  const flushLines = (text) => {
    text.split("\n").forEach((l) => {
      if (l.trim()) count++;
    });
  };
  while (i < n) {
    if (raw[i] === "{") {
      let depth = 0,
        inStr = false,
        esc = false,
        j = i;
      for (; j < n; j++) {
        const c = raw[j];
        if (inStr) {
          if (esc) esc = false;
          else if (c === "\\") esc = true;
          else if (c === '"') inStr = false;
        } else if (c === '"') inStr = true;
        else if (c === "{") depth++;
        else if (c === "}") {
          depth--;
          if (depth === 0) break;
        }
      }
      if (depth !== 0) break;
      flushLines(lineBuf);
      lineBuf = "";
      count++; // the JSON block
      i = j + 1;
    } else {
      lineBuf += raw[i];
      i++;
    }
  }
  flushLines(lineBuf);
  return count;
}

function selectMode(btn) {
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.classList.remove("is-active");
    b.setAttribute("aria-checked", "false");
  });
  btn.classList.add("is-active");
  btn.setAttribute("aria-checked", "true");
  mode = btn.dataset.mode;
}

async function loadPlugins() {
  try {
    const res = await fetch("/api/plugins");
    const data = await res.json();
    els.pills.innerHTML = "";
    (data.plugins || []).forEach((p) => {
      const span = document.createElement("span");
      span.className = "plugin-pill";
      span.textContent = p;
      els.pills.appendChild(span);
    });
  } catch (_) {}
}

// ---------- run ----------
async function run() {
  const keys = els.keys.value;
  if (!keys.split("\n").map((l) => l.trim()).filter(Boolean).length) {
    showWarn("请先粘贴至少一个密钥");
    return;
  }
  hideWarn();
  els.run.disabled = true;
  els.body.innerHTML = "";
  rows.clear();

  let job;
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        keys,
        mode,
        concurrency: parseInt(els.conc.value, 10),
        full_load: els.fullLoad.checked,
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    job = await res.json();
  } catch (e) {
    showWarn("提交失败: " + e.message);
    els.run.disabled = false;
    return;
  }

  currentJob = job.job_id;
  els.cancel.hidden = false;
  els.overallBar.hidden = false;
  renderJobMeta(job);
  job.results.forEach(upsertRow);
  startStream(job.job_id);
}

function startStream(jobId) {
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/jobs/${jobId}/stream`);
  evtSource.addEventListener("snapshot", (e) => {
    const job = JSON.parse(e.data);
    renderJobMeta(job);
    job.results.forEach(upsertRow);
  });
  evtSource.addEventListener("key", (e) => upsertRow(JSON.parse(e.data)));
  evtSource.addEventListener("job", (e) => renderJobMeta(JSON.parse(e.data)));
  evtSource.addEventListener("done", () => finish());
  evtSource.onerror = () => {
    // SSE auto-reconnects; if job already done we close in finish()
  };
}

function finish() {
  if (evtSource) evtSource.close();
  evtSource = null;
  els.run.disabled = false;
  els.cancel.hidden = true;
}

async function cancel() {
  if (!currentJob) return;
  await fetch(`/api/jobs/${currentJob}/cancel`, { method: "POST" });
  finish();
}

// ---------- render ----------
function renderJobMeta(job) {
  els.jobMeta.textContent =
    `job ${job.job_id} · ${job.mode} · 并发 ${job.concurrency} · ` +
    `${job.completed}/${job.total} · ${job.state}`;
  const pct = job.total ? Math.round((job.completed / job.total) * 100) : 0;
  els.overallFill.style.width = pct + "%";
  els.overallText.textContent = `${job.completed} / ${job.total} (${pct}%)`;
}

function upsertRow(r) {
  let tr = rows.get(r.index);
  if (!tr) {
    tr = document.createElement("tr");
    rows.set(r.index, tr);
    els.body.appendChild(tr);
  }
  const prog = Math.round((r.progress || 0) * 100);
  tr.innerHTML = `
    <td class="col-idx">${r.index + 1}</td>
    <td>${provBadge(r.provider)}</td>
    <td class="col-key">${escapeHtml(r.masked_key)}</td>
    <td>${statusChip(r.status)}</td>
    <td>${tierLabel(r.tier)}</td>
    <td>
      <div class="row-prog"><div class="row-prog-fill" style="width:${prog}%"></div></div>
      <span class="row-prog-label">${escapeHtml(r.progress_label || "")}</span>
    </td>
    <td>${remarks(r)}</td>`;
}

function provBadge(p) {
  if (!p) return `<span class="prov-badge prov-unknown">?</span>`;
  return `<span class="prov-badge prov-${p}">${p}</span>`;
}

function statusChip(s) {
  const labels = {
    pending: "等待", running: "检查中", alive: "存活", dead: "失效",
    graded: "已评级", error: "错误", unsupported: "不支持",
  };
  return `<span class="status-chip st-${s}">${labels[s] || s}</span>`;
}

function tierLabel(t) {
  if (!t) return "—";
  const ent = t.includes("企业") ? " tier-ent" : "";
  return `<span class="tier-label${ent}">${escapeHtml(t)}</span>`;
}

function remarks(r) {
  let html = "";
  if (r.remarks && r.remarks.length) {
    html += `<ul class="remarks-list">${r.remarks
      .map((x) => `<li>${escapeHtml(x)}</li>`)
      .join("")}</ul>`;
  }
  if (r.download_filename && currentJob) {
    const url = `/api/jobs/${encodeURIComponent(currentJob)}/download/${r.index}`;
    html += `<a class="dl-btn" href="${url}" download="${escapeHtml(
      r.download_filename
    )}">⬇ 下载完整信息</a>`;
  }
  if (r.error) html += `<div class="row-error">⚠ ${escapeHtml(r.error)}</div>`;
  return html || "—";
}

// ---------- utils ----------
function showWarn(msg) { els.warn.textContent = msg; els.warn.hidden = false; }
function hideWarn() { els.warn.hidden = true; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
