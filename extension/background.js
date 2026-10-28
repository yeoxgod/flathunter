/* Фон: хранение, очередь разбора, ИИ, мониторинг вкладок поиска, уведомления, отклик. */
"use strict";

const DEFAULTS = {
  criteria: { maxPrice: 0, minPrice: 0, minArea: 0, maxArea: 0, rooms: [], districts: [], excludeKeywords: [], wishes: "", summary: "" },
  profile: { name: "", about: "", move_in: "", lease: "", occupants: "", pets: "", extra: "", email: "", phone: "" },
  ai: { provider: "gemini", geminiKey: "", claudeKey: "", geminiModel: "gemini-3.8-flash", claudeModel: "claude-sonnet-5-5" },
  sendMode: "insert",          // copy | insert | send
  minScore: 5,
  monitor: { enabled: true, intervalMin: 7, urls: [] },
  notify: true,
};

const store = {
  async settings() {
    const { settings } = await browser.storage.local.get("settings");
    return deepMerge(DEFAULTS, settings || {});
  },
  async saveSettings(s) { await browser.storage.local.set({ settings: s }); },
  async items() { return (await browser.storage.local.get("items")).items || {}; },
  async saveItems(items) { await browser.storage.local.set({ items }); },
  async seen() { return new Set((await browser.storage.local.get("seen")).seen || []); },
  async saveSeen(set) { await browser.storage.local.set({ seen: Array.from(set).slice(-8000) }); },
  async seeded() { return new Set((await browser.storage.local.get("seeded")).seeded || []); },
  async saveSeeded(set) { await browser.storage.local.set({ seeded: Array.from(set) }); },
};

function deepMerge(base, over) {
  const out = JSON.parse(JSON.stringify(base));
  for (const [k, v] of Object.entries(over || {})) {
    out[k] = (v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object" && !Array.isArray(out[k]))
      ? deepMerge(out[k], v) : v;
  }
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const status = { busy: 0, last: "", queue: 0 };
function setStatus(text) { status.last = text; broadcast({ type: "status", status }); }
function broadcast(msg) { browser.runtime.sendMessage(msg).catch(() => {}); }

// ---------------- очередь разбора ----------------
let chain = Promise.resolve();
function enqueue(fn) {
  status.queue++;
  const p = chain.then(fn).catch((e) => { console.error(e); setStatus("Ошибка: " + e.message); })
    .finally(() => { status.queue--; broadcast({ type: "status", status }); });
  chain = p;
  return p;
}

async function fetchDetail(l) {
  // если объявление открыто во вкладке, берём данные прямо с неё
  const tabs = await browser.tabs.query({ url: ["*://*.otodom.pl/*", "*://*.olx.pl/*"] });
  const open = tabs.find((t) => t.url && t.url.split("?")[0].split("#")[0] === l.url);
  if (open) {
    try {
      const res = await browser.tabs.sendMessage(open.id, { type: "extract" });
      if (res && res.listing) return Object.assign(l, res.listing, { key: l.key, source: l.source, extId: l.extId });
    } catch (e) { /* вкладка без скрипта */ }
  }
  // иначе обычный запрос из вашего браузера (с вашими cookies), не чаще раза в пару секунд
  await sleep(1500 + Math.random() * 2500);
  const r = await fetch(l.url, { credentials: "include" });
  if (!r.ok) throw Object.assign(new Error("HTTP " + r.status), { status: r.status });
  const doc = new DOMParser().parseFromString(await r.text(), "text/html");
  return FH.parseDetail(doc, l);
}

async function processListing(l, opts) {
  opts = opts || {};
  const s = await store.settings();
  const items = await store.items();
  if (items[l.key] && !opts.force) return "already";
  if (!opts.force) {
    const [ok, why] = FH.prefilter(l, s.criteria);
    if (!ok) { await markSeen(l.key); return "filtered:" + why; }
  }
  setStatus("Открываю: " + (l.title || l.url).slice(0, 60));
  try {
    l = await fetchDetail(l);
  } catch (e) {
    l.params = Object.assign({}, l.params, { _note: `Страница объявления не загрузилась (${e.message}): оценка по данным из списка.` });
  }
  if (!opts.force) {
    const [ok] = FH.prefilter(l, s.criteria);
    if (!ok) { await markSeen(l.key); return "filtered"; }
  }
  setStatus("Оцениваю: " + (l.title || l.url).slice(0, 60));
  const a = await FH.ai.analyze(l, s);
  if (s.ai._workingModel) { s.ai.geminiModel = s.ai._workingModel; delete s.ai._workingModel; await store.saveSettings(s); }
  await markSeen(l.key);
  if (!opts.force && a.fitScore < s.minScore) {
    setStatus(`Низкая оценка ${a.fitScore}/10: ${l.title.slice(0, 50)}`);
    return "lowscore";
  }
  const fresh = await store.items();
  fresh[l.key] = { listing: l, analysis: a, draft: a.message, status: "new", ts: Date.now() };
  await store.saveItems(fresh);
  broadcast({ type: "items" });
  await updateBadge();
  if (s.notify && opts.notify !== false) {
    browser.notifications.create("fh:" + l.key, {
      type: "basic", iconUrl: browser.runtime.getURL("icons/icon.svg"),
      title: `${a.fitScore}/10 · ${l.price ? l.price + " zł" : ""} ${l.area ? l.area + " м²" : ""}`.trim(),
      message: (l.title || "") + "\n" + a.verdict.slice(0, 120),
    });
  }
  setStatus(`Готово: ${a.fitScore}/10 · ${l.title.slice(0, 50)}`);
  return "sent";
}

async function markSeen(key) {
  const seen = await store.seen();
  seen.add(key);
  await store.saveSeen(seen);
}

async function updateBadge() {
  const items = await store.items();
  const n = Object.values(items).filter((x) => x.status === "new").length;
  browser.browserAction.setBadgeText({ text: n ? String(n) : "" });
  browser.browserAction.setBadgeBackgroundColor({ color: "#1f6feb" });
}

// ---------------- страницы поиска ----------------
const normUrl = (u) => { try { const x = new URL(u); x.hash = ""; return x.href; } catch (e) { return u; } };

async function onSearchPage(url, listings, opts) {
  opts = opts || {};
  const s = await store.settings();
  const monitored = s.monitor.urls.some((m) => normUrl(m.url) === normUrl(url));
  if (!monitored && !opts.all) return { monitored: false };
  const seen = await store.seen();
  const items = await store.items();
  if (!opts.all) {
    const seeded = await store.seeded();
    if (!seeded.has(normUrl(url))) {
      // первый раз: запоминаем текущее, дальше приходят только новые
      listings.forEach((l) => seen.add(l.key));
      await store.saveSeen(seen);
      seeded.add(normUrl(url));
      await store.saveSeeded(seeded);
      setStatus(`Слежу за поиском: запомнил ${listings.length} текущих объявлений, дальше пришлю новые.`);
      return { seeded: listings.length };
    }
  }
  const fresh = listings.filter((l) => !items[l.key] && (opts.all || !seen.has(l.key)));
  const limit = opts.all ? 30 : 10;
  fresh.slice(0, limit).forEach((l) => enqueue(() => processListing(l)));
  if (!opts.all) fresh.slice(limit).forEach((l) => seen.add(l.key));
  await store.saveSeen(seen);
  if (fresh.length) setStatus(`Новых объявлений: ${fresh.length}, разбираю…`);
  return { queued: Math.min(fresh.length, limit), skipped: Math.max(0, fresh.length - limit) };
}

// ---------------- мониторинг ----------------
async function scheduleMonitor() {
  const s = await store.settings();
  await browser.alarms.clear("monitor");
  if (s.monitor.enabled && s.monitor.urls.length) {
    browser.alarms.create("monitor", { periodInMinutes: Math.max(3, s.monitor.intervalMin), delayInMinutes: 0.5 });
  }
}

async function runMonitor() {
  const s = await store.settings();
  if (!s.monitor.enabled) return;
  for (const m of s.monitor.urls) {
    const tabs = await browser.tabs.query({});
    let tab = tabs.find((t) => t.url && normUrl(t.url) === normUrl(m.url));
    if (tab) {
      if (tab.active) continue; // вы сейчас смотрите эту вкладку — не перезагружаем у вас под рукой
      await browser.tabs.reload(tab.id);
    } else {
      tab = await browser.tabs.create({ url: m.url, active: false, pinned: true });
    }
    await sleep(3000 + Math.random() * 4000);
  }
}

browser.alarms.onAlarm.addListener((al) => { if (al.name === "monitor") runMonitor().catch(console.error); });

// ---------------- отклик ----------------
async function waitTabComplete(tabId, timeoutMs) {
  const end = Date.now() + (timeoutMs || 30000);
  while (Date.now() < end) {
    const t = await browser.tabs.get(tabId);
    if (t.status === "complete") return t;
    await sleep(500);
  }
  return browser.tabs.get(tabId);
}

async function respond(key, channel) {
  const s = await store.settings();
  const items = await store.items();
  const it = items[key];
  if (!it) throw new Error("объявление не найдено");
  const l = it.listing, text = it.draft, send = s.sendMode === "send";
  let result;
  if (channel === "whatsapp") {
    const phone = (l.phones || [])[0];
    const tab = await browser.tabs.create({ url: `https://web.whatsapp.com/send?phone=${phone}&text=${encodeURIComponent(text)}`, active: true });
    await waitTabComplete(tab.id, 60000);
    await sleep(2000);
    result = await browser.tabs.sendMessage(tab.id, { type: "waFill", text, send }).catch((e) => ({ ok: false, message: "WhatsApp Web не ответил: " + e.message }));
  } else {
    const tabs = await browser.tabs.query({});
    let tab = tabs.find((t) => t.url && t.url.split("?")[0].split("#")[0] === l.url);
    if (tab) await browser.tabs.update(tab.id, { active: true });
    else tab = await browser.tabs.create({ url: l.url, active: true });
    await waitTabComplete(tab.id, 45000);
    await sleep(1500);
    result = await browser.tabs.sendMessage(tab.id, { type: "fill", text, send, profile: s.profile })
      .catch((e) => ({ ok: false, message: "страница не ответила: " + e.message }));
  }
  if (result && result.ok && result.sent) {
    it.status = "contacted";
    it.contactedAt = Date.now();
    await store.saveItems(items);
    await updateBadge();
    broadcast({ type: "items" });
  }
  return result;
}

// ---------------- сообщения ----------------
browser.runtime.onMessage.addListener((msg, sender) => {
  const h = handlers[msg && msg.type];
  return h ? Promise.resolve(h(msg, sender)) : undefined;
});

const handlers = {
  async searchPage(msg) { return onSearchPage(msg.url, msg.listings); },
  async state() {
    return { settings: await store.settings(), items: await store.items(), status };
  },
  async saveSettings(msg) {
    const cur = await store.settings();
    await store.saveSettings(deepMerge(cur, msg.patch));
    await scheduleMonitor();
    return store.settings();
  },
  async analyzeTab(msg) {
    const tab = msg.tabId ? await browser.tabs.get(msg.tabId) : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    const kind = tab && tab.url ? FH.pageKind(tab.url) : null;
    if (!kind) return { error: "Откройте страницу поиска или объявления Otodom/OLX." };
    if (kind.endsWith("_listing")) {
      const l = FH.listingFromUrl(tab.url);
      enqueue(() => processListing(l, { force: true, notify: false }));
      return { queued: 1 };
    }
    const res = await browser.tabs.sendMessage(tab.id, { type: "listSearch" });
    return onSearchPage(tab.url, res.listings || [], { all: true });
  },
  async monitorTab(msg) {
    const tab = (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    const kind = tab && tab.url ? FH.pageKind(tab.url) : null;
    if (!kind || !kind.endsWith("_search")) return { error: "Откройте страницу поиска Otodom или OLX с фильтрами." };
    const s = await store.settings();
    const url = normUrl(tab.url);
    if (!s.monitor.urls.some((m) => normUrl(m.url) === url)) s.monitor.urls.push({ url, title: tab.title || url, added: Date.now() });
    s.monitor.enabled = true;
    await store.saveSettings(s);
    await scheduleMonitor();
    const res = await browser.tabs.sendMessage(tab.id, { type: "listSearch" }).catch(() => ({ listings: [] }));
    return onSearchPage(url, res.listings || []);
  },
  async unmonitor(msg) {
    const s = await store.settings();
    s.monitor.urls = s.monitor.urls.filter((m) => normUrl(m.url) !== normUrl(msg.url));
    await store.saveSettings(s);
    await scheduleMonitor();
    return true;
  },
  async redraft(msg) {
    const s = await store.settings();
    const items = await store.items();
    const it = items[msg.key];
    it.draft = await FH.ai.redraft(it.listing, it.draft, msg.instruction, s);
    await store.saveItems(items);
    return it.draft;
  },
  async setDraft(msg) {
    const items = await store.items();
    items[msg.key].draft = msg.text;
    await store.saveItems(items);
    return true;
  },
  async setItemStatus(msg) {
    const items = await store.items();
    if (msg.status === "delete") delete items[msg.key];
    else items[msg.key].status = msg.status;
    await store.saveItems(items);
    await updateBadge();
    return true;
  },
  async respond(msg) { return respond(msg.key, msg.channel); },
  async parseCriteria(msg) {
    const s = await store.settings();
    return FH.ai.parseCriteria(msg.text, s.criteria, s);
  },
  async parseProfile(msg) {
    const s = await store.settings();
    return FH.ai.parseProfile(msg.text, s.profile, s);
  },
  async testAi(msg) {
    const s = await store.settings();
    const cfg = deepMerge(s.ai, msg.ai || {});
    try { return { ok: await FH.ai.ping(cfg), model: cfg._workingModel || "" }; } catch (e) { return { ok: false, error: e.message }; }
  },
  async clearAll() {
    await browser.storage.local.set({ items: {}, seen: [], seeded: [] });
    await updateBadge();
    return true;
  },
};

browser.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith("fh:")) return;
  const items = await store.items();
  const it = items[id.slice(3)];
  if (it) browser.tabs.create({ url: it.listing.url, active: true });
});

browser.browserAction.onClicked.addListener(() => { browser.sidebarAction.open(); });

scheduleMonitor();
updateBadge();
