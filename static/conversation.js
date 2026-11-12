"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids.filter((k) => k != null)); return n; };
const SVGNS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs = {}, ...kids) => { const n = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); n.append(...kids); return n; };
const ID = new URLSearchParams(location.search).get("id");

const RU = { c1_contact: "Установление контакта", c2_needs: "Выявление потребностей", c3_identification: "Идентификация",
  c4_consultation: "Консультация", c5_empathy: "Эмпатия", c6_management: "Ведение диалога",
  c7_conclusion: "Завершение", c8_violations: "Грубые нарушения" };
const SPEAKER = { client: "Клиент", operator: "Оператор", agent: "Оператор", bot: "Бот", system: "Система" };
const LANG = { ru: "Русский", kz: "Казахский", kk: "Казахский", en: "Английский" };
const nf = (x) => Number(x).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
const mmss = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const ICON_PLAY = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15l13-7.5z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>';

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
let offsets = [];          // seconds from the start for every message (null if unknown)
let duration = 0;
let selected = null;
let waveform = null;       // Float32 peaks if the audio could be decoded

/* ---------------- time ---------------- */
function secs(ts) {
  const t = String(ts ?? "").trim();
  if (!t) return null;
  if (/^\d{1,3}(:\d{2}){1,2}(\.\d+)?$/.test(t)) return t.split(":").reduce((a, p) => a * 60 + Number(p), 0);
  const d = Date.parse(t);
  return Number.isNaN(d) ? null : d / 1000;
}
function computeOffsets() {
  const raw = conv.transcript.map((m) => secs(m.timestamp));
  const first = raw.find((x) => x != null);
  offsets = raw.map((x) => (x == null || first == null ? null : x - first));
  duration = Math.max(0, ...offsets.filter((x) => x != null));
}
const isVoice = () => /voice|call/i.test(conv.channel || "");
const speakerOf = (m) => String(m.speaker || "").toLowerCase();

/* ---------------- header ---------------- */
function renderHeader() {
  const kind = isVoice() ? "Звонок" : "Чат";
  const lang = LANG[(conv.language || "").toLowerCase()] || (conv.language || "").toUpperCase();
  $("#subtitle").textContent = [`${kind} ${conv.id}`, lang, duration ? mmss(duration) : null].filter(Boolean).join(" · ");
  document.title = `${conv.id} · Анализ разговора`;
}

/* ---------------- timeline ---------------- */
function segments() {
  return conv.transcript.map((m, i) => {
    const start = offsets[i];
    if (start == null) return null;
    const next = offsets.slice(i + 1).find((x) => x != null) ?? duration;
    const text = String(m.text || "");
    const est = isVoice() ? Math.min(next - start, 60) : Math.min(next - start, Math.max(duration * 0.012, text.length / 12));
    return { i, start, end: start + Math.max(est, duration * 0.004), sp: speakerOf(m) };
  }).filter(Boolean);
}

function renderTimeline() {
  const box = $("#timeline");
  if (!duration) { box.replaceChildren(el("p", { className: "small" }, "Нет временных меток — хронологию построить нельзя.")); return; }
  const W = 1000, L = 110, R = 8;
  const lanes = [{ key: "client", label: "Клиент", color: "var(--client)" }, { key: "operator", label: "Оператор", color: "var(--operator)" }];
  if (conv.transcript.some((m) => ["bot", "system"].includes(speakerOf(m)))) lanes.push({ key: "bot", label: "Бот / система", color: "var(--bot)" });
  const top = waveform ? 46 : 8, laneH = 34;
  const H = top + lanes.length * laneH + 22;
  const x = (t) => L + (t / duration) * (W - L - R);
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Хронология реплик клиента и оператора" });

  if (waveform) {
    svg.append(sv("text", { x: 0, y: 30, class: "lane-label" }, "Запись"));
    const n = waveform.length, bw = (W - L - R) / n;
    waveform.forEach((p, k) => {
      const h = Math.max(1, p * 34);
      svg.append(sv("rect", { x: L + k * bw, y: 24 - h / 2, width: Math.max(1, bw * 0.6), height: h, rx: 0.5, fill: "var(--client)", opacity: 0.55 }));
    });
  }
  lanes.forEach((ln, k) => {
    const y = top + k * laneH;
    svg.append(sv("text", { x: 0, y: y + 21, class: "lane-label" }, ln.label));
    svg.append(sv("line", { x1: L, x2: W - R, y1: y + 16, y2: y + 16, stroke: "var(--line)", "stroke-width": 1 }));
  });
  if (selected != null) {
    const s = segments().find((g) => g.i === selected);
    if (s) svg.append(sv("rect", { x: x(s.start) - 6, y: 2, width: Math.max(14, x(s.end) - x(s.start) + 12), height: H - 24,
      rx: 4, fill: "var(--cream)", stroke: "var(--cream-line)", "stroke-width": 1.5, opacity: 0.85 }));
  }
  for (const g of segments()) {
    const lane = lanes.findIndex((ln) => ln.key === (["bot", "system"].includes(g.sp) ? "bot" : g.sp === "agent" ? "operator" : g.sp));
    if (lane < 0) continue;
    const y = top + lane * laneH;
    const r = sv("rect", { x: x(g.start), y: y + 7, width: Math.max(3, x(g.end) - x(g.start)), height: 18, rx: 4,
      fill: lanes[lane].color, opacity: g.i === selected ? 1 : 0.85 });
    r.append(sv("title", {}, `${mmss(g.start)} · ${SPEAKER[g.sp] || g.sp}: ${String(conv.transcript[g.i].text || "").slice(0, 120)}`));
    svg.append(r);
  }
  const ticks = 6;
  for (let k = 0; k <= ticks; k++) {
    const t = (duration * k) / ticks;
    svg.append(sv("text", { x: x(t), y: H - 4, class: "axis", "text-anchor": k === 0 ? "start" : k === ticks ? "end" : "middle" }, mmss(t)));
  }
  svg.append(sv("line", { id: "playhead", x1: L, x2: L, y1: 2, y2: H - 20, stroke: "var(--accent-ink)", "stroke-width": 2, opacity: 0 }));
  svg.addEventListener("click", (ev) => {
    const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    if (p.x < L) return;
    const t = ((p.x - L) / (W - L - R)) * duration;
    const segs = segments();
    const hit = segs.find((g) => t >= g.start && t <= g.end) || segs.filter((g) => g.start <= t).pop() || segs[0];
    if (hit) focusMessage(hit.i);
    if (conv.audio_url) seekTo(t);
  });
  box.replaceChildren(svg);
  $("#legend").replaceChildren(...lanes.map((ln) => el("span", {}, el("i", { style: `background:${ln.color}` }), ln.label)),
    el("span", {}, isVoice() ? "Клик по полосе — переход к реплике и месту записи" : "Ширина полосы — примерная длина сообщения"));
}

function movePlayhead(t) {
  const ph = document.getElementById("playhead");
  if (!ph || !duration) return;
  const xx = 110 + (Math.min(t, duration) / duration) * (1000 - 118);
  ph.setAttribute("x1", xx); ph.setAttribute("x2", xx); ph.setAttribute("opacity", 1);
}

/* ---------------- audio ---------------- */
function setupAudio() {
  const audio = $("#audio");
  if (!conv.audio_url) { $("#player").hidden = true; return; }
  audio.src = conv.audio_url;
  $("#player").hidden = false;
  const play = $("#play");
  play.innerHTML = ICON_PLAY;
  play.onclick = () => (audio.paused ? audio.play() : audio.pause());
  audio.onplay = () => { play.innerHTML = ICON_PAUSE; play.setAttribute("aria-label", "Пауза"); };
  audio.onpause = () => { play.innerHTML = ICON_PLAY; play.setAttribute("aria-label", "Воспроизвести"); };
  audio.onloadedmetadata = () => updateTime();
  audio.ontimeupdate = () => updateTime();
  $("#seek").oninput = (e) => { const d = audio.duration || duration; if (d) audio.currentTime = (Number(e.target.value) / 1000) * d; };
  loadWaveform();
}
function updateTime() {
  const audio = $("#audio");
  const d = audio.duration && Number.isFinite(audio.duration) ? audio.duration : duration;
  $("#time").textContent = `${mmss(audio.currentTime)} / ${mmss(d)}`;
  $("#seek").value = d ? String((audio.currentTime / d) * 1000) : "0";
  movePlayhead(audio.currentTime);
  let cur = -1;
  offsets.forEach((o, i) => { if (o != null && o <= audio.currentTime) cur = i; });
  $$(".msg.playing").forEach((n) => n.classList.remove("playing"));
  if (cur >= 0 && !audio.paused) document.getElementById(`m-${cur}`)?.classList.add("playing");
}
function seekTo(t) { const a = $("#audio"); if (conv.audio_url) { a.currentTime = Math.max(0, t); updateTime(); } }
async function loadWaveform() {
  try {   // works only if the audio server allows it (same origin or CORS); otherwise the segment view stays
    const buf = await (await fetch(conv.audio_url)).arrayBuffer();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const audio = await ctx.decodeAudioData(buf);
    const data = audio.getChannelData(0), n = 220, step = Math.floor(data.length / n);
    const peaks = new Float32Array(n);
    for (let k = 0; k < n; k++) { let m = 0; for (let j = k * step; j < (k + 1) * step; j += 32) m = Math.max(m, Math.abs(data[j] || 0)); peaks[k] = m; }
    const max = Math.max(...peaks) || 1;
    waveform = Array.from(peaks, (p) => p / max);
    renderTimeline();
  } catch { /* keep the transcript-based view */ }
}

/* ---------------- transcript ---------------- */
function renderTranscript() {
  $("#messages").replaceChildren(...conv.transcript.map((m, i) => {
    const sp = speakerOf(m);
    const node = el("div", { className: `msg ${sp}`, id: `m-${i}` },
      el("div", { className: "meta" }, el("span", { className: "t" }, offsets[i] != null ? mmss(offsets[i]) : `#${i}`),
        el("b", {}, SPEAKER[sp] || sp), el("span", {}, `#${i}`)),
      el("div", { className: "text" }, m.text ?? ""));
    node.addEventListener("click", () => { focusMessage(i); if (offsets[i] != null) seekTo(offsets[i]); });
    return node;
  }));
}

function clearMarks() {
  $$(".msg.focus").forEach((n) => n.classList.remove("focus"));
  $$(".msg .text").forEach((t) => { const i = Number(t.parentElement.id.slice(2)); t.textContent = conv.transcript[i]?.text ?? ""; });
  $$(".missing").forEach((n) => n.remove());
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
  selected = index;
  renderTimeline();
  if (conv.audio_url && offsets[index] != null) seekTo(offsets[index]);
}

function showMissing(missing) {
  clearMarks();
  const after = missing.after_message_index;
  const marker = el("div", { className: "missing" }, `Здесь ожидалось: ${missing.description ?? "обязательное действие пропущено"}`);
  const anchor = after >= 0 ? document.getElementById(`m-${after}`) : null;
  if (anchor) anchor.after(marker); else $("#messages").prepend(marker);
  marker.scrollIntoView({ behavior: "smooth", block: "center" });
  if (after >= 0) { selected = after; renderTimeline(); }
}

/* ---------------- score panel ---------------- */
function status(r) {
  const v = r.verdict;
  if (v == null) return "na";
  if (v === "N/A") return "na";
  if (r.kind === "violation") return v === "YES" ? "bad" : "ok";
  const p = Number(r.points), m = Number(r.max_points);
  if (p >= m) return "ok";
  return p === 0 ? "bad" : "warn";
}
function earned(r) { return Number(r.weight) * Number(r.points) / Number(r.max_points); }

function donut(value, label) {
  const r = 62, c = 2 * Math.PI * r, v = Math.max(0, Math.min(100, Number(value) || 0));
  const box = el("div", {});
  box.append(sv("svg", { viewBox: "0 0 150 150", "aria-hidden": "true" },
    sv("circle", { cx: 75, cy: 75, r, fill: "none", stroke: "var(--line-2)", "stroke-width": 12 }),
    sv("circle", { cx: 75, cy: 75, r, fill: "none", stroke: v >= 80 ? "var(--accent)" : v >= 60 ? "#c19a2b" : "var(--bad)", "stroke-width": 12,
      "stroke-linecap": "round", "stroke-dasharray": `${(c * v) / 100} ${c}` })));
  box.append(el("div", { className: "val" }, el("b", {}, label), el("span", {}, "/ 100")));
  return [...box.childNodes];
}

function renderScore(ev) {
  const d = $("#donut");
  const scoreText = ev.final_score != null ? nf(ev.final_score) : "—";
  d.replaceChildren(...donut(ev.final_score, scoreText));
  d.title = ev.final_score == null ? `Статус: ${ev.score_status}` : "";
  if (previousScore !== null && previousScore !== scoreText) { d.classList.add("changed"); setTimeout(() => d.classList.remove("changed"), 1600); }
  previousScore = scoreText;

  const rows = ev.results.filter((r) => r.kind !== "violation");
  $("#crit-list").replaceChildren(...rows.map((r) => {
    const st = status(r);
    const icon = { ok: "✓", bad: "✕", warn: "!", na: "–" }[st];
    const pts = r.verdict === "N/A" ? el("span", { className: "pts" }, el("span", { className: "of" }, "не применимо"))
      : r.verdict == null ? el("span", { className: "pts bad" }, "нет оценки")
      : el("span", { className: `pts ${st}` }, nf(earned(r)), el("span", { className: "of" }, ` / ${nf(r.weight)}`));
    const li = el("li", { title: "Открыть подробности" }, el("span", { className: `ic ${st}` }, icon),
      el("span", {}, RU[r.criterion_id] || r.name, r.source === "REVIEW" ? el("span", { className: "small" }, " · проверено") : null), pts);
    li.addEventListener("click", () => openCard(r.criterion_id));
    return li;
  }));
  const viol = ev.results.find((r) => r.kind === "violation");
  const box = $("#violations");
  box.hidden = !(viol && viol.verdict === "YES");
  box.textContent = box.hidden ? "" : "⚠ Обнаружено грубое нарушение — обязательная проверка руководителем (в балл не входит).";
  if (!box.hidden) { box.style.cursor = "pointer"; box.onclick = () => openCard(viol.criterion_id); }

  $("#meta").textContent = `Чек-лист: ${ev.checklist.name} · v${ev.checklist.version} · ${ev.model} · `
    + `${(ev.tokens_in + ev.tokens_out).toLocaleString("ru-RU")} токенов · $${ev.cost_usd} · ${(ev.duration_ms / 1000).toFixed(1)} с`
    + (conv.reference?.final_score != null ? ` · эталон: ${nf(conv.reference.final_score)}` : "");
}

async function renderWhy(ev) {
  const box = $("#why");
  let a;
  try { a = await api(`/api/analytics/conversations/${encodeURIComponent(ID)}`); } catch { box.hidden = true; return; }
  const items = [...a.serious, ...a.violations];
  box.hidden = false;
  if (!items.length) { box.replaceChildren(el("h3", {}, "Почему снижена оценка"), el("p", { className: "small" }, "Оценка не снижена: потерянных баллов нет.")); return; }
  const result = (cid) => ev.results.find((r) => r.criterion_id === cid);
  box.replaceChildren(el("h3", {}, "Почему снижена оценка"), ...items.map((v) => {
    const r = result(v.criterion_id);
    const first = v.evidence.find((q) => q.message_index != null);
    const go = el("button", { type: "button", className: "primary" }, "Перейти к фрагменту");
    go.addEventListener("click", () => {
      const e = r?.evidence.find((x) => x.message_index === first?.message_index);
      if (first) focusMessage(first.message_index, e?.char_start, e?.char_end);
      else if (r?.missing) showMissing(r.missing);
    });
    go.disabled = !first && !r?.missing;
    const dispute = el("button", { type: "button", className: "ghost" }, "Оспорить оценку");
    dispute.addEventListener("click", () => openCard(v.criterion_id, "appeal"));
    const lost = v.criterion_id === "c8_violations" ? "обязательная проверка" : `−${nf(v.lost_points)} балл.${v.split ? " (доля)" : ""}`;
    return el("div", { className: "why-item" },
      el("div", { className: "t" }, `${v.violation} · ${RU[v.criterion_id] || v.criterion_name} · ${lost}`),
      v.reason ? el("div", { className: "r" }, v.reason) : null,
      v.recommendation ? el("div", { className: "rec" }, v.recommendation) : null,
      el("div", { className: "btns" }, go, dispute));
  }));
}

function openCard(cid, part) {
  const box = $("#cards-box");
  box.open = true;
  const card = document.getElementById(`c-${cid}`);
  if (!card) return;
  $$(".card.focus").forEach((c) => c.classList.remove("focus"));
  card.classList.add("focus");
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  if (part === "appeal") {
    const d = card.querySelector("details.appeal");
    if (d) { d.open = true; d.querySelector("textarea")?.focus({ preventScroll: true }); }
  }
}

/* ---------------- detail cards ---------------- */
const VERDICT_RU = { YES: "Да", NO: "Нет", "N/A": "Не применимо" };
function pill(r) {
  const cls = { ok: "ok", bad: "bad", warn: "warn", na: "" }[status(r)];
  return el("span", { className: `badge ${cls}` }, r.verdict == null ? "нет вердикта" : (VERDICT_RU[r.verdict] ?? r.verdict));
}
function evidenceLink(e) {
  const statusCls = e.status === "EXACT" ? "ok" : e.status === "FUZZY" ? "warn" : "bad";
  const statusRu = { EXACT: "точная цитата", FUZZY: "близкая цитата", UNVERIFIED: "не найдена в тексте" }[e.status] || e.status;
  const a = el("a", { href: "#", className: "ev" },
    el("span", { className: `badge ${statusCls}` }, statusRu),
    ` #${e.message_index ?? e.cited_index} `, `«${e.matched_text || e.quote}»`,
    e.index_corrected ? el("span", { className: "small" }, ` (модель указала #${e.cited_index})`) : null);
  a.addEventListener("click", (ev) => { ev.preventDefault(); if (e.message_index != null) focusMessage(e.message_index, e.char_start, e.char_end); });
  return a;
}

function card(r) {
  const pointsText = r.kind === "violation" ? "в балл не входит"
    : r.verdict === "N/A" ? `вес ${nf(r.weight)} перераспределён`
    : r.verdict == null ? "" : `${nf(earned(r))} из ${nf(r.weight)}`;
  const ref = conv.reference?.verdicts?.[r.criterion_id];
  const refBadge = ref == null ? null
    : el("span", { className: `badge ${ref === r.ai_verdict ? "ok" : "bad"}`, title: "Эталонная разметка (eval/gold.json) и вердикт AI" },
         `эталон: ${VERDICT_RU[ref] ?? ref} ${ref === r.ai_verdict ? "✓" : "✗"}`);
  const body = [
    el("h3", {}, RU[r.criterion_id] || r.name, pill(r), refBadge,
      el("span", { className: "small" }, pointsText),
      el("span", { className: `badge ${r.source === "REVIEW" ? "ok" : ""}` }, r.source === "REVIEW" ? "решение проверяющего" : "AI"),
      r.needs_review && r.source === "AI" ? el("span", { className: "badge warn" }, "нужна проверка") : null),
    el("p", { className: "why-text" }, r.reasoning ?? ""),
  ];
  if (r.verdict === "N/A" && r.na_reason) body.push(el("p", { className: "small" }, `Почему не применимо: ${r.na_reason}`));
  r.evidence.forEach((e) => body.push(evidenceLink(e)));
  if (r.missing) {
    const a = el("a", { href: "#", className: "ev" }, el("span", { className: "badge bad" }, "пропущено"),
      ` после #${r.missing.after_message_index}: ${r.missing.description ?? ""}`);
    a.addEventListener("click", (e) => { e.preventDefault(); showMissing(r.missing); });
    body.push(a);
  }
  if (r.sub_checks?.length) {
    body.push(el("details", {}, el("summary", {}, "Подвопросы"),
      ...r.sub_checks.map((s) => el("div", { className: "small" }, `${s.answer === "YES" ? "Да" : "Нет"} — ${s.check_id}`))));
  }
  if (r.issues?.length) body.push(el("p", { className: "small" }, "Замечания проверки: " + r.issues.join(" | ")));
  if (r.decisions?.length) {
    body.push(el("details", {}, el("summary", {}, `История (${r.decisions.length})`),
      el("div", { className: "small" }, `AI: ${r.ai_verdict ?? "—"}`),
      ...r.decisions.map((d) => el("div", { className: "small" },
        `${d.reviewer} · ${d.decision === "CHANGE" ? "изменено" : "подтверждено"} · ${d.old_verdict} → ${d.new_verdict} · «${d.reason}»`))));
  }
  body.push(appealForm(r), reviewForm(r));
  return el("article", { className: `card ${r.needs_review && r.source === "AI" ? "flag" : ""}`, id: `c-${r.criterion_id}` }, ...body);
}

function appealForm(r) {
  const open = r.appeals.find((a) => a.status !== "RESOLVED");
  const comment = el("textarea", { placeholder: "Почему оценка неверна? Укажите номер сообщения, если можно.", required: true });
  const form = el("form", {}, comment, el("button", { type: "submit", className: "primary" }, "Подать апелляцию"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api(`/api/results/${r.id}/appeals`, { method: "POST", body: JSON.stringify({ comment: comment.value }) }); await load(); notice("Апелляция подана и появилась в разделе «Апелляции»."); }
    catch (err) { notice(err.message); }
  });
  return el("details", { className: "appeal" }, el("summary", {}, open ? `Апелляция открыта: «${open.comment}»` : "Оспорить оценку"), open ? null : form);
}

function reviewForm(r) {
  const open = r.appeals.find((a) => a.status !== "RESOLVED");
  const verdict = el("select", {}, ...r.allowed.map((v) => el("option", { value: v, selected: v === r.verdict }, VERDICT_RU[v] ?? v)));
  const reason = el("textarea", { placeholder: "Причина решения (обязательно)", required: true });
  const confirm = el("button", { type: "submit", value: "CONFIRM" }, "Подтвердить");
  const change = el("button", { type: "submit", value: "CHANGE", className: "primary" }, "Изменить вердикт");
  const form = el("form", {}, el("label", { className: "small" }, "Новый вердикт ", verdict), reason, el("div", {}, confirm, " ", change));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const decision = e.submitter?.value || "CONFIRM";
    try {
      const out = await api(`/api/results/${r.id}/reviews`, { method: "POST", body: JSON.stringify({
        decision, reason: reason.value, new_verdict: decision === "CHANGE" ? verdict.value : null,
        appeal_id: open ? open.id : null }) });
      notice(`Балл пересчитан: ${out.old_final_score ?? "—"} → ${out.new_final_score ?? out.score_status}`);
      await load();
    } catch (err) { notice(err.message); }
  });
  return el("details", { open: !!open }, el("summary", {}, open ? "Рассмотреть апелляцию" : "Проверка руководителем"), form);
}

function renderLedger(ev) {
  const rows = ev.ledger?.rows ?? [];
  $("#ledger").replaceChildren(...rows.map((r) => {
    const key = `${r.verdict}|${r.contribution}`;
    const tr = el("tr", { className: previousLedger[r.criterion_id] && previousLedger[r.criterion_id] !== key ? "changed" : "" },
      el("td", {}, RU[r.criterion_id] || r.name), el("td", {}, VERDICT_RU[r.verdict] ?? r.verdict ?? "—"),
      el("td", {}, r.source === "REVIEW" ? "проверяющий" : r.source),
      el("td", {}, r.weight), el("td", {}, r.effective_weight), el("td", {}, r.contribution));
    previousLedger[r.criterion_id] = key;
    return tr;
  }));
  const trig = { AI: "AI", REVIEW: "проверка", RECALC: "пересчёт" };
  $("#history").textContent = "История балла: " + ev.history.map((h) => `${trig[h.trigger] ?? h.trigger} ${h.final_score ?? "—"}`).join(" → ")
    + (ev.ledger?.note ? ` · ${ev.ledger.note}` : "");
}

function renderEvaluation() {
  const ev = conv.evaluation;
  if (!ev) {
    $("#donut").replaceChildren(...donut(0, "—"));
    $("#crit-list").replaceChildren(el("li", { className: "small", style: "cursor:default" }, "", "Диалог ещё не оценён. Нажмите «Оценить».", ""));
    $("#meta").textContent = ""; $("#cards").replaceChildren(); $("#why").hidden = true; $("#violations").hidden = true;
    return;
  }
  renderScore(ev);
  renderLedger(ev);
  $("#cards").replaceChildren(...ev.results.map((r) => card(r)));
  renderWhy(ev);
}

async function load() {
  conv = await api(`/api/conversations/${encodeURIComponent(ID)}`);
  if (!$("#messages").children.length) {
    computeOffsets();
    renderHeader();
    renderTranscript();
    renderTimeline();
    setupAudio();
  }
  renderEvaluation();
}

$("#btn-eval").addEventListener("click", async () => {
  const btn = $("#btn-eval");
  btn.disabled = true; $("#busy").textContent = "Оцениваем… (~6 с)"; notice("");
  try {
    await api("/api/evaluations", { method: "POST", body: JSON.stringify({ conversation_id: ID, checklist_slug: $("#checklist").value || null }) });
    previousLedger = {};
    await load();
  } catch (e) { notice(e.message); }
  finally { btn.disabled = false; $("#busy").textContent = ""; }
});

if (!ID) notice("В адресе нет ?id= диалога.");
else load().catch((e) => notice(e.message));
