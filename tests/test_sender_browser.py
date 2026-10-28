"""Отправка через браузер на локальных макетах страниц (не настоящие WhatsApp/OLX/Otodom).
Проверяет логику: вставка текста, тест-режим, отправка, капча, «не залогинен», неверный номер."""
import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from flathunter.sender import BrowserSender

PAGES = {
    "/wa/send": """<html><body><div id=chat></div><footer>
<div contenteditable="true" data-tab="10" id=box style="min-height:20px;border:1px solid"></div>
<button aria-label="Send" onclick="document.getElementById('chat').innerText=box.innerText;box.innerText=''">Send</button></footer>
<script>const q=new URLSearchParams(location.search);
if(q.get('phone').endsWith('000')){document.body.innerHTML='<div>Phone number shared via url is invalid.</div>'}
else{document.getElementById('box').innerText=q.get('text')}</script></body></html>""",
    "/waqr/send": "<html><body><canvas aria-label='Scan this QR code to link a device!' width=200 height=200></canvas></body></html>",
    "/olx/ok": """<html><body><h1>Mieszkanie</h1>
<button id=onetrust-accept-btn-handler onclick="this.remove()">OK</button>
<button data-testid="ad-contact-message-button" onclick="document.getElementById('f').style.display='block'">Napisz wiadomość</button>
<form id=f style="display:none" onsubmit="event.preventDefault();this.message.value='';document.title='sent'">
<textarea name=message></textarea><button type=submit data-testid="chat-form-submit">Wyślij</button></form></body></html>""",
    "/olx/captcha": "<html><body><iframe src='/recaptcha/frame'></iframe><p>Potwierdź, że jesteś człowiekiem</p></body></html>",
    "/recaptcha/frame": "<html><body>captcha</body></html>",
    "/otodom/ok": """<html><body><form onsubmit="event.preventDefault();document.body.insertAdjacentHTML('beforeend','<p>Wiadomość została wysłana</p>')">
<input name=name><input name=email><input name=phone><textarea name=text></textarea>
<button type=submit data-cy="contact-form.submit-button">Wyślij wiadomość</button></form></body></html>""",
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(urlparse(self.path).path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "nope").encode())

    def log_message(self, *a):
        pass


class BrowserSenderTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = BrowserSender(f"{self.tmp.name}/p", f"{self.tmp.name}/shots", {"whatsapp": f"{self.base}/wa"}, headless=True)
        self.s.timeout_ms = 8000

    async def asyncTearDown(self):
        await self.s.close()
        self.tmp.cleanup()

    async def test_whatsapp(self):
        text = "Dzień dobry,\nczy mieszkanie jest dostępne?"
        r = await self.s.send("whatsapp", {"phones": ["48600123456"]}, text, {}, dry_run=True)
        self.assertTrue(r.ok and r.dry_run, r.message)
        self.assertTrue(r.screenshot)
        r = await self.s.send("whatsapp", {"phones": ["48600123456"]}, text, {}, dry_run=False)
        self.assertTrue(r.ok and not r.dry_run, r.message)
        r = await self.s.send("whatsapp", {"phones": ["48600000000"]}, text, {}, dry_run=False)
        self.assertEqual(r.kind, "invalid_phone")
        self.s.base["whatsapp"] = f"{self.base}/waqr"
        r = await self.s.send("whatsapp", {"phones": ["48600123456"]}, text, {}, dry_run=False)
        self.assertEqual(r.kind, "login")

    async def test_olx(self):
        r = await self.s.send("olx", {"url": f"{self.base}/olx/ok"}, "Dzień dobry", {}, dry_run=True)
        self.assertTrue(r.ok and r.dry_run, r.message)
        r = await self.s.send("olx", {"url": f"{self.base}/olx/ok"}, "Dzień dobry", {}, dry_run=False)
        self.assertTrue(r.ok, r.message)
        r = await self.s.send("olx", {"url": f"{self.base}/olx/captcha"}, "Dzień dobry", {}, dry_run=False)
        self.assertEqual(r.kind, "captcha")

    async def test_otodom(self):
        r = await self.s.send("otodom", {"url": f"{self.base}/otodom/ok"}, "Dzień dobry", {}, dry_run=False)
        self.assertEqual(r.kind, "error")  # нет имени/email в профиле
        prof = {"name": "Ivan", "email": "i@x.pl", "phone": "600123456"}
        r = await self.s.send("otodom", {"url": f"{self.base}/otodom/ok"}, "Dzień dobry", prof, dry_run=False)
        self.assertTrue(r.ok, r.message)


if __name__ == "__main__":
    unittest.main()
