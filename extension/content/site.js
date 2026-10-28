/* Скрипт на страницах Otodom/OLX: отдаёт объявления со страницы и заполняет чат/форму по команде. */
(function () {
  "use strict";
  if (window.__fhLoaded) return;
  window.__fhLoaded = true;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const kind = () => FH.pageKind(location.href);

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
  }
  function first(selectors) {
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) if (visible(el)) return el;
    }
    return null;
  }
  function byText(tags, re) {
    for (const el of document.querySelectorAll(tags)) if (visible(el) && re.test(el.textContent.trim())) return el;
    return null;
  }
  async function waitFor(fn, ms) {
    const end = Date.now() + (ms || 15000);
    while (Date.now() < end) { const v = fn(); if (v) return v; await sleep(300); }
    return null;
  }
  function captcha() {
    if ([...document.querySelectorAll("iframe")].some((f) => /captcha|recaptcha|hcaptcha|challenges\.cloudflare|turnstile/i.test(f.src || ""))) return true;
    return /verify you are human|nie jesteś robotem|potwierdź, że jesteś/i.test((document.body.innerText || "").slice(0, 5000));
  }
  // React/Vue не видят присваивание value: используем «родной» сеттер и событие input
  function setValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function acceptCookies() {
    const b = first(["#onetrust-accept-btn-handler"]) || byText("button", /^(akceptuję|zaakceptuj wszystkie|accept all)$/i);
    if (b) b.click();
  }

  // ---------------- поиск ----------------
  function listSearch() {
    return FH.parseSearch(document, location.href);
  }

  async function reportSearch() {
    if (!kind() || !kind().endsWith("_search")) return;
    await sleep(1500);
    const listings = listSearch();
    if (listings.length) browser.runtime.sendMessage({ type: "searchPage", url: location.href, listings }).catch(() => {});
  }

  // ---------------- отклик ----------------
  const OLX = {
    textarea: ["textarea[name='message']", "[data-testid='chat-textarea'] textarea", "textarea[data-cy='chat-textarea']",
      "[data-testid='message-input'] textarea", "form textarea"],
    open: ["[data-testid='ad-contact-message-button']", "[data-cy='ad-contact-message-button']"],
    submit: ["[data-testid='chat-form-submit']", "[data-cy='chat-form-submit']", "form button[type='submit']"],
  };
  const OTODOM = {
    textarea: ["textarea[name='text']", "textarea[name='message']", "form textarea"],
    fields: { name: ["input[name='name']"], email: ["input[name='email']", "input[type='email']"], phone: ["input[name='phone']", "input[type='tel']"] },
    submit: ["[data-cy='contact-form.submit-button']", "form button[type='submit']"],
  };

  async function fillOlx(text, send) {
    acceptCookies();
    let ta = first(OLX.textarea);
    if (!ta) {
      const btn = first(OLX.open) || byText("button, a", /napisz wiadomość|wyślij wiadomość/i);
      if (!btn) return { ok: false, message: captcha() ? "OLX показал капчу" : "не нашёл кнопку «Napisz wiadomość»" };
      btn.click();
      ta = await waitFor(() => first(OLX.textarea), 12000);
    }
    if (/login|logowanie/.test(location.href)) return { ok: false, message: "OLX просит войти в аккаунт" };
    if (!ta) return { ok: false, message: captcha() ? "OLX показал капчу" : "не нашёл поле сообщения" };
    ta.focus();
    setValue(ta, text);
    if (!send) return { ok: true, sent: false, message: "Текст вставлен в чат OLX. Проверьте и нажмите «Wyślij»." };
    const sb = first(OLX.submit) || byText("button", /^wyślij$/i);
    if (!sb) return { ok: true, sent: false, message: "Текст вставлен, но кнопку отправки не нашёл: нажмите сами." };
    sb.click();
    const done = await waitFor(() => !ta.isConnected || ta.value.trim() === "", 8000);
    if (captcha()) return { ok: false, message: "OLX показал капчу после отправки: проверьте вручную." };
    return done ? { ok: true, sent: true, message: "Отправлено в чат OLX." } : { ok: true, sent: false, message: "Нажал «Wyślij», но не уверен, что ушло: проверьте." };
  }

  async function fillOtodom(text, send, profile) {
    acceptCookies();
    let ta = first(OTODOM.textarea);
    if (!ta) {
      const btn = byText("button", /wyślij wiadomość|napisz wiadomość|zapytaj/i);
      if (btn) { btn.click(); ta = await waitFor(() => first(OTODOM.textarea), 10000); }
    }
    if (!ta) return { ok: false, message: captcha() ? "Otodom показал капчу" : "не нашёл форму контакта" };
    for (const [field, sels] of Object.entries(OTODOM.fields)) {
      const inp = first(sels);
      if (inp && !inp.value.trim() && profile && profile[field]) setValue(inp, profile[field]);
    }
    setValue(ta, text);
    const missing = Object.entries(OTODOM.fields).filter(([f, sels]) => { const i = first(sels); return i && i.required && !i.value.trim(); });
    if (!send) return { ok: true, sent: false, message: "Форма Otodom заполнена. Проверьте и нажмите «Wyślij wiadomość»." };
    if (missing.length) return { ok: true, sent: false, message: "Не хватает полей (" + missing.map((m) => m[0]).join(", ") + "): заполните «О себе» в панели." };
    const sb = first(OTODOM.submit) || byText("button", /wyślij wiadomość|^wyślij$/i);
    if (!sb) return { ok: true, sent: false, message: "Форма заполнена, кнопку отправки не нашёл: нажмите сами." };
    sb.click();
    const ok = await waitFor(() => /wiadomość (została )?wysłan|dziękujemy/i.test(document.body.innerText || ""), 10000);
    if (captcha()) return { ok: false, message: "Otodom показал капчу после отправки: проверьте вручную." };
    return ok ? { ok: true, sent: true, message: "Отправлено через форму Otodom." } : { ok: true, sent: false, message: "Нажал «Wyślij», подтверждения не видно: проверьте." };
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (msg.type === "listSearch") return Promise.resolve({ listings: listSearch() });
    if (msg.type === "extract") {
      const l = FH.listingFromUrl(location.href);
      return Promise.resolve({ listing: l ? FH.parseDetail(document, l) : null });
    }
    if (msg.type === "fill") {
      const k = kind();
      if (k === "olx_listing") return fillOlx(msg.text, msg.send);
      if (k === "otodom_listing") return fillOtodom(msg.text, msg.send, msg.profile);
      return Promise.resolve({ ok: false, message: "это не страница объявления" });
    }
    return undefined;
  });

  reportSearch();
})();
