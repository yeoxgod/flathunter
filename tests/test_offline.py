"""Офлайн-тесты на синтетических фикстурах (живые сайты из этого окружения недоступны).

Запуск из корня проекта: python -m unittest discover -s tests -t . -v
"""
import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta

from flathunter.analyzer import Analyzer, AnalysisError, normalize_criteria
from flathunter.bot import card_keyboard, classify_url, settings_view
from flathunter.db import DB
from flathunter.filters import prefilter
from flathunter.models import Listing
from flathunter.pipeline import Pipeline, listing_from_url
from flathunter.render import render_card, wa_link
from flathunter.sender import SendJob, SendManager, SendResult, choose_channel
from flathunter.settings import Settings
from flathunter.sources import olx, otodom, tgchannel
from flathunter.sources.base import FetchError
from flathunter.util import find_phones, parse_int, parse_number
OTODOM_SEARCH = """<html><body><script id="__NEXT_DATA__" type="application/json">%s</script></body></html>""" % json.dumps(
    {"props": {"pageProps": {"data": {"searchAds": {"items": [
        {"id": 111, "title": "Przytulne 2 pokoje Mokotów", "slug": "przytulne-2-pokoje-mokotow-ID4abcd",
         "totalPrice": {"value": 4200, "currency": "PLN"}, "rentPrice": {"value": 600, "currency": "PLN"},
         "areaInSquareMeters": 45.5, "roomsNumber": "TWO",
         "locationLabel": {"value": "Mokotów, Warszawa, mazowieckie"}, "images": [{"medium": "https://img/1.jpg"}]},
        {"id": 222, "title": "Kawalerka Wola", "slug": "kawalerka-wola-ID4efgh",
         "totalPrice": {"value": 2900, "currency": "PLN"}, "areaInSquareMeters": 28, "roomsNumber": "ONE",
         "locationLabel": {"value": "Wola, Warszawa"}, "images": []},
    ]}}}}}
)

OTODOM_DETAIL = """<script id="__NEXT_DATA__" type="application/json">%s</script>""" % json.dumps(
    {"props": {"pageProps": {"ad": {
        "title": "Przytulne 2 pokoje Mokotów",
        "description": "<p>Wynajmę mieszkanie.</p><p>Tel. 600 123 456</p>",
        "target": {"Price": 4200, "Rent": 650, "Area": 45.5, "Rooms_num": ["2"], "Deposit": 4200},
        "characteristics": [{"key": "floor", "label": "Piętro", "value": "3"}],
        "location": {"address": {"street": {"name": "ul. Puławska"}, "city": {"name": "Warszawa"}}},
    }}}}
)

OLX_SEARCH = """
<div data-cy="l-card"><a href="/d/oferta/mieszkanie-2-pokoje-CID3-IDabc12.html"><h4>Mieszkanie 2 pokoje</h4></a>
<p data-testid="ad-price">3 800 zł<span>do negocjacji</span></p>
<p data-testid="location-date">Warszawa, Ursynów - Odświeżono dnia 27 października 2026</p>
<span>48 m²</span><img src="https://img/x.jpg"></div>
<div data-cy="l-card"><a href="https://www.otodom.pl/pl/oferta/jakies-ID4zzz">Z Otodom</a></div>
"""

OLX_DETAIL = """
<h4 data-cy="ad_title">Mieszkanie 2 pokoje</h4>
<div data-cy="ad_description">Ładne mieszkanie.<br>Kontakt: 501-222-333</div>
<div data-testid="ad-price-container"><h3>3 800 zł</h3></div>
<div data-testid="ad-parameters-container"><p>Czynsz (dodatkowo): 500 zł</p><p>Powierzchnia: 48 m²</p>
<p>Liczba pokoi: 2 pokoje</p><p>Umeblowane: Tak</p></div>
"""


def post(n, olx_id, headline, tags, body):
    tag_html = " ".join(f'<a href="?q=%23{t}">#{t}</a>' for t in tags)
    return (
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="home_Warszawa/{n}">'
        f'<div class="tgme_widget_message_text"><b><a href="https://www.olx.pl/d/oferta/x-CID3-ID{olx_id}.html?reason=1">'
        f"{headline}</a></b><br/>{tag_html}<br/>{body}</div>"
        f'<a class="tgme_widget_message_date" href="https://t.me/home_Warszawa/{n}">'
        f'<time datetime="2026-10-28T10:00:00+00:00">10:00</time></a></div></div>'
    )


POST_101 = post(101, "aaa11", "2 POKOJE MOKOTÓW BEZ PROWIZJI", ["Mokotów", "2pokoje"],
                "Cena: 4 200 zł + 600 zł media<br/>Powierzchnia: 45 m²<br/>Sprzedawca: osoba prywatna")
POST_102 = post(102, "bbb22", "KAWALERKA WOLA", ["Wola", "kawalerka"],
                "Cena: 2 900 zł<br/>Powierzchnia: 28 m²<br/>Sprzedawca: agencja")
POST_NO_LINK = '<div class="tgme_widget_message" data-post="home_Warszawa/103"><div class="tgme_widget_message_text">Reklama</div></div>'


class ParsingTests(unittest.TestCase):
    def test_numbers_and_phones(self):
        self.assertEqual(parse_number("3 500,50 zł"), 3500.5)
        self.assertEqual(parse_int("45 m²"), 45)
        self.assertIsNone(parse_number("zapytaj o cenę"))
        self.assertEqual(find_phones("Tel. +48 600 123 456 lub 501-222-333"), ["48600123456", "48501222333"])
        self.assertEqual(find_phones("Cena 3500 zł, 45 m²"), [])

    def test_otodom_search(self):
        r = otodom.parse_search(OTODOM_SEARCH)
        self.assertEqual(len(r), 2)
        a = r[0]
        self.assertEqual((a.ext_id, a.price, a.admin_rent, a.area, a.rooms), ("111", 4200, 600, 45.5, 2))
        self.assertTrue(a.url.endswith("/pl/oferta/przytulne-2-pokoje-mokotow-ID4abcd"))
        self.assertIn("Mokotów", a.location)
        self.assertEqual(a.photo, "https://img/1.jpg")
        self.assertEqual(r[1].rooms, 1)

    def test_otodom_fallback_links(self):
        html = '<a href="/pl/oferta/foo-bar-ID4xyz">Foo bar</a>'
        r = otodom.parse_search(html)
        self.assertEqual((r[0].ext_id, r[0].title), ("4xyz", "Foo bar"))

    def test_otodom_detail(self):
        l = otodom.parse_search(OTODOM_SEARCH)[0]
        l = otodom.parse_detail(OTODOM_DETAIL, l)
        self.assertIn("Wynajmę", l.description)
        self.assertNotIn("<p>", l.description)
        self.assertEqual((l.admin_rent, l.deposit), (650, 4200))
        self.assertEqual(l.phones, ["48600123456"])
        self.assertIn("Puławska", l.location)
        self.assertEqual(l.params["Piętro"], "3")

    def test_olx_search_skips_otodom(self):
        r = olx.parse_search(OLX_SEARCH)
        self.assertEqual(len(r), 1)
        l = r[0]
        self.assertEqual((l.ext_id, l.price, l.area), ("abc12", 3800, 48.0))
        self.assertEqual(l.location, "Warszawa, Ursynów")

    def test_olx_detail(self):
        l = olx.parse_search(OLX_SEARCH)[0]
        l = olx.parse_detail(OLX_DETAIL, l)
        self.assertEqual((l.admin_rent, l.area, l.rooms), (500, 48.0, 2))
        self.assertEqual(l.phones, ["48501222333"])
        self.assertIn("Ładne", l.description)

    def test_channel_parse(self):
        html = POST_101 + POST_102 + POST_NO_LINK
        r = tgchannel.parse_search(html)
        self.assertEqual([x.ext_id for x in r], ["bbb22", "aaa11"])  # новые первыми
        b, a = r
        self.assertEqual(a.url, "https://www.olx.pl/d/oferta/x-CID3-IDaaa11.html")  # без трекинга
        self.assertEqual((a.price, a.admin_rent, a.area, a.rooms), (4200, 600, 45.0, 2))
        self.assertEqual(a.location, "Mokotów")
        self.assertEqual(a.params["seller"], "частное лицо")
        self.assertIn("4 200 zł", a.params["post_text"])
        self.assertEqual((b.price, b.rooms, b.location, b.params["seller"]), (2900, 1, "Wola", "агентство"))


class FilterTests(unittest.TestCase):
    def test_prefilter(self):
        l = Listing("olx", "1", "u", title="Mieszkanie", price=4200, admin_rent=600, area=45, rooms=2, location="Mokotów")
        self.assertTrue(prefilter(l, {"max_price": 5000})[0])
        self.assertFalse(prefilter(l, {"max_price": 4500})[0])  # 4200 + 600
        self.assertFalse(prefilter(l, {"min_area": 50})[0])
        self.assertFalse(prefilter(l, {"rooms": [3]})[0])
        self.assertFalse(prefilter(l, {"districts": ["Wola"]})[0])
        self.assertTrue(prefilter(l, {"districts": ["mokot"]})[0])
        self.assertFalse(prefilter(l, {"exclude_keywords": ["MIESZKANIE"]})[0])
        self.assertTrue(prefilter(Listing("olx", "2", "u"), {"max_price": 100, "rooms": [9]})[0])  # неизвестное не отсекает




class StubLLM:
    def __init__(self, structured=None, text=""):
        self._structured = structured
        self._text = text
        self.calls = []

    async def structured(self, system, user, schema):
        self.calls.append(("structured", system, user, schema))
        if isinstance(self._structured, Exception):
            raise self._structured
        return self._structured

    async def text(self, system, user):
        self.calls.append(("text", system, user))
        return self._text


SETTINGS = {"search": {"max_price": 5000, "wishes": "метро рядом"},
            "profile": {"name": "Ivan", "pets": "", "email": "i@x.pl"}}


class AnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_normalizes(self):
        raw = {"fit_score": "9", "verdict": "ok", "pros": "один плюс", "cons": [], "red_flags": None,
               "questions": ["kaucja?"], "message_pl": " Dzień dobry, ... "}
        llm = StubLLM(raw)
        a = await Analyzer(llm, lambda: SETTINGS).analyze(Listing("olx", "1", "u", title="T", phones=["48600"]))
        self.assertEqual((a["fit_score"], a["pros"], a["red_flags"], a["message_pl"]), (9, ["один плюс"], [], "Dzień dobry, ..."))
        prompt = llm.calls[0][2]
        self.assertIn("Ivan", prompt)
        self.assertNotIn('"pets"', prompt)   # пустые поля профиля не уходят
        self.assertNotIn("48600", prompt)    # телефон владельца модели не нужен
        self.assertNotIn("i@x.pl", prompt)   # email нужен только форме Otodom

    async def test_analyze_errors(self):
        bad = {"fit_score": 5, "verdict": "", "pros": [], "cons": [], "red_flags": [], "questions": [], "message_pl": " "}
        with self.assertRaises(AnalysisError):
            await Analyzer(StubLLM(bad), lambda: SETTINGS).analyze(Listing("olx", "1", "u"))

    async def test_redraft_and_parse(self):
        a = Analyzer(StubLLM(text="Krótko."), lambda: SETTINGS)
        self.assertEqual(await a.redraft(Listing("olx", "1", "u"), "Długo...", "krócej"), "Krótko.")
        crit = normalize_criteria({"max_price": "5500", "rooms": [2, "3", 0, 15], "districts": "Mokotów",
                                   "exclude_keywords": None, "wishes": " мебель "})
        self.assertEqual((crit["max_price"], crit["rooms"], crit["districts"], crit["wishes"]), (5500, [2, 3], ["Mokotów"], "мебель"))
        prof = await Analyzer(StubLLM({"name": "Ivan", "about": None}), lambda: SETTINGS).parse_profile("я Иван", {})
        self.assertEqual((prof["name"], prof["about"], prof["email"]), ("Ivan", "", ""))


class FakeFetcher:
    def __init__(self):
        self.channel = ""
        self.blocked = set()

    async def get(self, url):
        if url.startswith("https://t.me/s/"):
            return self.channel
        if any(b in url for b in self.blocked):
            raise FetchError("HTTP 403", blocked=True)
        return OLX_DETAIL


class FakeAnalyzer:
    def __init__(self):
        self.seen = []

    async def analyze(self, l):
        self.seen.append(l)
        score = 3 if "ddd44" in l.url else 8
        return {"fit_score": score, "verdict": "ok", "pros": ["p"], "cons": ["c"], "red_flags": [],
                "questions": ["q"], "message_pl": "Dzień dobry, test"}


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/t.db")
            st = Settings(db)
            s = st.get()
            s["sources"] = {"channel": {"enabled": True, "name": "home_Warszawa"},
                            "otodom": {"enabled": False, "search_url": ""}, "olx": {"enabled": False, "search_url": ""}}
            s["search"].update(max_price=5000, rooms=[1, 2, 3])
            st.save(s)
            fetcher, analyzer = FakeFetcher(), FakeAnalyzer()
            sent, alerts = [], []

            async def notify(row_id, listing, analysis, allow_auto=True):
                sent.append((row_id, listing, analysis))

            async def alert(text):
                alerts.append(text)

            p = Pipeline(db, st.get, fetcher, analyzer, notify, alert)
            fetcher.channel = POST_101 + POST_102
            self.assertEqual((await p.cycle())["seeded"], 2)
            self.assertEqual((sent, len(alerts)), ([], 1))

            new = post(104, "ccc33", "2 POKOJE URSYNÓW", ["Ursynów", "2pokoje"], "Cena: 3 800 zł<br/>Powierzchnia: 48 m²")
            fetcher.channel = POST_101 + POST_102 + new
            self.assertEqual((await p.cycle())["sent"], 1)
            self.assertEqual(sent[0][1]["phones"], ["48501222333"])  # телефон со страницы OLX
            await p.cycle()
            self.assertEqual(len(sent), 1)  # повтора нет

            extra = (
                post(105, "fff66", "ДОРОГО", ["Mokotów", "3pokoje"], "Cena: 9 000 zł<br/>Powierzchnia: 60 m²")
                + post(106, "ddd44", "SŁABE", ["Praga", "2pokoje"], "Cena: 3 000 zł<br/>Powierzchnia: 40 m²")
                + post(107, "eee55", "BLOK", ["Bemowo", "2pokoje"], "Cena: 3 500 zł<br/>Powierzchnia: 42 m²")
            )
            fetcher.channel = POST_101 + POST_102 + new + extra
            fetcher.blocked = {"eee55"}
            before = len(analyzer.seen)
            stats = await p.cycle()
            self.assertEqual((stats["filtered"], stats["lowscore"], stats["sent"]), (1, 1, 1))
            self.assertEqual(len(analyzer.seen) - before, 2)  # дорогое до модели не дошло
            self.assertIn("недоступна", [l for l in analyzer.seen if "eee55" in l.url][0].params["_note"])

            # ручная ссылка: без фильтров и порога, даже если объявление уже видели
            row_id = await p.process_url("https://www.olx.pl/d/oferta/x-CID3-IDfff66.html?ref=1")
            self.assertIsNotNone(row_id)
            self.assertEqual(db.get(row_id)["status"], "sent")
            self.assertIsNone(await p.process_url("https://example.com/x"))

    async def test_relink_reseeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/t.db")
            st = Settings(db)
            st.update("sources.olx", {"enabled": False, "search_url": ""})
            st.update("sources.otodom", {"enabled": False, "search_url": ""})
            st.update("sources.channel", {"enabled": True, "name": "a"})
            fetcher = FakeFetcher()
            fetcher.channel = POST_101
            alerts = []

            async def alert(t):
                alerts.append(t)

            async def notify(*a):
                pass

            p = Pipeline(db, st.get, fetcher, FakeAnalyzer(), notify, alert)
            await p.cycle()
            st.update("sources.channel.name", "b")  # другая выдача -> новое запоминание
            fetcher.channel = POST_101 + POST_102
            stats = await p.cycle()
            self.assertEqual(stats.get("seeded"), 1)
            self.assertEqual(stats.get("sent", 0), 0)


class FakeSender:
    def __init__(self, result_kind=""):
        self.calls = []
        self.kind = result_kind

    async def send(self, channel, listing, text, profile, dry_run):
        self.calls.append((channel, dry_run))
        ok = self.kind == ""
        return SendResult(ok, channel, dry_run, "msg", None, self.kind)


class SendManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(f"{self.tmp.name}/t.db")
        self.st = Settings(self.db)
        self.results = []
        self.sleeps = []
        self.hour = 12

        async def on_result(job, res):
            self.results.append(res)

        async def sleep(sec):
            self.sleeps.append(sec)

        self.sender = FakeSender()
        self.m = SendManager(self.sender, self.db, self.st.get, on_result, sleep=sleep,
                             now=lambda: datetime(2026, 10, 28, self.hour, 0))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _row(self, ext="1", phones=("48600123456",)):
        l = Listing("olx", ext, f"https://www.olx.pl/d/oferta/x-ID{ext}.html", phones=list(phones))
        return self.db.insert(l, "sent", analysis={"fit_score": 9}, draft="Dzień dobry"), asdict(l)

    async def test_dry_run_does_not_mark(self):
        rid, l = self._row()
        await self.m._handle(SendJob(rid, "whatsapp", l, "x"))
        self.assertEqual(self.sender.calls, [("whatsapp", True)])
        self.assertEqual(self.db.get(rid)["status"], "sent")
        self.assertEqual((self.m.sent_today(), self.sleeps), (0, []))

    async def test_real_send_dedup_limit_hours(self):
        self.st.update("send.dry_run", False)
        self.st.update("send.daily_limit", 2)
        rid, l = self._row("1")
        await self.m._handle(SendJob(rid, "whatsapp", l, "x"))
        self.assertEqual(self.db.get(rid)["status"], "contacted")
        self.assertEqual(self.m.sent_today(), 1)
        self.assertEqual(len(self.sleeps), 1)

        await self.m._handle(SendJob(rid, "whatsapp", l, "x"))           # то же объявление
        self.assertEqual(self.results[-1].kind, "duplicate")
        rid2, l2 = self._row("2")                                          # тот же номер
        await self.m._handle(SendJob(rid2, "whatsapp", l2, "x"))
        self.assertEqual(self.results[-1].kind, "duplicate")
        await self.m._handle(SendJob(rid2, "olx", l2, "x"))                # тот же номер, но чат OLX
        self.assertTrue(self.results[-1].ok)

        rid3, l3 = self._row("3", phones=())
        await self.m._handle(SendJob(rid3, "olx", l3, "x"))
        self.assertEqual(self.results[-1].kind, "limit")

        self.st.update("send.daily_limit", 10)
        self.hour = 23
        await self.m._handle(SendJob(rid3, "olx", l3, "x", auto=True))
        self.assertEqual(self.results[-1].kind, "hours")
        await self.m._handle(SendJob(rid3, "olx", l3, "x", auto=False))   # по кнопке часы не важны
        self.assertTrue(self.results[-1].ok)
        self.assertEqual(len(self.sender.calls), 3)

    async def test_unverified_blocks_repeat(self):
        self.st.update("send.dry_run", False)
        self.sender.kind = "unverified"
        rid, l = self._row()
        await self.m._handle(SendJob(rid, "olx", l, "x"))
        self.assertEqual(self.db.get(rid)["status"], "contacted")


class BotViewTests(unittest.TestCase):
    def test_card_keyboard_and_channel(self):
        s = {"send": {"channels": {"whatsapp": True, "olx": True, "otodom": True}}}
        wa = {"url": "https://www.olx.pl/d/oferta/x-IDa.html", "phones": ["48600123456"]}
        self.assertEqual(choose_channel(wa, s), "whatsapp")
        s["send"]["channels"]["whatsapp"] = False
        self.assertEqual(choose_channel(wa, s), "olx")
        self.assertEqual(choose_channel({"url": "https://www.otodom.pl/pl/oferta/x", "phones": []}, s), "otodom")
        s["send"]["channels"]["otodom"] = False
        self.assertIsNone(choose_channel({"url": "https://www.otodom.pl/pl/oferta/x", "phones": []}, s))

        kb = card_keyboard(5, wa, "Dzień dobry", "sent", "whatsapp", pending=False)
        self.assertEqual(len(kb.inline_keyboard[0]), 2)
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "a:send:5")
        kb = card_keyboard(5, wa, "x", "sent", "olx", pending=True)
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "a:cancel:5")
        self.assertEqual(len(card_keyboard(5, wa, "x", "contacted", "olx", False).inline_keyboard), 1)

    def test_settings_view_and_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings(DB(f"{tmp}/t.db")).get()
        text, kb = settings_view(s)
        self.assertIn("по кнопке", text)
        self.assertIn("ВКЛ", text)  # тест-режим по умолчанию включён
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas))
        self.assertEqual(classify_url("https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/mazowieckie/warszawa"), "otodom_search")
        self.assertEqual(classify_url("https://www.otodom.pl/pl/oferta/abc-ID4x"), "otodom_listing")
        self.assertEqual(classify_url("https://www.olx.pl/nieruchomosci/mieszkania/wynajem/warszawa/"), "olx_search")
        self.assertEqual(classify_url("https://www.olx.pl/d/oferta/x-IDa.html"), "olx_listing")
        self.assertIsNone(classify_url("https://google.com"))
        self.assertEqual(listing_from_url("https://www.olx.pl/d/oferta/x-CID3-IDabc.html#q")[0].ext_id, "abc")


class RenderTests(unittest.TestCase):
    def test_card_escapes_and_fits(self):
        l = asdict(Listing("olx", "1", "https://olx.pl/d/oferta/x-IDa.html", title="A & B <x>", price=3800,
                           admin_rent=500, area=48.0, rooms=2, location="Ursynów", params={"seller": "агентство"}))
        a = {"fit_score": 8, "verdict": "Хорошо <b>", "pros": ["p"] * 20, "cons": ["c" * 400] * 20,
             "red_flags": ["r"], "questions": ["q"] * 20, "message_pl": "Dzień dobry & hello"}
        text = render_card(l, a, "Dzień dobry & hello", note="✅ ok")
        self.assertLessEqual(len(text), 4096)
        self.assertIn("A &amp; B &lt;x&gt;", text)
        self.assertIn("<pre>Dzień dobry &amp; hello</pre>", text)

    def test_wa_link(self):
        link = wa_link("48600123456", "Dzień dobry! " * 400)
        self.assertTrue(link.startswith("https://wa.me/48600123456?text="))
        self.assertLess(len(link), 1700)


if __name__ == "__main__":
    unittest.main()


class ClaudeShimTests(unittest.TestCase):
    def test_resolve_npm_shims(self):
        from pathlib import Path
        from flathunter.llm import _resolve_shim
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            exe = d / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("x")
            (d / "node.exe").write_text("x")
            shim = d / "claude.cmd"
            shim.write_text('@ECHO off\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n)\r\n'
                            '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n')
            self.assertEqual(_resolve_shim(shim), [str(exe)])
            exe.unlink()
            js = d / "node_modules/@anthropic-ai/claude-code/cli.js"
            js.write_text("x")
            shim.write_text('IF EXIST "%dp0%\\node.exe" (SET "_prog=%dp0%\\node.exe")\r\n'
                            '"%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n')
            self.assertEqual(_resolve_shim(shim)[1], str(js))


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler):
        import httpx
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @staticmethod
    def _ok(text):
        import httpx
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": text}]}, "finishReason": "STOP"}]})

    async def test_structured_schema_and_fallbacks(self):
        import httpx
        from flathunter.analyzer import REPORT_SCHEMA
        from flathunter.llm import GeminiLLM
        seen = []

        def handler(req):
            body = json.loads(req.content)
            seen.append((req.url.path, body))
            self.assertEqual(req.headers["x-goog-api-key"], "K")
            if "gemini-3.8-flash" in req.url.path:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            report = {"fit_score": 7, "verdict": "ok", "pros": [], "cons": [], "red_flags": [], "questions": [],
                      "message_pl": "Dzień dobry"}
            return self._ok("```json\n" + json.dumps(report) + "\n```")

        llm = GeminiLLM("K", client=self._client(handler))
        a = Analyzer(llm, lambda: SETTINGS)
        rep = await a.analyze(Listing("olx", "1", "u", title="T"))
        self.assertEqual((rep["fit_score"], rep["message_pl"]), (7, "Dzień dobry"))
        self.assertEqual(llm.model, "gemini-flash-latest")   # 404 -> следующая модель
        schema = seen[-1][1]["generationConfig"]["responseSchema"]
        self.assertNotIn("minimum", schema["properties"]["fit_score"])  # неподдерживаемое убрано
        self.assertIn("systemInstruction", seen[-1][1])

    async def test_schema_rejected_then_plain_json(self):
        import httpx
        from flathunter.llm import GeminiLLM
        calls = []

        def handler(req):
            body = json.loads(req.content)
            calls.append(body)
            if "responseSchema" in body["generationConfig"]:
                return httpx.Response(400, json={"error": {"message": "Invalid schema"}})
            return self._ok('Sure: {"ok": true}')

        out = await GeminiLLM("K", client=self._client(handler)).structured("s", "u", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 2)
        text = await GeminiLLM("K", client=self._client(lambda r: self._ok(" Cześć "))).text("s", "u")
        self.assertEqual(text, "Cześć")


def otodom_page(items):
    return "<script id='__NEXT_DATA__' type='application/json'>%s</script>" % json.dumps(
        {"props": {"pageProps": {"data": {"searchAds": {"items": items}}}}})


def oto_item(i, created, price=4000):
    return {"id": i, "title": f"Mieszkanie {i}", "slug": f"m-{i}-ID{i}x", "totalPrice": {"value": price},
            "areaInSquareMeters": 45, "roomsNumber": "TWO", "locationLabel": {"value": "Mokotów"},
            "dateCreatedFirst": created}


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_posted(self):
        from flathunter.util import parse_posted
        now = datetime(2026, 10, 28, 15, 0)
        self.assertEqual(parse_posted("Dzisiaj o 10:15", now), datetime(2026, 10, 28, 10, 15))
        self.assertEqual(parse_posted("Odświeżono wczoraj o 22:01", now), datetime(2026, 10, 27, 22, 1))
        self.assertEqual(parse_posted("Odświeżono dnia 25 października 2026", now).date().isoformat(), "2026-10-25")
        self.assertEqual(parse_posted("2026-10-27 09:00:00", now), datetime(2026, 10, 27, 9, 0))
        self.assertIsNotNone(parse_posted("2026-10-27T09:00:00Z", now))
        self.assertIsNone(parse_posted("", now))
        l = olx.parse_search(OLX_SEARCH)[0]
        self.assertTrue(l.posted.startswith("2026-10-27"))
        from flathunter.pipeline import page_url
        self.assertEqual(page_url("https://x.pl/a?b=1&page=3", 2), "https://x.pl/a?b=1&page=2")

    async def test_backfill(self):
        now = datetime.now()
        fmt = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        pages = {
            1: otodom_page([oto_item(1, fmt(200)),  # продвигаемое старое сверху
                            oto_item(2, fmt(1)), oto_item(3, fmt(5), price=9000), oto_item(4, fmt(20))]),
            2: otodom_page([oto_item(5, fmt(40)), oto_item(6, fmt(60))]),
            3: otodom_page([oto_item(7, fmt(80))]),
        }
        fetched = []

        class F:
            async def get(self, url):
                fetched.append(url)
                if "/wyniki/" in url:
                    n = int(url.split("page=")[1]) if "page=" in url else 1
                    return pages.get(n, otodom_page([]))
                return OTODOM_DETAIL

        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/t.db")
            st = Settings(db)
            st.update("sources.olx.enabled", False)
            st.update("sources.otodom.search_url", "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/warszawa?by=LATEST")
            st.update("search.max_price", 6000)
            got, autos = [], []

            async def notify(row_id, listing, analysis, allow_auto=True):
                got.append(listing["ext_id"])
                autos.append(allow_auto)

            async def alert(t):
                pass

            p = Pipeline(db, st.get, F(), FakeAnalyzer(), notify, alert)
            stats = await p.backfill(hours=48)
            self.assertEqual(got, ["2", "4", "5"])          # свежие первыми; 3 дорогое; 1, 6, 7 старые
            self.assertEqual(set(autos), {False})          # без автоотправки
            self.assertEqual((stats["found"], stats["filtered"]), (4, 1))
            self.assertFalse(any("page=4" in u for u in fetched))  # дальше старых страниц не идём
            # обычный проход после backfill не шлёт старое повторно и не «запоминает» заново
            res = await p.cycle()
            self.assertEqual((res.get("sent", 0), res.get("seeded", 0)), (0, 0))
            again = await p.backfill(hours=48)
            self.assertEqual(again.get("already"), 3)


class LinksAndUpdaterTests(unittest.TestCase):
    def test_links_from_criteria(self):
        from urllib.parse import parse_qs, urlparse
        from flathunter.links import apply_auto_links, olx_url, otodom_url
        c = {"max_price": 5500, "min_area": 35, "rooms": [3, 2]}
        o = urlparse(otodom_url(c))
        self.assertEqual(classify_url(otodom_url(c)), "otodom_search")
        q = parse_qs(o.query)
        self.assertEqual((q["priceMax"], q["areaMin"], q["roomsNumber"], q["by"]), (["5500"], ["35"], ["[TWO,THREE]"], ["LATEST"]))
        x = parse_qs(urlparse(olx_url({"max_price": 5500, "rooms": [1, 5]})).query)
        self.assertEqual(x["search[order]"], ["created_at:desc"])
        self.assertEqual((x["search[filter_enum_rooms][0]"], x["search[filter_enum_rooms][1]"]), (["one"], ["four"]))
        self.assertEqual(classify_url(olx_url(c)), "olx_search")
        with tempfile.TemporaryDirectory() as tmp:
            st = Settings(DB(f"{tmp}/t.db"))
            s = st.get()
            s["search"].update(c)
            s["sources"]["olx"].update(search_url="https://www.olx.pl/mine", auto=False)
            s = apply_auto_links(s)
            self.assertIn("priceMax=5500", s["sources"]["otodom"]["search_url"])
            self.assertEqual(s["sources"]["olx"]["search_url"], "https://www.olx.pl/mine")  # своя ссылка не трогается

    def test_updater_apply(self):
        import io as _io
        import zipfile as _zip
        from pathlib import Path
        from unittest import mock
        import flathunter.updater as up
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w") as z:
            z.writestr("flat-hunter-abc/flathunter/x.py", "print(1)\n")
            z.writestr("flat-hunter-abc/start.bat", "@echo off\necho hi\n")
            z.writestr("flat-hunter-abc/requirements.txt", "httpx\n")
            z.writestr("flat-hunter-abc/data/secrets.json", "{}")  # не должно попасть
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "flathunter").mkdir()
            (root / "flathunter/x.py").write_text("print(1)\n")
            with mock.patch.object(up, "ROOT", root), mock.patch.object(up, "STATE", root / "data/v.json"), \
                    mock.patch.object(up, "NEEDS_PIP", root / "data/needs_pip"), \
                    mock.patch("httpx.get", return_value=mock.Mock(content=buf.getvalue(), status_code=200, raise_for_status=lambda: None)):
                res = up.apply({"sha": "abc", "message": "fix", "date": ""})
                self.assertEqual(sorted(res["changed"]), ["requirements.txt", "start.bat"])  # x.py не изменился
                self.assertTrue(res["pip"])
                self.assertEqual((root / "start.bat").read_bytes(), b"@echo off\r\necho hi\r\n")
                self.assertFalse((root / "data/secrets.json").exists())
                self.assertEqual(up.installed()["sha"], "abc")


class TgBotSourceTests(unittest.IsolatedAsyncioTestCase):
    def test_text_formats(self):
        from flathunter.sources.textparse import listings_from_text
        msg = ("🏠 Mieszkanie 2 pokoje, Mokotów\n💰 4 300 zł + 650 zł czynsz\n📐 46 m²\n🚪 2 pokoje\n"
               "👤 Prywatnie\nhttps://www.otodom.pl/pl/oferta/mieszkanie-ID4abc1?utm=tg")
        (l,) = listings_from_text(msg)
        self.assertEqual((l.source, l.ext_id, l.price, l.admin_rent, l.area, l.rooms, l.location),
                         ("otodom", "4abc1", 4300, 650, 46.0, 2, "Mokotów"))
        self.assertEqual(l.url, "https://www.otodom.pl/pl/oferta/mieszkanie-ID4abc1")
        self.assertEqual(l.params["seller"], "частное лицо")
        ru = "Квартира 3-комн., 58 м2, район: Wola\nЦена 5200 PLN\nОткрыть"
        (l2,) = listings_from_text(ru, ["https://www.olx.pl/d/oferta/mieszkanie-CID3-IDzz9.html?reason=x"])
        self.assertEqual((l2.source, l2.ext_id, l2.price, l2.area, l2.rooms, l2.location), ("olx", "zz9", 5200, 58.0, 3, "Wola"))
        self.assertEqual(listings_from_text("Добро пожаловать! /help"), [])

    def test_message_urls(self):
        from types import SimpleNamespace
        from telethon.tl.types import MessageEntityTextUrl
        from flathunter.tguser import listings_from_message
        text = "Kawalerka Wola 2900 zł 28 m² link"
        msg = SimpleNamespace(
            raw_text=text, message=text, date=datetime(2026, 10, 28, 9, 0, tzinfo=__import__("datetime").timezone.utc),
            entities=[MessageEntityTextUrl(offset=text.index("link"), length=4, url="https://www.olx.pl/d/oferta/k-CID3-IDq1.html")],
            buttons=[[SimpleNamespace(url="https://www.otodom.pl/pl/oferta/k-ID4q2")]],
        )
        items = listings_from_message(msg)
        self.assertEqual([(i.source, i.ext_id) for i in items], [("olx", "q1"), ("otodom", "4q2")])
        self.assertEqual((items[0].rooms, items[0].price), (1, 2900))
        self.assertTrue(items[0].posted.startswith("2026-10-28"))

    async def test_ingest_and_backfill_extra(self):
        from flathunter.sources.textparse import listings_from_text

        class Blocked:
            async def get(self, url):
                raise FetchError("HTTP 403", blocked=True)

        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/t.db")
            st = Settings(db)
            st.update("search.max_price", 6000)
            for n in ("otodom", "olx"):
                st.update(f"sources.{n}.enabled", False)
            got = []

            async def notify(row_id, listing, analysis, allow_auto=True):
                got.append((listing["ext_id"], allow_auto, listing["params"].get("_note", "")))

            async def alert(t):
                pass

            p = Pipeline(db, st.get, Blocked(), FakeAnalyzer(), notify, alert)
            a = listings_from_text("2 pokoje 4000 zł 45 m² https://www.olx.pl/d/oferta/a-CID3-IDa1.html")
            b = listings_from_text("3 pokoje 9000 zł https://www.olx.pl/d/oferta/b-CID3-IDb1.html")
            stats = await p.ingest(a + b)
            self.assertEqual((stats["sent"], stats["filtered"]), (1, 1))
            self.assertEqual(got[0][:2], ("a1", True))
            self.assertIn("недоступна", got[0][2])          # 403 -> оценка по сообщению
            self.assertEqual((await p.ingest(a))["already"], 1)

            now = datetime.now().isoformat(timespec="minutes")
            c = listings_from_text("2 pokoje 4500 zł https://www.otodom.pl/pl/oferta/c-ID4c1", posted=now)

            async def extra(hours):
                return a + c

            p.extra_backfill = extra
            res = await p.backfill(hours=48)
            self.assertEqual((res.get("sent"), res.get("already")), (1, 1))
            self.assertEqual(got[-1][:2], ("4c1", False))   # из backfill без автоотправки


class ForwardAndChannelTests(unittest.IsolatedAsyncioTestCase):
    def test_message_listings_from_ptb_message(self):
        from datetime import timezone as tz
        from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity
        from telegram import MessageOriginUser, User
        from flathunter.bot import FlatBot
        text = "🏠 2 pokoje Mokotów\n4 100 zł, 44 m²\nZobacz ogłoszenie"
        msg = Message(
            message_id=1, date=datetime(2026, 10, 28, 1, 0, tzinfo=tz.utc), chat=Chat(1, "private"), text=text,
            entities=[MessageEntity("text_link", text.index("Zobacz"), 6, url="https://www.olx.pl/d/oferta/x-CID3-IDf1.html?a=1")],
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Otodom", url="https://www.otodom.pl/pl/oferta/y-ID4f2")]]),
            forward_origin=MessageOriginUser(date=datetime(2026, 10, 27, 20, 0, tzinfo=tz.utc), sender_user=User(5, "rent", True)),
        )
        items = FlatBot.message_listings(msg)
        self.assertEqual([(i.source, i.ext_id) for i in items], [("olx", "f1"), ("otodom", "4f2")])
        self.assertEqual((items[0].price, items[0].area, items[0].rooms, items[0].location), (4100, 44.0, 2, "Mokotów"))
        self.assertTrue(items[0].posted.startswith("2026-10-2"))

    async def test_blocked_source_callback(self):
        class Blocked:
            async def get(self, url):
                raise FetchError("HTTP 403", blocked=True)

        with tempfile.TemporaryDirectory() as tmp:
            db = DB(f"{tmp}/t.db")
            st = Settings(db)
            st.update("sources.otodom.search_url", "https://www.otodom.pl/pl/wyniki/wynajem/x")
            st.update("sources.olx.enabled", False)
            alerts, blocked = [], []

            async def alert(t):
                alerts.append(t)

            async def on_blocked(name):
                blocked.append(name)
                st.update(f"sources.{name}.enabled", False)

            p = Pipeline(db, st.get, Blocked(), FakeAnalyzer(), None, alert)
            p.on_source_blocked = on_blocked
            for _ in range(3):
                await p.cycle()
            self.assertEqual((blocked, alerts), (["otodom"], []))
            self.assertEqual((await p.cycle()).get("no_sources"), 1)  # больше не долбим сайт
