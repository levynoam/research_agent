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
  if (r.state === "done" && !r.digest && r.scanned === 0) return "No new papers";
  const bits = [];
  if (r.digest && r.note) bits.push("Incomplete");
  if (r.matched != null) bits.push(`${r.matched} matched`);
  if (r.scanned != null) bits.push(`${r.scanned} scanned`);
  bits.push(`$${(r.usd || 0).toFixed(2)}`);
  return bits.join(" · ");
}

function writtenAt(r) {
  // When the digest was last written, e.g. "written Tue 10 Nov, 22:14".
  if (!r.digest || !r.finished) return "";
  const when = new Date(r.finished);
  if (isNaN(when)) return "";
  return " · written " + when.toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
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
  history.replaceState(null, "", id ? `#run=${encodeURIComponent(id)}` : location.pathname);
  renderRuns();
  const frame = $("#frame"), empty = $("#empty"), open = $("#open-tab");
  $("#viewer-title").textContent = r ? r.label : "Pick a digest";
  $("#viewer-meta").textContent = r ? (r.kind === "adhoc" ? "Ad-hoc digest · " : "Daily digest · ") + runMeta(r) + writtenAt(r) : "";
  const fbButton = $("#feedback-btn");
  $("#delete-btn").hidden = !r || ["running", "queued"].includes(r.state);
  $("#retry-btn").hidden = !r || !r.can_retry || ["running", "queued"].includes(r.state);
  if (r && r.digest) {
    const src = `${r.digest}?v=${encodeURIComponent(r.usd)}`;
    if (!keepFrame || !frame.src.includes(r.digest)) frame.src = src;
    frame.hidden = false;
    empty.hidden = true;
    open.href = r.digest;
    open.hidden = false;
    fbButton.hidden = r.state !== "done";
  } else {
    frame.hidden = true;
    empty.hidden = false;
    open.hidden = true;
    fbButton.hidden = true;
    empty.innerHTML = r && (r.state === "running" || r.state === "queued")
      ? `<p>This digest is still being made.</p><p class="muted">Progress is shown above.</p>`
      : r && r.note
        ? `<p>No digest for this run.</p><p class="muted note">${escapeHtml(r.note)}</p>`
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
        if (r && r.state === "done" && !r.digest && r.note) {
          toast("No new papers, so no digest");
          showRun(id);
        } else if (r && r.state === "done") {
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

// --- prompt files (criteria.md, summary_format.md) ---------------------------------------
const prompts = { files: [], current: null, edits: {} }; // edits: name -> unsaved text

function promptFile(name) { return prompts.files.find((f) => f.name === name); }
function isDirty(name) {
  const f = promptFile(name);
  return f && name in prompts.edits && prompts.edits[name] !== f.text;
}

function renderPromptTabs() {
  $("#prompt-tabs").innerHTML = prompts.files.map((f) => `
    <button class="tab ${f.name === prompts.current ? "active" : ""}" data-name="${f.name}">
      ${escapeHtml(f.title)}${isDirty(f.name) ? " •" : ""}</button>`).join("");
}

function renderPromptStatus(message, kind = "") {
  const f = promptFile(prompts.current);
  const summary = $("#prompt-summary");
  if (message) {
    summary.textContent = message;
    summary.className = `prompt-summary ${kind}`;
  } else if (f.problem) {
    summary.textContent = `This file has a problem the agent will stop on: ${f.problem}`;
    summary.className = "prompt-summary bad";
  } else {
    summary.textContent = f.summary;
    summary.className = "prompt-summary";
  }
  const dirty = isDirty(prompts.current);
  $("#prompt-state").textContent = dirty ? "Unsaved changes" : `${f.file} · saved`;
  $("#prompt-save").disabled = !dirty;
  $("#prompt-revert").disabled = !dirty;
  renderPromptTabs();
}

function selectPrompt(name) {
  if (prompts.current) prompts.edits[prompts.current] = $("#prompt-text").value;
  prompts.current = name;
  const f = promptFile(name);
  $("#prompt-help").textContent = f.help;
  const box = $("#prompt-text");
  box.value = prompts.edits[name] ?? f.text;
  box.setSelectionRange(0, 0);
  box.scrollTop = 0;
  renderPromptStatus();
}

async function openPrompts() {
  try {
    prompts.files = await api("/api/prompts");
  } catch (e) {
    toast(`Could not load the files: ${e.message}`);
    return;
  }
  // Keep unsaved edits from last time only if the file hasn't changed underneath them.
  for (const f of prompts.files) {
    if (prompts.edits[f.name] !== undefined && prompts.edits[`${f.name}:base`] !== f.version) {
      delete prompts.edits[f.name];
    }
  }
  const name = prompts.current && promptFile(prompts.current) ? prompts.current : prompts.files[0].name;
  prompts.current = null;
  selectPrompt(name);
  $("#prompts-dialog").showModal();
  $("#prompt-text").focus();
}

async function savePrompt() {
  const name = prompts.current, f = promptFile(name);
  const text = $("#prompt-text").value;
  $("#prompt-save").disabled = true;
  try {
    const res = await api(`/api/prompts/${name}`, {
      method: "POST", body: JSON.stringify({ text, version: f.version }),
    });
    f.text = text.endsWith("\n") ? text : text + "\n";
    f.version = res.version;
    f.summary = res.summary;
    f.problem = null;
    delete prompts.edits[name];
    $("#prompt-text").value = f.text;
    renderPromptStatus();
    toast(`${f.file} saved. The next run uses it.`);
    if (name === "criteria") loadOverview();
  } catch (e) {
    renderPromptStatus(e.message, "bad");
  }
}

function closePrompts(event) {
  prompts.edits[prompts.current] = $("#prompt-text").value;
  for (const f of prompts.files) prompts.edits[`${f.name}:base`] = f.version;
  const unsaved = prompts.files.filter((f) => isDirty(f.name)).map((f) => f.file);
  if (unsaved.length && !confirm(`Close without saving ${unsaved.join(" and ")}? ` +
                                 "Your changes stay here until you reload the page.")) {
    event?.preventDefault();
    return;
  }
  $("#prompts-dialog").close();
}

$("#edit-prompts").addEventListener("click", openPrompts);
$("#prompt-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) selectPrompt(tab.dataset.name);
});
$("#prompt-text").addEventListener("input", () => {
  prompts.edits[prompts.current] = $("#prompt-text").value;
  renderPromptStatus();
});
$("#prompt-save").addEventListener("click", savePrompt);
$("#prompt-revert").addEventListener("click", () => {
  delete prompts.edits[prompts.current];
  $("#prompt-text").value = promptFile(prompts.current).text;
  renderPromptStatus();
});
$("#prompt-close").addEventListener("click", () => closePrompts());
$("#prompts-dialog").addEventListener("cancel", closePrompts); // the Esc key
$("#prompts-dialog").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") {
    e.preventDefault();
    if (isDirty(prompts.current)) savePrompt();
  }
});

// --- feedback: +/- marks in the digest drive a revision of criteria.md -------------------
const feedback = { runId: null, timer: null };

function roundStatus(r) {
  return { running: "Being applied…", done: "Applied", unchanged: "No change needed",
           undone: "Undone", failed: "Failed" }[r.status] || r.status;
}

function renderRounds(rounds) {
  $("#feedback-rounds").innerHTML = rounds.length ? `<h3>Rounds on this digest</h3>` + rounds.map((r) => `
    <div class="round ${r.status}">
      <div class="round-head"><b>${escapeHtml(r.created.replace("T", " "))}</b>
        · ${roundStatus(r)}${r.usd ? ` · $${r.usd.toFixed(2)}` : ""}
        <span class="muted">· ${r.n_pos} marked +, ${r.n_neg} marked −${r.text ? ", with a message" : ""}</span>
        ${r.status === "done" ? `<button type="button" class="btn small" data-undo="${r.rid}">Undo</button>` : ""}
      </div>
      ${r.status === "running" ? `<div class="bar indeterminate"><div></div></div>` : ""}
      ${r.changes.length ? `<ul>${r.changes.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>` : ""}
      ${r.notes ? `<p class="muted">${escapeHtml(r.notes)}</p>` : ""}
      ${r.error ? `<p class="form-error">${escapeHtml(r.error)}</p>` : ""}
      ${r.diff ? `<details><summary>Show the change to criteria.md</summary><pre>${escapeHtml(r.diff)}</pre></details>` : ""}
    </div>`).join("") : "";
}

function redrawDigestMarks() {
  // Reloads the digest page so its +/− buttons show the pending marks (they clear once a
  // round is sent). A reload works for digests written by any version; the scroll spot is kept.
  const frame = $("#frame"), win = frame.contentWindow;
  if (!win || !frame.src) return;
  const y = win.scrollY;
  frame.addEventListener("load", () => frame.contentWindow.scrollTo(0, y), { once: true });
  win.location.reload();
}

async function loadFeedback() {
  const data = await api(`/api/feedback/${encodeURIComponent(feedback.runId)}`);
  const marks = Object.values(data.verdicts);
  const pos = marks.filter((v) => v.verdict > 0).length, neg = marks.filter((v) => v.verdict < 0).length;
  $("#feedback-counts").textContent = pos || neg
    ? `${pos} paper${pos === 1 ? "" : "s"} marked +, ${neg} marked − in this digest.`
    : "No papers marked yet. Use the + / − buttons next to each paper in the digest " +
      "(and “should have been in” under Screened out), or just write a message.";
  $("#submit-feedback").disabled = data.busy;
  $("#submit-feedback").textContent = data.busy ? "Applying a round…" : "Send feedback";
  renderRounds(data.rounds);
  if (data.rounds.some((r) => r.status === "running")) watchRefinement();
}

async function openFeedback() {
  feedback.runId = state.selected;
  $("#feedback-error").hidden = true;
  try {
    await loadFeedback();
  } catch (e) {
    toast(e.message);
    return;
  }
  $("#feedback-dialog").showModal();
}

function watchRefinement() {
  clearTimeout(feedback.timer);
  feedback.timer = setTimeout(async () => {
    try {
      const data = await api(`/api/feedback/${encodeURIComponent(feedback.runId)}`);
      if (data.rounds.some((r) => r.status === "running")) return watchRefinement();
      const last = data.rounds[0];
      if (last) {
        toast(last.status === "done" ? "criteria.md revised from your feedback"
              : last.status === "unchanged" ? "Feedback noted; criteria.md didn't need a change"
              : "The feedback round failed");
      }
      if ($("#feedback-dialog").open) await loadFeedback();
      if (last && last.status === "failed") redrawDigestMarks();  // the marks are back
      loadOverview();
    } catch (e) {
      console.error(e);
    }
  }, 2500);
}

async function submitFeedback(event) {
  event.preventDefault();
  const form = $("#feedback-form"), button = $("#submit-feedback");
  button.disabled = true;
  try {
    await api(`/api/feedback/${encodeURIComponent(feedback.runId)}/refine`, {
      method: "POST", body: JSON.stringify({ text: form.text.value }),
    });
    form.text.value = "";  // the marks and the message went with this round; start clean
    $("#feedback-error").hidden = true;
    redrawDigestMarks();
    await loadFeedback();
  } catch (e) {
    $("#feedback-error").textContent = e.message;
    $("#feedback-error").hidden = false;
    button.disabled = false;
  }
}

// --- continuing a run that failed or was cut short -------------------------------------
async function retryRun() {
  const id = state.selected, button = $("#retry-btn");
  if (!id) return;
  button.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}/resume`, { method: "POST" });
    toast("Continuing from where it stopped");
    state.watching.add(id);
    await loadRuns();
    pollStatus();
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
}

$("#retry-btn").addEventListener("click", retryRun);

// --- deleting a digest ---------------------------------------------------------------
async function deleteRun() {
  const id = state.selected, r = state.runs.find((x) => x.id === id);
  if (!id) return;
  const button = $("#delete-btn");
  button.disabled = true;
  try {
    await api(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.selected = null;
    $("#frame").hidden = true;
    $("#frame").removeAttribute("src");
    toast(`Deleted ${r ? r.label : id}`);
    await Promise.all([loadRuns(), loadOverview()]);
    showRun(null);
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
}

$("#delete-btn").addEventListener("click", deleteRun);
$("#feedback-btn").addEventListener("click", openFeedback);
$("#cancel-feedback").addEventListener("click", () => $("#feedback-dialog").close());
$("#feedback-form").addEventListener("submit", submitFeedback);
$("#feedback-rounds").addEventListener("click", async (e) => {
  const button = e.target.closest("[data-undo]");
  if (!button) return;
  if (!confirm("Put the previous criteria.md back?")) return;
  try {
    await api(`/api/refinements/${button.dataset.undo}/undo`, { method: "POST" });
    toast("criteria.md restored");
    await loadFeedback();
  } catch (err) {
    $("#feedback-error").textContent = err.message;
    $("#feedback-error").hidden = false;
  }
});

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
