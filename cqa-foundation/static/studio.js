/* Criterion Studio — vanilla JS, no framework.
 *
 * Data flow on every keystroke:
 *   form ──input event (delegated)──► readForm() ──► state
 *        ──requestAnimationFrame──► local draft preview (instant, mirrors Python)
 *        ──debounce 300 ms + AbortController──► POST /api/studio/preview
 *        ──► authoritative prompt + schema from the same Python code the judge uses
 * A parity check compares the browser's criterion block with the server's; a
 * "drift" badge means the JS mirror and guardrails.py disagree (a bug to fix).
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const SLUG = new URLSearchParams(location.search).get("checklist") || "official-chat";
const state = { checklist: null, version: null, index: 0, lastPrompt: "", serverOk: false };

const form = $("#criterion-form");
const els = {
  picker: $("#criterion-picker"), version: $("#version-badge"), prompt: $("#prompt-preview"),
  schema: $("#schema-preview"), sync: $("#sync-status"), schemaStatus: $("#schema-status"),
  errors: $("#form-errors"), save: $("#save-btn"), options: $("#options"), subchecks: $("#subchecks"),
};

/* ------------------------------------------------------------------ */
/* Form <-> criterion object                                          */
/* ------------------------------------------------------------------ */
const val = (name) => (form.elements[name]?.value ?? "").trim();
const checked = (name) => !!form.elements[name]?.checked;
const num = (s) => (s === "" || s == null ? null : Number(s));

function readRows(container) {
  return $$("[data-f]", container).reduce((rows, input) => {
    const row = input.closest(".opt-row, .sub-row");
    let r = rows.find((x) => x._el === row);
    if (!r) rows.push((r = { _el: row }));
    r[input.dataset.f] = input.value.trim();
    return rows;
  }, []).map(({ _el, ...r }) => r);
}

function readForm() {
  const subs = readRows(els.subchecks).filter((s) => s.id || s.question);
  const notes = {};
  if (val("note_chat")) notes.chat = val("note_chat");
  if (val("note_voice")) notes.voice = val("note_voice");
  const naAllowed = checked("na_allowed");
  return {
    id: val("id"), name: val("name"), definition: val("definition"), kind: val("kind"),
    weight: num(val("weight")) ?? 0,
    options: readRows(els.options).map((o) => ({ value: o.value, points: num(o.points), definition: o.definition })),
    na_allowed: naAllowed, na_condition: naAllowed ? val("na_condition") || null : null,
    subject: "operator", evidence_required: true,
    sub_checks: subs,
    sub_check_rule: subs.length ? val("sub_check_rule") : null,
    pass_value: subs.length ? val("pass_value") : null,
    fail_value: subs.length ? val("fail_value") : null,
    channel_notes: notes, section: val("section") || null, enabled: checked("enabled"),
  };
}

function addRow(templateId, container, data = {}) {
  const node = $(templateId).content.firstElementChild.cloneNode(true);
  for (const input of $$("[data-f]", node)) input.value = data[input.dataset.f] ?? "";
  container.append(node);
  return node;
}

function fillForm(c) {
  form.reset();
  for (const k of ["id", "name", "definition", "kind", "weight", "section", "na_condition",
                   "sub_check_rule", "pass_value", "fail_value"]) {
    if (form.elements[k] && c[k] != null) form.elements[k].value = c[k];
  }
  form.elements.enabled.checked = c.enabled !== false;
  form.elements.na_allowed.checked = !!c.na_allowed;
  form.elements.note_chat.value = c.channel_notes?.chat ?? "";
  form.elements.note_voice.value = c.channel_notes?.voice ?? "";
  els.options.replaceChildren(); els.subchecks.replaceChildren();
  (c.options ?? []).forEach((o) => addRow("#option-row", els.options, o));
  (c.sub_checks ?? []).forEach((s) => addRow("#subcheck-row", els.subchecks, s));
  syncDependentFields();
  state.lastPrompt = "";
  scheduleRender();
}

function syncDependentFields() {
  const na = form.elements.na_condition;
  na.disabled = !checked("na_allowed");
  na.required = !na.disabled;
}

/* ------------------------------------------------------------------ */
/* Local mirror of guardrails.render_criterion_block (keep in sync!)   */
/* ------------------------------------------------------------------ */
const fmtPoints = (p) => (p == null || Number.isNaN(p) ? "?" : String(Number(p)));

function renderCriterionBlock(c, channel = "chat") {
  const parts = [`CRITERION: ${c.name} (id: ${c.id})`, `DEFINITION: ${c.definition}`, "ALLOWED VERDICTS:"];
  if (c.kind === "violation") parts.push("(YES = a violation was found, NO = none found)");
  for (const o of c.options) parts.push(`- "${o.value}" (${fmtPoints(o.points)} pts): ${o.definition}`);
  parts.push(c.na_allowed ? `- "N/A": only if ${c.na_condition ?? ""}`
                          : `- "N/A" is NOT allowed for this criterion.`);
  if (c.sub_checks.length) {
    parts.push("SUB-CHECKS (answer each YES or NO with evidence):");
    for (const s of c.sub_checks) parts.push(`- ${s.id}: ${s.question}`);
  }
  if (c.channel_notes[channel]) parts.push(`CHANNEL NOTE (${channel}): ${c.channel_notes[channel]}`);
  return parts.join("\n");
}

function draftSchema(c) {
  const verdicts = [...c.options.map((o) => o.value), ...(c.na_allowed ? ["N/A"] : [])];
  const evidence = { type: "array", items: { type: "object",
    properties: { message_index: { type: "integer", minimum: 0 }, quote: { type: "string" } },
    required: ["message_index", "quote"], additionalProperties: false } };
  const props = {};
  if (c.sub_checks.length) props.sub_checks = { type: "array", minItems: c.sub_checks.length,
    maxItems: c.sub_checks.length, items: { type: "object", properties: {
      check_id: { enum: c.sub_checks.map((s) => s.id) }, answer: { enum: ["YES", "NO"] }, evidence } } };
  Object.assign(props, {
    evidence, missing_location: { anyOf: [{ type: "object" }, { type: "null" }] },
    na_reason: { anyOf: [{ type: "string" }, { type: "null" }] },
    reasoning: { type: "string" }, verdict: { enum: verdicts },
    confidence: { type: "number", minimum: 0, maximum: 1 },
  });
  return { title: `Judge_${c.id}`, type: "object", properties: props,
           required: ["reasoning", "verdict", "confidence"], additionalProperties: false };
}

/* ------------------------------------------------------------------ */
/* Client-side validation (server re-validates; this is for speed)     */
/* ------------------------------------------------------------------ */
function clientErrors(c) {
  const e = [];
  if (!/^[a-z][a-z0-9_]{1,63}$/.test(c.id)) e.push("ID must be snake_case (2–64 chars).");
  if (c.name.length < 2) e.push("Name is required.");
  if (c.options.length < 2) e.push("At least two verdict options are required.");
  const vals = c.options.map((o) => o.value);
  if (new Set(vals).size !== vals.length) e.push("Verdict values must be unique.");
  if (vals.some((v) => /^n\/?a$/i.test(v))) e.push("Do not add N/A as an option; use the N/A toggle.");
  if (c.options.some((o) => o.points == null || o.points < 0)) e.push("Every option needs points ≥ 0.");
  if (c.kind === "binary" && c.options.length !== 2) e.push("Yes/No criteria need exactly two options.");
  if (c.kind === "violation" && c.weight !== 0) e.push("Violation criteria must have weight 0.");
  if (c.kind !== "violation" && !c.options.some((o) => o.points > 0)) e.push("One option must award points > 0.");
  if (c.na_allowed && !c.na_condition) e.push("Describe when N/A applies.");
  if (c.sub_checks.length) {
    if (c.kind !== "binary") e.push("Sub-checks work with Yes/No criteria only.");
    if (![c.pass_value, c.fail_value].every((v) => vals.includes(v))) e.push("Pass/Fail values must be option values.");
  }
  return e;
}

/* ------------------------------------------------------------------ */
/* Rendering: rAF-coalesced, changed lines highlighted                 */
/* ------------------------------------------------------------------ */
function renderLines(pre, text, previous) {
  const prev = new Set(previous.split("\n"));
  const frag = document.createDocumentFragment();
  text.split("\n").forEach((line, i, all) => {
    if (previous && !prev.has(line) && line.trim()) {
      const m = document.createElement("mark");
      m.textContent = line;
      frag.append(m);
      requestAnimationFrame(() => setTimeout(() => m.classList.add("fade"), 50));
    } else {
      frag.append(document.createTextNode(line));
    }
    if (i < all.length - 1) frag.append("\n");
  });
  pre.replaceChildren(frag);  // textContent semantics: user input is never parsed as HTML
}

function setBadge(el, text, kind = "") {
  el.textContent = text;
  el.className = `badge ${kind}`.trim();
}

let rafId = 0;
function scheduleRender() {
  cancelAnimationFrame(rafId);
  rafId = requestAnimationFrame(() => {
    const c = readForm();
    const errors = clientErrors(c);
    showErrors(errors);
    const draft = [renderCriterionBlock(c), "", "CHANNEL: chat", "", "<transcript>…</transcript>"].join("\n");
    renderLines(els.prompt, draft, state.lastPrompt);
    state.lastPrompt = draft;
    els.schema.textContent = JSON.stringify(draftSchema(c), null, 2);
    setBadge(els.sync, errors.length ? "invalid" : "draft (browser)", errors.length ? "bad" : "warn");
    setBadge(els.schemaStatus, "draft (browser)", "warn");
    state.serverOk = false;
    els.save.disabled = true;
    if (!errors.length) requestServerPreview(c);
  });
}

function showErrors(list) {
  els.errors.replaceChildren(...list.map((t) => Object.assign(document.createElement("li"), { textContent: t })));
}

/* ------------------------------------------------------------------ */
/* Authoritative preview: debounced, stale requests cancelled          */
/* ------------------------------------------------------------------ */
let debounceId = 0, inflight = null;
function requestServerPreview(c) {
  clearTimeout(debounceId);
  debounceId = setTimeout(async () => {
    inflight?.abort();
    inflight = new AbortController();
    try {
      const res = await fetch("/api/studio/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ criterion: c, channel: "chat" }), signal: inflight.signal,
      });
      const data = await res.json();
      if (!data.valid) {
        showErrors(data.errors.map((e) => `${e.field}: ${e.message}`));
        setBadge(els.sync, "rejected by server", "bad");
        return;
      }
      const full = `SYSTEM:\n${data.system}\n\nUSER:\n${data.user}`;
      renderLines(els.prompt, full, state.lastPrompt);
      state.lastPrompt = full;
      els.schema.textContent = JSON.stringify(data.schema, null, 2);
      const parity = data.criterion_block === renderCriterionBlock(c);
      setBadge(els.sync, parity ? `server ✓ ${data.prompt_hash.slice(0, 8)}` : "server ✓ (JS mirror drift)",
               parity ? "ok" : "warn");
      setBadge(els.schemaStatus, "server ✓", "ok");
      state.serverOk = true;
      els.save.disabled = false;
    } catch (err) {
      if (err.name !== "AbortError") setBadge(els.sync, "server unreachable", "bad");
    }
  }, 300);
}

/* ------------------------------------------------------------------ */
/* Events: one delegated listener per kind                             */
/* ------------------------------------------------------------------ */
form.addEventListener("input", scheduleRender);
form.addEventListener("change", (e) => {
  if (e.target.name === "na_allowed") syncDependentFields();
  scheduleRender();
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action], [data-copy]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "add-option") addRow("#option-row", els.options).querySelector("input").focus();
  else if (action === "add-subcheck") addRow("#subcheck-row", els.subchecks).querySelector("input").focus();
  else if (action === "remove-row") btn.closest(".opt-row, .sub-row").remove();
  else if (btn.dataset.copy) navigator.clipboard?.writeText($("#" + btn.dataset.copy).textContent);
  if (action) scheduleRender();
});

els.picker.addEventListener("change", () => {
  commitCurrent();
  state.index = Number(els.picker.value);
  fillForm(state.checklist.criteria[state.index]);
});

$("#new-criterion").addEventListener("click", () => {
  commitCurrent();
  state.checklist.criteria.push({
    id: `custom_${state.checklist.criteria.length + 1}`, name: "New criterion", definition: "",
    kind: "binary", weight: 10, na_allowed: false, sub_checks: [], channel_notes: {}, enabled: true,
    options: [{ value: "YES", points: 1, definition: "" }, { value: "NO", points: 0, definition: "" }],
  });
  state.index = state.checklist.criteria.length - 1;
  populatePicker();
  fillForm(state.checklist.criteria[state.index]);
});

function commitCurrent() {
  if (state.checklist) state.checklist.criteria[state.index] = readForm();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.serverOk) return;
  commitCurrent();
  els.save.disabled = true;
  try {
    const res = await fetch(`/api/studio/checklists/${encodeURIComponent(SLUG)}/versions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ checklist: state.checklist, change_note: $("#change-note").value || null,
                             author: "studio" }),
    });
    const data = await res.json();
    if (!res.ok) { showErrors(data.detail.map((d) => `${d.field}: ${d.message}`)); return; }
    state.version = data.version;
    setBadge(els.version, `v${data.version} saved`, "ok");
    populatePicker();
  } finally {
    els.save.disabled = !state.serverOk;
  }
});

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */
function populatePicker() {
  els.picker.replaceChildren(...state.checklist.criteria.map((c, i) =>
    Object.assign(document.createElement("option"), { value: i, textContent: `${c.name} (${c.weight})`,
                                                      selected: i === state.index })));
}

(async function boot() {
  const res = await fetch(`/api/studio/checklists/${encodeURIComponent(SLUG)}/latest`);
  if (!res.ok) { showErrors([`Could not load checklist "${SLUG}".`]); return; }
  const data = await res.json();
  state.checklist = structuredClone(data.config);
  state.version = data.version;
  setBadge(els.version, `v${data.version}`);
  populatePicker();
  fillForm(state.checklist.criteria[0]);
})();
