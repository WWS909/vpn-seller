"""
Browser run against the real service.

  * the real backend (uvicorn) with a throw-away database,
  * real 3x-ui panels (scripts/dev-3xui.sh),
  * Chromium with a Telegram WebApp stand-in that carries properly
    signed initData — the backend cannot tell it from Telegram,
  * a fake Bot API, so nothing leaves the machine.

Every scenario checks what the person would see AND what ended up on the
panels. Screenshots of both themes and languages go to --shots.

    eval "$(scripts/dev-3xui.sh env)"
    python3 tests/e2e/e2e.py --shots /tmp/shots
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BOT = "123456:E2E-token"
SECRET = "e2e-secret-key-" + "x" * 30
ADMIN_PASS = "e2e-admin-pass"
ADMIN_TG = 555001
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok    " if ok else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(name)


# ---------------------------------------------------------------- fake Bot API
class TG:
    calls: list = []
    members: set = set()
    message_id: int = 100


class _TG(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method = self.path.rsplit("/", 1)[1]
        TG.calls.append((method, body))
        result = True
        if method == "getMe":
            result = {"id": 1, "username": "shop_e2e_bot"}
        elif method == "createInvoiceLink":
            result = "https://t.me/$invoice-" + body["payload"]
        elif method == "getChatMember":
            uid = int(body.get("user_id", 0))
            result = {"status": "administrator" if uid == 1 else "member" if uid in TG.members else "left"}
        elif method == "getChat":
            result = {"id": -100500, "title": "Orbit News", "username": "orbit_news"}
        elif method in ("sendMessage", "copyMessage"):
            TG.message_id += 1
            result = {"message_id": TG.message_id, "chat": {"id": body.get("chat_id")}}
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_cdn_cache: dict = {}


def cdn(route):
    """Fonts and the QR library come from CDNs. Fetch them from Python (which
    honours the machine's proxy) so screenshots show the real typefaces."""
    url = route.request.url
    if url not in _cdn_cache:
        try:
            r = httpx.get(url, timeout=20, headers={"User-Agent": route.request.headers.get("user-agent", "")})
            _cdn_cache[url] = (r.status_code, r.content, r.headers.get("content-type", ""))
        except httpx.HTTPError:
            _cdn_cache[url] = (503, b"", "text/plain")
    status, body, ctype = _cdn_cache[url]
    route.fulfill(status=status, body=body, content_type=ctype,
                  headers={"Access-Control-Allow-Origin": "*"})


def with_cdn(ctx):
    for host in ("https://fonts.googleapis.com/**", "https://fonts.gstatic.com/**",
                 "https://cdnjs.cloudflare.com/**"):
        ctx.route(host, cdn)
    return ctx


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- Telegram stand-in
def init_data(uid: int, lang: str, start: str = "") -> str:
    user = {"id": uid, "first_name": "Анна" if lang == "ru" else "Anna",
            "username": f"anna{uid % 1000}", "language_code": lang}
    fields = {"auth_date": str(int(time.time())), "query_id": "AAE",
              "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False)}
    if start:
        fields["start_param"] = start
    check_str = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


STUB = """
window.__opened = []; window.__invoices = []; window.__mb = {text:"", visible:false};
window.Telegram = { WebApp: {
  initData: %(init)s, initDataUnsafe: { user: %(user)s, start_param: "" },
  platform: "android", colorScheme: %(scheme)s, version: "8.0",
  safeAreaInset: {top: 24, bottom: 16}, contentSafeAreaInset: {top: 22, bottom: 0},
  ready(){}, expand(){}, disableVerticalSwipes(){}, setHeaderColor(){}, setBackgroundColor(){},
  setBottomBarColor(){}, onEvent(){}, showConfirm(t, cb){ cb(true); },
  enableClosingConfirmation(){ window.__guard = true; }, disableClosingConfirmation(){ window.__guard = false; },
  openLink(u){ window.__opened.push(u); }, openTelegramLink(u){ window.__opened.push(u); },
  openInvoice(link, cb){ window.__invoices.push(link); window.__invoiceCb = cb; },
  HapticFeedback: { impactOccurred(){}, notificationOccurred(){}, selectionChanged(){} },
  BackButton: { visible:false, show(){ this.visible = true; }, hide(){ this.visible = false; }, onClick(f){ this.cb = f; } },
  MainButton: { setText(t){ window.__mb.text = t; }, show(){ window.__mb.visible = true; },
                hide(){ window.__mb.visible = false; }, enable(){}, setParams(){}, onClick(f){ this.cb = f; } },
}};
"""


def new_page(browser, base: str, uid: int, lang: str = "ru", scheme: str = "dark", start: str = "",
             csp: bool = False, width: int = 390):
    # Playwright's wait_for_function evaluates strings, which the mini app's
    # Content-Security-Policy forbids — the CSP itself is checked in run_csp.
    ctx = browser.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2,
                              color_scheme=scheme, locale="ru-RU" if lang == "ru" else "en-US",
                              ignore_https_errors=True, bypass_csp=not csp)
    with_cdn(ctx)
    user = {"id": uid, "first_name": "Анна" if lang == "ru" else "Anna",
            "username": f"anna{uid % 1000}", "language_code": lang}
    script = STUB % {"init": json.dumps(init_data(uid, lang, start)), "user": json.dumps(user),
                     "scheme": json.dumps("light" if scheme == "light" else "dark")}
    # The real telegram-web-app.js would replace our stand-in; serve an empty file.
    ctx.route("**/telegram-web-app.js", lambda r: r.fulfill(status=200, body="/* stub */",
                                                               content_type="application/javascript"))
    page = ctx.new_page()
    page.add_init_script(script)
    page.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation',"
                         " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/app", wait_until="domcontentloaded")
    if csp:
        for _ in range(150):
            if page.evaluate("document.getElementById('brandName').textContent !== 'VPN'"):
                break
            page.wait_for_timeout(100)
    else:
        page.wait_for_function("document.getElementById('brandName').textContent !== 'VPN'", timeout=15000)
    # The opening screen lifts away by itself (about 2 s): wait for it, so a
    # screenshot shows the app, not both halfway.
    for _ in range(60):
        if not page.evaluate("document.documentElement.classList.contains('splashing')"):
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(700)
    return page, errors


ADMIN_PAGES = ("dash", "setup", "stats", "orders", "sources", "plans", "warm", "promos", "gifts", "users",
               "support", "refs", "casts", "reviews", "servers", "look", "docs", "settings", "logs")
OVERFLOW_JS = """() => {
  const w = document.documentElement.clientWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (!r.width || cs.visibility === 'hidden' || cs.position === 'fixed') continue;
    let p = el.parentElement, clipped = false;
    while (p && p !== document.body) {          // inside a scroll box (a wide table): fine
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') { clipped = true; break; }
      p = p.parentElement;
    }
    if (!clipped && r.right > w + 1) out.push((el.id || el.className || el.tagName) + ' ' + Math.round(r.right));
  }
  return {page: document.documentElement.scrollWidth, width: w, wide: out.slice(-5)};
}"""


def run_ui_sweep(browser, base, uid, shots):
    """Every admin page on a phone (390 px, light and English too) and every
    tab of the mini app on a small phone (360 px): nothing sticks out sideways."""
    print("UI sweep: admin on a phone, mini app at 360 px")
    for lang, scheme in (("ru", "dark"), ("en", "light")):
        page, errors = new_page(browser, base, uid, lang=lang, scheme=scheme, width=360)
        for i in range(1, 5):
            page.click(f".nav button:nth-child({i})")
            page.wait_for_timeout(350)
            res = page.evaluate(OVERFLOW_JS)
            check(f"app tab {i} fits 360 px ({lang}, {scheme})", res["page"] <= res["width"], res)
        check(f"app without errors ({lang})", not errors, errors[:3])
        page.context.close()
    for scheme, lang in (("dark", "ru"), ("light", "en")):
        ctx = with_cdn(browser.new_context(viewport={"width": 390, "height": 844}, locale="ru-RU",
                                           color_scheme=scheme))
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e, errs=errors: errs.append(str(e)))
        page.add_init_script(f"try {{ localStorage.setItem('kn_admin_lang', '{lang}'); }} catch (e) {{}}")
        page.goto(base + "/admin", wait_until="domcontentloaded")
        page.wait_for_selector("#loginBox:not([hidden])")
        page.fill("#loginPass", ADMIN_PASS)
        page.click(".lcard .btn")
        page.wait_for_selector(".kpis", timeout=10000)
        bad = []
        for name in ADMIN_PAGES:
            page.goto(base + "/admin#" + name)
            page.wait_for_timeout(700)
            res = page.evaluate(OVERFLOW_JS)
            if res["page"] > res["width"]:
                bad.append(f"{name}: {res['page']} {res['wide']}")
                shot(page, shots, f"80-overflow-{name}-{scheme}")
        check(f"every admin page fits a phone ({scheme}, {lang})", not bad, bad)
        check(f"admin pages without script errors ({scheme})", not errors, errors[:3])
        ctx.close()


def run_csp(browser, base, uid, shots):
    """The mini app with its real Content-Security-Policy: every screen opens,
    the QR code draws, and the browser reports no blocked script or request."""
    print("Mini app under its Content-Security-Policy (360 px)")
    page, errors = new_page(browser, base, uid, csp=True, width=360)
    for i in range(1, 5):
        page.click(f".nav button:nth-child({i})")
        page.wait_for_timeout(350)
    page.click(".nav button:nth-child(1)")
    page.wait_for_timeout(300)
    check("the app ran without script errors", not errors, errors[:3])
    check("nothing was blocked by the CSP", page.evaluate("window.__csp.length") == 0,
          page.evaluate("window.__csp"))
    check("the QR library is the shop's own copy", page.evaluate("typeof qrcode") == "function")
    width = page.evaluate("document.documentElement.scrollWidth")
    check("no sideways scroll at 360 px", width <= 360, width)
    shot(page, shots, "70-app-360-csp")
    page.context.close()


# ---------------------------------------------------------------- the run
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    need = ["XUI_TEST_V3_URL", "XUI_TEST_V3_TOKEN", "XUI_TEST_V2_URL", "XUI_TEST_V2_USER"]
    if any(not os.getenv(k) for k in need):
        print("start the panels first: eval \"$(scripts/dev-3xui.sh env)\"")
        return 2

    tg_srv = ThreadingHTTPServer(("127.0.0.1", 0), _TG)
    threading.Thread(target=tg_srv.serve_forever, daemon=True).start()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "BOT_TOKEN": BOT, "SECRET_KEY": SECRET, "ADMIN_PASSWORD": ADMIN_PASS,
           "PUBLIC_URL": base, "DATABASE_URL": f"sqlite:///{tmp}/e2e.db", "DATA_DIR": tmp,
           "TG_API_BASE": f"http://127.0.0.1:{tg_srv.server_address[1]}", "SHOP_NO_JOBS": "1",
           "BOT_SETUP": "0", "PAYMENTS_TEST_MODE": "0", "CARD_GATEWAY": "", "LOG_LEVEL": "WARNING",
           "ADMIN_TELEGRAM_IDS": str(ADMIN_TG), "NALOG_INN": "500100732259", "NALOG_PASSWORD": "e2e-nalog"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                               "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(60):
            try:
                if httpx.get(base + "/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        http = httpx.Client(base_url=base, timeout=20, trust_env=False)
        token = http.post("/api/admin/login", json={"login": "admin", "password": ADMIN_PASS}).json()["token"]
        adm = {"Authorization": f"Bearer {token}"}
        for name, cc, pre in (("Germany", "DE", "V3"), ("Finland", "FI", "V2")):
            r = http.post("/api/admin/servers", headers=adm, json={
                "name": name, "country": cc, "url": os.environ[f"XUI_TEST_{pre}_URL"],
                "api_token": os.getenv(f"XUI_TEST_{pre}_TOKEN", ""),
                "username": os.getenv(f"XUI_TEST_{pre}_USER", ""),
                "password": os.getenv(f"XUI_TEST_{pre}_PASS", ""),
                "inbound_ids": os.environ[f"XUI_TEST_{pre}_INBOUND"],
                "sub_url": os.getenv(f"XUI_TEST_{pre}_SUB", "")})
            assert r.status_code == 200, r.text
        http.post("/api/admin/settings", headers=adm, json={
            "brand_name": "Orbit VPN", "support_url": "@orbit_support"})
        http.put("/api/admin/trial", headers=adm, json={"enabled": True, "days": 3, "devices": 1})

        with sync_playwright() as pw:
            # Playwright's own Chromium, or another one: CHROME_PATH=/path/to/chrome
            browser = pw.chromium.launch(executable_path=os.getenv("CHROME_PATH") or None)
            uid = int(time.time()) % 1_000_000 + 7_000_000
            run_clean_start(browser, base, http, adm, uid, shots)
            run_app(browser, base, http, adm, uid, shots)
            run_admin(browser, base, uid, shots)
            run_receipts(browser, base, Path(tmp) / "e2e.db", shots)
            run_extras(browser, base, http, adm, uid, shots)
            run_public(browser, base, shots)
            run_marketing(browser, base, http, adm, uid, shots)
            run_look(browser, base, uid, shots)
            run_admin_in_telegram(browser, base, shots)
            run_growth(browser, base, http, adm, shots)
            run_referral_money(browser, base, http, adm, uid, shots)
            run_load_and_tune(browser, base, http, adm, uid, shots)
            run_sources(browser, base, http, adm, uid, shots)
            run_csp(browser, base, uid, shots)
            run_ui_sweep(browser, base, uid, shots)
            browser.close()
    finally:
        server.terminate()
        server.wait(10)
        tg_srv.shutdown()

    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)}")
        for f in FAILED:
            print("  -", f)
        return 1
    print("all scenarios passed")
    return 0


def shot(page, shots, name):
    if shots:
        page.wait_for_timeout(700)          # entrance animations and sheets settle
        page.screenshot(path=str(shots / f"{name}.png"), full_page=False)


def panel_page(browser, base, width=1400, height=900):
    ctx = with_cdn(browser.new_context(viewport={"width": width, "height": height}, locale="ru-RU"))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBox:not([hidden])")
    page.fill("#loginPass", ADMIN_PASS)
    page.click(".lcard .btn")
    page.wait_for_selector(".kpis", timeout=10000)
    return page, errors


def run_clean_start(browser, base, http, adm, uid, shots):
    """A fresh install sells nothing and gives nothing away; the owner sets
    it up from the launch checklist with templates."""
    print("Clean start → launch checklist → templates")
    cfg = http.get("/api/app/config").json()
    check("fresh shop has no paid plans", not [p for p in cfg["plans"] if not p["is_trial"]], str(cfg["plans"])[:200])
    check("referral program is off", cfg["referral"]["enabled"] is False)
    check("no documents", cfg["docs"] is False)
    warm = {t["key"]: t["on"] for t in http.get("/api/admin/warm", headers=adm).json()["triggers"]}
    check("no discount notices are on", not any(warm[k] for k in ("trial_end", "expired", "winback", "no_purchase")), str(warm))

    app, errs = new_page(browser, base, uid - 1)
    check("no invite row on a fresh shop", "Пригласить друзей" not in app.text_content("#quickRows"))
    app.click(".nav button:nth-child(4)")
    app.wait_for_timeout(300)
    check("empty shop says plans are coming", "Тарифы скоро появятся" in app.text_content("#subBody"))
    shot(app, shots, "40-fresh-no-plans")
    app.evaluate("sheetProfile()")
    app.wait_for_timeout(400)
    check("no documents row without documents", "Тарифы и документы" not in app.text_content("#sheet"))
    check("no JS errors (fresh app)", not errs, "; ".join(errs))
    app.context.close()

    page, errors = panel_page(browser, base)
    check("overview shows the launch card", page.locator(".setup").count() == 1)
    shot(page, shots, "41-admin-overview-launch")
    page.goto(base + "/admin#setup")
    page.wait_for_selector(".setup.page", timeout=10000)
    page.wait_for_timeout(300)
    blocks = page.locator(".setup.page").count()
    check("launch page: required and recommended", blocks == 2, str(blocks))
    req = page.eval_on_selector_all(".setup.page:first-of-type .step b", "els => els.map(e => e.textContent)")
    check("four required steps", len(req) == 4, str(req))
    shot(page, shots, "42-admin-launch")
    # "Add plans" goes to the plans page and opens the templates.
    page.click(".setup.page .step:has-text('Добавьте тарифы') .go")
    page.wait_for_selector("#tpOn0", timeout=10000)
    prices = [page.input_value(f"#tpPrice{i}") for i in range(4)]
    check("templates suggest prices", prices == ["199", "499", "899", "1490"], str(prices))
    page.fill("#tpPrice0", "149")
    shot(page, shots, "43-admin-plan-templates")
    page.click(".modal .mf .btn:last-child")
    page.wait_for_function("document.querySelectorAll('#content tbody tr').length === 4", timeout=10000)
    plans = http.get("/api/admin/plans", headers=adm).json()
    check("four plans made from the template", [p["price"] for p in plans] == [149, 499, 899, 1490], str(plans)[:200])
    http.put(f"/api/admin/plans/{plans[0]['id']}", headers=adm, json={**{k: plans[0][k] for k in (
        "name", "name_en", "days", "old_price", "badge", "devices", "traffic_gb", "enabled", "sort")}, "price": 199})
    # Documents from the template.
    page.goto(base + "/admin#docs")
    page.wait_for_selector("#dk-terms_ru", timeout=10000)
    page.fill("[data-dk=legal_owner]", "ИП Орбитов О. О.")
    page.fill("[data-dk=legal_contact]", "help@orbit.example")
    page.click(".card:has(#dk-terms_ru) .btn.ghost")
    page.click(".card:has(#dk-privacy_ru) .btn.ghost")
    check("template inserted", "{brand}" in page.input_value("#dk-terms_ru"))
    page.click("text=Вставить готовые вопросы")
    check("FAQ template inserted", page.locator(".faqrow").count() == 10)
    page.click("#tbRight .btn:last-child")
    page.wait_for_timeout(900)
    shot(page, shots, "44-admin-docs")
    pub = http.get("/api/public").json()
    check("documents are public", "Orbit VPN" in pub["docs"].get("terms", {}).get("ru", ""), str(pub["docs"])[:120])
    check("no JS errors (launch, templates, docs)", not errors, "; ".join(errors))
    page.context.close()


def run_app(browser, base, http, adm, uid, shots):
    print("Mini app — first open (ru, dark)")
    page, errors = new_page(browser, base, uid)
    check("brand comes from the server", page.text_content("#brandName") == "Orbit VPN")
    check("no subscription yet", "Нет подписки" in page.text_content("#stateTxt"))
    check("locations tile counts real servers", page.text_content("#tLoc") == "2")
    check("Telegram main button offers a plan",
          page.evaluate("window.__mb.visible && window.__mb.text.startsWith('Выбрать подписку')"),
          page.evaluate("window.__mb.text"))
    shot(page, shots, "01-home-new-ru-dark")

    page.click(".nav button:nth-child(2)")
    page.wait_for_timeout(300)
    names = page.eval_on_selector_all("#locList .loc b", "els => els.map(e => e.textContent)")
    check("locations list the servers", names == ["Germany", "Finland"], str(names))
    sub = page.eval_on_selector_all("#locList .loc .t span", "els => els.map(e => e.textContent)")
    check("country names are localised", sub == ["Германия", "Финляндия"], str(sub))
    shot(page, shots, "02-locations-ru")

    page.click(".nav button:nth-child(4)")
    page.wait_for_timeout(300)
    check("trial is offered once", page.is_visible(".trial"))
    plans = page.eval_on_selector_all(".plan .t b", "els => els.map(e => e.firstChild.textContent)")
    check("paid plans are shown", len(plans) == 4, str(plans))
    methods = page.eval_on_selector_all(".pay b", "els => els.map(e => e.textContent)")
    check("only configured payment methods", methods == ["Stars"], str(methods))
    # devices stepper changes the total
    before = page.text_content(".total b")
    page.click(".stepper button:last-of-type")
    page.wait_for_timeout(200)
    after = page.text_content(".total b")
    check("extra device raises the price", before != after, f"{before} → {after}")
    page.click(".stepper button:first-of-type")
    shot(page, shots, "03-plans-ru")

    # ---- pay with Stars: invoice → Telegram says paid → webhook → access
    chosen_days = page.evaluate("CFG.plans.find(p => p.id === pick.planId).days")
    page.click("#payBtn")
    page.wait_for_function("window.__invoices.length === 1", timeout=10000)
    link = page.evaluate("window.__invoices[0]")
    check("Stars invoice opened in Telegram", link.startswith("https://t.me/$invoice-tx:"), link)
    order_id = int(link.rsplit(":", 1)[1])
    page.evaluate("window.__invoiceCb('paid')")
    page.wait_for_timeout(400)
    check("closing asks for confirmation while the payment settles", page.evaluate("window.__guard === true"))
    shot(page, shots, "04-waiting-payment")
    # Telegram's webhook: pre-checkout, then the payment.
    from app import notify  # noqa: E402 — same secret derivation as the server
    os.environ["SECRET_KEY"] = SECRET
    from app.config import reload
    reload()
    hook = {"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()}
    stars = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]["prices"][0]["amount"]
    http.post("/api/bot/webhook", headers=hook, json={"pre_checkout_query": {
        "id": "q", "invoice_payload": f"tx:{order_id}", "currency": "XTR", "total_amount": stars}})
    ans = [c for m, c in TG.calls if m == "answerPreCheckoutQuery"][-1]
    check("pre-checkout accepted", ans["ok"] is True)
    http.post("/api/bot/webhook", headers=hook, json={"message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": stars,
                               "invoice_payload": f"tx:{order_id}", "telegram_payment_charge_id": "e2e"}}})
    page.wait_for_function("document.getElementById('stateTxt').textContent === 'Работает'", timeout=15000)
    check("subscription is active in the app", True)
    page.wait_for_timeout(300)
    check("closing confirmation released after payment", page.evaluate("window.__guard === false"))
    # the days roll up to the new value once the purchase moment is gone
    page.wait_for_function("document.getElementById('done').hidden && !document.querySelector('.days b.rolling')",
                           timeout=8000)
    days = page.text_content(".days b")
    check("days left match the plan", days in (str(chosen_days), str(chosen_days + 1)),
          f"{days} vs {chosen_days}")
    check("payment message sent to the person",
          any(m == "sendMessage" and c.get("chat_id") == uid and "Оплата прошла" in c["text"]
              for m, c in TG.calls))
    shot(page, shots, "05-home-active-ru-dark")

    # ---- connect: the subscription link really contains both servers
    page.click(".hero .btn.pri")
    page.wait_for_timeout(400)
    shot(page, shots, "06-connect")
    sub_url = page.evaluate("ME.sub_url")
    r = httpx.get(sub_url, headers={"X-HWID": "e2e-phone", "X-Device-Model": "Pixel 9",
                                     "X-Device-OS": "Android", "X-Ver-OS": "15",
                                     "User-Agent": "Happ/3.2"}, trust_env=False)
    import base64
    links = base64.b64decode(r.text).decode().splitlines()
    check("one link carries both servers", len(links) == 2, str(len(links)))
    from urllib.parse import parse_qs as _pq, urlsplit as _us

    def via_open(u):
        """Inside Telegram the app link goes through the shop's /open page."""
        parts = _us(u)
        assert parts.path == "/open", u
        target = _pq(parts.query)["to"][0]
        page_html = httpx.get(u, trust_env=False).text      # the page itself hands it on
        assert target in page_html.replace("&amp;", "&"), page_html[:300]
        return target
    page.click("#connBody .btn.pri")
    opened = via_open(page.evaluate("window.__opened[window.__opened.length - 1]"))
    check("“Add subscription” opens Happ with the link", opened.startswith("happ://add/" + sub_url), opened)
    # another app from the list: its own link, the shop's name after #
    chips = page.eval_on_selector_all("#connBody .app-pick .chip", "els => els.map(e => e.textContent.trim())")
    check("the app choice lists the apps", chips[:3] == ["Happ", "v2RayTun", "Incy"] and "OneXray" in chips, str(chips))
    page.click("#connBody .app-pick .chip:has-text('OneXray')")
    shot(page, shots, "06b-connect-onexray")
    page.click("#connBody .btn.pri")
    opened = via_open(page.evaluate("window.__opened[window.__opened.length - 1]"))
    from urllib.parse import quote as _q
    check("OneXray gets its own link", opened.startswith("onexray://onexray.com/sub/add?url=" + _q(sub_url, safe="") + "#"), opened)
    page.click(".sheet-x")
    page.wait_for_timeout(300)
    page.click(".hero .btn.pri")
    page.wait_for_timeout(400)
    check("the chosen app is remembered",
          page.text_content("#connBody .app-pick .chip.on").strip() == "OneXray")
    page.click("#connBody .app-pick .chip:has-text('Happ')")
    page.click(".sheet-x")

    # ---- devices tab shows the phone that fetched the subscription
    page.click("#btnSync")
    page.wait_for_timeout(800)
    page.click(".nav button:nth-child(3)")
    page.wait_for_timeout(300)
    dev = page.text_content("#devList")
    check("device from the VPN app is listed", "Pixel 9" in dev and "Android 15" in dev, dev.strip()[:80])
    check("device counter", page.text_content("#devUsed") == "1")
    shot(page, shots, "07-devices")
    page.click("#devList .x")
    page.wait_for_timeout(600)
    check("device can be removed", page.text_content("#devUsed") == "0")
    check("a removed device is listed apart, with a way back",
          page.is_visible("#devRemovedSec") and "Pixel 9" in page.text_content("#devRemoved"))
    shot(page, shots, "07b-devices-removed")
    page.click("#devRemoved .btn")
    page.wait_for_timeout(600)
    check("a removed device can be brought back", page.text_content("#devUsed") == "1"
          and page.is_hidden("#devRemovedSec"))
    page.click("#devList .x")
    page.wait_for_timeout(600)

    # ---- buy extra devices
    page.click(".nav button:nth-child(3)")
    page.click("text=Расширить лимит")
    page.wait_for_timeout(300)
    page.click(".sheet .rows .row:first-child")
    page.wait_for_function("window.__invoices.length === 2", timeout=10000)
    oid = int(page.evaluate("window.__invoices[1]").rsplit(":", 1)[1])
    stars2 = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]["prices"][0]["amount"]
    page.evaluate("window.__invoiceCb('paid')")
    http.post("/api/bot/webhook", headers=hook, json={"message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": stars2,
                               "invoice_payload": f"tx:{oid}", "telegram_payment_charge_id": "e2e2"}}})
    page.wait_for_function("ME && ME.device_limit === 4", timeout=15000)
    check("device limit raised to 4", True)

    # ---- gift for a friend, redeemed through the link
    page.click(".nav button:nth-child(1)")
    page.click("text=Подарить подписку")
    page.wait_for_timeout(300)
    shot(page, shots, "08-gift")
    page.click(".sheet .rows .row:first-child")
    page.wait_for_function("window.__invoices.length === 3", timeout=10000)
    gid = int(page.evaluate("window.__invoices[2]").rsplit(":", 1)[1])
    stars3 = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]["prices"][0]["amount"]
    page.evaluate("window.__invoiceCb('paid')")
    http.post("/api/bot/webhook", headers=hook, json={"message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": stars3,
                               "invoice_payload": f"tx:{gid}", "telegram_payment_charge_id": "e2e3"}}})
    page.wait_for_selector(".sheet .code.big", timeout=15000)
    code = page.text_content(".sheet .code.big")
    check("gift code shown after payment", len(code) == 9 and code[4] == "-", code)
    shot(page, shots, "09-gift-ready")
    page.click(".sheet-x")
    page.wait_for_timeout(300)

    friend, ferr = new_page(browser, base, uid + 1, lang="en", scheme="light")
    check("friend sees English", friend.text_content(".nav button:nth-child(1)").strip() == "Home")
    friend.goto(base + "/app?gift=" + code.replace("-", ""), wait_until="domcontentloaded")
    friend.wait_for_selector("#rcode", timeout=10000)
    check("gift link pre-fills the code", friend.input_value("#rcode") == code)
    friend.click(".sheet .btn.pri")
    friend.wait_for_function("document.getElementById('stateTxt').textContent === 'Active'", timeout=15000)
    check("friend got the subscription", True)
    # the days roll up after the gift opens: shoot the settled number
    friend.wait_for_function("document.getElementById('done').hidden && !document.querySelector('.days b.rolling')"
                             " && !document.documentElement.classList.contains('splashing')", timeout=15000)
    check("the gift's days are counted on the home screen",
          int(friend.text_content(".days b").strip() or 0) >= 28)
    shot(friend, shots, "10-home-active-en-light")
    friend.click(".nav button:nth-child(4)")
    friend.wait_for_timeout(300)
    check("trial is not offered after a gift", not friend.is_visible(".trial"))
    shot(friend, shots, "11-plans-en-light")
    friend.click(".tool:nth-child(2)")
    friend.wait_for_timeout(400)
    shot(friend, shots, "12-profile-en-light")
    check("no JS errors (friend)", not ferr, "; ".join(ferr))
    friend.context.close()

    # ---- profile: language switch
    page.click(".tool:nth-child(2)")
    page.wait_for_timeout(300)
    shot(page, shots, "13-profile-ru-dark")
    page.click("text=English")
    page.wait_for_timeout(300)
    check("language switches live", page.text_content(".nav button:nth-child(1)").strip() == "Home")
    page.click("text=Русский")
    page.click(".sheet-x")

    # ---- a review from a customer goes to moderation
    page.evaluate("sheetReviews()")
    page.wait_for_selector("#rvText", timeout=5000)
    page.fill("#rvText", "Подключилось за минуту, всё летает")
    page.click(".sheet .btn.pri")
    page.wait_for_timeout(600)
    check("review sent for moderation", "после проверки" in page.text_content("#toast"),
          page.text_content("#toast"))

    # ---- the panels really have this person
    for pre in ("V3", "V2"):
        from types import SimpleNamespace
        from app.providers import XuiPanel
        p = XuiPanel(SimpleNamespace(id=900 + len(pre), url=os.environ[f"XUI_TEST_{pre}_URL"],
                                     username=os.getenv(f"XUI_TEST_{pre}_USER", ""),
                                     password=os.getenv(f"XUI_TEST_{pre}_PASS", ""),
                                     api_token=os.getenv(f"XUI_TEST_{pre}_TOKEN", ""),
                                     inbound_ids=os.environ[f"XUI_TEST_{pre}_INBOUND"],
                                     sub_url=os.getenv(f"XUI_TEST_{pre}_SUB", ""), verify_tls=True,
                                     api_version=""))
        stats = p.traffic_by_email()
        check(f"client exists on the {pre} panel", f"tg{uid}" in stats)

    check("no JS errors (main)", not errors, "; ".join(errors))
    page.context.close()

    # ---- light theme screenshots of the fresh state
    fresh, _ = new_page(browser, base, uid + 5, lang="ru", scheme="light")
    shot(fresh, shots, "14-home-new-ru-light")
    fresh.click(".nav button:nth-child(4)")
    fresh.wait_for_timeout(300)
    shot(fresh, shots, "15-plans-ru-light")
    fresh.context.close()


def run_admin(browser, base, uid, shots):
    print("Admin panel")
    ctx = with_cdn(browser.new_context(viewport={"width": 1400, "height": 900}, device_scale_factor=1,
                                       locale="ru-RU"))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBox:not([hidden])")
    page.fill("#loginPass", "wrong-password")
    page.click(".lcard .btn")
    page.wait_for_timeout(500)
    check("wrong password is refused", "Неверный" in page.text_content("#loginNote"))
    page.fill("#loginPass", ADMIN_PASS)
    page.click(".lcard .btn")
    page.wait_for_selector(".kpis", timeout=10000)
    page.wait_for_timeout(600)
    kpis = page.eval_on_selector_all(".kpi .v", "els => els.map(e => e.textContent)")
    check("overview shows revenue from real orders", kpis[2] != "0 ₽", str(kpis))
    shot(page, shots, "20-admin-overview")

    page.goto(base + "/admin#servers")
    page.wait_for_selector(".srv")
    page.wait_for_timeout(300)
    page.click(".srv .acts .btn:first-child")
    page.wait_for_timeout(1500)
    check("server check reports the version", "3x-ui 3" in page.text_content("#content"))
    shot(page, shots, "21-admin-servers")
    page.click(".card:nth-child(2) .acts .btn:last-child")
    page.wait_for_timeout(300)
    page.click("text=Проверить подключение")
    page.wait_for_selector("#sIbs .ib", timeout=10000)
    check("inbounds are listed for picking", page.locator("#sIbs .ib").count() >= 1)
    shot(page, shots, "22-admin-server-edit")
    page.keyboard.press("Escape")
    page.evaluate("closeAll()")

    if os.getenv("MARZBAN_TEST_URL"):
        page.click("#tbRight .btn")                             # add server
        page.wait_for_selector("#sKind")
        page.click("#sKind button[data-k=marzban]")
        check("Marzban form hides the API token and sub URL", page.is_hidden("#fToken") and page.is_hidden("#fSub"))
        page.fill("#sName", "Netherlands")
        page.fill("#sCountry", "NL")
        page.fill("#sUrl", os.environ["MARZBAN_TEST_URL"] + "dashboard/")
        page.fill("#sUser", os.environ["MARZBAN_TEST_USER"])
        page.fill("#sPass", os.environ["MARZBAN_TEST_PASS"])
        page.click("text=Проверить подключение")
        page.wait_for_selector("#sIbs .ib", timeout=10000)
        check("Marzban inbounds listed by tag", "VLESS TCP" in page.text_content("#sIbs"))
        page.check("#sIbs .ib:first-child input")
        shot(page, shots, "22b-admin-marzban")
        page.click(".modal .mf .btn:last-child")
        page.wait_for_timeout(800)
        check("Marzban server saved", "Marzban" in page.text_content("#content"))

    page.goto(base + "/admin#users")
    page.wait_for_selector("#ulist tbody tr")
    page.fill("#uq", str(uid))
    page.wait_for_timeout(700)
    page.click("#ulist tbody tr")
    page.wait_for_selector(".drawer.show")
    page.wait_for_timeout(300)
    shot(page, shots, "23-admin-user")
    page.click(".drawer .acts-grid .btn:first-child")         # +7 days
    page.wait_for_timeout(1500)
    servers_n = 3 if os.getenv("MARZBAN_TEST_URL") else 2
    toast = page.text_content("#toast")
    errs = "" if f"{servers_n} ок" in toast else page.evaluate(
        "fetch('/api/admin/servers', {headers: {Authorization: 'Bearer ' + TOKEN}}).then(r => r.json())"
        ".then(d => (d.items || d).map(s => s.name + ': ' + (s.last_error || 'ok')).join('; '))")
    check("extend pushes to every server", f"{servers_n} ок" in toast, f"{toast} — {errs}")
    page.keyboard.press("Escape")

    page.goto(base + "/admin#reviews")
    page.wait_for_timeout(800)
    check("review waits for moderation in the panel",
          "всё летает" in page.text_content("#content"))

    page.goto(base + "/admin#orders")
    page.wait_for_selector("#olist tbody tr")
    check("orders are listed", page.locator("#olist tbody tr").count() >= 3)
    shot(page, shots, "24-admin-orders")

    page.goto(base + "/admin#settings")
    page.wait_for_selector("[data-k=welcome_ru]")
    shot(page, shots, "25-admin-settings")
    page.click(".langs button[data-l=en]")
    page.wait_for_timeout(500)
    check("admin switches to English", page.text_content("#pgTitle") == "Settings")
    page.goto(base + "/admin#dash")
    page.wait_for_selector(".kpis")
    page.wait_for_timeout(500)
    shot(page, shots, "26-admin-overview-en")
    check("no JS errors (admin)", not errors, "; ".join(errors))
    ctx.close()


CONTRAST_JS = """sel => {
  const lum = c => { const m = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(v => v / 255)
      .map(v => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
    return 0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]; };
  let bg = 'rgb(0,0,0)';
  for (let el = document.querySelector(sel); el; el = el.parentElement) {
    const b = getComputedStyle(el).backgroundColor;
    if (b && !b.startsWith('rgba(0, 0, 0, 0)') && b !== 'transparent') { bg = b; break; }
  }
  if (bg === 'rgb(0,0,0)') bg = getComputedStyle(document.body).backgroundColor;
  const a = lum(getComputedStyle(document.querySelector(sel)).color), b = lum(bg);
  return Math.round((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) * 10) / 10;
}"""


def http_receipt_state(base, tx_id):
    tok = httpx.post(base + "/api/admin/login", json={"login": "admin", "password": ADMIN_PASS},
                     trust_env=False).json()["token"]
    items = httpx.get(base + "/api/admin/receipts", headers={"Authorization": f"Bearer {tok}"},
                      trust_env=False).json()["items"]
    return str(next((i for i in items if i["tx_id"] == tx_id), None))


def run_extras(browser, base, http, adm, uid, shots):
    """The owner's names for payment methods, the review reward, the AI
    helper card (not connected here: it says so instead of pretending)."""
    print("Payment names, review reward, AI helper")
    labels = {"stars": {"title_ru": "Звёзды", "note_ru": "прямо в Telegram"},
              "card": {"title_ru": "Карта РФ", "note_ru": "без комиссии"}}
    r = http.post("/api/admin/settings", headers=adm, json={"pay_labels": json.dumps(labels, ensure_ascii=False),
                                                            "review_reward_days": "7"})
    check("names and reward saved", r.status_code == 200, r.text[:200])
    page, errors = new_page(browser, base, uid)
    page.click(".nav button:nth-child(4)")
    page.wait_for_timeout(500)
    check("the app shows the owner's name of a method", "прямо в Telegram" in page.text_content("#subBody"))
    shot(page, shots, "60-app-pay-names")
    page.click(".nav button:nth-child(1)")
    page.evaluate("sheetReviews()")
    page.wait_for_timeout(700)
    check("the review sheet promises the reward", "7 дней" in page.text_content(".sheet"))
    shot(page, shots, "61-app-review-reward")
    check("no JS errors (app extras)", not errors, "; ".join(errors)[:200])
    page.context.close()
    ctx, page, errors = admin_page(browser, base, shots)
    page.goto(base + "/admin#reviews")
    page.wait_for_selector("#rwDays")
    check("review reward shown in the panel", page.input_value("#rwDays") == "7")
    shot(page, shots, "62-admin-review-reward")
    page.goto(base + "/admin#support")
    page.wait_for_selector("text=ИИ-помощник")
    check("AI helper says it is not connected", "AI_API_KEY" in page.text_content("#content"))
    shot(page, shots, "63-admin-ai")
    page.goto(base + "/admin#settings")
    page.wait_for_selector("text=Названия способов оплаты")
    check("method names editable", page.input_value("[data-pl='card.title_ru']") == "Карта РФ")
    page.locator("text=Названия способов оплаты").scroll_into_view_if_needed()
    shot(page, shots, "64-admin-pay-names")
    page.fill("[data-pl='crypto.title_ru']", "Крипта USDT")
    page.click("#tbRight .btn")
    page.wait_for_timeout(700)
    got = http.get("/api/app/config").json()["pay_labels"]
    check("names saved from the panel keep the others", got.get("crypto", {}).get("title_ru") == "Крипта USDT"
          and got.get("card", {}).get("title_ru") == "Карта РФ", str(got)[:200])
    check("no JS errors (panel extras)", not errors, "; ".join(errors)[:200])
    ctx.close()


def run_receipts(browser, base, db_path, shots):
    """«Мой налог»: the admin sees every receipt, and an uncertain one is
    settled by a person — the shop never resends it by itself."""
    import sqlite3
    print("Receipts («Мой налог»)")
    t = int(time.time())
    con = sqlite3.connect(db_path)
    rows = [(9001, 299.0, "sent", "20abc1d2e3", ""), (9002, 199.0, "uncertain", "", "ответ «Мой налог» не получен: ReadTimeout"),
            (9003, 549.0, "failed", "", "ФНС не приняла ИНН или пароль «Мой налог» (401)"), (9004, 99.0, "pending", "", ""),
            (9005, 149.0, "skipped", "", "выдано вручную, оплата не подтверждена кассой — если деньги пришли, отправьте чек")]
    con.executemany("INSERT INTO receipts (tx_id, amount, name, paid_at, status, uuid, error, tries, next_try_at,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                    [(tx, a, f"Доступ к VPN-сервису — заказ №{tx}", t - 600, st, u, e, t, t) for tx, a, st, u, e in rows])
    con.commit()
    con.close()
    ctx, page, errors = admin_page(browser, base, shots)
    page.goto(base + "/admin#orders")
    page.wait_for_selector(".filters .tabs")
    page.click("text=Чеки")
    page.wait_for_selector("#olist table")
    text = page.text_content("#olist")
    check("every receipt is listed with its state",
          all(w in text for w in ("создан", "проверьте вручную", "отклонён", "в очереди")), text[:120])
    check("an uncertain receipt is explained", "не отправит его снова" in text)
    check("an uncertain receipt has no “send again”",
          page.locator("tr:has-text('9002') >> text=Отправить снова").count() == 0)
    shot(page, shots, "24-admin-receipts")
    page.once("dialog", lambda d: d.accept("20abc9"))
    page.click("tr:has-text('9002') >> text=Есть в «Мой налог»")
    page.wait_for_timeout(700)
    check("the admin settles an uncertain receipt",
          "проверьте вручную" not in page.text_content("#olist"))
    page.click("tr:has-text('9003') >> text=Отправить снова")
    page.wait_for_timeout(700)
    check("a refused receipt goes back to the queue",
          page.locator("tr:has-text('9003') >> text=в очереди").count() == 1)
    page.once("dialog", lambda d: d.accept())
    page.click("tr:has-text('9005') button:has-text('Деньги пришли')")
    try:
        page.wait_for_selector("tr:has-text('9005') >> text=в очереди", timeout=5000)
        held_ok = True
    except Exception:  # noqa: BLE001
        held_ok = False
    check("a held receipt is sent when the admin says the money came", held_ok,
          http_receipt_state(base, 9005))
    page.goto(base + "/admin#settings")
    page.wait_for_selector("text=Чеки «Мой налог»")
    check("settings say receipts are on and for which gateways",
          "включены" in page.text_content("#content") and "Lava" in page.text_content("#content"))
    page.locator("text=Чеки «Мой налог»").scroll_into_view_if_needed()
    shot(page, shots, "25-admin-receipts-settings")
    check("receipts pages without script errors", not errors, "; ".join(errors)[:200])
    ctx.close()


def run_public(browser, base, shots):
    print("Public page")
    for scheme in ("light", "dark"):
        ctx = with_cdn(browser.new_context(viewport={"width": 900, "height": 1000}, locale="ru-RU",
                                           color_scheme=scheme))
        page = ctx.new_page()
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#pricing table")
        rows = page.locator("#pricing tr").count()
        check(f"public page lists the plans ({scheme})", rows == 6, str(rows))
        check("support contact shown", "t.me/orbit_support" in page.text_content("#contact"))
        check("documents are on the page", page.locator("#terms h3").count() >= 5 and page.locator("#privacy ul li").count() == 4)
        check("the seller is shown", "ИП Орбитов" in page.text_content("#contact"))
        for sel in ("h1", "#pricing td", "p"):
            ratio = page.evaluate(CONTRAST_JS, sel)
            check(f"readable {sel} on the public page ({scheme})", ratio >= 4.5, ratio)
        shot(page, shots, f"30-public-ru-{scheme}")
        ctx.close()


def run_marketing(browser, base, http, adm, uid, shots):
    print("Warm-up, sale, referrals, channel")
    http.put("/api/admin/sale", headers=adm, json={"percent": 20, "days": 3, "title_ru": "Осенняя распродажа"})
    http.post("/api/admin/settings", headers=adm, json={"ref_enabled": "1", "top_enabled": "1", "ref_days": "7",
                                                        "ref_friend_days": "3"})
    app, errs = new_page(browser, base, uid + 20)
    check("sale banner on home", "−20%" in app.text_content("#homeDeal") and "Осенняя" in app.text_content("#homeDeal"))
    app.click(".nav button:nth-child(4)")
    app.wait_for_timeout(300)
    prices = app.eval_on_selector_all(".plan .pr b", "els => els.map(e => e.textContent)")
    check("plan prices are discounted", prices[0].replace("\u00a0", " ").startswith("159"), str(prices))
    shot(app, shots, "18-plans-sale")
    app.click(".nav button:nth-child(1)")
    app.click("text=Пригласить друзей")
    app.wait_for_selector(".reflink", timeout=10000)
    check("invite sheet shows the link", "start=r" in app.text_content(".reflink"))
    shot(app, shots, "19-invite")
    check("no JS errors (sale, invite)", not errs, "; ".join(errs))
    app.context.close()

    # Mandatory channel: the whole service is behind it.
    http.post("/api/admin/settings", headers=adm, json={"channel_id": "@orbit_news", "channel_mode": "all"})
    chk = http.post("/api/admin/channel/check", headers=adm).json()
    check("channel check in the panel", chk.get("ok") is True, str(chk))
    gated, gerrs = new_page(browser, base, uid + 21)
    gated.wait_for_selector("#gate", timeout=10000)
    check("gate shown to a non-subscriber", "Orbit News" in gated.text_content("#gate"))
    shot(gated, shots, "20a-channel-gate")
    TG.members.add(uid + 21)
    gated.click("#gate .btn.gho")
    gated.wait_for_selector("#gate", state="detached", timeout=10000)
    check("gate opens after subscribing", True)
    check("no JS errors (gate)", not gerrs, "; ".join(gerrs))
    gated.context.close()
    http.post("/api/admin/settings", headers=adm, json={"channel_mode": "off"})

    # The panel pages.
    ctx = with_cdn(browser.new_context(viewport={"width": 1400, "height": 900}, locale="ru-RU"))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBox:not([hidden])")
    page.fill("#loginPass", ADMIN_PASS)
    page.click(".lcard .btn")
    page.wait_for_selector(".kpis", timeout=10000)
    page.goto(base + "/admin#warm")
    page.wait_for_selector(".trig", timeout=10000)
    sys.path.insert(0, str(ROOT))
    from app import marketing as _mk
    check(f"{len(_mk.TRIGGERS)} system notices (with the abandoned payment)",
          page.locator(".trig").count() == len(_mk.TRIGGERS) and page.locator("#tr-unpaid").count() == 1)
    page.click("#tbRight .btn")                                # new campaign
    page.wait_for_selector("#cName")
    page.fill("#cName", "Выходные −25%")
    page.fill("#cPct", "25")
    page.wait_for_timeout(300)
    shot(page, shots, "31-admin-campaign")
    page.once("dialog", lambda d: d.accept())                   # "send to N customers?"
    page.click(".modal .mf .btn:last-child")
    page.wait_for_timeout(1500)
    check("campaign is listed", "Выходные" in page.text_content("#content"))
    page.evaluate("window.scrollTo(0, 0)")
    shot(page, shots, "32-admin-warm")
    page.goto(base + "/admin#refs")
    page.wait_for_selector("[data-rk=ref_days]", state="attached", timeout=10000)
    page.wait_for_timeout(400)
    shot(page, shots, "33-admin-refs")
    check("no JS errors (warm, refs)", not errors, "; ".join(errors))
    ctx.close()
    http.put("/api/admin/sale", headers=adm, json={"percent": 0})


def run_referral_money(browser, base, http, adm, uid, shots):
    """Money mode from the owner's side and the customer's: a friend pays,
    the inviter sees the share, asks for a payout, the owner marks it paid."""
    print("Referrals in money: share → withdrawal → paid")
    page, errors = panel_page(browser, base)
    page.goto(base + "/admin#refs")
    page.wait_for_selector("#refModes", timeout=10000)
    page.click("#refModes .mode[data-m=money]")
    page.fill("[data-rk=ref_percent]", "25")
    page.fill("[data-rk=ref_min_payout]", "30")
    page.click("#pmCard .quick button:has-text('Карта')")
    page.click("#pmCard .quick button:has-text('СБП')")
    page.click("#tbRight .btn")
    page.wait_for_timeout(900)
    v = http.get("/api/admin/settings", headers=adm).json()["values"]
    check("money mode saved", v["ref_mode"] == "money" and v["ref_percent"] == "25", v["ref_mode"])
    methods = json.loads(v["ref_payout_methods"])
    check("two payout methods", [m["kind"] for m in methods] == ["card", "sbp"], str(methods))
    shot(page, shots, "45-admin-refs-money")

    inviter = uid + 40
    a, aerr = new_page(browser, base, inviter)
    a.click("text=Пригласить друзей")
    a.wait_for_selector(".reflink", timeout=10000)
    link = a.text_content(".reflink")
    code = link.rsplit("start=", 1)[1].strip()
    a.context.close()
    friend = uid + 41
    b, berr = new_page(browser, base, friend, start=code)
    b.context.close()
    # The friend pays for a month in Stars.
    plan = [p for p in http.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]
    from app import notify  # noqa: E402
    hook = {"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()}
    fh = {"X-Init-Data": init_data(friend, "ru")}
    r = http.post("/api/app/order", headers=fh, json={"plan_id": plan["id"], "method": "stars"}).json()
    stars = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]["prices"][0]["amount"]
    http.post("/api/bot/webhook", headers=hook, json={"update_id": 990001, "message": {
        "chat": {"id": friend, "type": "private"}, "from": {"id": friend},
        "successful_payment": {"currency": "XTR", "total_amount": stars, "invoice_payload": f"tx:{r['order_id']}",
                               "telegram_payment_charge_id": "e2e-ref"}}})
    a, aerr = new_page(browser, base, inviter)
    a.click("text=Пригласить друзей")
    a.wait_for_selector(".wallet", timeout=10000)
    big = a.text_content(".wallet .big").replace("\u00a0", " ")
    check("inviter sees 25% of 199", big.startswith("49,75") or big.startswith("49.75"), big)
    check("the friend is listed as paid", "+49" in a.text_content(".frow .v"))
    shot(a, shots, "46-app-wallet")
    a.click(".wallet .btn.pri")
    a.wait_for_selector("#wdAcc")
    a.fill("#wdAcc", "4111 1111 1111 1111")
    shot(a, shots, "47-app-withdraw")
    a.click("#wdBtn")
    a.wait_for_selector(".wallet .pend", timeout=10000)
    check("request is waiting", "ждёт выплаты" in a.text_content(".wallet .pend"))
    check("balance went to the request", a.text_content(".wallet .big").replace("\u00a0", " ").startswith("0"))
    shot(a, shots, "48-app-wallet-pending")
    check("admin got the request in Telegram", any(m == "sendMessage" and c.get("chat_id") == ADMIN_TG and "a:po:" in json.dumps(c.get("reply_markup"))
                                                    for m, c in TG.calls))
    page.goto(base + "/admin#dash")
    page.goto(base + "/admin#refs")
    page.wait_for_selector("#poList .btn.ok", timeout=10000)
    shot(page, shots, "49-admin-payouts")
    page.once("dialog", lambda d: d.accept())
    page.click("#poList .btn.ok")
    page.wait_for_timeout(1200)
    paid = http.get("/api/admin/payouts", headers=adm).json()
    check("marked paid in the panel", paid and paid[0]["status"] == "paid", str(paid)[:200])
    check("inviter is told", any(m == "sendMessage" and c.get("chat_id") == inviter and "Выплата" in c.get("text", "")
                                 for m, c in TG.calls))
    check("no JS errors (money referrals)", not (errors or aerr or berr), "; ".join(errors + aerr + berr))
    a.context.close()
    page.context.close()


def run_load_and_tune(browser, base, http, adm, uid, shots):
    """The load of servers in Locations, and the speed-up command in the server form."""
    print("Server load and the speed-up command")
    page, errors = panel_page(browser, base)
    page.goto(base + "/admin#servers")
    page.wait_for_selector(".srv", timeout=10000)
    page.click(".card:first-child .acts .btn:first-child")          # check → counts people online
    page.wait_for_timeout(1500)
    page.click(".card:first-child .acts .btn:last-child")           # edit
    page.wait_for_selector("#sCap", state="attached")
    page.fill("#sCap", "150")
    page.click("details.foldbox summary")
    cmd = page.text_content("#tuneCmd")
    check("speed-up command uses the shop's own address", cmd.startswith(f"curl -fsSL {base}/scripts/tune.sh"), cmd)
    check("no relay field left in the form", page.locator("#sRelay").count() == 0)
    page.wait_for_timeout(300)
    shot(page, shots, "50-admin-server-form")
    page.click(".modal .mf .btn:last-child")
    page.wait_for_timeout(900)
    srv = http.get("/api/admin/servers", headers=adm).json()[0]
    check("capacity saved", srv["capacity"] == 150, str(srv)[:200])
    check("people online counted by the check", srv["online"] >= 0 and srv["load"], str(srv.get("load")))
    shot(page, shots, "51-admin-servers")
    check("the script is served", httpx.get(base + "/scripts/tune.sh", trust_env=False).status_code == 200)
    check("no JS errors (server form)", not errors, "; ".join(errors))
    page.context.close()

    app, aerr = new_page(browser, base, uid + 60)
    app.click(".nav button:nth-child(2)")
    app.wait_for_timeout(400)
    check("load is shown in Locations", app.locator("#locList .load").count() >= 1)
    shot(app, shots, "52-app-locations-load")
    check("no JS errors (locations)", not aerr, "; ".join(aerr))
    app.context.close()


def run_sources(browser, base, http, adm, uid, shots):
    """A labelled link for a video: made in the panel, a person comes through
    it, buys, and the report shows it. Plus the VPN check of the servers."""
    print("Source links and the VPN check")
    page, errors = panel_page(browser, base)
    page.goto(base + "/admin#sources")
    page.wait_for_selector("#content .empty, #content table", timeout=10000)
    page.click("#tbRight .btn")
    page.wait_for_selector("#srName")
    page.fill("#srName", "Ролик №5 в TikTok")
    page.fill("#srCost", "300")
    page.click(".modal .mf .btn:last-child")
    page.wait_for_selector("#content table", timeout=10000)
    for _ in range(30):                      # the save may still be on its way
        made = [r for r in http.get("/api/admin/sources", headers=adm).json()["items"] if r["id"]]
        if made:
            break
        time.sleep(0.2)
    src = made[0]
    check("link made from the name", src["code"] == "rolik-5-v-tiktok" and src["link"].endswith("start=src_" + src["code"]),
          src["link"])
    # A person comes through it and pays.
    who = uid + 80
    app, aerr = new_page(browser, base, who, start="src_" + src["code"])
    app.context.close()
    plan = [p for p in http.get("/api/app/config").json()["plans"] if not p["is_trial"]][0]
    r = http.post("/api/app/order", headers={"X-Init-Data": init_data(who, "ru")},
                  json={"plan_id": plan["id"], "method": "stars"}).json()
    stars = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]["prices"][0]["amount"]
    from app import notify  # noqa: E402
    http.post("/api/bot/webhook", headers={"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()},
              json={"update_id": 990101, "message": {"chat": {"id": who, "type": "private"}, "from": {"id": who},
                    "successful_payment": {"currency": "XTR", "total_amount": stars,
                                           "invoice_payload": f"tx:{r['order_id']}", "telegram_payment_charge_id": "src"}}})
    page.goto(base + "/admin#dash")
    page.goto(base + "/admin#sources")
    page.wait_for_selector("#content table", timeout=10000)
    page.wait_for_timeout(500)
    row = [r for r in http.get("/api/admin/sources", headers=adm).json()["items"] if r["code"] == src["code"]][0]
    check("the report counts the person and the money", row["came"] == 1 and row["paid"] == 1 and row["revenue"] == plan["price"],
          str(row))
    check("the table shows it", "Ролик №5" in page.text_content("#content"))
    shot(page, shots, "53-admin-sources")
    # The VPN check: the servers' ports from the keys.
    from app import vpncheck  # noqa: E402
    page.goto(base + "/admin#servers")
    page.wait_for_selector(".srv", timeout=10000)
    page.click(".card:first-child .acts .btn:first-child")
    page.wait_for_timeout(1500)
    srv = http.get("/api/admin/servers", headers=adm).json()[0]
    check("VPN endpoints learned from the keys", bool(srv["endpoints"]), srv["endpoints"])
    check("the panel's VPN port answers", all(vpncheck.reachable(e) for e in srv["endpoints"].split(",")), srv["endpoints"])
    check("no JS errors (sources, servers)", not (errors or aerr), "; ".join(errors + aerr))
    page.context.close()


def make_logo(path: Path) -> None:
    """A small original mark for the test: a ring and a dot."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, 255, 255), radius=56, fill=(14, 20, 34, 255))
    d.ellipse((56, 56, 200, 200), outline=(77, 163, 255, 255), width=22)
    d.ellipse((112, 112, 144, 144), fill=(242, 244, 248, 255))
    im.save(path)


def run_look(browser, base, uid, shots):
    print("Look: theme and logo")
    ctx = with_cdn(browser.new_context(viewport={"width": 1400, "height": 900}, locale="ru-RU"))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBox:not([hidden])")
    page.fill("#loginPass", ADMIN_PASS)
    page.click(".lcard .btn")
    page.wait_for_selector(".kpis", timeout=10000)
    page.goto(base + "/admin#look")
    page.wait_for_selector("#themeGrid .dot")
    # no finished bundles: the colour is built by hand, the dots only fill it in
    sys.path.insert(0, str(ROOT))
    from app import theme as _theme
    check(f"{len(_theme.PRESETS)} colour shortcuts", page.locator("#themeGrid .dot").count() == len(_theme.PRESETS))
    check("no style gallery", page.locator("#styleGrid").count() == 0)
    page.fill("#accHex", "#12AB34")
    page.wait_for_timeout(200)
    check("a hand-typed colour leaves the shortcuts", page.locator("#themeGrid .dot.on").count() == 0)
    page.click("#themeGrid .dot:nth-child(3)")                # Ocean: blue on midnight
    check("a shortcut fills in accent and background",
          page.input_value("#accHex").upper() == "#4DA3FF" and page.input_value("#sfIn") == "midnight",
          page.input_value("#accHex") + " " + page.input_value("#sfIn"))
    logo = Path(tempfile.mkdtemp()) / "logo.png"
    make_logo(logo)
    page.set_input_files("#logoIn", str(logo))
    page.wait_for_selector("#logoPv img", timeout=10000)
    check("logo uploaded and previewed", True)
    page.wait_for_timeout(500)
    shot(page, shots, "27-admin-look")
    page.click("#tbRight .btn")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    accent = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    check("the panel itself takes the new theme", accent.upper() == "#4DA3FF", accent)
    check("logo in the panel sidebar", page.locator(".logo .m img").count() == 1)
    check("no JS errors (look)", not errors, "; ".join(errors))
    ctx.close()

    app, errs = new_page(browser, base, uid + 9)
    sig = app.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--sig').trim()")
    check("the app uses the new accent", sig.upper() == "#4DA3FF", sig)
    ground = app.evaluate("getComputedStyle(document.body).backgroundColor")
    check("the app uses the midnight background", ground == "rgb(6, 9, 19)", ground)
    check("logo in the app header", app.locator("#brandMark img").count() == 1)
    shot(app, shots, "16-home-ocean")
    app.click(".nav button:nth-child(4)")
    app.wait_for_timeout(300)
    shot(app, shots, "17-plans-ocean")
    check("no JS errors (themed app)", not errs, "; ".join(errs))
    app.context.close()


def run_admin_in_telegram(browser, base, shots):
    print("Admin panel inside Telegram")
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="ru-RU")
    with_cdn(ctx)
    ctx.route("**/telegram-web-app.js", lambda r: r.fulfill(status=200, body="/* stub */",
                                                               content_type="application/javascript"))
    user = {"id": ADMIN_TG, "first_name": "Boss", "username": "boss", "language_code": "ru"}
    page = ctx.new_page()
    page.add_init_script(STUB % {"init": json.dumps(init_data(ADMIN_TG, "ru")), "user": json.dumps(user),
                                 "scheme": json.dumps("dark")})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector(".kpis", timeout=10000)
    check("admin is signed in by Telegram, no password", page.is_hidden("#loginBox"))
    page.wait_for_timeout(600)
    shot(page, shots, "28-admin-in-telegram")
    page.goto(base + "/admin#users")
    page.wait_for_selector("#ulist tbody tr")
    page.wait_for_timeout(400)
    width = page.evaluate("document.documentElement.scrollWidth")
    check("panel fits a phone screen", width <= 392, str(width))
    shot(page, shots, "29-admin-users-phone")
    check("no JS errors (admin in Telegram)", not errors, "; ".join(errors))
    ctx.close()


def start_fake_remnawave() -> str:
    """The Remnawave stand-in from the tests, on a free port."""
    import uvicorn
    sys.path.insert(0, str(ROOT / "tests"))
    from fake_remnawave import create_app
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app("rw-e2e-token"), host="127.0.0.1", port=port,
                                        log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/"


def admin_page(browser, base, shots):
    ctx = with_cdn(browser.new_context(viewport={"width": 1400, "height": 900}, locale="ru-RU"))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/admin", wait_until="domcontentloaded")
    page.wait_for_selector("#loginBox:not([hidden])")
    page.fill("#loginPass", ADMIN_PASS)
    page.click(".lcard .btn")
    page.wait_for_selector(".kpis", timeout=10000)
    return ctx, page, errors


def run_growth(browser, base, http, adm, shots):
    """Remnawave server, the support chat, the funnel."""
    print("Remnawave, support, analytics")
    rw = start_fake_remnawave()
    actx, admin, aerr = admin_page(browser, base, shots)
    admin.goto(base + "/admin#servers")
    admin.wait_for_selector(".srv")
    admin.click("#tbRight .btn")
    admin.wait_for_selector("#sKind")
    admin.click("#sKind button[data-k=remnawave]")
    check("Remnawave form: token shown, sub URL hidden", admin.is_visible("#fToken") and admin.is_hidden("#fSub"))
    check("Remnawave form asks for squads", admin.text_content("#lInb") == "Внутренние сквады")
    admin.fill("#sName", "Sweden")
    admin.fill("#sCountry", "SE")
    admin.fill("#sUrl", rw + "dashboard")
    admin.fill("#sToken", "rw-e2e-token")
    admin.click("text=Проверить подключение")
    admin.wait_for_selector("#sIbs .ib", timeout=10000)
    check("squads listed by name", "Default-Squad" in admin.text_content("#sIbs"), admin.text_content("#sIbs"))
    check("version detected", "Remnawave 3.4.4" in admin.text_content("#sTest"), admin.text_content("#sTest"))
    admin.check("#sIbs .ib:first-child input")
    shot(admin, shots, "30-admin-remnawave")
    admin.click(".modal .mf .btn:last-child")
    admin.wait_for_timeout(800)
    check("Remnawave server saved", "Remnawave" in admin.text_content("#content"))

    uid = int(time.time()) % 1_000_000 + 8_000_000
    page, errors = new_page(browser, base, uid)
    page.click(".nav button:nth-child(4)")
    page.wait_for_timeout(300)
    page.evaluate("setPlan(CFG.plans.find(p => p.days === 30 && !p.is_trial).id)")
    page.wait_for_timeout(200)
    check("no auto-renewal anywhere", page.locator(".renew, .sw").count() == 0
          and "Автосписаний нет" in page.text_content(".fine"))
    page.click("#payBtn")
    page.wait_for_function("window.__invoices.length === 1", timeout=10000)
    inv = [c for m, c in TG.calls if m == "createInvoiceLink"][-1]
    check("Stars invoice is one-time", "subscription_period" not in inv, str(inv))
    order_id = int(page.evaluate("window.__invoices[0]").rsplit(":", 1)[1])
    page.evaluate("window.__invoiceCb('paid')")
    from app import notify
    hook = {"X-Telegram-Bot-Api-Secret-Token": notify.webhook_secret()}
    http.post("/api/bot/webhook", headers=hook, json={"message": {
        "chat": {"id": uid, "type": "private"}, "from": {"id": uid},
        "successful_payment": {"currency": "XTR", "total_amount": inv["prices"][0]["amount"],
                               "invoice_payload": f"tx:{order_id}", "telegram_payment_charge_id": "rw-e2e"}}})
    page.wait_for_function("document.getElementById('stateTxt').textContent === 'Работает'", timeout=15000)
    rw_user = httpx.get(rw + "api/users/by-username/tg" + str(uid), trust_env=False,
                        headers={"Authorization": "Bearer rw-e2e-token", "X-Forwarded-Proto": "https",
                                 "X-Forwarded-For": "1.1.1.1"}).json()["response"]
    check("customer created on Remnawave", rw_user["status"] == "ACTIVE" and rw_user["telegramId"] == uid)
    sub = httpx.get(page.evaluate("ME.sub_url"), headers={"User-Agent": "Happ/3", "X-HWID": "rw-phone"},
                    trust_env=False).text
    import base64 as b64
    from urllib.parse import unquote
    links = b64.b64decode(sub).decode().splitlines()
    check("Remnawave links in the subscription", any("Sweden" in unquote(x) for x in links), str(len(links)))

    # ---- support: the app → the admin in Telegram and the panel → back
    page.evaluate("sheetSupport()")
    page.wait_for_selector("#chatIn")
    page.fill("#chatIn", "Не могу подключиться на телевизоре")
    page.click("#chatBtn")
    page.wait_for_timeout(700)
    check("question shows in the app chat", "телевизоре" in page.text_content("#chatBox"))
    to_admin = [c for m, c in TG.calls if m == "sendMessage" and c.get("chat_id") == ADMIN_TG]
    check("admin got the question in Telegram", to_admin and "телевизоре" in to_admin[-1]["text"])
    admin.goto(base + "/admin#support")
    admin.wait_for_selector("#content tbody tr", timeout=10000)
    check("panel lists the waiting thread", "ждёт ответа" in admin.text_content("#content"))
    check("support badge in the menu", admin.locator('.nav a[href="#support"] .cnt').count() == 1)
    admin.click("#content tbody tr")
    admin.wait_for_selector("#supIn")
    admin.fill("#supIn", "Установите Hiddify на телевизор и добавьте ссылку")
    shot(admin, shots, "33-admin-support")
    admin.click(".drawer .mf .btn:last-child")
    admin.wait_for_timeout(800)
    got = [c for m, c in TG.calls if m == "sendMessage" and c.get("chat_id") == uid]
    check("answer delivered from the bot", got and "Hiddify" in got[-1]["text"])
    page.evaluate("loadSupport()")
    page.wait_for_timeout(600)
    check("answer shows in the app", "Hiddify" in page.text_content("#chatBox"))
    shot(page, shots, "34-app-support")

    admin.goto(base + "/admin#stats")
    admin.wait_for_selector(".funnel", timeout=10000)
    admin.wait_for_timeout(300)
    vals = admin.eval_on_selector_all(".fn .fv b", "els => els.map(e => e.textContent)")
    check("funnel has six steps with numbers", len(vals) == 6 and int(vals[0]) >= 2, str(vals))
    check("revenue split by gateway", "Telegram Stars" in admin.text_content("#content"))
    shot(admin, shots, "35-admin-analytics")
    check("no JS errors (growth, app)", not errors, "; ".join(errors))
    check("no JS errors (growth, admin)", not aerr, "; ".join(aerr))
    page.context.close()
    actx.close()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
