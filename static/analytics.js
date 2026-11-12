"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const num = (x) => (x == null ? "—" : Number(x).toLocaleString("ru-RU", { maximumFractionDigits: 2 }));
let opData = null;
const RU = { c1_contact: "Установление контакта", c2_needs: "Выявление потребностей", c3_identification: "Идентификация",
  c4_consultation: "Консультация", c5_empathy: "Эмпатия", c6_management: "Ведение диалога",
  c7_conclusion: "Завершение", c8_violations: "Грубые нарушения" };

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
const q = () => { const c = $("#channel").value; return c ? `?channel=${c}` : ""; };
function kpi(label, value, bad = false) {
  return el("div", { className: "kpi" + (bad ? " bad" : "") }, el("div", { className: "n" }, String(value)), el("div", { className: "l" }, label));
}
const convLink = (id) => el("a", { href: `conversation.html?id=${encodeURIComponent(id)}` }, id);

function chart(rows) {
  const box = $("#chart");
  if (!rows.length) { box.replaceChildren(el("p", { className: "small" }, "Нет подтверждённых нарушений. Оцените диалоги на главной странице.")); return; }
  const max = Math.max(...rows.map((r) => r.priority_score)) || 1;
  box.replaceChildren(...rows.flatMap((r) => {
    const pct = (r.priority_score / max) * 100;
    return [el("div", { className: "lab" }, `${r.rank}. ${r.violation}`),
      el("div", { className: "track", title: `${r.lost_points} × ${r.frequency}% = ${r.priority_score}` },
        el("div", { className: "fill", style: `width:${pct}%` }),
        el("span", { className: "val" + (pct < 25 ? " out" : ""), style: pct < 25 ? `left:calc(${pct}% + 6px)` : "" }, num(r.priority_score)))];
  }));
}

async function loadCompany() {
  const a = await api(`/api/analytics/company${q()}`);
  $("#kpis").replaceChildren(
    kpi("Проанализировано диалогов", a.analysed_dialogues),
    kpi("Выявлено нарушений", a.violations_total),
    kpi("Диалогов с нарушениями", a.dialogues_with_violations),
    kpi("Серьёзных нарушений (C8)", a.serious.length, a.serious.length > 0),
    kpi("Не подтверждено цитатой (исключено)", a.unconfirmed_excluded));
  chart(a.top5);
  $("#method").textContent = `Lost Points: ${a.method.lost_points}. Frequency: ${a.method.frequency}. `
    + `Priority Score: ${a.method.priority_score}. Учитываются только подтверждённые нарушения: ${a.method.confirmed}. `
    + `Priority Score показывает влияние на QA-оценку, а не финансовые потери или риск для клиента.`;
  $("#ranking").replaceChildren(...a.ranking.map((r) => el("tr", {},
    el("td", {}, String(r.rank)),
    el("td", {}, r.violation, " ", el("span", { className: "tag" + (r.pattern === "единичная" ? "" : " rep") }, r.pattern)),
    el("td", {}, RU[r.criterion_id] || r.criterion),
    el("td", { title: r.dialogues.join(", ") }, `${r.count} из ${r.applicable}`),
    el("td", {}, num(r.lost_points) + (r.split_share ? "*" : "")),
    el("td", {}, `${num(r.frequency)}%`),
    el("td", {}, el("b", {}, num(r.priority_score))),
    el("td", { className: "small" }, r.recommendation))));
  if (a.ranking.some((r) => r.split_share)) $("#ranking").append(el("tr", {}, el("td", { colSpan: 8, className: "small" },
    "* в части диалогов потеря по критерию разделена поровну между несколькими причинами")));
  $("#serious").replaceChildren(a.serious.length
    ? el("div", { className: "serious" }, ...a.serious.map((v) => el("div", {}, convLink(v.conversation_id), " — ",
        v.evidence.map((e) => `#${e.message_index} «${(e.quote || "").slice(0, 120)}»`).join("; ") || "без цитаты",
        v.confirmed ? "" : " (не подтверждено цитатой)")))
    : el("p", { className: "small" }, "Серьёзных нарушений не выявлено."));
  $("#mgmt").replaceChildren(...a.management_recommendations.map((m) => el("li", {},
    el("b", {}, `${m.violation}`), ` — ${m.pattern}`,
    el("ul", {}, ...m.actions.map((x) => el("li", {}, x))))));
  if (a.management_recommendations.length) $("#mgmt").append(el("p", { className: "method" },
    "Причины в рекомендациях — гипотезы для проверки. Эффект изменений можно утверждать только после повторной оценки и сравнения Frequency."));
}

function dialogueCard(d) {
  const items = [...d.serious, ...d.violations];
  return [
    el("div", {}, convLink(d.conversation_id), ` · ${d.channel ?? ""} · ${(d.language ?? "").toUpperCase()}`,
      d.operator_id ? ` · оператор ${d.operator_id}` : ""),
    el("div", { className: "kpis", style: "margin:8px 0" }, kpi("Итоговая QA-оценка", d.final_score ?? "—"), kpi("Потеряно баллов", num(d.lost_points))),
    ...(items.length ? items.map((v) => el("div", { className: "rec" },
      el("div", {}, el("b", {}, v.violation), ` · ${RU[v.criterion_id] || v.criterion_name}`,
        v.criterion_id === "c8_violations" ? " · обязательная проверка" : ` · −${num(v.lost_points)}`,
        v.confirmed ? "" : el("span", { className: "small" }, " · не подтверждено цитатой")),
      v.evidence.length ? el("div", { className: "small" }, "Подтверждение: ",
        v.evidence.map((e) => `#${e.message_index} «${(e.quote || "").slice(0, 120)}»`).join("; ")) : null,
      v.reason ? el("div", { className: "small" }, "Причина: ", v.reason) : null,
      el("div", {}, "→ ", v.recommendation)))
      : [el("p", { className: "small" }, "Потерянных баллов нет.")]),
  ];
}

async function loadOperators() {
  opData = await api(`/api/analytics/operators${q()}`);
  $("#operators").replaceChildren(opData.operator_ids_available
    ? el("div", {}, el("h2", {}, "Повторяющиеся ошибки операторов"),
        el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Оператор"), el("th", {}, "Диалогов"), el("th", {}, "Чаще всего"))),
          el("tbody", {}, ...opData.operators.map((o) => el("tr", {}, el("td", {}, o.operator_id), el("td", {}, String(o.dialogues)),
            el("td", { className: "small" }, o.repeated.slice(0, 3).map((x) => `${x.violation} (${x.count})`).join("; ") || "—"))))))
    : el("p", { className: "notice" }, "В данных нет ID оператора, поэтому повторяющиеся ошибки по операторам посчитать нельзя. "
        + "Ниже — разбор по каждому диалогу. Если API организаторов передаёт operator_id, таблица появится автоматически."));
  const sel = $("#dialogue");
  const keep = sel.value;
  sel.replaceChildren(...opData.dialogues.map((d) => el("option", { value: d.conversation_id },
    `${d.conversation_id} · ${d.final_score ?? "—"} · нарушений: ${d.violations.filter((v) => v.confirmed).length + d.serious.length}`)));
  if (keep && opData.dialogues.some((d) => d.conversation_id === keep)) sel.value = keep;
  showDialogue();
}

function showDialogue() {
  const d = opData?.dialogues.find((x) => x.conversation_id === $("#dialogue").value);
  $("#dialogue-card").replaceChildren(...(d ? dialogueCard(d) : [el("p", { className: "small" }, "Нет оценённых диалогов.")]));
}

async function refresh() {
  $("#notice").textContent = "";
  try { await Promise.all([loadCompany(), loadOperators()]); } catch (e) { $("#notice").textContent = e.message; }
}

$$("[data-tab]").forEach((b) => b.addEventListener("click", () => {
  $$("[data-tab]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  $("#tab-company").hidden = b.dataset.tab !== "company";
  $("#tab-operator").hidden = b.dataset.tab !== "operator";
}));
$("#dialogue").addEventListener("change", showDialogue);
$("#channel").addEventListener("change", refresh);
$("#btn-refresh").addEventListener("click", refresh);
refresh();
