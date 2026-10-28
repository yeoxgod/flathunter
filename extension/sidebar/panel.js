"use strict";

const $ = (id) => document.getElementById(id);
const send = (msg) => browser.runtime.sendMessage(msg);
const INSTR = {
  short: "Make it noticeably shorter (about 45-60 words) and keep the key questions.",
  formal: "Make the tone more formal and polite.",
  casual: "Make the tone friendlier and more relaxed, still polite.",
};
const MODE_LABEL = { copy: "Копировать", insert: "Вставить в чат", send: "Отправить" };
let state = { settings: null, items: {} };

// ---------------- вкладки ----------------
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
}));

function flash(el, text, ok) {
  el.textContent = text;
  el.style.color = ok === false ? "var(--bad)" : ok ? "var(--good)" : "";
}
async function busy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}

// ---------------- загрузка ----------------
async function load() {
  state = await send({ type: "state" });
  renderStatus(state.status);
  renderItems();
  fillSettings();
}

function renderStatus(st) {
  const q = st && st.queue ? ` · в очереди: ${st.queue}` : "";
  $("status").textContent = ((st && st.last) || "Готов.") + q;
}

browser.runtime.onMessage.addListener((msg) => {
  if (msg.type === "status") renderStatus(msg.status);
  if (msg.type === "items") send({ type: "state" }).then((s) => { state = s; renderItems(); });
});

// ---------------- объявления ----------------
const zl = (v) => Number(v).toLocaleString("pl-PL") + " zł";

function renderItems() {
  const s = state.settings;
  const ob = [];
  if (!s.ai.geminiKey && !s.ai.claudeKey) ob.push("1) Настройки → вставьте ключ ИИ.");
  if (!s.criteria.summary && !s.criteria.maxPrice) ob.push("2) Поиск → опишите, что ищете.");
  if (!s.profile.name && !s.profile.about) ob.push("3) О себе → пару фраз о себе.");
  if (!s.monitor.urls.length) ob.push("4) Откройте поиск на Otodom/OLX и нажмите «Следить за текущей вкладкой» во вкладке «Поиск».");
  $("onboarding").textContent = ob.join(" ");

  const filter = $("itemFilter").value;
  const list = Object.entries(state.items)
    .filter(([, it]) => it.status !== "hidden" && (filter === "all" || it.status === filter))
    .sort((a, b) => b[1].ts - a[1].ts);
  const nNew = Object.values(state.items).filter((x) => x.status === "new").length;
  $("count").textContent = nNew || "";
  const box = $("items");
  box.textContent = "";
  if (!list.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = filter === "new" ? "Новых карточек нет. Откройте поиск Otodom/OLX и нажмите «Разобрать эту страницу» или включите слежение." : "Пусто.";
    box.appendChild(p);
  }
  for (const [key, it] of list) box.appendChild(card(key, it));
}

function li(ul, items) {
  ul.textContent = "";
  for (const t of items || []) { const x = document.createElement("li"); x.textContent = t; ul.appendChild(x); }
  ul.style.display = items && items.length ? "" : "none";
}

function card(key, it) {
  const node = $("cardTpl").content.firstElementChild.cloneNode(true);
  const l = it.listing, a = it.analysis;
  node.classList.toggle("contacted", it.status === "contacted");
  const sc = node.querySelector(".score");
  sc.textContent = a.fitScore + "/10";
  sc.classList.add(a.fitScore >= 8 ? "good" : a.fitScore >= 6 ? "mid" : "bad");
  const t = node.querySelector(".title");
  t.textContent = l.title || l.url;
  t.href = l.url;
  const facts = [];
  if (l.price) facts.push(zl(l.price) + (l.adminRent ? " + " + zl(l.adminRent) : ""));
  if (l.area) facts.push(l.area + " м²");
  if (l.rooms) facts.push(l.rooms + " комн.");
  if (l.deposit) facts.push("kaucja " + zl(l.deposit));
  if (l.location) facts.push(l.location);
  facts.push(l.source === "otodom" ? "Otodom" : "OLX");
  node.querySelector(".facts").textContent = facts.join(" · ");
  node.querySelector(".note").textContent = (l.params && l.params._note) || "";
  node.querySelector(".verdict").textContent = a.verdict;
  li(node.querySelector(".flags"), (a.redFlags || []).map((x) => "🚩 " + x));
  li(node.querySelector(".pros"), a.pros);
  li(node.querySelector(".cons"), a.cons);
  li(node.querySelector(".questions"), a.questions);
  const ta = node.querySelector(".draft");
  ta.value = it.draft;
  ta.addEventListener("change", () => send({ type: "setDraft", key, text: ta.value }).then(() => { it.draft = ta.value; }));
  const res = node.querySelector(".result");
  const show = (text, ok) => { res.textContent = text; res.className = "result " + (ok ? "ok" : "err"); };

  const mode = state.settings.sendMode;
  const rb = node.querySelector(".respond");
  rb.textContent = MODE_LABEL[mode] + (mode === "copy" ? "" : l.source === "otodom" ? " Otodom" : " OLX");
  rb.addEventListener("click", () => busy(rb, async () => {
    if (mode === "copy") { await navigator.clipboard.writeText(ta.value); show("Скопировано.", true); return; }
    await send({ type: "setDraft", key, text: ta.value });
    const r = await send({ type: "respond", key, channel: l.source });
    show(r.message, r.ok);
  }));
  const wa = node.querySelector(".wa");
  if (l.phones && l.phones.length) {
    wa.title = "+" + l.phones[0];
    wa.addEventListener("click", () => busy(wa, async () => {
      await send({ type: "setDraft", key, text: ta.value });
      const r = await send({ type: "respond", key, channel: "whatsapp" });
      show(r.message, r.ok);
    }));
  } else wa.remove();
  const cp = node.querySelector(".copy");
  if (mode === "copy") cp.remove();
  else cp.addEventListener("click", async () => { await navigator.clipboard.writeText(ta.value); show("Скопировано.", true); });

  node.querySelectorAll("[data-instr]").forEach((b) => b.addEventListener("click", () => busy(b, async () => {
    show("Переписываю…", true);
    try {
      const d = await send({ type: "redraft", key, instruction: INSTR[b.dataset.instr] });
      ta.value = d; it.draft = d; show("Готово.", true);
    } catch (e) { show(e.message, false); }
  })));
  node.querySelector(".done").addEventListener("click", async () => {
    await send({ type: "setItemStatus", key, status: "contacted" }); it.status = "contacted"; renderItems();
  });
  node.querySelector(".hide").addEventListener("click", async () => {
    await send({ type: "setItemStatus", key, status: "hidden" }); it.status = "hidden"; renderItems();
  });
  return node;
}

$("itemFilter").addEventListener("change", renderItems);
$("analyzeTab").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await send({ type: "analyzeTab" });
  if (r.error) renderStatus({ last: r.error });
  else renderStatus({ last: r.queued ? `Разбираю ${r.queued} объявл.…` : "Новых объявлений на странице нет (уже разобраны).", queue: r.queued });
}));

// ---------------- поиск ----------------
const C_FIELDS = ["maxPrice", "minPrice", "minArea", "maxArea"];
function fillCriteria(c) {
  C_FIELDS.forEach((k) => { $("c_" + k).value = c[k] || ""; });
  $("c_rooms").value = (c.rooms || []).join(", ");
  $("c_districts").value = (c.districts || []).join(", ");
  $("c_excludeKeywords").value = (c.excludeKeywords || []).join(", ");
  $("c_wishes").value = c.wishes || "";
}
const splitList = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
function readCriteria() {
  const c = { summary: state.settings.criteria.summary || "" };
  C_FIELDS.forEach((k) => { c[k] = parseInt($("c_" + k).value, 10) || 0; });
  c.rooms = splitList($("c_rooms").value).map((x) => parseInt(x, 10)).filter((x) => x > 0);
  c.districts = splitList($("c_districts").value);
  c.excludeKeywords = splitList($("c_excludeKeywords").value);
  c.wishes = $("c_wishes").value.trim();
  return c;
}
$("critParse").addEventListener("click", (e) => busy(e.target, async () => {
  if (!$("critText").value.trim()) return;
  flash($("critMsg"), "Разбираю…");
  try {
    const c = await send({ type: "parseCriteria", text: $("critText").value });
    fillCriteria(c);
    state.settings.criteria.summary = c.summary;
    flash($("critMsg"), "Понял так: " + c.summary + " Проверьте поля и нажмите «Сохранить».", true);
  } catch (err) { flash($("critMsg"), err.message, false); }
}));
$("critSave").addEventListener("click", async () => {
  state.settings = await send({ type: "saveSettings", patch: { criteria: readCriteria() } });
  flash($("critMsg"), "Сохранено.", true);
  renderItems();
});

function renderMonitor() {
  const ul = $("monitorList");
  ul.textContent = "";
  for (const m of state.settings.monitor.urls) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = m.url; a.target = "_blank"; a.textContent = m.title || m.url; a.title = m.url;
    const b = document.createElement("button");
    b.textContent = "✕";
    b.title = "Не следить";
    b.addEventListener("click", async () => { await send({ type: "unmonitor", url: m.url }); await load(); });
    li.append(a, b);
    ul.appendChild(li);
  }
  if (!state.settings.monitor.urls.length) {
    const li = document.createElement("li"); li.textContent = "Пока ни за чем не слежу."; ul.appendChild(li);
  }
  $("m_enabled").checked = state.settings.monitor.enabled;
  $("m_interval").value = state.settings.monitor.intervalMin;
}
$("monitorTab").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await send({ type: "monitorTab" });
  renderStatus({ last: r.error || (r.seeded !== undefined ? `Слежу. Запомнил ${r.seeded} текущих объявлений, пришлю новые.` : "Слежу за этой вкладкой.") });
  await load();
}));
$("m_enabled").addEventListener("change", async () => {
  state.settings = await send({ type: "saveSettings", patch: { monitor: { enabled: $("m_enabled").checked } } });
});
$("m_interval").addEventListener("change", async () => {
  const v = Math.max(3, Math.min(120, parseInt($("m_interval").value, 10) || 7));
  state.settings = await send({ type: "saveSettings", patch: { monitor: { intervalMin: v } } });
  $("m_interval").value = v;
});

// ---------------- о себе ----------------
const P_FIELDS = ["name", "about", "move_in", "lease", "occupants", "pets", "extra", "email", "phone"];
const fillProfile = (p) => P_FIELDS.forEach((k) => { $("p_" + k).value = p[k] || ""; });
$("meParse").addEventListener("click", (e) => busy(e.target, async () => {
  if (!$("meText").value.trim()) return;
  flash($("meMsg"), "Разбираю…");
  try { fillProfile(await send({ type: "parseProfile", text: $("meText").value })); flash($("meMsg"), "Проверьте поля и нажмите «Сохранить».", true); }
  catch (err) { flash($("meMsg"), err.message, false); }
}));
$("meSave").addEventListener("click", async () => {
  const p = Object.fromEntries(P_FIELDS.map((k) => [k, $("p_" + k).value.trim()]));
  state.settings = await send({ type: "saveSettings", patch: { profile: p } });
  flash($("meMsg"), "Сохранено.", true);
  renderItems();
});

// ---------------- настройки ----------------
function fillSettings() {
  const s = state.settings;
  fillCriteria(s.criteria);
  fillProfile(s.profile);
  renderMonitor();
  $("s_provider").value = s.ai.provider;
  $("s_geminiKey").value = s.ai.geminiKey;
  $("s_claudeKey").value = s.ai.claudeKey;
  $("s_geminiModel").value = s.ai.geminiModel;
  $("s_claudeModel").value = s.ai.claudeModel;
  document.querySelectorAll("input[name=sendMode]").forEach((r) => { r.checked = r.value === s.sendMode; });
  $("s_minScore").value = s.minScore;
  $("s_notify").checked = s.notify;
  toggleProvider();
}
function toggleProvider() {
  const p = $("s_provider").value;
  $("geminiBox").style.display = p === "gemini" ? "" : "none";
  $("claudeBox").style.display = p === "claude" ? "" : "none";
}
$("s_provider").addEventListener("change", toggleProvider);
function readAi() {
  return {
    provider: $("s_provider").value, geminiKey: $("s_geminiKey").value.trim(), claudeKey: $("s_claudeKey").value.trim(),
    geminiModel: $("s_geminiModel").value.trim() || "gemini-3.8-flash", claudeModel: $("s_claudeModel").value.trim() || "claude-sonnet-5-5",
  };
}
$("aiTest").addEventListener("click", (e) => busy(e.target, async () => {
  flash($("aiMsg"), "Проверяю…");
  const r = await send({ type: "testAi", ai: readAi() });
  flash($("aiMsg"), r.ok ? "Работает ✓" + (r.model ? " (" + r.model + ")" : "") : "Ошибка: " + (r.error || "нет ответа"), r.ok);
}));
$("settingsSave").addEventListener("click", async () => {
  const mode = (document.querySelector("input[name=sendMode]:checked") || {}).value || "insert";
  state.settings = await send({ type: "saveSettings", patch: {
    ai: readAi(), sendMode: mode,
    minScore: Math.max(0, Math.min(10, parseInt($("s_minScore").value, 10) || 0)), notify: $("s_notify").checked,
  } });
  flash($("settingsMsg"), "Сохранено.", true);
  renderItems();
});
$("clearAll").addEventListener("click", async () => {
  await send({ type: "clearAll" });
  await load();
});

load();
