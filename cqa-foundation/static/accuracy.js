"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const NAMES = { c1_contact: "C1 Contact", c2_needs: "C2 Needs", c3_identification: "C3 Identification",
  c4_consultation: "C4 Consultation", c5_empathy: "C5 Empathy", c6_management: "C6 Management",
  c7_conclusion: "C7 Conclusion", c8_violations: "C8 Violations" };
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
  if (!a.reference_available) { $("#notice").textContent = "No eval/gold.json found."; return; }
  if (!a.conversations_compared) { $("#notice").textContent = "No evaluated conversations with reference labels yet. Load the 65 dev dialogues and evaluate them."; }
  else $("#notice").textContent = "";
  $("#kpis").replaceChildren(
    kpi("Verdict accuracy (all criteria)", fmt(a.overall_accuracy)),
    kpi("Conversations compared", String(a.conversations_compared)),
    kpi("Final score MAE (points)", a.score_mae == null ? "—" : String(a.score_mae)),
    kpi("Final score within ±5", fmt(a.score_within_5)),
    kpi(`N/A precision / recall`, `${fmt(a.na.precision, "")} / ${fmt(a.na.recall, "")}`),
    kpi("Violations precision / recall", `${fmt(a.violations.precision, "")} / ${fmt(a.violations.recall, "")}`),
    kpi("Avg cost per evaluation", a.avg_cost_usd == null ? "—" : `$${a.avg_cost_usd}`));
  $("#crit").replaceChildren(...Object.entries(a.criteria).map(([k, d]) => rateRow(NAMES[k] ?? k, d)));
  $("#lang").replaceChildren(...Object.entries(a.languages).map(([k, d]) => rateRow(k, d)));
  $("#mis").replaceChildren(...a.mismatches.map((m) => el("tr", {},
    el("td", {}, el("a", { href: `conversation.html?id=${encodeURIComponent(m.conversation_id)}#c-${m.criterion_id}` }, m.conversation_id)),
    el("td", {}, NAMES[m.criterion_id] ?? m.criterion_id),
    el("td", {}, m.reference), el("td", {}, m.ai ?? "—"), el("td", {}, m.language))));
}

$("#btn-refresh").addEventListener("click", load);
load().catch((e) => { $("#notice").textContent = e.message; });
