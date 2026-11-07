"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const ID = new URLSearchParams(location.search).get("id");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text.slice(0, 500) }; }
  if (!res.ok) throw new Error(`HTTP ${res.status}: ` + (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data)));
  return data;
}
const notice = (t) => { $("#notice").textContent = t || ""; };

let conv = null;
let previousScore = null;
let previousLedger = {};

/* ---------------- transcript ---------------- */
function renderTranscript() {
  const t0 = conv.transcript.length ? Date.parse(conv.transcript[0].timestamp) : NaN;
  $("#messages").replaceChildren(...conv.transcript.map((m, i) => {
    const speaker = String(m.speaker || "").toLowerCase();
    const node = el("div", { className: `msg ${speaker}`, id: `m-${i}` },
      el("div", { className: "meta" }, `#${i} · ${m.timestamp ?? ""} · ${speaker.toUpperCase()}`),
      el("div", { className: "text" }, m.text ?? ""));
    const ts = Date.parse(m.timestamp);
    if (conv.audio_url && !Number.isNaN(ts) && !Number.isNaN(t0)) node.dataset.offset = String((ts - t0) / 1000);
    return node;
  }));
  const audio = $("#audio");
  if (conv.audio_url) { audio.src = conv.audio_url; audio.hidden = false; }
}

function clearMarks() {
  document.querySelectorAll(".msg.focus").forEach((n) => n.classList.remove("focus"));
  document.querySelectorAll(".msg .text").forEach((t, i) => { t.textContent = conv.transcript[i]?.text ?? ""; });
  document.querySelectorAll(".missing").forEach((n) => n.remove());
}

function focusMessage(index, start, end) {
  clearMarks();
  const node = document.getElementById(`m-${index}`);
  if (!node) return;
  const text = conv.transcript[index].text ?? "";
  const box = node.querySelector(".text");
  if (Number.isInteger(start) && Number.isInteger(end) && end > start) {
    box.replaceChildren(text.slice(0, start), el("mark", {}, text.slice(start, end)), text.slice(end));
  }
  node.classList.add("focus");
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  const audio = $("#audio");
  if (!audio.hidden && node.dataset.offset) { audio.currentTime = Math.max(0, Number(node.dataset.offset)); }
}

function showMissing(missing) {
  clearMarks();
  const after = missing.after_message_index;
  const marker = el("div", { className: "missing" }, `Expected here: ${missing.description ?? "a required action is missing"}`);
  const anchor = after >= 0 ? document.getElementById(`m-${after}`) : null;
  if (anchor) anchor.after(marker); else $("#messages").prepend(marker);
  marker.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ---------------- evaluation ---------------- */
function pill(r) {
  const v = r.verdict;
  let cls = "";
  if (v === "N/A") cls = "";
  else if (r.kind === "violation") cls = v === "YES" ? "bad" : "ok";
  else if (v === "YES" || (r.max_points && Number(v) === Number(r.max_points))) cls = "ok";
  else if (v === "NO" || Number(v) === 0) cls = "bad";
  else cls = "warn";
  return el("span", { className: `badge ${cls}` }, v ?? "no verdict");
}

function evidenceLink(e) {
  const statusCls = e.status === "EXACT" ? "ok" : e.status === "FUZZY" ? "warn" : "bad";
  const a = el("a", { href: "#", className: "ev" },
    el("span", { className: `badge ${statusCls}` }, e.status),
    ` #${e.message_index ?? e.cited_index} `, `“${e.matched_text || e.quote}”`,
    e.index_corrected ? el("span", { className: "small" }, ` (cited #${e.cited_index})`) : null);
  a.addEventListener("click", (ev) => { ev.preventDefault(); if (e.message_index != null) focusMessage(e.message_index, e.char_start, e.char_end); });
  return a;
}

function card(r, ev) {
  const pointsText = r.kind === "violation" ? "reported separately"
    : r.verdict === "N/A" ? `weight ${r.weight} redistributed`
    : `${r.points ?? "—"} / ${r.max_points} · weight ${r.weight}`;
  const ref = conv.reference?.verdicts?.[r.criterion_id];
  const refBadge = ref == null ? null
    : el("span", { className: `badge ${ref === r.ai_verdict ? "ok" : "bad"}`, title: "Reference label (eval/gold.json) vs AI verdict" },
         `ref: ${ref} ${ref === r.ai_verdict ? "✓" : "✗"}`);
  const body = [
    el("h3", {}, r.name, pill(r), refBadge,
      el("span", { className: "small" }, pointsText),
      el("span", { className: `badge ${r.source === "REVIEW" ? "ok" : ""}` }, r.source),
      r.needs_review && r.source === "AI" ? el("span", { className: "badge warn" }, "needs review") : null),
    el("p", { className: "why" }, r.reasoning ?? ""),
  ];
  if (r.verdict === "N/A" && r.na_reason) body.push(el("p", { className: "small" }, `N/A reason: ${r.na_reason}`));
  r.evidence.forEach((e) => body.push(evidenceLink(e)));
  if (r.missing) {
    const a = el("a", { href: "#", className: "ev" }, el("span", { className: "badge bad" }, "MISSING"),
      ` after #${r.missing.after_message_index}: ${r.missing.description ?? ""}`);
    a.addEventListener("click", (e) => { e.preventDefault(); showMissing(r.missing); });
    body.push(a);
  }
  if (r.sub_checks?.length) {
    body.push(el("details", {}, el("summary", {}, "Sub-checks"),
      ...r.sub_checks.map((s) => el("div", { className: "small" }, `${s.answer === "YES" ? "✓" : "✗"} ${s.check_id}`))));
  }
  if (r.issues?.length) body.push(el("p", { className: "small" }, "Issues: " + r.issues.join(" | ")));
  if (r.decisions?.length) {
    body.push(el("details", {}, el("summary", {}, `History (${r.decisions.length})`),
      el("div", { className: "small" }, `AI: ${r.ai_verdict ?? "—"}`),
      ...r.decisions.map((d) => el("div", { className: "small" },
        `${d.reviewer} · ${d.decision} · ${d.old_verdict} → ${d.new_verdict} · “${d.reason}”`))));
  }
  body.push(appealForm(r), reviewForm(r));
  return el("article", { className: `card ${r.needs_review && r.source === "AI" ? "flag" : ""}`, id: `c-${r.criterion_id}` }, ...body);
}

function appealForm(r) {
  const open = r.appeals.find((a) => a.status !== "RESOLVED");
  const comment = el("textarea", { placeholder: "Why is this evaluation wrong?", required: true });
  const form = el("form", {}, comment, el("button", { type: "submit" }, "Submit appeal"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api(`/api/results/${r.id}/appeals`, { method: "POST", body: JSON.stringify({ comment: comment.value }) }); await load(); }
    catch (err) { notice(err.message); }
  });
  return el("details", {}, el("summary", {}, open ? `Appeal open: “${open.comment}”` : "Appeal"), open ? null : form);
}

function reviewForm(r) {
  const open = r.appeals.find((a) => a.status !== "RESOLVED");
  const verdict = el("select", {}, ...r.allowed.map((v) => el("option", { value: v, selected: v === r.verdict }, v)));
  const reason = el("textarea", { placeholder: "Reason for the decision (required)", required: true });
  const confirm = el("button", { type: "submit", value: "CONFIRM" }, "Confirm");
  const change = el("button", { type: "submit", value: "CHANGE", className: "primary" }, "Change verdict");
  const form = el("form", {}, el("label", { className: "small" }, "New verdict ", verdict), reason, el("div", {}, confirm, " ", change));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const decision = e.submitter?.value || "CONFIRM";
    try {
      const out = await api(`/api/results/${r.id}/reviews`, { method: "POST", body: JSON.stringify({
        decision, reason: reason.value, new_verdict: decision === "CHANGE" ? verdict.value : null,
        appeal_id: open ? open.id : null }) });
      notice(`Score recalculated: ${out.old_final_score ?? "—"} → ${out.new_final_score ?? out.score_status}`);
      await load();
    } catch (err) { notice(err.message); }
  });
  return el("details", { open: !!open }, el("summary", {}, open ? "Review this appeal" : "Review"), form);
}

function renderEvaluation() {
  const ev = conv.evaluation;
  if (!ev) { $("#score").textContent = "—"; $("#meta").textContent = "Not evaluated yet."; $("#cards").replaceChildren(); return; }
  const score = $("#score");
  const newScore = ev.final_score ?? ev.score_status;
  score.textContent = newScore;
  if (previousScore !== null && previousScore !== newScore) { score.classList.add("changed"); setTimeout(() => score.classList.remove("changed"), 1600); }
  previousScore = newScore;
  $("#meta").replaceChildren(
    el("div", {}, `${ev.checklist.name} · v${ev.checklist.version}`),
    el("div", {}, `${ev.provider} · ${ev.model} · ${ev.tokens_in + ev.tokens_out} tokens · $${ev.cost_usd} · ${(ev.duration_ms / 1000).toFixed(1)} s`),
    el("div", {}, `AI score: ${ev.ai_final_score ?? "—"}` +
                  (conv.reference?.final_score != null ? ` · reference: ${conv.reference.final_score}` : "")));
  const v = ev.ledger?.violations ?? [];
  $("#violations").hidden = !v.length;
  $("#violations").textContent = v.length ? "Serious violation reported: " + v.map((x) => x.name).join(", ") : "";

  const rows = ev.ledger?.rows ?? [];
  $("#ledger").replaceChildren(...rows.map((r) => {
    const key = `${r.verdict}|${r.contribution}`;
    const tr = el("tr", { className: previousLedger[r.criterion_id] && previousLedger[r.criterion_id] !== key ? "changed" : "" },
      el("td", {}, r.name), el("td", {}, r.verdict ?? "—"), el("td", {}, r.source),
      el("td", {}, r.weight), el("td", {}, r.effective_weight), el("td", {}, r.contribution));
    previousLedger[r.criterion_id] = key;
    return tr;
  }));
  $("#history").textContent = "History: " + ev.history.map((h) => `${h.trigger} ${h.final_score ?? "—"}`).join(" → ")
    + (ev.ledger?.note ? ` · ${ev.ledger.note}` : "");
  $("#cards").replaceChildren(...ev.results.map((r) => card(r, ev)));
}

async function load() {
  conv = await api(`/api/conversations/${encodeURIComponent(ID)}`);
  document.title = `${conv.id} · CQA`;
  $("#title").textContent = `${conv.id} · ${conv.channel} · ${(conv.language ?? "").toUpperCase()}`;
  if (!$("#messages").children.length) renderTranscript();
  renderEvaluation();
}

$("#btn-eval").addEventListener("click", async () => {
  const btn = $("#btn-eval");
  btn.disabled = true; $("#busy").textContent = "Evaluating…"; notice("");
  try {
    await api("/api/evaluations", { method: "POST", body: JSON.stringify({ conversation_id: ID, checklist_slug: $("#checklist").value || null }) });
    previousScore = null; previousLedger = {};
    await load();
  } catch (e) { notice(e.message); }
  finally { btn.disabled = false; $("#busy").textContent = ""; }
});

if (!ID) notice("Missing ?id= in the URL.");
else load().catch((e) => notice(e.message));
