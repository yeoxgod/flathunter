/* WhatsApp Web: текст уже подставлен ссылкой wa.me; при режиме «отправлять» жмём кнопку отправки. */
(function () {
  "use strict";
  if (window.__fhWa) return;
  window.__fhWa = true;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const COMPOSE = ["footer div[contenteditable='true']", "div[contenteditable='true'][data-tab='10']"];
  const SEND = ["button[aria-label='Send']", "button[aria-label='Wyślij']", "button[aria-label='Отправить']",
    "span[data-icon='send']", "span[data-icon='wds-ic-send-filled']"];
  const q = (sels) => { for (const s of sels) { const e = document.querySelector(s); if (e) return e; } return null; };

  async function fill(text, send) {
    const end = Date.now() + 45000;
    let box = null;
    while (Date.now() < end) {
      box = q(COMPOSE);
      if (box) break;
      if (document.querySelector("canvas[aria-label], div[data-ref]")) return { ok: false, message: "WhatsApp Web не залогинен: отсканируйте QR в открывшейся вкладке." };
      if (/invalid|nieprawidłow|недействител/i.test((document.body.innerText || "").slice(0, 3000))) return { ok: false, message: "Этого номера нет в WhatsApp." };
      await sleep(700);
    }
    if (!box) return { ok: false, message: "не дождался чата WhatsApp" };
    await sleep(800);
    if (!box.innerText.trim()) {
      box.focus();
      document.execCommand("insertText", false, text);
    }
    if (!send) return { ok: true, sent: false, message: "Текст вставлен в WhatsApp. Нажмите Enter, чтобы отправить." };
    const btn = q(SEND);
    if (btn) (btn.closest("button") || btn).click();
    else box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    for (let i = 0; i < 16; i++) { await sleep(500); if (!box.innerText.trim()) return { ok: true, sent: true, message: "Отправлено в WhatsApp." }; }
    return { ok: true, sent: false, message: "Не уверен, что сообщение ушло: проверьте WhatsApp." };
  }

  browser.runtime.onMessage.addListener((msg) => (msg.type === "waFill" ? fill(msg.text, msg.send) : undefined));
})();
