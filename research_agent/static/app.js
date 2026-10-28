// Research Agent web app: list digests, show one, start ad-hoc runs, watch progress.

const $ = (sel) => document.querySelector(sel);
const state = { runs: [], filter: "all", selected: null, overview: null, watching: new Set() };

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.error || `HTTP ${res.status}`);
  return body;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 4000);
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}

// --- overview (spend, defaults) ------------------------------------------------
async function loadOverview() {
  const o = await api("/api/overview");
  state.overview = o;
  $("#spend").textContent =
    `Today $${o.spend_today.toFixed(2)} of $${o.budget_daily} · ` +
    `Month $${o.spend_month.toFixed(2)} of $${o.budget_monthly}`;
}

// --- digest list -----------------------------------------------------------------
function runMeta(r) {
  if (r.state === "failed") return r.error ? `Failed: ${r.error}` : "Failed";
  if (r.state === "running" || r.state === "queued") return "In progress…";
  const bits = [];
  if (r.matched != null) bits.push(`${r.matched} matched`);
  if (r.scanned != null) bits.push(`${r.scanned} scanned`);
  bits.push(`$${(r.usd || 0).toFixed(2)}`);
  return bits.join(" · ");
}

function renderRuns() {
  const list = $("#runs");
  const runs = state.runs.filter((r) => state.filter === "all" || r.kind === state.filter);
  if (!runs.length) {
    list.innerHTML = `<li class="none">No digests yet.</li>`;
    return;
  }
  list.innerHTML = runs.map((r) => {
    const badges = [
      r.kind === "adhoc" ? `<span class="badge adhoc">Ad-hoc</span>` : "",
      ["running", "queued", "failed"].includes(r.state)
        ? `<span class="badge ${r.state}">${r.state === "failed" ? "Failed" : "Running"}</span>` : "",
    ].join("");
    return `<li class="run ${r.id === state.selected ? "selected" : ""}" data-id="${escapeHtml(r.id)}"
              title="${escapeHtml(r.label)}">
      <div class="run-top"><span class="run-label">${escapeHtml(r.label)}</span>${badges}</div>
      <div class="run-meta">${escapeHtml(runMeta(r))}</div>
    </li>`;
  }).join("");
}

async function loadRuns() {
  state.runs = await api("/api/runs");
  renderRuns();
  if (state.selected) showRun(state.selected, { keepFrame: true });
}

function showRun(id, { keepFrame = false } = {}) {
  const r = state.runs.find((x) => x.id === id);
  state.selected = id;
  history.replaceState(null, "", `#run=${encodeURIComponent(id)}`);
  renderRuns();
  const frame = $("#frame"), empty = $("#empty"), open = $("#open-tab");
  $("#viewer-title").textContent = r ? r.label : "Pick a digest";
  $("#viewer-meta").textContent = r ? (r.kind === "adhoc" ? "Ad-hoc digest · " : "Daily digest · ") + runMeta(r) : "";
  if (r && r.digest) {
    const src = `${r.digest}?v=${encodeURIComponent(r.usd)}`;
    if (!keepFrame || !frame.src.includes(r.digest)) frame.src = src;
    frame.hidden = false;
    empty.hidden = true;
    open.href = r.digest;
    open.hidden = false;
  } else {
    frame.hidden = true;
    empty.hidden = false;
    open.hidden = true;
    empty.innerHTML = r && (r.state === "running" || r.state === "queued")
      ? `<p>This digest is still being made.</p><p class="muted">Progress is shown above.</p>`
      : r && r.state === "failed"
        ? `<p>This run failed.</p><p class="muted">${escapeHtml(r.error || "See the logs folder.")}</p>`
        : `<p>No digest selected.</p><p class="muted">Pick one on the left, or make a new one.</p>`;
  }
}

// --- live status --------------------------------------------------------------------
function renderStatus(active) {
  const box = $("#status");
  if (!active.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const openLogs = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.id));
  box.hidden = false;
  box.innerHTML = active.map((s) => {
    const pct = s.total > 0 ? Math.min(100, (100 * s.done) / s.total) : null;
    const counts = s.total > 0 ? ` · ${s.done} of ${s.total}` : "";
    const elapsed = fmtDuration(Date.now() / 1000 - (s.started || s.requested || Date.now() / 1000));
    return `<div class="job">
      <div class="job-head">
        <span class="job-title">${s.kind === "adhoc" ? "Ad-hoc" : "Daily"}: ${escapeHtml(s.label)}</span>
        <span class="job-stage">${escapeHtml(s.stage_label)}${counts}</span>
        <span class="job-meta">${escapeHtml(s.detail || "")} · $${(s.usd || 0).toFixed(2)} so far · ${elapsed}</span>
      </div>
      <div class="bar ${pct === null ? "indeterminate" : ""}"><div style="width:${pct ?? 30}%"></div></div>
      <details data-id="${escapeHtml(s.run_id)}" ${openLogs.has(s.run_id) ? "open" : ""}>
        <summary>Log</summary><pre>${escapeHtml((s.log || []).join("\n")) || "Starting…"}</pre>
      </details>
    </div>`;
  }).join("");
}

async function pollStatus() {
  let delay = 10000;
  try {
    const { active, last_finished: last } = await api("/api/status");
    renderStatus(active);
    const activeIds = new Set(active.map((s) => s.run_id));
    // A run we were watching has finished: refresh and show its digest.
    for (const id of [...state.watching]) {
      if (!activeIds.has(id)) {
        state.watching.delete(id);
        await Promise.all([loadRuns(), loadOverview()]);
        const r = state.runs.find((x) => x.id === id);
        if (r && r.state === "done") {
          toast(`Digest ready: ${r.matched ?? 0} papers matched`);
          showRun(id);
        } else if (r && r.state === "failed") {
          toast("The run failed. See its entry for details.");
          showRun(id);
        }
      }
    }
    for (const s of active) {
      if (!state.watching.has(s.run_id)) {
        state.watching.add(s.run_id);
        loadRuns();
      }
    }
    if (active.length) {
      delay = 2000;
      loadOverview();
    }
  } catch (e) {
    console.error(e);
  }
  clearTimeout(pollStatus.timer);
  pollStatus.timer = setTimeout(pollStatus, delay);
}

// --- ad-hoc form ----------------------------------------------------------------------
function isoDaysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toLocaleDateString("en-CA"); // YYYY-MM-DD in local time
}

function updateEstimate() {
  const form = $("#adhoc-form");
  const from = new Date(form.date_from.value), to = new Date(form.date_to.value);
  const days = Math.round((to - from) / 86400000) + 1;
  const max = state.overview?.max_adhoc_days ?? 14;
  const err = $("#form-error");
  if (!form.date_from.value || !form.date_to.value || isNaN(days)) {
    $("#estimate").textContent = "";
    return;
  }
  if (days < 1) {
    err.textContent = "The start date is after the end date.";
    err.hidden = false;
  } else if (days > max) {
    err.textContent = `Pick at most ${max} days.`;
    err.hidden = false;
  } else {
    err.hidden = true;
  }
  $("#estimate").textContent = days >= 1 && days <= max
    ? `${days} day${days > 1 ? "s" : ""} of submissions. At most 60 papers get a full read, ` +
      `so this costs roughly $2–6 and takes about 15–40 minutes (arXiv asks for slow downloads).`
    : "";
}

function openAdhoc() {
  const form = $("#adhoc-form");
  form.date_from.value = isoDaysAgo(6);
  form.date_to.value = isoDaysAgo(0);
  form.date_from.max = form.date_to.max = isoDaysAgo(0);
  form.categories.value = (state.overview?.categories || []).join(", ");
  $("#form-error").hidden = true;
  updateEstimate();
  $("#adhoc-dialog").showModal();
}

async function submitAdhoc(event) {
  event.preventDefault();
  const form = $("#adhoc-form");
  const button = $("#submit-adhoc");
  button.disabled = true;
  try {
    const { run_id } = await api("/api/adhoc", {
      method: "POST",
      body: JSON.stringify({
        date_from: form.date_from.value,
        date_to: form.date_to.value,
        focus: form.focus.value,
        categories: form.categories.value.split(",").map((c) => c.trim()).filter(Boolean),
        research: form.research.checked,
        images: form.images.checked,
      }),
    });
    $("#adhoc-dialog").close();
    form.focus.value = "";
    state.watching.add(run_id);
    toast("Ad-hoc digest started");
    await loadRuns();
    showRun(run_id);
    pollStatus();
  } catch (e) {
    $("#form-error").textContent = e.message;
    $("#form-error").hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function runDaily() {
  try {
    const { run_id } = await api("/api/daily", { method: "POST" });
    state.watching.add(run_id);
    toast("Today's digest started");
    await loadRuns();
    pollStatus();
  } catch (e) {
    toast(e.message);
  }
}

// --- wiring ------------------------------------------------------------------------------
$("#runs").addEventListener("click", (e) => {
  const li = e.target.closest(".run");
  if (li) showRun(li.dataset.id);
});
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  state.filter = tab.dataset.filter;
  renderRuns();
}));
$("#new-adhoc").addEventListener("click", openAdhoc);
$("#cancel-adhoc").addEventListener("click", () => $("#adhoc-dialog").close());
$("#adhoc-form").addEventListener("submit", submitAdhoc);
$("#adhoc-form").addEventListener("input", updateEstimate);
$("#run-daily").addEventListener("click", runDaily);

(async function start() {
  await Promise.all([loadOverview(), loadRuns()]);
  const fromHash = decodeURIComponent((location.hash.match(/run=([^&]+)/) || [])[1] || "");
  const first = state.runs.find((r) => r.id === fromHash) || state.runs.find((r) => r.digest);
  if (first) showRun(first.id);
  pollStatus();
})();
