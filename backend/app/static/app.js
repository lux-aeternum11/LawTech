const state = {
  cases: [],
  tab: "all",
  detail: null,
  viewMode: "all",
  viewDoc: 0,
};

const steps = [
  "1. Извлечение текста",
  "2. Анализ YandexGPT",
  "3. Верификация цитат и норм закона",
];

document.addEventListener("DOMContentLoaded", () => {
  lucide.createIcons();
  bindDashboard();
  refresh();
});

function bindDashboard() {
  const dropzone = document.getElementById("dropzone");
  const input = document.getElementById("file-input");
  input.addEventListener("change", () => uploadFiles(input.files));
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("bg-slate-50");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("bg-slate-50"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("bg-slate-50");
    uploadFiles(event.dataTransfer.files);
  });
  document.getElementById("number-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const number = document.getElementById("case-number").value.trim();
    const error = document.getElementById("number-error");
    error.classList.add("hidden");
    try {
      const response = await fetch("/api/cases/fetch-by-number", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_number: number }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Не удалось открыть дело");
      await refresh();
      if (payload.case_id) await openCase(payload.case_id);
    } catch (reason) {
      error.textContent = reason.message;
      error.classList.remove("hidden");
    }
  });
  document.querySelectorAll(".tab-btn").forEach((button) => {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tab;
      paintTabs();
      paintCases();
    });
  });
  document.getElementById("back-btn").addEventListener("click", () => {
    document.getElementById("review").classList.add("hidden");
    document.getElementById("dashboard").classList.remove("hidden");
    document.getElementById("progress").classList.add("hidden");
    refresh();
  });
}

async function refresh() {
  await Promise.all([loadHealth(), loadCases()]);
}

async function loadHealth() {
  const badge = document.getElementById("llm-badge");
  try {
    const health = await (await fetch("/api/health")).json();
    if (health.llm_provider === "yandex" && health.llm_configured) {
      badge.className = "rounded-full bg-emerald-100 px-3 py-1 text-sm font-medium text-emerald-900";
      badge.textContent = `Yandex AI подключен (${health.llm_model})`;
      return;
    }
    if (health.llm_configured) {
      badge.className = "rounded-full bg-sky-100 px-3 py-1 text-sm font-medium text-sky-900";
      badge.textContent = `LLM подключен (${health.llm_model})`;
      return;
    }
  } catch (_error) {
    badge.textContent = "Сервис не отвечает";
    return;
  }
  badge.className = "rounded-full bg-amber-100 px-3 py-1 text-sm font-medium text-amber-900";
  badge.textContent = "Режим Regex Fallback (проверьте .env)";
}

async function loadCases() {
  const response = await fetch("/api/cases");
  state.cases = response.ok ? await response.json() : [];
  paintKpis();
  paintCases();
}

function paintKpis() {
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById("kpi-urgent").textContent = String(state.cases.filter((item) => isUrgent(item, today)).length);
  document.getElementById("kpi-hearings").textContent = String(state.cases.filter((item) => item.next_hearing_date).length);
  document.getElementById("kpi-total").textContent = String(state.cases.length);
}

function paintTabs() {
  document.querySelectorAll(".tab-btn").forEach((button) => {
    const active = button.dataset.tab === state.tab;
    button.className = active
      ? "tab-btn rounded-full bg-navy px-4 py-1.5 text-sm text-white"
      : "tab-btn rounded-full bg-white px-4 py-1.5 text-sm text-navy";
  });
}

function paintCases() {
  const today = new Date().toISOString().slice(0, 10);
  const rows = state.cases.filter((item) => {
    if (state.tab === "today") return isUrgent(item, today) || item.nearest_deadline === today || item.next_hearing_date === today;
    if (state.tab === "hearings") return Boolean(item.next_hearing_date);
    return true;
  });
  const host = document.getElementById("case-list");
  if (!rows.length) {
    host.innerHTML = '<p class="rounded-xl bg-white p-6 text-sm text-slate-500 shadow-sm">Дел пока нет. Загрузите PDF или найдите номер.</p>';
    return;
  }
  host.innerHTML = rows.map((item) => {
    const totalTasks = (item.done_tasks_count || 0) + (item.open_tasks_count || 0);
    return `
    <article class="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-white p-4 shadow-sm">
      <div>
        <p class="font-medium text-navy">${escapeHtml(item.case_number)} · ${escapeHtml(item.court_name || "")}</p>
        <p class="text-sm text-slate-600">${escapeHtml(item.case_stage || "Стадия не указана")}</p>
        <p class="mt-1 text-sm text-slate-500">Заседание: ${formatDate(item.next_hearing_date)} · Дедлайн: ${formatDate(item.nearest_deadline)}</p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span class="rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-700">${item.document_count || 0} док.</span>
        <span class="rounded-full bg-sky-50 px-2 py-1 text-xs text-navy">Задачи: ${item.done_tasks_count || 0}/${totalTasks}</span>
        <span class="rounded-full px-2 py-1 text-xs ${item.urgency === "critical" ? "bg-red-100 text-red-800" : "bg-slate-100 text-slate-700"}">${item.urgency === "critical" ? "Срочно" : "Обычный"}</span>
        <span class="rounded-full bg-slate-100 px-2 py-1 text-xs uppercase">${escapeHtml(item.extraction_mode || "—")}</span>
        <button type="button" data-open="${item.id}" class="rounded-lg bg-navy px-3 py-1.5 text-sm text-white">Открыть</button>
        <button type="button" data-delete="${item.id}" class="rounded-lg border border-red-200 px-3 py-1.5 text-sm text-red-700">Удалить</button>
      </div>
    </article>`;
  }).join("");
  host.querySelectorAll("[data-open]").forEach((button) => {
    button.addEventListener("click", () => openCase(Number(button.dataset.open)));
  });
  host.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", () => removeCase(Number(button.dataset.delete)));
  });
}

function isUrgent(item, today) {
  return item.urgency === "critical" || (item.nearest_deadline && item.nearest_deadline <= today);
}

async function uploadFiles(fileList) {
  const files = [...fileList].filter((file) => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"));
  const error = document.getElementById("upload-error");
  error.classList.add("hidden");
  if (!files.length) {
    error.textContent = "Нужен файл PDF.";
    error.classList.remove("hidden");
    return;
  }
  const progress = document.getElementById("progress");
  const bar = document.getElementById("progress-bar");
  const list = document.getElementById("progress-steps");
  progress.classList.remove("hidden");
  list.innerHTML = steps.map((step, index) => `<li data-step="${index}">${step}</li>`).join("");
  let stepIndex = 0;
  bar.style.width = "15%";
  const timer = setInterval(() => {
    stepIndex = Math.min(stepIndex + 1, steps.length - 1);
    bar.style.width = `${20 + stepIndex * 30}%`;
    list.querySelectorAll("li").forEach((item, index) => {
      item.className = index <= stepIndex ? "font-medium text-navy" : "";
    });
  }, 700);
  try {
    const body = new FormData();
    files.forEach((file) => body.append("files", file));
    const response = await fetch("/api/documents/upload", { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Не удалось разобрать PDF");
    bar.style.width = "100%";
    await refresh();
    const caseId = payload.case_id || payload.documents?.[0]?.case_id;
    if (caseId) await openCase(caseId);
  } catch (reason) {
    error.textContent = reason.message;
    error.classList.remove("hidden");
  } finally {
    clearInterval(timer);
    document.getElementById("file-input").value = "";
    progress.classList.add("hidden");
  }
}

async function openCase(caseId) {
  const response = await fetch(`/api/cases/${caseId}`);
  if (!response.ok) return;
  state.detail = await response.json();
  state.viewMode = "all";
  state.viewDoc = 0;
  document.getElementById("dashboard").classList.add("hidden");
  document.getElementById("review").classList.remove("hidden");
  paintReview();
}

function selectedDocuments() {
  const documents = state.detail?.documents || [];
  if (state.viewMode === "all") return documents;
  const one = documents[Number(state.viewMode)];
  return one ? [one] : [];
}

function paintReview() {
  const detail = state.detail;
  const documents = detail.documents || [];
  const urgency = document.getElementById("case-urgency");
  urgency.innerHTML = `<span class="rounded-full px-2 py-1 text-xs ${detail.urgency === "critical" ? "bg-red-100 text-red-800" : "bg-slate-100 text-slate-700"}">${detail.urgency === "critical" ? "Срочно" : "Обычный срок"}</span>`;
  const switcher = document.getElementById("doc-switch");
  const summaryClass = state.viewMode === "all" ? "bg-navy text-white" : "bg-white text-navy";
  const fileButtons = documents.map((documentItem, index) => {
    const active = String(state.viewMode) === String(index);
    return `<button type="button" data-mode="${index}" class="max-w-[16rem] truncate rounded-full px-3 py-1 text-sm ${active ? "bg-navy text-white" : "bg-white text-navy"}">${escapeHtml(documentItem.filename)}</button>`;
  }).join("");
  switcher.innerHTML = `
    <button type="button" data-mode="all" class="rounded-full px-3 py-1 text-sm ${summaryClass}">Сводка по делу (${documents.length} док.)</button>
    ${fileButtons}
  `;
  switcher.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      state.viewMode = button.dataset.mode;
      paintReview();
    });
  });
  const shown = selectedDocuments();
  if (!documents.length) {
    document.getElementById("analysis").innerHTML = courtCardHtml(detail);
    paintViewer(null);
    return;
  }
  document.getElementById("analysis").innerHTML = analysisHtml(shown, documents.length > 1 && state.viewMode === "all");
  paintViewer(documents[state.viewDoc] || documents[0]);
  bindReview();
}

function analysisHtml(documents, showSource) {
  const newest = documents[0];
  const parties = uniqueParties(documents);
  const claims = uniqueStrings(documents.flatMap((item) => item.analysis.financial_claims || []));
  const basis = uniqueStrings(documents.flatMap((item) => item.analysis.legal_basis || []));
  const knowledge = (documents.find((item) => item.analysis.legal_knowledge) || newest).analysis.legal_knowledge;
  const warnings = documents.flatMap((item) => item.warnings || []);
  const tasks = collectTasks(documents);
  const hearing = futureHearing(documents);
  const openCount = tasks.active.filter((entry) => !taskDone(entry)).length;
  return `
    <section class="rounded-xl border-l-4 border-navy bg-white p-4 shadow-sm">
      <h2 class="mb-2 font-semibold text-navy">Краткая сводка по делу ${modeBadge(newest.extraction_mode)} ${tokenBadge(documents)}</h2>
      <p class="text-sm leading-6">${escapeHtml(caseBrief(documents, parties, claims, openCount, hearing))}</p>
      ${warnings.map((warning) => `<p class="mt-2 rounded bg-amber-50 px-2 py-1 text-amber-900">${escapeHtml(warning)}</p>`).join("")}
    </section>
    ${block("Актуальные задачи и сроки", `
      ${tasks.active.map((entry) => taskCard(entry.item, entry.kind, entry.index, entry.task, showSource, false)).join("") || "<p>Актуальных задач по последним актам нет.</p>"}
      ${tasks.archived.length ? `<details class="mt-3"><summary class="cursor-pointer text-sm text-slate-600">Предыдущие этапы (архив, ${tasks.archived.length})</summary><div class="mt-2 space-y-2">${tasks.archived.map((entry) => taskCard(entry.item, entry.kind, entry.index, entry.task, showSource, true)).join("")}</div></details>` : ""}
    `)}
    ${block("Реквизиты, участники и суммы", `
      <p><b>Дело:</b> ${escapeHtml(newest.analysis.case_number)} · ${escapeHtml(newest.analysis.court_name)}</p>
      <p><b>Судья:</b> ${escapeHtml(newest.analysis.judge_name || "не указан")} · <b>Стадия:</b> ${escapeHtml(newest.analysis.case_stage)}</p>
      <ul class="mt-2 space-y-1">${parties.map((party) => `<li>${escapeHtml(party.role)}: ${escapeHtml(party.name)}${party.inn ? `, ИНН ${escapeHtml(party.inn)}` : ""}${party.snils ? `, СНИЛС ${escapeHtml(party.snils)}` : ""}</li>`).join("") || "<li>Участники не выделены.</li>"}</ul>
      ${hearing ? `<p class="mt-2">Ближайшее заседание: ${formatDate(hearing.h.date)} ${escapeHtml(hearing.h.time || "")} ${escapeHtml(hearing.h.location || "")} ${pdfButton(hearing.doc, hearing.h.source_quote)}</p>` : ""}
      <ul class="mt-2 list-disc pl-5">${claims.map((claim) => `<li>${escapeHtml(claim)}</li>`).join("") || "<li>Суммы в тексте не выделены.</li>"}</ul>
    `)}
    ${block("Хронология актов", documents.map((item) => `
      <article class="border-b border-slate-100 pb-2">
        <p class="text-xs text-slate-500">${formatDate(item.analysis.act_date)} · ${escapeHtml(item.analysis.document_type)} ${tokenBadge([item])}</p>
        <p>${escapeHtml(item.analysis.verdict_summary)}</p>
        ${pdfButton(item, (item.analysis.key_points || [])[0] || null)}
      </article>
    `).join(""))}
    <details class="rounded-xl bg-white p-4 shadow-sm">
      <summary class="cursor-pointer font-semibold text-navy">Подробности: ключевые цитаты и статьи из актов ${tokenBadge(documents)}</summary>
      <div class="mt-3 space-y-2 text-sm">
        ${documents.flatMap((item) => (item.analysis.key_points || []).map((quote) => `<p>${pdfButton(item, quote)} <span class="text-xs text-slate-500">${formatDate(item.analysis.act_date)} · ${escapeHtml(item.analysis.document_type)}.</span> <span>${escapeHtml(quote.exact_quote)}</span></p>`)).join("")}
        <ul class="mt-2 list-disc pl-5">${basis.map((item) => `<li>${consultantLink(item)}</li>`).join("") || "<li>Статьи в тексте не названы.</li>"}</ul>
      </div>
    </details>
    ${knowledgeHtml(knowledge)}
  `;
}

function caseBrief(documents, parties, claims, openCount, hearing) {
  const analysis = documents[0].analysis;
  const debtor = parties.find((party) => party.role === "Должник");
  const manager = parties.find((party) => party.role === "Финансовый управляющий");
  const judge = analysis.judge_name ? `, судья ${analysis.judge_name}` : "";
  let text = `Дело № ${analysis.case_number} (${analysis.court_name}${judge}):`;
  if (debtor) text += ` должник ${shortName(debtor.name)}`;
  text += ` — ${analysis.case_stage}.`;
  if (manager) text += ` Финансовый управляющий — ${shortName(manager.name).replace(/\.$/, "")}.`;
  if (analysis.verdict_summary) text += ` ${analysis.verdict_summary}`;
  if (claims.length) text += ` Суммы из актов: ${claims.slice(0, 2).join("; ")}.`;
  if (hearing) text += ` Ближайшее заседание — ${formatDate(hearing.h.date)}.`;
  text += openCount ? ` На контроле открытых задач: ${openCount}.` : " Открытых задач по актуальным актам нет.";
  return text;
}

function shortName(name) {
  const parts = String(name || "").split(/\s+/).filter(Boolean);
  if (parts.length < 2) return name;
  return `${parts[0]} ${parts.slice(1).map((part) => `${part[0].toUpperCase()}.`).join("")}`;
}

function collectTasks(documents) {
  const active = [];
  const archived = [];
  documents.forEach((item) => {
    const bucket = isArchived(item, documents) ? archived : active;
    (item.analysis.court_orders || []).forEach((task, index) => bucket.push({ item, kind: "order", index, task }));
    (item.analysis.deadlines || []).forEach((task, index) => bucket.push({ item, kind: "deadline", index, task }));
  });
  return { active, archived };
}

function procedureRank(analysis) {
  const blob = `${analysis.stage_code} ${analysis.case_stage} ${analysis.verdict_summary || ""}`.toLowerCase().replaceAll("ё", "е");
  if (["bankruptcy_individual_restructuring", "bankruptcy_individual_realization", "bankruptcy_individual_intro"].includes(analysis.stage_code) || blob.includes("реализац") || blob.includes("реструктуризац") || blob.includes("признан банкротом") || blob.includes("признать банкротом")) return 2;
  if (blob.includes("без движения")) return 0;
  return 1;
}

function isArchived(documentItem, documents) {
  const own = documentItem.analysis.act_date || "";
  if (!own) return false;
  const rank = procedureRank(documentItem.analysis);
  return documents.some((other) => other !== documentItem && (other.analysis.act_date || "") > own && procedureRank(other.analysis) > rank);
}

function futureHearing(documents) {
  const today = new Date().toISOString().slice(0, 10);
  const found = [];
  documents.forEach((doc) => {
    const hearing = doc.analysis.next_hearing;
    if (hearing && hearing.date && hearing.date >= today) found.push({ doc, h: hearing });
  });
  found.sort((left, right) => left.h.date.localeCompare(right.h.date));
  return found[0] || null;
}

function taskDone(entry) {
  const key = `doc:${entry.item.doc_id}:${entry.kind}:${entry.index}`;
  return Boolean((state.detail.task_states || {})[key]?.is_done);
}

function documentChip(item) {
  const index = (state.detail.documents || []).indexOf(item);
  return `<p class="flex flex-wrap items-center gap-2 text-sm">
    <span>${formatDate(item.analysis.act_date)} · ${escapeHtml(item.analysis.document_type)} · ${escapeHtml(item.filename)}</span>
    <button type="button" data-view="${index}" class="rounded bg-slate-100 px-2 py-0.5 text-xs text-navy">Посмотреть в PDF</button>
  </p>`;
}

function taskCard(documentItem, kind, index, item, showSource, archived) {
  const key = `doc:${documentItem.doc_id}:${kind}:${index}`;
  const saved = (state.detail.task_states || {})[key] || {};
  const done = Boolean(saved.is_done);
  const due = saved.calculated_due_date || item.due_date;
  const critical = !archived && kind === "deadline" && item.urgency === "critical" && !done;
  return `<article class="rounded-lg border p-3 ${critical ? "border-red-200 bg-red-50" : "border-slate-200"} ${done ? "opacity-60" : ""}">
    <label class="flex items-start gap-2">
      <input type="checkbox" class="mt-1" data-task="${escapeHtml(key)}" ${done ? "checked" : ""}>
      <span class="${done ? "line-through" : ""}">
        ${kind === "order" ? `<span class="rounded-full bg-navy/10 px-2 py-0.5 text-xs font-medium text-navy">${escapeHtml(item.actor)}</span>` : ""}
        <span class="mt-1 block font-medium">${escapeHtml(kind === "order" ? item.action : item.description)}</span>
      </span>
    </label>
    ${showSource ? `<p class="mt-1 text-xs text-slate-500">${escapeHtml(documentItem.filename)}</p>` : ""}
    <p class="mt-1 text-sm text-slate-600">${taskCaption(item, saved, due)}</p>
    ${item.anchor_event === "receipt_date" ? `<label class="mt-2 block text-xs text-slate-500">Дата получения <input type="date" data-receipt="${escapeHtml(key)}" value="${escapeHtml(saved.receipt_date || "")}" class="ml-1 rounded border border-slate-300 px-1 py-0.5 text-sm text-slate-900"></label>` : ""}
    <div class="mt-2">${pdfButton(documentItem, item.source_quote)}</div>
  </article>`;
}

function taskCaption(item, saved, due) {
  if (item.anchor_event === "receipt_date" && !saved.receipt_date) return "укажите дату получения для расчёта";
  if (saved.calculated_due_date) return `→ до ${formatDate(saved.calculated_due_date)}`;
  if (item.due_date) {
    const months = item.months_limit ? `${item.months_limit} мес. · ` : "";
    const days = item.days_limit ? `${item.days_limit} ${item.days_type === "business" ? "рабочих" : "календарных"} дн. · ` : "";
    return `${months}${days}до ${formatDate(item.due_date)}`;
  }
  if (item.deadline_text) return item.deadline_text;
  return "без фиксированной даты";
}

function pdfButton(documentItem, quote) {
  if (!quote || !documentItem) return "";
  const index = (state.detail.documents || []).indexOf(documentItem);
  const warn = quote.verified === false ? '<span class="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-900">⚠ проверить текст</span>' : "";
  return `<button type="button" class="mr-2 inline-flex items-center gap-1 rounded bg-slate-100 px-2 py-0.5 text-xs text-navy" data-view="${index}" data-page="${quote.page_number}" data-quote="${encodeURIComponent(quote.exact_quote)}">Посмотреть в PDF · стр. ${quote.page_number}</button>${warn}`;
}

function knowledgeHtml(knowledge) {
  if (!knowledge) return "";
  const articles = (knowledge.statutory_articles || []).map((item) => `<li><a href="${escapeHtml(item.url || consultantUrl(item.article))}" target="_blank" rel="noopener noreferrer" class="text-navy underline decoration-sky-400 hover:text-sky-700">${escapeHtml(item.article)} ↗</a>: ${escapeHtml(item.summary)}</li>`).join("");
  const prose = (title, items) => `
    <div class="mt-3">
      <p class="font-medium">${title}</p>
      <ul class="mt-1 list-disc pl-5">${(items || []).map((item) => `<li>${linkifyArticles(item)}</li>`).join("")}</ul>
    </div>`;
  return `
    <details class="rounded-xl border border-sky-200 bg-sky-50 p-4">
      <summary class="cursor-pointer text-lg font-semibold text-navy">Правовая справка из законодательства РФ (помимо текста документа)</summary>
      <p class="mt-1 text-sm text-sky-950">${escapeHtml(knowledge.source_disclaimer)}</p>
      <p class="mt-2 font-medium">${escapeHtml(knowledge.stage_title)}</p>
      <div class="mt-3"><p class="font-medium">Применимые статьи</p><ul class="mt-1 list-disc pl-5">${articles}</ul></div>
      ${prose("Правовые последствия", knowledge.legal_consequences)}
      ${prose("Обязательства (чек-лист)", knowledge.mandatory_obligations)}
      ${prose("Права и ограничения сторон", knowledge.rights_and_restrictions)}
    </details>`;
}

function paintViewer(documentItem) {
  const head = document.getElementById("viewer-head");
  const pages = document.getElementById("pages");
  if (!documentItem) {
    head.innerHTML = "";
    pages.innerHTML = '<p class="text-sm text-slate-500">По этому делу ещё нет загруженного PDF. Текст для подсветки появится после загрузки акта.</p>';
    return;
  }
  const options = (state.detail.documents || []).map((item, index) => `<option value="${index}" ${index === state.viewDoc ? "selected" : ""}>${escapeHtml(item.filename)}</option>`).join("");
  const original = documentItem.pdf_url ? `<a class="text-sm text-navy underline" target="_blank" rel="noopener" href="${escapeHtml(documentItem.pdf_url)}#page=1">Оригинал PDF ↗</a>` : "";
  head.innerHTML = `<div class="flex flex-wrap items-center justify-between gap-2">
    <p class="text-sm font-medium text-navy">${escapeHtml(documentItem.filename)}</p>
    <div class="flex items-center gap-2"><select id="viewer-select" class="rounded border border-slate-300 px-2 py-1 text-sm">${options}</select>${original}</div>
  </div>`;
  pages.innerHTML = (documentItem.pages || []).map((page) => `
    <article id="page-${page.page}" class="border-b border-slate-100 pb-4">
      <p class="mb-2 text-sm font-medium text-navy">Страница ${page.page}</p>
      <pre class="whitespace-pre-wrap font-sans text-sm leading-6 text-slate-800">${escapeHtml(page.text) || "Пустая страница"}</pre>
    </article>
  `).join("");
  const select = document.getElementById("viewer-select");
  if (select) {
    select.addEventListener("change", () => {
      state.viewDoc = Number(select.value);
      paintViewer(state.detail.documents[state.viewDoc]);
    });
  }
}

function bindReview() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.view);
      state.viewDoc = Number.isNaN(index) ? 0 : index;
      const quote = button.dataset.quote ? decodeURIComponent(button.dataset.quote) : "";
      const pageNumber = button.dataset.page;
      paintViewer(state.detail.documents[state.viewDoc]);
      if (!pageNumber) return;
      const article = document.getElementById(`page-${pageNumber}`);
      if (!article) return;
      const pre = article.querySelector("pre");
      const original = state.detail.documents[state.viewDoc].pages.find((page) => String(page.page) === String(pageNumber));
      if (pre && original && quote) pre.innerHTML = highlight(original.text, quote);
      const link = document.querySelector("#viewer-head a");
      if (link && original) {
        const base = state.detail.documents[state.viewDoc].pdf_url;
        if (base) link.href = `${base}#page=${pageNumber}`;
      }
      article.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  document.querySelectorAll("[data-task]").forEach((input) => {
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", () => patchTask(input.dataset.task, { is_done: input.checked }));
  });
  document.querySelectorAll("[data-receipt]").forEach((input) => {
    input.addEventListener("change", () => patchTask(input.dataset.receipt, { receipt_date: input.value }));
  });
}

async function patchTask(taskKey, body) {
  const response = await fetch(`/api/cases/${state.detail.id}/tasks/${encodeURIComponent(taskKey)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) return;
  const payload = await response.json();
  state.detail.task_states = state.detail.task_states || {};
  state.detail.task_states[payload.task.task_key] = payload.task;
  state.detail.urgency = payload.case_urgency;
  state.detail.nearest_deadline = payload.case_nearest_deadline;
  paintReview();
}

function uniqueParties(documents) {
  const parties = [];
  const rank = { "Финансовый управляющий": 4, "СРО АУ": 3, "Должник": 2, "Кредитор": 2, "Истец": 2, "Ответчик": 2, "Третье лицо": 1 };
  documents.forEach((item) => {
    (item.analysis.parties || []).forEach((party) => {
      const match = parties.find((existing) => samePartyName(existing.name, party.name));
      if (!match) {
        parties.push({ ...party });
        return;
      }
      match.inn = match.inn || party.inn;
      match.snils = match.snils || party.snils;
      if ((rank[party.role] || 0) > (rank[match.role] || 0)) {
        match.role = party.role;
        match.name = party.name;
      }
    });
  });
  return parties;
}

function samePartyName(left, right) {
  const stem = (name) => {
    const token = String(name || "").toLowerCase().replaceAll("ё", "е").split(/\s+/).find((part) => part && !["ооо", "ао", "пао", "зао", "ип"].includes(part)) || "";
    return token.length >= 5 ? token.slice(0, -2) : token;
  };
  const a = stem(left);
  const b = stem(right);
  if (a.length < 4 || b.length < 4) return String(left).trim().toLowerCase() === String(right).trim().toLowerCase();
  return a === b || a.startsWith(b) || b.startsWith(a);
}

function uniqueStrings(values) {
  const seen = new Set();
  return values.filter((value) => {
    const key = String(value).trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function courtCardHtml(detail) {
  const card = detail.card || {};
  const parties = (card.parties || []).map((party) => `<li>${escapeHtml(party.role)}: ${escapeHtml(party.name)}</li>`).join("");
  const hearings = (card.hearings || []).map((item) => `<li>${formatDate(item.date)} · ${escapeHtml(item.result || item.description || "")}</li>`).join("");
  return block("Карточка дела с сайта суда", `
    <p><b>${escapeHtml(card.case_number || detail.case_number)}</b></p>
    <p>${escapeHtml(card.court_name || detail.court_name)}</p>
    <p>Судья: ${escapeHtml(card.judge_name || "не указан")}</p>
    <p>Статус: ${escapeHtml(card.case_status || "")}</p>
    <ul class="mt-2 list-disc pl-5">${parties}</ul>
    <p class="mt-3 font-medium">Хронология</p>
    <ul class="list-disc pl-5">${hearings}</ul>
    <p class="mt-2 text-sm text-slate-500">${escapeHtml(card.note || "")}</p>
  `);
}

function highlight(pageText, quote) {
  const words = quote.split(/\s+/).map((word) => word.replace(/[^\p{L}\p{N}-]/gu, "")).filter((word) => word.length > 1).slice(0, 14);
  if (!words.length) return escapeHtml(pageText);
  const pattern = words.map(escapeRegExp).join("[\\s\\S]{0,8}");
  const match = pageText.match(new RegExp(pattern, "i"));
  if (!match || match.index === undefined) return escapeHtml(pageText);
  const start = match.index;
  const end = start + match[0].length;
  return `${escapeHtml(pageText.slice(0, start))}<mark class="hit">${escapeHtml(pageText.slice(start, end))}</mark>${escapeHtml(pageText.slice(end))}`;
}

function consultantUrl(article) {
  return `https://www.consultant.ru/search/?q=${encodeURIComponent(article.replace(/[«»"]/g, " ").replace(/\s+/g, " ").trim())}`;
}

function consultantLink(label) {
  return `<a href="${consultantUrl(label)}" target="_blank" rel="noopener noreferrer" class="text-navy underline decoration-sky-400 hover:text-sky-700">${escapeHtml(label)} ↗</a>`;
}

function tokenBadge(documents) {
  const total = documents.reduce((sum, item) => sum + Number(item.token_usage?.total_tokens || 0), 0);
  if (!total) return "";
  const formatted = String(total).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return `<span class="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">${formatted} ток.</span>`;
}

function linkifyArticles(text) {
  const pattern = /(?:ч\.\s*\d+\s+|п\.\s*[\d–\-]+\s+|абз\.\s*\d+\s+)*ст\.\s*[\d.]+(?:\s*(?:,|и)\s*[\d.]+)*(?:\s+(?:ФЗ-\d+|АПК\s*РФ|ГК\s*РФ|КоАП\s*РФ|ФЗ-229))?/gi;
  let html = "";
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    html += escapeHtml(text.slice(last, match.index));
    html += consultantLink(match[0]);
    last = match.index + match[0].length;
  }
  return html + escapeHtml(text.slice(last));
}

function block(title, body) {
  return `<section class="rounded-xl bg-white p-4 shadow-sm"><h2 class="mb-2 flex flex-wrap items-center gap-2 font-semibold text-navy">${title}</h2><div class="space-y-2 text-sm">${body}</div></section>`;
}

function modeBadge(mode) {
  if (mode === "llm") {
    return `<span class="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">LLM (YandexGPT)</span>`;
  }
  return `<span class="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900">Regex Fallback</span>`;
}

async function removeCase(caseId) {
  if (!confirm("Удалить дело и сохранённые документы?")) return;
  await fetch(`/api/cases/${caseId}`, { method: "DELETE" });
  await loadCases();
}

function formatDate(value) {
  if (!value) return "нет";
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
