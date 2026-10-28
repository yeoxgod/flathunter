/* Разбор страниц Otodom и OLX из DOM (страница уже открыта в вашем браузере). Общий для фона и страниц. */
var FH = (typeof FH !== "undefined") ? FH : {};

(function (FH) {
  "use strict";

  FH.parseNumber = function (v) {
    if (v === null || v === undefined || typeof v === "boolean") return null;
    if (typeof v === "number") return v;
    const m = String(v).match(/\d[\d\s ]*(?:[.,]\d+)?/);
    if (!m) return null;
    const n = parseFloat(m[0].replace(/[\s ]/g, "").replace(",", "."));
    return isNaN(n) ? null : n;
  };
  FH.parseInt = function (v) {
    const n = FH.parseNumber(v);
    return n === null ? null : Math.round(n);
  };

  FH.findPhones = function (text, limit) {
    limit = limit || 3;
    const re = /(?<!\d)(?:(?:\+|00)?48[\s-]?)?([4-9]\d{2})[\s-]?(\d{3})[\s-]?(\d{3})(?!\d)/g;
    const out = [];
    let m;
    while ((m = re.exec(text || "")) && out.length < limit) {
      const d = "48" + m[1] + m[2] + m[3];
      if (!out.includes(d)) out.push(d);
    }
    return out;
  };

  const PL_MONTHS = {
    stycznia: 1, lutego: 2, marca: 3, kwietnia: 4, maja: 5, czerwca: 6, lipca: 7, sierpnia: 8,
    "września": 9, "października": 10, listopada: 11, grudnia: 12,
  };
  FH.parsePosted = function (text, now) {
    if (!text) return null;
    now = now || new Date();
    const t = String(text).trim();
    if (/^\d{4}-\d{2}-\d{2}/.test(t)) {
      const d = new Date(t.replace(" ", "T"));
      return isNaN(d) ? null : d;
    }
    const low = t.toLowerCase();
    const tm = low.match(/(\d{1,2}):(\d{2})/);
    const hh = tm ? +tm[1] : 12, mm = tm ? +tm[2] : 0;
    if (low.includes("dzisiaj") || low.includes("dziś")) {
      const d = new Date(now); d.setHours(hh, mm, 0, 0); return d;
    }
    if (low.includes("wczoraj")) {
      const d = new Date(now); d.setDate(d.getDate() - 1); d.setHours(hh, mm, 0, 0); return d;
    }
    const m = low.match(/(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})/);
    if (m && PL_MONTHS[m[2]]) return new Date(+m[3], PL_MONTHS[m[2]] - 1, +m[1], 12, 0);
    return null;
  };

  function newListing(source, extId, url) {
    return {
      source, extId, url, key: source + ":" + extId, title: "", price: null, adminRent: null, deposit: null,
      area: null, rooms: null, location: "", description: "", params: {}, photo: "", phones: [], posted: "",
    };
  }
  FH.newListing = newListing;

  // ---------------- Otodom ----------------
  const ROOMS = { ONE: 1, TWO: 2, THREE: 3, FOUR: 4, FIVE: 5, SIX: 6, SEVEN: 7, EIGHT: 8, NINE: 9, TEN: 10 };
  const OTODOM_ID = /-ID([A-Za-z0-9]+)(?:[/?#.]|$)/;

  function nextData(doc) {
    const tag = doc.getElementById("__NEXT_DATA__");
    if (!tag) return null;
    try { return JSON.parse(tag.textContent); } catch (e) { return null; }
  }
  function* walk(o) {
    if (Array.isArray(o)) { for (const v of o) yield* walk(v); }
    else if (o && typeof o === "object") { yield o; for (const v of Object.values(o)) yield* walk(v); }
  }
  const money = (v) => FH.parseInt(v && typeof v === "object" ? v.value : v);
  function rooms(v) {
    if (Array.isArray(v)) return v.length ? rooms(v[0]) : null;
    if (typeof v === "string" && ROOMS[v.toUpperCase()]) return ROOMS[v.toUpperCase()];
    return FH.parseInt(v);
  }
  const label = (v) => String((v && typeof v === "object") ? (v.value || v.fullName || "") : (v || ""));

  FH.otodomSearch = function (doc, base) {
    base = base || "https://www.otodom.pl";
    const data = nextData(doc);
    const out = [], seen = new Set();
    if (data) {
      for (const d of walk(data)) {
        if (typeof d.slug !== "string" || d.id === undefined || !("title" in d)) continue;
        const id = String(d.id);
        if (seen.has(id)) continue;
        seen.add(id);
        const l = newListing("otodom", id, base + "/pl/oferta/" + d.slug);
        l.title = String(d.title || "");
        l.price = money(d.totalPrice);
        l.adminRent = money(d.rentPrice);
        l.area = FH.parseNumber(d.areaInSquareMeters);
        l.rooms = rooms(d.roomsNumber);
        l.location = label(d.locationLabel);
        const imgs = d.images;
        if (Array.isArray(imgs) && imgs[0]) l.photo = imgs[0].medium || imgs[0].large || imgs[0].small || "";
        for (const k of ["dateCreatedFirst", "createdAtFirst", "dateCreated", "createdAt", "pushedUpAt"]) {
          if (typeof d[k] === "string" && /^\d{4}/.test(d[k])) { l.posted = d[k]; break; }
        }
        out.push(l);
      }
    }
    if (out.length) return out;
    for (const a of doc.querySelectorAll('a[href*="/pl/oferta/"]')) {
      const url = new URL(a.getAttribute("href"), base).href.split("#")[0].split("?")[0];
      const m = url.match(OTODOM_ID);
      const id = m ? m[1] : url.replace(/\/$/, "").split("/").pop();
      if (seen.has(id)) continue;
      seen.add(id);
      const l = newListing("otodom", id, url);
      l.title = a.textContent.trim().slice(0, 150);
      out.push(l);
    }
    return out;
  };

  function htmlToText(html) {
    const marked = String(html || "").replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|h\d)>/gi, "\n");
    const text = new DOMParser().parseFromString(marked, "text/html").body.textContent || "";
    return text.replace(/\n\s*\n+/g, "\n").trim();
  }

  FH.otodomDetail = function (doc, l) {
    const data = nextData(doc);
    let ad = data && data.props && data.props.pageProps && data.props.pageProps.ad;
    if (!ad && data) for (const d of walk(data)) { if ("description" in d && d.target) { ad = d; break; } }
    if (ad) {
      l.title = String(ad.title || l.title);
      l.description = htmlToText(String(ad.description || ""));
      const t = ad.target || {};
      l.price = money(t.Price) || l.price;
      l.adminRent = money(t.Rent) || l.adminRent;
      l.deposit = money(t.Deposit) || l.deposit;
      l.area = FH.parseNumber(t.Area) || l.area;
      l.rooms = rooms(t.Rooms_num) || l.rooms;
      for (const c of ad.characteristics || []) {
        const lab = c.label || c.key, val = c.localizedValue || c.value;
        if (lab && val !== undefined && val !== null) l.params[String(lab)] = val;
      }
      try {
        const a = (ad.location || {}).address || {};
        const parts = ["street", "district", "city"].map((k) => (a[k] && typeof a[k] === "object") ? a[k].name : a[k]).filter(Boolean);
        if (parts.length) l.location = parts.join(", ");
      } catch (e) { /* ignore */ }
    } else {
      const h1 = doc.querySelector("h1");
      if (h1) l.title = h1.textContent.trim() || l.title;
      const d = doc.querySelector('[data-cy="adPageAdDescription"]');
      if (d) l.description = (d.innerText || d.textContent).trim();
    }
    l.phones = FH.findPhones(l.description);
    return l;
  };

  // ---------------- OLX ----------------
  const OLX_ID = /-ID([A-Za-z0-9]+)\.html/;
  FH.olxExtId = function (href) {
    const m = href.match(OLX_ID);
    return m ? m[1] : href.split("?")[0].replace(/\/$/, "").split("/").pop();
  };
  // текст с пробелами между узлами: «10:15» и «48 m²» в соседних элементах не должны слипаться
  function textOf(el) {
    const w = el.ownerDocument.createTreeWalker(el, 4);
    const parts = [];
    while (w.nextNode()) parts.push(w.currentNode.nodeValue);
    return parts.join(" ");
  }

  FH.olxSearch = function (doc, base) {
    base = base || "https://www.olx.pl";
    let cards = Array.from(doc.querySelectorAll('[data-cy="l-card"]')).map((c) => [c, c.querySelector("a[href]")]);
    if (!cards.length) cards = Array.from(doc.querySelectorAll('a[href*="/d/oferta/"]')).map((a) => [a.parentElement, a]);
    const out = [], seen = new Set();
    for (const [card, a] of cards) {
      if (!card || !a) continue;
      const href = new URL(a.getAttribute("href"), base).href.split("#")[0];
      if (new URL(href).hostname.endsWith("otodom.pl")) continue;
      const id = FH.olxExtId(href);
      if (seen.has(id)) continue;
      seen.add(id);
      const l = newListing("olx", id, href.split("?")[0]);
      const t = card.querySelector("h4, h6, h3, h2") || a;
      l.title = t.textContent.trim();
      const p = card.querySelector('[data-testid="ad-price"]');
      if (p) l.price = FH.parseInt(p.textContent);
      const loc = card.querySelector('[data-testid="location-date"]');
      if (loc) {
        const txt = loc.textContent.trim();
        l.location = txt.split(" - ")[0];
        const d = txt.includes(" - ") ? FH.parsePosted(txt.split(" - ").slice(1).join(" - ")) : null;
        if (d) l.posted = d.toISOString();
      }
      const am = textOf(card).match(/(?<![\d:.,])(\d+(?:[.,]\d+)?)\s*m[²2]/);
      if (am) l.area = FH.parseNumber(am[1]);
      const img = card.querySelector("img");
      if (img) l.photo = img.getAttribute("src") || "";
      out.push(l);
    }
    return out;
  };

  FH.olxDetail = function (doc, l) {
    const t = doc.querySelector('[data-cy="ad_title"]') || doc.querySelector("h1") || doc.querySelector("h4");
    if (t && t.textContent.trim()) l.title = t.textContent.trim();
    const d = doc.querySelector('[data-cy="ad_description"]');
    if (d) l.description = (d.innerText || d.textContent).trim();
    const p = doc.querySelector('[data-testid="ad-price-container"]');
    if (p) { const v = FH.parseInt(p.textContent); if (v) l.price = v; }
    for (const el of doc.querySelectorAll('[data-testid="ad-parameters-container"] p, [data-testid="ad-parameters-container"] li')) {
      const txt = el.textContent.trim();
      const i = txt.indexOf(":");
      if (i < 0) continue;
      const k = txt.slice(0, i).trim(), v = txt.slice(i + 1).trim(), kl = k.toLowerCase();
      l.params[k] = v;
      if (kl.includes("czynsz")) l.adminRent = FH.parseInt(v) || l.adminRent;
      else if (kl.includes("kaucj")) l.deposit = FH.parseInt(v) || l.deposit;
      else if (kl.includes("powierzchnia")) l.area = FH.parseNumber(v) || l.area;
      else if (kl.includes("liczba pokoi")) l.rooms = /kawalerka|garsoniera/i.test(v) ? 1 : (FH.parseInt(v) || l.rooms);
    }
    const loc = doc.querySelector('[data-testid="map-aside-section"]');
    if (loc) { const txt = loc.textContent.replace(/^\s*Lokalizacja/, "").trim(); if (txt) l.location = txt; }
    l.phones = FH.findPhones(l.description);
    return l;
  };

  // ---------------- общий вход ----------------
  FH.pageKind = function (url) {
    const u = new URL(url);
    const host = u.hostname;
    if (host.endsWith("otodom.pl")) return u.pathname.includes("/oferta/") ? "otodom_listing" : (u.pathname.includes("/wyniki/") ? "otodom_search" : null);
    if (host.endsWith("olx.pl")) return u.pathname.includes("/d/oferta/") ? "olx_listing" : (u.pathname.includes("/nieruchomosci/") ? "olx_search" : null);
    return null;
  };

  FH.listingFromUrl = function (url) {
    const kind = FH.pageKind(url);
    const clean = url.split("#")[0].split("?")[0];
    if (kind === "otodom_listing") { const m = clean.match(OTODOM_ID); return newListing("otodom", m ? m[1] : clean, clean); }
    if (kind === "olx_listing") return newListing("olx", FH.olxExtId(clean), clean);
    return null;
  };

  FH.parseSearch = function (doc, url) {
    const kind = FH.pageKind(url);
    if (kind === "otodom_search") return FH.otodomSearch(doc, new URL(url).origin);
    if (kind === "olx_search") return FH.olxSearch(doc, new URL(url).origin);
    return [];
  };

  FH.parseDetail = function (doc, l) {
    return l.source === "otodom" ? FH.otodomDetail(doc, l) : FH.olxDetail(doc, l);
  };

  if (typeof module !== "undefined") module.exports = FH;
})(FH);
