"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const NAMES = { c1_contact: "C1 Контакт", c2_needs: "C2 Потребности", c3_identification: "C3 Идентификация",
  c4_consultation: "C4 Консультация", c5_empathy: "C5 Эмпатия", c6_management: "C6 Ведение диалога",
  c7_conclusion: "C7 Завершение", c8_violations: "C8 Грубые нарушения" };
const fmt = (v, suffix = "%") => (v == null ? "—" : `${v}${suffix}`);

function kpi(label, value) { return el("div", { className: "kpi" }, el("div", { className: "n" }, value), el("div", { className: "l" }, label)); }

function rateRow(name, d) {
  return el("tr", {}, el("td", {}, name), el("td", {}, fmt(d.accuracy)),
    el("td", { className: "bar-cell" }, el("div", { className: "bar-fill", style: `width:${d.accuracy ?? 0}%` })),
    el("td", { className: "small" }, `${d.match}/${d.n}`));
}

async function load() {
  const res = await fetch("/api/accuracy");
  const a = await res.json();
  if (!a.reference_available) { $("#notice").textContent = "Файл eval/gold.json не найден."; return; }
  if (!a.conversations_compared) { $("#notice").textContent = "Нет оценённых диалогов с эталоном. Загрузите 65 dev-диалогов и оцените их."; }
  else $("#notice").textContent = "";
  $("#kpis").replaceChildren(
    kpi("Точность вердиктов (все критерии)", fmt(a.overall_accuracy)),
    kpi("Диалогов сравнено", String(a.conversations_compared)),
    kpi("Средняя ошибка балла", a.score_mae == null ? "—" : String(a.score_mae)),
    kpi("Балл в пределах ±5", fmt(a.score_within_5)),
    kpi("N/A: точность / полнота", `${fmt(a.na.precision, "")} / ${fmt(a.na.recall, "")}`),
    kpi("Грубые нарушения: точность / полнота", `${fmt(a.violations.precision, "")} / ${fmt(a.violations.recall, "")}`),
    kpi("Средняя стоимость оценки", a.avg_cost_usd == null ? "—" : `$${a.avg_cost_usd}`));
  $("#crit").replaceChildren(...Object.entries(a.criteria).map(([k, d]) => rateRow(NAMES[k] ?? k, d)));
  $("#lang").replaceChildren(...Object.entries(a.languages).map(([k, d]) => rateRow(k, d)));
  $("#confusion").replaceChildren(...Object.entries(a.confusion || {}).map(([cid, refs]) => {
    const cols = [...new Set(Object.values(refs).flatMap((r) => Object.keys(r)))].sort();
    const rows = Object.keys(refs).sort();
    return el("section", {}, el("h2", {}, NAMES[cid] ?? cid),
      el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "ref \\ AI"), ...cols.map((c) => el("th", {}, c)))),
        el("tbody", {}, ...rows.map((r) => el("tr", {}, el("th", {}, r),
          ...cols.map((c) => {
            const n = refs[r][c] || 0;
            return el("td", { style: n ? (r === c ? "background:var(--ok-bg);font-weight:700" : "background:var(--bad-bg)") : "" }, n ? String(n) : "");
          }))))));
  }));
  $("#mis").replaceChildren(...a.mismatches.map((m) => el("tr", {},
    el("td", {}, el("a", { href: `conversation.html?id=${encodeURIComponent(m.conversation_id)}#c-${m.criterion_id}` }, m.conversation_id)),
    el("td", {}, NAMES[m.criterion_id] ?? m.criterion_id),
    el("td", {}, m.reference), el("td", {}, m.ai ?? "—"), el("td", {}, m.language))));
}

$("#btn-refresh").addEventListener("click", load);
load().catch((e) => { $("#notice").textContent = e.message; });
