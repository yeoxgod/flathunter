"""Проверка content/site.js и whatsapp.js в настоящем браузере (Chromium) на макетах страниц OLX/Otodom/WhatsApp."""
import asyncio, pathlib, sys
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHIM = "window.browser={runtime:{onMessage:{addListener(f){window.__h=f}},sendMessage:async(m)=>{(window.__sent=window.__sent||[]).push(m)}}};"
PAGES = {
  "https://www.olx.pl/d/oferta/x-CID3-IDa1.html": """<html><body><h1>Mieszkanie</h1>
<button id=onetrust-accept-btn-handler onclick="this.remove()">OK</button>
<button data-testid="ad-contact-message-button" onclick="document.getElementById('f').style.display='block'">Napisz wiadomość</button>
<div data-cy="ad_description">Opis. Tel 600 111 222</div>
<form id=f style="display:none" onsubmit="event.preventDefault();window.__got=this.message.value;this.message.value=''">
<textarea name=message></textarea><button type=submit data-testid="chat-form-submit">Wyślij</button></form></body></html>""",
  "https://www.otodom.pl/pl/oferta/x-ID4a": """<html><body><form onsubmit="event.preventDefault();window.__got=[this.name.value,this.email.value,this.text.value];document.body.insertAdjacentHTML('beforeend','<p>Wiadomość została wysłana</p>')">
<input name=name><input name=email required><input name=phone><textarea name=text></textarea>
<button type=submit data-cy="contact-form.submit-button">Wyślij wiadomość</button></form></body></html>""",
  "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/": """<html><body>
<div data-cy="l-card"><a href="/d/oferta/m-CID3-IDz9.html"><h4>M 2 pok</h4></a><p data-testid="ad-price">3 900 zł</p>
<p data-testid="location-date">Warszawa, Wola - Dzisiaj o 09:00</p><span>41 m²</span></div></body></html>""",
  "https://web.whatsapp.com/send": """<html><body><div id=chat></div><footer><div contenteditable="true" data-tab="10" id=box style="min-height:20px"></div>
<button aria-label="Send" onclick="chat.innerText=box.innerText;box.innerText=''">S</button></footer>
<script>box.innerText=new URLSearchParams(location.search).get('text')||''</script></body></html>""",
}

async def main():
    fails = 0
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()

        async def route(r):
            url = r.request.url.split("?")[0]
            await r.fulfill(status=200, content_type="text/html; charset=utf-8", body=PAGES.get(url, "<html></html>"))
        await page.route("**/*", route)

        async def load(url, scripts):
            await page.goto(url)
            await page.evaluate(SHIM)
            for s in scripts:
                await page.add_script_tag(content=(ROOT / s).read_text(encoding="utf-8"))

        def check(name, cond, info=""):
            nonlocal fails
            print(("ok  - " if cond else "FAIL- ") + name, "" if cond else info)
            fails += 0 if cond else 1

        # OLX: вставить без отправки, затем отправить
        await load("https://www.olx.pl/d/oferta/x-CID3-IDa1.html", ["lib/parse.js", "content/site.js"])
        r = await page.evaluate("window.__h({type:'fill', text:'Dzień dobry', send:false})")
        check("olx insert", r["ok"] and not r["sent"] and await page.input_value("textarea") == "Dzień dobry", r)
        r = await page.evaluate("window.__h({type:'fill', text:'Dzień dobry 2', send:true})")
        check("olx send", r["ok"] and r["sent"] and await page.evaluate("window.__got") == "Dzień dobry 2", r)
        r = await page.evaluate("window.__h({type:'extract'})")
        check("olx extract", r["listing"]["phones"] == ["48600111222"], r)

        # Otodom: без email не отправляет, с профилем отправляет
        await load("https://www.otodom.pl/pl/oferta/x-ID4a", ["lib/parse.js", "content/site.js"])
        r = await page.evaluate("window.__h({type:'fill', text:'Hej', send:true, profile:{}})")
        check("otodom missing email", r["ok"] and not r["sent"] and "email" in r["message"], r)
        r = await page.evaluate("window.__h({type:'fill', text:'Hej', send:true, profile:{name:'Ivan', email:'i@x.pl'}})")
        check("otodom send", r["ok"] and r["sent"] and await page.evaluate("window.__got") == ["Ivan", "i@x.pl", "Hej"], r)

        # страница поиска OLX сама сообщает объявления
        await load("https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/", ["lib/parse.js", "content/site.js"])
        await page.wait_for_timeout(1800)
        sent = await page.evaluate("window.__sent || []")
        check("olx search report", sent and sent[0]["listings"][0]["key"] == "olx:z9" and sent[0]["listings"][0]["area"] == 41, sent)

        # WhatsApp
        await load("https://web.whatsapp.com/send?phone=48600&text=Cze%C5%9B%C4%87", ["content/whatsapp.js"])
        r = await page.evaluate("window.__h({type:'waFill', text:'Cześć', send:true})")
        check("whatsapp send", r["ok"] and r["sent"] and await page.inner_text("#chat") == "Cześć", r)
        await b.close()
    sys.exit(1 if fails else 0)

asyncio.run(main())
