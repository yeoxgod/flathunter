/* Тесты расширения в node + jsdom: парсеры, фильтры, ИИ (с подменой fetch), фон (с подменой browser), панель.
   Запуск: NODE_PATH=<папка с jsdom> node test/run.js */
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");
let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

function loadLibs(ctx) {
  for (const f of ["lib/parse.js", "lib/filters.js", "lib/ai.js"]) vm.runInContext(read(f), ctx, { filename: f });
}
function makeCtx(extra) {
  const dom = new JSDOM("<!doctype html><body></body>");
  const ctx = vm.createContext(Object.assign({
    console, URL, setTimeout, clearTimeout, Promise, JSON, Date, Math, Set, Map, Array, Object, String, Number, RegExp, Error,
    DOMParser: dom.window.DOMParser, document: dom.window.document, encodeURIComponent,
  }, extra || {}));
  loadLibs(ctx);
  return ctx;
}
const doc = (html) => new JSDOM(html).window.document;

// ---------- фикстуры ----------
const OTODOM_SEARCH = `<html><body><script id="__NEXT_DATA__" type="application/json">${JSON.stringify({ props: { pageProps: { data: { searchAds: { items: [
  { id: 111, title: "Przytulne 2 pokoje Mokotów", slug: "przytulne-2-pokoje-ID4abcd", totalPrice: { value: 4200 }, rentPrice: { value: 600 },
    areaInSquareMeters: 45.5, roomsNumber: "TWO", locationLabel: { value: "Mokotów, Warszawa" }, images: [{ medium: "https://img/1.jpg" }],
    dateCreatedFirst: "2026-10-27 10:00:00" },
  { id: 222, title: "Kawalerka", slug: "kawalerka-ID4efgh", totalPrice: { value: 2900 }, areaInSquareMeters: 28, roomsNumber: "ONE",
    locationLabel: { value: "Wola" }, images: [] },
] } } } } })}</script></body></html>`;
const OTODOM_DETAIL = `<script id="__NEXT_DATA__" type="application/json">${JSON.stringify({ props: { pageProps: { ad: {
  title: "Przytulne 2 pokoje", description: "<p>Wynajmę mieszkanie.</p><p>Tel. 600 123 456</p>",
  target: { Price: 4200, Rent: 650, Area: 45.5, Rooms_num: ["2"], Deposit: 4200 },
  characteristics: [{ key: "floor", label: "Piętro", value: "3" }],
  location: { address: { street: { name: "ul. Puławska" }, city: { name: "Warszawa" } } },
} } } })}</script>`;
const OLX_SEARCH = `<div data-cy="l-card"><a href="/d/oferta/mieszkanie-2-pokoje-CID3-IDabc12.html"><h4>Mieszkanie 2 pokoje</h4></a>
<p data-testid="ad-price">3 800 zł</p><p data-testid="location-date">Warszawa, Ursynów - Dzisiaj o 10:15</p><span>48 m²</span><img src="https://img/x.jpg"></div>
<div data-cy="l-card"><a href="https://www.otodom.pl/pl/oferta/jakies-ID4zzz">Z Otodom</a></div>`;
const OLX_DETAIL = `<h4 data-cy="ad_title">Mieszkanie 2 pokoje</h4><div data-cy="ad_description">Ładne mieszkanie. Kontakt: 501-222-333</div>
<div data-testid="ad-parameters-container"><p>Czynsz (dodatkowo): 500 zł</p><p>Powierzchnia: 48 m²</p><p>Liczba pokoi: 2 pokoje</p></div>`;

// ---------- парсеры ----------
test("otodom search/detail", () => {
  const ctx = makeCtx();
  const FH = ctx.FH;
  const r = FH.parseSearch(doc(OTODOM_SEARCH), "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/warszawa?by=LATEST");
  assert.strictEqual(r.length, 2);
  assert.deepStrictEqual([r[0].extId, r[0].price, r[0].adminRent, r[0].area, r[0].rooms], ["111", 4200, 600, 45.5, 2]);
  assert.strictEqual(r[0].url, "https://www.otodom.pl/pl/oferta/przytulne-2-pokoje-ID4abcd");
  assert.ok(r[0].posted.startsWith("2026-10-27"));
  const l = FH.parseDetail(doc(OTODOM_DETAIL), r[0]);
  assert.ok(l.description.includes("Wynajmę"));
  assert.deepStrictEqual([l.adminRent, l.deposit, l.phones[0]], [650, 4200, "48600123456"]);
  assert.ok(l.location.includes("Puławska"));
});

test("olx search/detail", () => {
  const FH = makeCtx().FH;
  const r = FH.parseSearch(doc(OLX_SEARCH), "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/");
  assert.strictEqual(r.length, 1);
  assert.deepStrictEqual([r[0].extId, r[0].price, r[0].area, r[0].location], ["abc12", 3800, 48, "Warszawa, Ursynów"]);
  assert.ok(r[0].posted);
  const l = FH.parseDetail(doc(OLX_DETAIL), r[0]);
  assert.deepStrictEqual([l.adminRent, l.rooms, l.phones[0]], [500, 2, "48501222333"]);
});

test("page kinds, urls, phones, dates", () => {
  const FH = makeCtx().FH;
  assert.strictEqual(FH.pageKind("https://www.otodom.pl/pl/wyniki/wynajem/x"), "otodom_search");
  assert.strictEqual(FH.pageKind("https://www.otodom.pl/pl/oferta/x-ID4a"), "otodom_listing");
  assert.strictEqual(FH.pageKind("https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/"), "olx_search");
  assert.strictEqual(FH.pageKind("https://www.olx.pl/d/oferta/x-CID3-IDq.html"), "olx_listing");
  assert.strictEqual(FH.listingFromUrl("https://www.olx.pl/d/oferta/x-CID3-IDq1.html?a=1").key, "olx:q1");
  assert.deepStrictEqual(Array.from(FH.findPhones("+48 600 123 456 / 501-222-333")), ["48600123456", "48501222333"]);
  const now = new Date(2026, 9, 28, 15, 0);
  assert.strictEqual(FH.parsePosted("Wczoraj o 22:01", now).getDate(), 27);
  assert.strictEqual(FH.parsePosted("Odświeżono dnia 25 października 2026", now).getMonth(), 9);
});

test("prefilter", () => {
  const FH = makeCtx().FH;
  const l = Object.assign(FH.newListing("olx", "1", "u"), { title: "Mieszkanie", price: 4200, adminRent: 600, area: 45, rooms: 2, location: "Mokotów" });
  assert.ok(FH.prefilter(l, { maxPrice: 5000 })[0]);
  assert.ok(!FH.prefilter(l, { maxPrice: 4500 })[0]);
  assert.ok(!FH.prefilter(l, { rooms: [3] })[0]);
  assert.ok(!FH.prefilter(l, { districts: ["Wola"] })[0]);
  assert.ok(!FH.prefilter(l, { excludeKeywords: ["MIESZK"] })[0]);
  assert.ok(FH.prefilter(FH.newListing("olx", "2", "u"), { maxPrice: 1, rooms: [9] })[0]);
});

// ---------- ИИ ----------
function fakeFetch(handler) {
  const calls = [];
  const f = async (url, opts) => {
    calls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const [status, body] = handler(url, opts, calls.length);
    return { ok: status >= 200 && status < 300, status, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
  };
  f.calls = calls;
  return f;
}
const REPORT = { fit_score: 8, verdict: "ok", pros: ["p"], cons: [], red_flags: [], questions: ["kaucja?"], message_pl: "Dzień dobry, test" };
const SETTINGS = { criteria: { maxPrice: 5000, wishes: "metro" }, profile: { name: "Ivan", email: "i@x.pl", pets: "" },
  ai: { provider: "gemini", geminiKey: "K", geminiModel: "gemini-3.8-flash", claudeKey: "C", claudeModel: "claude-sonnet-5-5" } };

test("gemini: 404 -> next model, schema, privacy", async () => {
  const fetch = fakeFetch((url) => url.includes("gemini-3.8-flash") ? [404, "nope"]
    : [200, { candidates: [{ content: { parts: [{ text: "x", thought: true }, { text: "```json\n" + JSON.stringify(REPORT) + "\n```" }] } }] }]);
  const ctx = makeCtx({ fetch });
  const s = JSON.parse(JSON.stringify(SETTINGS));
  const l = Object.assign(ctx.FH.newListing("olx", "1", "https://olx.pl/x"), { title: "T", phones: ["48600"] });
  const a = await ctx.FH.ai.analyze(l, s);
  assert.deepStrictEqual([a.fitScore, a.message, a.questions[0]], [8, "Dzień dobry, test", "kaucja?"]);
  assert.strictEqual(s.ai._workingModel, "gemini-flash-latest");
  const last = fetch.calls[fetch.calls.length - 1];
  assert.strictEqual(last.opts.headers["x-goog-api-key"], "K");
  const prompt = last.body.contents[0].parts[0].text;
  assert.ok(prompt.includes("Ivan") && !prompt.includes("i@x.pl") && !prompt.includes("48600") && !prompt.includes('"pets"'));
});

test("claude: tool use + browser header", async () => {
  const fetch = fakeFetch(() => [200, { content: [{ type: "tool_use", input: REPORT }] }]);
  const ctx = makeCtx({ fetch });
  const s = JSON.parse(JSON.stringify(SETTINGS)); s.ai.provider = "claude";
  const a = await ctx.FH.ai.analyze(ctx.FH.newListing("olx", "1", "u"), s);
  assert.strictEqual(a.fitScore, 8);
  const c = fetch.calls[0];
  assert.strictEqual(c.opts.headers["anthropic-dangerous-direct-browser-access"], "true");
  assert.strictEqual(c.body.tool_choice.name, "report");
});

test("criteria/profile parsing + missing key", async () => {
  const fetch = fakeFetch(() => [200, { candidates: [{ content: { parts: [{ text: JSON.stringify({
    max_price: "5500", min_price: 0, min_area: 35, max_area: 0, rooms: [3, "2", 0], districts: "Mokotów", exclude_keywords: null,
    wishes: " мебель ", summary_ru: "2-3 комнаты" }) }] } }] }]);
  const ctx = makeCtx({ fetch });
  const c = await ctx.FH.ai.parseCriteria("2-3 комнаты", {}, SETTINGS);
  assert.deepStrictEqual([c.maxPrice, c.minArea, Array.from(c.rooms), Array.from(c.districts), c.wishes], [5500, 35, [2, 3], ["Mokotów"], "мебель"]);
  const s = JSON.parse(JSON.stringify(SETTINGS)); s.ai.geminiKey = "";
  await assert.rejects(ctx.FH.ai.analyze(ctx.FH.newListing("olx", "1", "u"), s), /Нет ключа Gemini/);
});

// ---------- фон ----------
function fakeBrowser() {
  const storage = {};
  const sent = [], notes = [], created = [];
  let listener = null, alarmListener = null;
  const b = {
    _storage: storage, _notes: notes, _created: created, _sent: sent,
    storage: { local: {
      get: async (k) => (k in storage ? { [k]: JSON.parse(JSON.stringify(storage[k])) } : {}),
      set: async (o) => { for (const [k, v] of Object.entries(o)) storage[k] = JSON.parse(JSON.stringify(v)); },
    } },
    runtime: { sendMessage: async (m) => { sent.push(m); }, onMessage: { addListener: (f) => { listener = f; } }, getURL: (p) => "moz-extension://x/" + p },
    alarms: { clear: async () => true, create: () => {}, onAlarm: { addListener: (f) => { alarmListener = f; } } },
    notifications: { create: (id, o) => notes.push([id, o]), onClicked: { addListener: () => {} } },
    tabs: {
      query: async () => [], get: async (id) => ({ id, status: "complete" }), reload: async () => {},
      create: async (o) => { created.push(o); return { id: 9 }; }, update: async () => {},
      sendMessage: async () => ({ ok: true, sent: true, message: "Отправлено в чат OLX." }),
    },
    browserAction: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {}, onClicked: { addListener: () => {} } },
    sidebarAction: { open: () => {} },
    call: (msg) => listener(msg, {}),
  };
  return b;
}

test("background: seed, new listings, filters, lowscore, respond", async () => {
  const browser = fakeBrowser();
  let score = 8;
  const fetch = fakeFetch((url) => {
    if (url.includes("generativelanguage")) return [200, { candidates: [{ content: { parts: [{ text: JSON.stringify(Object.assign({}, REPORT, { fit_score: score })) }] } }] }];
    if (url.includes("olx.pl")) return [403, "blocked"];
    return [200, OTODOM_DETAIL];
  });
  const ctx = makeCtx({ fetch, browser });
  vm.runInContext(read("background.js").replace("const sleep = (ms) => new Promise((r) => setTimeout(r, ms));", "const sleep = () => Promise.resolve();"), ctx, { filename: "background.js" });
  const B = browser;
  await B.call({ type: "saveSettings", patch: { ai: { geminiKey: "K" }, criteria: { maxPrice: 5000 },
    monitor: { urls: [{ url: "https://www.otodom.pl/pl/wyniki/wynajem/x" }] } } });
  const FH = ctx.FH;
  const url = "https://www.otodom.pl/pl/wyniki/wynajem/x";
  const first = FH.parseSearch(doc(OTODOM_SEARCH), url);
  let r = await B.call({ type: "searchPage", url, listings: first });
  assert.strictEqual(r.seeded, 2);                                   // первый раз только запоминаем
  r = await B.call({ type: "searchPage", url, listings: first });
  assert.strictEqual(r.queued, 0);                                   // повтор ничего не шлёт
  const mk = (id, price, title) => Object.assign(FH.newListing("otodom", id, "https://www.otodom.pl/pl/oferta/x-ID" + id), { price, title });
  r = await B.call({ type: "searchPage", url, listings: first.concat([mk("n1", 4000, "Nowe"), mk("n2", 9000, "Drogie")]) });
  assert.strictEqual(r.queued, 2);
  await ctx.chain;
  await new Promise((res) => setTimeout(res, 50));
  let st = await B.call({ type: "state" });
  assert.deepStrictEqual(Object.keys(st.items), ["otodom:n1"]);       // дорогое отсеяно
  assert.strictEqual(st.items["otodom:n1"].listing.description.includes("Wynajmę"), true);
  assert.strictEqual(B._notes.length, 1);
  // низкая оценка не попадает в список; страница OLX 403 -> пометка
  score = 2;
  await B.call({ type: "searchPage", url, listings: [Object.assign(FH.newListing("olx", "o1", "https://www.olx.pl/d/oferta/a-IDo1.html"), { price: 3000, title: "x" })] });
  await new Promise((res) => setTimeout(res, 50));
  st = await B.call({ type: "state" });
  assert.ok(!st.items["olx:o1"]);
  // отклик
  const res = await B.call({ type: "respond", key: "otodom:n1", channel: "otodom" });
  assert.ok(res.ok && res.sent);
  st = await B.call({ type: "state" });
  assert.strictEqual(st.items["otodom:n1"].status, "contacted");
  assert.strictEqual(B._created[0].url, "https://www.otodom.pl/pl/oferta/x-IDn1");
});

// ---------- панель ----------
test("panel renders cards and settings without errors", async () => {
  const html = read("sidebar/panel.html").replace('<script src="panel.js"></script>', "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "moz-extension://x/sidebar/panel.html" });
  const w = dom.window;
  const state = {
    settings: { criteria: { maxPrice: 5000, rooms: [2], districts: [], excludeKeywords: [], wishes: "", summary: "s" },
      profile: { name: "Ivan" }, ai: { provider: "gemini", geminiKey: "K", claudeKey: "", geminiModel: "g", claudeModel: "c" },
      sendMode: "insert", minScore: 5, monitor: { enabled: true, intervalMin: 7, urls: [{ url: "https://www.olx.pl/nieruchomosci/x", title: "OLX" }] }, notify: true },
    items: { "olx:1": { listing: { key: "olx:1", source: "olx", url: "https://www.olx.pl/d/oferta/x-ID1.html", title: "A <b>", price: 3800, adminRent: 500,
      area: 48, rooms: 2, location: "Ursynów", params: {}, phones: ["48600123456"] },
      analysis: { fitScore: 8, verdict: "Хорошо", pros: ["p"], cons: ["c"], redFlags: ["r"], questions: ["q"], message: "Dzień dobry" },
      draft: "Dzień dobry", status: "new", ts: 1 } },
    status: { last: "", queue: 0 },
  };
  const msgs = [];
  w.browser = { runtime: { sendMessage: async (m) => { msgs.push(m); return m.type === "state" ? state : {}; }, onMessage: { addListener: () => {} } } };
  const errors = [];
  w.addEventListener("error", (e) => errors.push(e.error || e.message));
  w.eval(read("sidebar/panel.js"));
  await new Promise((r) => setTimeout(r, 50));
  assert.deepStrictEqual(errors, []);
  const card = w.document.querySelector("#items .card");
  assert.ok(card, "карточка отрисована");
  assert.strictEqual(card.querySelector(".title").textContent, "A <b>");      // без HTML-инъекций
  assert.strictEqual(card.querySelector(".respond").textContent, "Вставить в чат OLX");
  assert.ok(card.querySelector(".wa"));
  assert.strictEqual(w.document.getElementById("c_maxPrice").value, "5000");
  assert.strictEqual(w.document.getElementById("count").textContent, "1");
  assert.strictEqual(w.document.querySelectorAll("#monitorList li").length, 1);
});

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log("ok  -", name); }
    catch (e) { console.log("FAIL-", name, "\n   ", e && e.stack ? e.stack.split("\n").slice(0, 4).join("\n    ") : e); process.exitCode = 1; }
  }
  console.log(`\n${passed}/${tests.length} passed`);
})();
