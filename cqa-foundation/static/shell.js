"use strict";
/* Builds the sidebar. Each page: <aside class="side" id="side" data-page="dialogs"></aside> */
(function () {
  const side = document.getElementById("side");
  if (!side) return;
  const icon = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const items = [
    ["dialogs", "index.html", "Диалоги", icon('<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>')],
    ["checklists", "studio.html", "Чек-листы", icon('<rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8.5 12 2.5 2.5 4.5-5"/>')],
    ["appeals", "appeals.html", "Апелляции", icon('<path d="M12 3v18M5 7h14M7 7l-3 7a3 3 0 0 0 6 0zM17 7l-3 7a3 3 0 0 0 6 0zM8 21h8"/>')],
    ["analytics", "analytics.html", "Аналитика", icon('<path d="M5 20v-6M12 20V8M19 20V4"/>')],
    ["accuracy", "accuracy.html", "Точность", icon('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".6" fill="currentColor"/>')],
  ];
  const page = side.dataset.page;
  side.innerHTML = `<div class="logo">CQA<small>Freedom Broker</small></div>
    <nav class="side-nav" aria-label="Разделы">${items.map(([id, href, label, svg]) =>
      `<a href="${href}"${id === page ? ' aria-current="page"' : ""}>${svg}<span>${label}</span></a>`).join("")}</nav>
    <div class="side-foot">Conversation Quality Analytics<br>прототип для хакатона</div>`;
})();
