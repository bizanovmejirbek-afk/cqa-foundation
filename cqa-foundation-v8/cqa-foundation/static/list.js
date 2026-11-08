"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text.slice(0, 500) }; }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ` + (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data)));
  return data;
}
const notice = (t) => { $("#notice").textContent = t || ""; };

const SHORT = (name, i) => `C${i + 1}`;

function verdictClass(v, crit) {
  if (v == null) return "";
  if (v === "N/A") return "NA";
  if (crit.kind === "violation") return v === "YES" ? "NO" : "YES";      // red if a violation was found
  if (v === "YES" || v === "NO") return v;
  const n = Number(v), max = Number(crit.max);
  return n === max ? "full" : n === 0 ? "zero" : "part";
}

let rows = [];

async function load() {
  const [status, list] = await Promise.all([api("/api/status"), api("/api/conversations")]);
  const b = $("#llm-badge");
  b.textContent = `LLM: ${status.llm_provider}${status.llm_model ? " · " + status.llm_model : ""}`;
  b.className = "badge " + (status.llm_provider === "mock" ? "warn" : "ok");
  if (status.llm_provider === "mock") notice("Demo mode: no LLM key in .env, so a placeholder judge is used. Results are NOT real evaluations.");
  $("#btn-sync").disabled = !status.source_api_configured;
  $("#btn-sync").title = status.source_api_configured ? "" : "Set CQA_API_URL and CQA_GROUP_ID in .env";
  rows = list;
  render();
}

function render() {
  const criteria = rows.find((r) => r.evaluation)?.evaluation.criteria ?? [];
  const all = el("input", { type: "checkbox", title: "Select all" });
  all.addEventListener("change", () => { document.querySelectorAll(".pick").forEach((c) => (c.checked = all.checked)); updateButton(); });
  $("#thead").replaceChildren(el("tr", {},
    el("th", {}, all), el("th", {}, "Conversation"), el("th", {}, "Channel"), el("th", {}, "Lang"),
    el("th", {}, "Msgs"), el("th", {}, "Score"), el("th", { title: "Reference score from eval/gold.json" }, "Ref"),
    el("th", { title: "AI verdicts equal to reference / compared" }, "Match"),
    ...criteria.map((c, i) => el("th", { title: c.name }, SHORT(c.name, i))),
    el("th", {}, "Flags")));

  $("#tbody").replaceChildren(...rows.map((r) => {
    const ev = r.evaluation;
    const pick = el("input", { type: "checkbox", className: "pick", value: r.id });
    pick.addEventListener("change", updateButton);
    const flags = [];
    if (ev?.violations?.length) flags.push(el("span", { className: "badge bad", title: ev.violations.map((v) => v.name).join(", ") }, "⚠ violation"));
    if (ev?.needs_review) flags.push(el("span", { className: "badge warn" }, `⚑ ${ev.needs_review} to review`));
    if (r.has_audio) flags.push(el("span", { className: "badge" }, "♪ audio"));
    return el("tr", {},
      el("td", {}, pick),
      el("td", {}, el("a", { href: `conversation.html?id=${encodeURIComponent(r.id)}` }, r.id)),
      el("td", {}, r.channel ?? ""), el("td", {}, (r.language ?? "").toUpperCase()), el("td", {}, String(r.messages)),
      el("td", { className: "score" }, ev ? (ev.final_score ?? ev.score_status) : "—"),
      el("td", { className: "small" }, r.reference_score != null ? String(r.reference_score) : ""),
      matchCell(ev),
      ...criteria.map((c) => {
        const v = ev?.verdicts?.[c.id];
        return el("td", { className: `v ${verdictClass(v, c)}`, title: c.name }, v ?? "");
      }),
      el("td", {}, ...flags));
  }));
  $("#empty").hidden = rows.length > 0;
  updateButton();
}

function matchCell(ev) {
  const m = ev?.match;
  if (!m || !m.compared) return el("td", {}, "");
  const cls = m.matched === m.compared ? "ok" : m.matched >= m.compared - 1 ? "warn" : "bad";
  return el("td", {}, el("span", { className: `badge ${cls}` }, `${m.matched}/${m.compared}`));
}

function updateButton() {
  $("#btn-eval").disabled = !document.querySelector(".pick:checked");
}

async function evaluateSelected() {
  const ids = [...document.querySelectorAll(".pick:checked")].map((c) => c.value);
  const bar = $("#progress"), txt = $("#progress-text");
  bar.hidden = false; bar.max = ids.length; bar.value = 0;
  $("#btn-eval").disabled = true;
  const failed = [];
  for (const [i, id] of ids.entries()) {
    txt.textContent = `Evaluating ${i + 1}/${ids.length}: ${id}`;
    try { await api("/api/evaluations", { method: "POST", body: JSON.stringify({ conversation_id: id }) }); }
    catch (e) { failed.push(`${id}: ${e.message}`); }
    bar.value = i + 1;
  }
  bar.hidden = true;
  txt.textContent = `Done: ${ids.length - failed.length}/${ids.length}`;
  notice(failed.length ? "Failed: " + failed.join(" | ") : "");
  await load();
}

$("#btn-eval").addEventListener("click", evaluateSelected);
$("#btn-dev").addEventListener("click", async () => {
  try { const r = await api("/api/import-dev", { method: "POST" }); notice(`Dev dialogues: ${r.added} added, ${r.updated} updated, ${r.skipped} already present.`); await load(); }
  catch (e) { notice(e.message); }
});
$("#btn-sample").addEventListener("click", async () => {
  try { const r = await api("/api/import-sample", { method: "POST" }); notice(`Demo dialogues: ${r.added} added, ${r.skipped} already present.`); await load(); }
  catch (e) { notice(e.message); }
});
$("#btn-sync").addEventListener("click", async () => {
  try { const r = await api("/api/sync", { method: "POST", body: "{}" }); notice(`API: ${r.received} received, ${r.added} added, ${r.updated} updated.${r.errors.length ? " Errors: " + r.errors.join("; ") : ""}`); await load(); }
  catch (e) { notice("Sync failed: " + e.message); }
});

load().catch((e) => notice("Cannot reach the server: " + e.message));
