"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const nf = (x) => Number(x).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
const CH = (c) => (/voice|call/i.test(c || "") ? "Звонок" : "Чат");
const VRU = { YES: "Да", NO: "Нет", "N/A": "н/п" };

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
function kpi(label, value, cls = "") { return el("div", { className: `kpi ${cls}` }, el("div", { className: "n" }, String(value)), el("div", { className: "l" }, label)); }

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
  b.textContent = `модель: ${status.llm_model || status.llm_provider}`;
  b.className = "badge " + (status.llm_provider === "mock" ? "warn" : "ok");
  if (status.llm_provider === "mock") notice("Демо-режим: в .env нет ключа модели, используется mock-судья. Это НЕ настоящие оценки.");
  $("#btn-sync").disabled = !status.source_api_configured;
  $("#btn-sync").title = status.source_api_configured ? "" : "Укажите CQA_API_URL и CQA_GROUP_ID в .env";
  rows = list;
  render();
}

function renderKpis() {
  const ev = rows.filter((r) => r.evaluation);
  const scores = ev.map((r) => Number(r.evaluation.final_score)).filter((x) => Number.isFinite(x));
  const avg = scores.length ? nf(scores.reduce((a, b) => a + b, 0) / scores.length) : "—";
  $("#kpis").replaceChildren(
    kpi("Диалогов загружено", rows.length),
    kpi("Оценено", ev.length, "ok"),
    kpi("Средний балл", avg),
    kpi("С грубыми нарушениями", ev.filter((r) => r.evaluation.violations?.length).length, "bad"),
    kpi("Ждут проверки человеком", ev.reduce((a, r) => a + (r.evaluation.needs_review || 0), 0)));
}

function render() {
  renderKpis();
  const q = ($("#filter").value || "").trim().toLowerCase();
  const shown = q ? rows.filter((r) => r.id.toLowerCase().includes(q)) : rows;
  const criteria = rows.find((r) => r.evaluation)?.evaluation.criteria ?? [];
  const all = el("input", { type: "checkbox", title: "Select all" });
  all.addEventListener("change", () => { document.querySelectorAll(".pick").forEach((c) => (c.checked = all.checked)); updateButton(); });
  $("#thead").replaceChildren(el("tr", {},
    el("th", {}, all), el("th", {}, "Диалог"), el("th", {}, "Канал"), el("th", {}, "Язык"),
    el("th", {}, "Сообщ."), el("th", {}, "Балл"), el("th", { title: "Балл по эталонной разметке (eval/gold.json)" }, "Эталон"),
    el("th", { title: "Совпало вердиктов AI с эталоном / сравнено" }, "Совпадение"),
    ...criteria.map((c, i) => el("th", { title: c.name }, SHORT(c.name, i))),
    el("th", {}, "Отметки")));

  $("#tbody").replaceChildren(...shown.map((r) => {
    const ev = r.evaluation;
    const pick = el("input", { type: "checkbox", className: "pick", value: r.id });
    pick.addEventListener("change", updateButton);
    const flags = [];
    if (ev?.violations?.length) flags.push(el("span", { className: "badge bad", title: ev.violations.map((v) => v.name).join(", ") }, "⚠ грубое нарушение"));
    if (ev?.needs_review) flags.push(el("span", { className: "badge warn" }, `⚑ на проверку: ${ev.needs_review}`));
    if (r.has_audio) flags.push(el("span", { className: "badge" }, "♪ аудио"));
    return el("tr", {},
      el("td", {}, pick),
      el("td", {}, el("a", { href: `conversation.html?id=${encodeURIComponent(r.id)}` }, r.id)),
      el("td", {}, CH(r.channel)), el("td", {}, (r.language ?? "").toUpperCase()), el("td", {}, String(r.messages)),
      el("td", { className: "score" }, ev ? (ev.final_score != null ? nf(ev.final_score) : ev.score_status) : "—",
        ev?.final_score != null ? el("span", { className: "scorebar" }, el("i", { style: `width:${Math.max(0, Math.min(100, Number(ev.final_score)))}%` })) : null),
      el("td", { className: "small" }, r.reference_score != null ? String(r.reference_score) : ""),
      matchCell(ev),
      ...criteria.map((c) => {
        const v = ev?.verdicts?.[c.id];
        return el("td", { className: `v ${verdictClass(v, c)}`, title: c.name }, v == null ? "" : el("span", {}, VRU[v] ?? v));
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
    txt.textContent = `Оцениваем ${i + 1}/${ids.length}: ${id}`;
    try { await api("/api/evaluations", { method: "POST", body: JSON.stringify({ conversation_id: id }) }); }
    catch (e) { failed.push(`${id}: ${e.message}`); }
    bar.value = i + 1;
  }
  bar.hidden = true;
  txt.textContent = `Готово: ${ids.length - failed.length}/${ids.length}`;
  notice(failed.length ? "Ошибки: " + failed.join(" | ") : "");
  await load();
}

$("#btn-eval").addEventListener("click", evaluateSelected);
$("#btn-dev").addEventListener("click", async () => {
  try { const r = await api("/api/import-dev", { method: "POST" }); notice(`Dev-диалоги: добавлено ${r.added}, обновлено ${r.updated}, уже были ${r.skipped}.`); await load(); }
  catch (e) { notice(e.message); }
});
$("#btn-sample").addEventListener("click", async () => {
  try { const r = await api("/api/import-sample", { method: "POST" }); notice(`Демо-диалоги: добавлено ${r.added}, уже были ${r.skipped}.`); await load(); }
  catch (e) { notice(e.message); }
});
$("#btn-sync").addEventListener("click", async () => {
  try { const r = await api("/api/sync", { method: "POST", body: "{}" }); notice(`API: получено ${r.received}, добавлено ${r.added}, обновлено ${r.updated}.${r.errors.length ? " Ошибки: " + r.errors.join("; ") : ""}`); await load(); }
  catch (e) { notice("Загрузка не удалась: " + e.message); }
});

$("#filter").addEventListener("input", render);
load().catch((e) => notice("Сервер недоступен: " + e.message));
