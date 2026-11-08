"""
Entry point.

  /app            the mini app (opened by Telegram)
  /admin          the admin panel
  /  and /legal   public page: prices, terms, privacy, contacts
  /sub/<token>    subscription link for VPN clients
  /api/app/*      mini app API          /api/admin/*  admin API
  /api/pay/*      payment notifications /api/bot/*    Telegram webhook
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import access, clients, core, db as dbmod, jobs, notify, release, theme
from .config import settings as env
from .db import get_db
from .models import Plan, Server, User
from .routers import admin_api, app_api, bot, export, pay

WEB = Path(__file__).resolve().parent.parent / "web"
VERSION = release.version()             # the VERSION file; app/release.py

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO — Bot API URLs carry the bot token.
# Keep the HTTP clients quiet whatever LOG_LEVEL is.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


def seed(db: Session) -> None:
    """
    First start. The shop starts empty on purpose: no plans, no discounts,
    no documents — the owner decides every one of them in the panel (with
    templates to start from). Only the free trial's row exists, switched
    off, so /trial and the trial card have something to turn on.
    A server from .env is added if one is given.
    """
    if db.query(Plan).count() == 0:
        db.add(Plan(name="Пробный период", name_en="Free trial", days=3, price=0, devices=1,
                    is_trial=True, enabled=False, sort=0))
        db.commit()
    if env.xui_url and db.query(Server).count() == 0:
        from .providers import normalize_url
        db.add(Server(name="Server 1", url=normalize_url(env.xui_url), username=env.xui_username,
                      password=env.xui_password, api_token=env.xui_token,
                      inbound_ids=env.xui_inbounds, sub_url=env.xui_sub_url,
                      verify_tls=env.xui_verify_tls))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbmod.init_engine()
    dbmod.create_all()
    from . import migrate
    migrate.run(dbmod.engine)          # columns added by newer versions
    db = dbmod.SessionLocal()
    try:
        seed(db)
        from . import paycfg
        paycfg.apply(db)               # payment keys entered in the panel, over .env
        from . import billing
        stuck = billing.recover_interrupted(db)
        if stuck:
            core.audit(db, "Interrupted orders queued for retry", "system", str(stuck),
                       level="warn")
        for problem in env.problems():
            core.log.warning("CONFIG: %s", problem["text"])
        if env.bot_setup and env.bot_token and env.https and not os.getenv("SHOP_NO_JOBS"):
            for line in notify.setup(db):
                core.log.info("bot %s", line)
    finally:
        db.close()
    tasks = [] if os.getenv("SHOP_NO_JOBS") else jobs.start()
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="VPN Seller", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(app_api.router)
app.include_router(export.router)          # before admin_api: /users/export is not /users/{tg_id}
app.include_router(admin_api.router)
app.include_router(pay.router)
app.include_router(bot.router)


class BodyLimit:
    """Refuse request bodies over a few megabytes before they are read. The
    biggest real one is a logo (2 MB, sent as base64); without a limit
    anybody could make the server read gigabytes into memory."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.limit:
            await PlainTextResponse("request too large", status_code=413)(scope, receive, send)
            return
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    raise HTTPException(413, "request too large")
            return message

        await self.app(scope, limited, send)


app.add_middleware(BodyLimit, limit=6 * 1024 * 1024)

# The mini app runs with the person's Telegram signature in memory: no
# script from anywhere but the shop and Telegram, and it may talk to the
# shop only — a tampered third-party file could not send the signature away.
APP_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob:; "
           "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path == "/app":
        response.headers.setdefault("Content-Security-Policy", APP_CSP)
    if request.url.path.startswith("/admin"):
        # Only Telegram may frame the panel (Telegram Web shows mini apps
        # in an iframe; admins open the panel from the bot).
        response.headers.setdefault("Content-Security-Policy",
                                    "frame-ancestors 'self' https://web.telegram.org "
                                    "https://*.telegram.org")
    return response


# ---------------------------------------------------------------- pages
_pages: dict[str, str] = {}
# A built page is kept while the settings it was built from stay the same
# (the very same dict, see core.snapshot); any change in the panel builds it
# anew. The browser keeps it too and only asks "still the same?" (ETag):
# a repeated opening of the app does not download it again.
_built: dict[str, tuple] = {}


def _page(request: Request, db: Session, name: str, kind: str) -> Response:
    """The page with the configured theme, logo and name already in it."""
    dev = bool(os.getenv("SHOP_DEV"))
    if name not in _pages or dev:
        _pages[name] = (WEB / name).read_text(encoding="utf-8")
    snap = core.snapshot(db)
    hit = _built.get(name)
    if dev or hit is None or hit[0] is not snap:
        body = theme.inject(db, _pages[name], kind).encode("utf-8")
        # Packed once per build: the app is ~450 KB as text and ~100 KB packed,
        # which is what a phone on a weak network waits for before anything shows.
        hit = (core.snapshot(db), body, '"' + hashlib.sha256(body).hexdigest()[:24] + '"',
               gzip.compress(body, 6))
        if not dev:
            _built[name] = hit
    headers = {"Cache-Control": "no-cache", "ETag": hit[2], "Vary": "Accept-Encoding"}
    if hit[2] in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(hit[3], media_type="text/html; charset=utf-8",
                        headers={**headers, "Content-Encoding": "gzip"})
    return Response(hit[1], media_type="text/html; charset=utf-8", headers=headers)


@app.get("/app", include_in_schema=False)
def miniapp(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "index.html", "app")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, "admin.html", "admin")


@app.get("/", include_in_schema=False)
@app.get("/legal", include_in_schema=False)
def legal_page(request: Request, db: Session = Depends(get_db)):
    # The root must open: payment providers check the shop by this address.
    return _page(request, db, "legal.html", "legal")


# Telegram opens only http(s) links from a mini app: a link like happ://add/...
# is passed through this page, which then hands it to the phone. Only the
# known VPN apps' schemes, so it cannot be used to send people elsewhere.
_OPEN_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0c10;color:#eef0f4;
font:16px/1.45 system-ui,-apple-system,sans-serif;text-align:center;padding:24px;box-sizing:border-box}}
@media(prefers-color-scheme:light){{body{{background:#f5f6f8;color:#15171c}}p{{color:#5b616e}}}}
a.b{{display:inline-block;margin-top:18px;padding:15px 26px;border-radius:14px;background:#3b82f6;color:#fff;
text-decoration:none;font-weight:600}}p{{color:#9aa0ab;max-width:320px;margin:10px auto 0}}
h1{{font-size:20px;margin:0}}</style></head><body><div><h1>{title}</h1><p>{note}</p>
<a class="b" id="go" href="{href}">{button}</a></div>
<script>setTimeout(function(){{location.href={js};}},150);</script></body></html>"""


@app.get("/open", include_in_schema=False)
def open_app(request: Request, to: str = ""):
    scheme = to.split("://", 1)[0].lower() if "://" in to else ""
    if scheme not in clients.SCHEMES or len(to) > 2048 or any(c in to for c in "\r\n<>\"'"):
        return Response("not found", status_code=404, media_type="text/plain")
    ru = request.headers.get("accept-language", "").lower().startswith(("ru", "uk", "be", "kk"))
    body = _OPEN_PAGE.format(
        lang="ru" if ru else "en",
        title="Открываем приложение…" if ru else "Opening the app…",
        note=("Если ничего не произошло — нажмите кнопку. Приложение должно быть установлено."
              if ru else "If nothing happened, tap the button. The app has to be installed."),
        button="Открыть" if ru else "Open",
        href=html.escape(to, quote=True), js=json.dumps(to))
    return Response(body, media_type="text/html; charset=utf-8", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"})


@app.get("/brand/{name}", include_in_schema=False)
def brand_file(name: str):
    path = theme.logo_path(name)
    if not path:
        raise HTTPException(404)
    kind = theme.LOGO_TYPES[name.rsplit(".", 1)[1]]
    # The name carries a hash of the content, so it can be cached forever.
    # An SVG opened directly must not run anything: a sandbox without scripts.
    return FileResponse(path, media_type=kind, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.get("/api/public")
def public_info(db: Session = Depends(get_db)):
    """What the public page shows: brand, prices, contacts, seller."""
    from . import legal
    plans = db.query(Plan).filter(Plan.enabled.is_(True)).order_by(Plan.is_trial, Plan.sort, Plan.id).all()
    return {"brand": core.get(db, "brand_name"), "support": core.get(db, "support_url"),
            "docs": legal.public(db),
            "about": {"ru": core.get(db, "about_ru"), "en": core.get(db, "about_en")},
            "owner": core.get(db, "legal_owner"), "contact": core.get(db, "legal_contact"),
            "currency": env.currency, "symbol": env.currency_symbol,
            "bot": notify.username(), "device_price": core.get_float(db, "device_price", 50),
            "plans": [app_api.plan_out(p) for p in plans],
            "servers": len(access.servers(db))}


@app.get("/sub/{token}", include_in_schema=False)
def subscription(token: str, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.sub_token == token).first() if len(token) >= 16 else None
    if not user:
        raise HTTPException(404, "not found")
    try:
        body, headers = access.subscription(db, user, dict(request.headers))
    except access.Unavailable:
        # No panel answered: VPN apps keep their current keys on an error.
        return PlainTextResponse("panels unavailable, try later", status_code=503,
                                 headers={"Retry-After": "300", "Cache-Control": "no-store"})
    return PlainTextResponse(body, headers=headers)


VENDOR = ("qrcode.js",)


NEON_ICON = re.compile(r"[a-z]{2,12}\.webp")


@app.get("/icons/neon/{name}", include_in_schema=False)
def neon_icon(name: str):
    """The glowing icons of the Neon Night style (web/icons/neon)."""
    path = WEB / "icons" / "neon" / name
    if not NEON_ICON.fullmatch(name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=604800"})


# ---------------------------------------------------------------------------
#  The checkers inside Russia (app/probe.py, scripts/ru-probe.py)
# ---------------------------------------------------------------------------
@app.get("/probe/ru-probe.py", include_in_schema=False)
def probe_script():
    """The checker itself: public, it carries no secret (the token is given on install)."""
    return FileResponse(Path(__file__).resolve().parents[1] / "scripts" / "ru-probe.py",
                        media_type="text/x-python", headers={"Cache-Control": "no-store"})


@app.get("/api/probe/targets", include_in_schema=False)
def probe_targets(request: Request, db: Session = Depends(get_db)):
    from . import probe
    if not probe.authorized(db, request.headers.get("authorization", "")):
        raise HTTPException(401)
    return {"targets": probe.targets(db), "interval": probe.INTERVAL}


@app.post("/api/probe/report", include_in_schema=False)
async def probe_report(request: Request, db: Session = Depends(get_db)):
    from . import probe
    if not probe.authorized(db, request.headers.get("authorization", "")):
        raise HTTPException(401)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "not JSON")
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise HTTPException(400, "results: a list")
    return probe.report(db, str(body.get("probe") or ""), body["results"])


@app.get("/blacklist.txt", include_in_schema=False)
def blacklist_txt(db: Session = Depends(get_db)):
    """The shop's own blocked customers, for other shops to follow (only
    when the owner turned publishing on: IDs only, no names)."""
    from . import blacklist
    if core.get(db, "blacklist_publish") != "1":
        raise HTTPException(404)
    return PlainTextResponse(blacklist.own_list(db), headers={"Cache-Control": "public, max-age=600"})


@app.get("/vendor/{name}", include_in_schema=False)
def vendor_file(name: str):
    """Third-party scripts served by the shop itself, not from a CDN: a
    CDN that is hacked or blocked can then neither change nor break them."""
    if name not in VENDOR:
        raise HTTPException(404)
    return FileResponse(WEB / "vendor" / name, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
PUBLIC_SCRIPTS = ("tune.sh",)


@app.get("/scripts/{name}", include_in_schema=False)
def server_script(name: str):
    """The helper script for VPN servers (speed settings), served by the
    shop itself so the command in the panel works without GitHub. It holds
    no secrets."""
    if name not in PUBLIC_SCRIPTS or not (SCRIPTS / name).is_file():
        raise HTTPException(404)
    return PlainTextResponse((SCRIPTS / name).read_text(encoding="utf-8"),
                             media_type="text/x-shellscript",
                             headers={"Cache-Control": "no-cache"})


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True, "version": VERSION}


@app.get("/{name}.txt", include_in_schema=False)
def verification_file(name: str):
    """Domain verification files (payment providers, search engines):
    put them into data/verify/ and they are served from the site root."""
    if "/" in name or ".." in name or len(name) > 128:
        raise HTTPException(404)
    target = env.data_dir / "verify" / f"{name}.txt"
    if not target.is_file():
        raise HTTPException(404)
    return PlainTextResponse(target.read_text(encoding="utf-8").strip())


@app.get("/favicon.ico", include_in_schema=False)
def favicon(db: Session = Depends(get_db)):
    logo = theme.logo_url(db)
    if logo:
        return RedirectResponse(logo, status_code=302)
    th = theme.current(db)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" '
           f'rx="14" fill="{th["fill"]}"/><path d="M32 12 16 18v12c0 10 7 18 16 22 9-4 16-12 16-22V18z" '
           f'fill="none" stroke="{th["on_fill"]}" stroke-width="4" stroke-linejoin="round"/></svg>')
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})
