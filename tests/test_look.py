"""The look built from parts: colour, background, icons, cards, motion, the opening
screen and the bot texts. There are no finished bundles."""

from __future__ import annotations

import json
import re

from conftest import Fake


def test_every_colour_shortcut_is_readable_in_both_schemes():
    from app import theme
    for key, c in theme.PRESETS.items():
        th = theme.resolve(c["accent"], c["surface"])
        assert theme.contrast(th["text_dark"], th["s_card"]) >= 4.5, key
        assert theme.contrast(th["text_light"], "#FFFFFF") >= 5.5, key
        assert theme.contrast(th["on_fill"], th["fill"]) >= 4.5, key
        assert theme.contrast(th["on_fill_light"], th["fill_light"]) >= 4.5, key
        assert c["surface"] in theme.SURFACES, key


def test_there_are_no_finished_bundles(client, admin):
    """The owner builds the look himself: no style gallery, no style setting."""
    from app import core, theme
    assert not hasattr(theme, "STYLES") and "style" not in core.DEFAULTS
    d = client.get("/api/admin/theme", headers=admin).json()
    assert "styles" not in d and "style" not in d["look"]
    panel = client.get("/admin").text
    assert 'id="styleGrid"' not in panel and "pickStyle" not in panel and "matchStyle" not in panel
    # the colour is a constructor: accent and background are always there, the dots are shortcuts
    for part in ('id="accIn"', 'id="sfIn"', 'id="themeGrid"', "function setSurface(", "function matchPreset(",
                 'colorT:"Цвет"', 'colorT:"Colour"'):
        assert part in panel, part
    # an old "style" sent by an old panel is simply ignored
    assert client.post("/api/admin/settings", headers=admin, json={"style": "neon"}).status_code == 200


def test_look_reaches_the_app_page(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "neon", "font": "mono", "radius": "sharp",
        "icons": "thin", "backdrop": "grid", "motion": "calm", "effect": "snow",
        "splash": "1", "splash_text_ru": "  Быстрый VPN  "})
    assert r.status_code == 200, r.text
    page = client.get("/app").text
    tag = re.search(r"<html[^>]*>", page).group(0)
    for attr in ('data-backdrop="grid"', 'data-icons="thin"', 'data-motion="calm"', 'data-effect="snow"'):
        assert attr in tag, tag
    boot = json.loads(re.search(r"window\.KV_BRAND=(\{.*?\});</script>", page).group(1))
    assert boot["motion"] == "calm" and boot["splash"] is True
    assert boot["splash_text"]["ru"] == "Быстрый VPN"          # trimmed
    assert "#3DFF9A" in page and "JetBrains+Mono" in page
    # the public page gets the background too; the admin panel keeps its own
    assert 'data-backdrop="grid"' in client.get("/").text
    assert "data-backdrop" not in re.search(r"<html[^>]*>", client.get("/admin").text).group(0)


def test_brand_name_with_odd_characters(client, admin):
    """A backslash or "<" in the shop name must not break the page."""
    name = r"VPN \1 <Pro> & Co"
    assert client.post("/api/admin/settings", headers=admin, json={"brand_name": name}).status_code == 200
    page = client.get("/app")
    assert page.status_code == 200
    assert "<title>VPN \\1 &lt;Pro&gt; &amp; Co</title>" in page.text


def test_opening_screen_can_be_turned_off(client, admin):
    client.post("/api/admin/settings", headers=admin, json={"splash": "0", "motion": "off"})
    boot = json.loads(re.search(r"window\.KV_BRAND=(\{.*?\});</script>", client.get("/app").text).group(1))
    assert boot["splash"] is False and boot["motion"] == "off"


def test_kind_of_opening_screen(client, admin):
    """The kind is on <html> from the first byte (the screen is drawn before any
    script), it changes with the style untouched, and the preview gets it too."""
    page = client.get("/app").text
    assert 'data-splash="auto"' in re.search(r"<html[^>]*>", page).group(0)     # the style's own by default
    for kind in ("pulse", "orbit", "ring", "burst", "type", "minimal", "auto"):
        assert client.post("/api/admin/settings", headers=admin, json={"splash_kind": kind}).status_code == 200
        tag = re.search(r"<html[^>]*>", client.get("/app").text).group(0)
        assert f'data-splash="{kind}"' in tag, tag
        assert client.get("/api/admin/theme", headers=admin).json()["look"]["splash_kind"] == kind
    pv = client.post("/api/admin/theme/preview", headers=admin, json={"font": "unbounded", "splash_kind": "orbit"}).json()
    assert pv["attrs"]["splash"] == "orbit"
    # every kind has its rules in the app, and a picture and words in the panel
    app, panel = client.get("/app").text, client.get("/admin").text
    for kind in ("pulse", "orbit", "ring", "burst", "type", "minimal"):
        assert f'[data-splash="{kind}"]' in app, kind
        assert f".k-{kind}" in panel and f"spk_{kind}:" in panel and f"spkd_{kind}:" in panel, kind
    assert "window.spType" in app and 'id="spOpts"' in panel


def _png(draw, size=(900, 300), mode="RGBA", fmt="PNG"):
    import io
    from PIL import Image, ImageDraw
    im = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else (40, 90, 200))
    draw(ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def _boot(client):
    return json.loads(re.search(r"window\.KV_BRAND=(\{.*?\});</script>", client.get("/app").text).group(1))


def test_opening_screen_fits_the_logo(client, admin):
    """The logo is looked at on upload: where the drawing is (the transparent
    margins are cut off on the screen), its proportions, whether it is a full
    picture, how light it is and its main colour."""
    wide = _png(lambda d: d.ellipse((30, 60, 870, 240), fill=(16, 185, 129, 255)))
    r = client.post("/api/admin/brand/logo", headers=admin, files={"file": ("w.png", wide, "image/png")})
    assert r.status_code == 200, r.text
    info = r.json()["logo_info"]
    assert 4.2 < info["ratio"] < 5.2 and info["solid"] is False and info["color"] == "#10B981"
    x, y, w, h = info["box"]
    assert x < .06 and w > .88 and .3 < y < .45 and h < .25            # the margins of the square file
    assert _boot(client)["logo_info"]["ratio"] == info["ratio"]
    # a photo: a full rectangle, no transparency
    photo = _png(lambda d: d.ellipse((60, 60, 240, 240), fill=(255, 190, 40)), (300, 300), "RGB", "JPEG")
    info = client.post("/api/admin/brand/logo", headers=admin, files={"file": ("p.jpg", photo, "image/jpeg")}).json()["logo_info"]
    assert info["solid"] is True and abs(info["ratio"] - 1) < .02
    # black and white: no colour of its own
    mono = _png(lambda d: d.rectangle((100, 100, 200, 200), fill=(15, 15, 15, 255)), (300, 300))
    info = client.post("/api/admin/brand/logo", headers=admin, files={"file": ("m.png", mono, "image/png")}).json()["logo_info"]
    assert info["color"] is None and info["lum"] < .1
    # the analysis is not a setting anyone can write, and goes away with the logo
    client.post("/api/admin/settings", headers=admin, json={"logo_info": '{"ratio": 99}'})
    assert _boot(client)["logo_info"]["ratio"] != 99
    assert client.delete("/api/admin/brand/logo", headers=admin).status_code == 200
    assert _boot(client)["logo_info"] is None


def test_opening_screen_choices(client, admin):
    assert _boot(client)["splash_cfg"] == {"mark": "logo", "plate": "auto", "size": "m", "bg": "app",
                                           "name": "auto", "speed": "normal", "color": "style"}
    body = {"splash_mark": "letter", "splash_plate": "circle", "splash_size": "l", "splash_bg": "flood",
            "splash_name": "hide", "splash_speed": "slow", "splash_color": "#ff6b00"}
    assert client.post("/api/admin/settings", headers=admin, json=body).status_code == 200
    assert _boot(client)["splash_cfg"] == {"mark": "letter", "plate": "circle", "size": "l", "bg": "flood",
                                           "name": "hide", "speed": "slow", "color": "#FF6B00"}
    assert client.post("/api/admin/settings", headers=admin, json={"splash_color": "logo"}).status_code == 200
    for bad in ({"splash_mark": "emoji"}, {"splash_plate": "star"}, {"splash_size": "xl"}, {"splash_bg": "video"},
                {"splash_name": "maybe"}, {"splash_speed": "instant"}, {"splash_color": "red"}, {"splash_color": "#12345"}):
        assert client.post("/api/admin/settings", headers=admin, json=bad).status_code == 400, bad
    # the app knows how to use all of it, and the panel offers it
    app, panel = client.get("/app").text, client.get("/admin").text
    for part in ("window.spApply", "window.spFit", "window.spLineText", '#splash[data-bg="flood"]',
                 '#splash[data-plate="circle"]', '#splash[data-name="hide"]', "SP_SPEED", "{name}"):
        assert part in app, part
    for part in ('id="spCard"', "function drawSplashCtl", "spFitNote", "splash_mark:LOOK.sp.mark", "logo_info:LOOK.logoInfo"):
        assert part in panel, part


def test_bad_look_values_are_refused(client, admin):
    for bad in ({"backdrop": "lava"}, {"icons": "emoji"},
                {"motion": "crazy"}, {"effect": "rain"}, {"splash": "yes"}, {"font": "papyrus"},
                {"cards": "metal"}, {"splash_kind": "fireworks"}):
        assert client.post("/api/admin/settings", headers=admin, json=bad).status_code == 400, bad


def test_theme_info_lists_colours_and_parts(client, admin):
    from app import theme
    d = client.get("/api/admin/theme", headers=admin).json()
    assert [p["id"] for p in d["presets"]] == list(theme.PRESETS)
    neon = next(p for p in d["presets"] if p["id"] == "neon")
    assert neon["accent"] == "#3DFF9A" and neon["surface"] == "black" and neon["ground"] == "#000000"
    assert set(d["options"]) == {"backdrop", "icons", "motion", "effect", "cards"}
    assert "winter" in d["options"]["backdrop"] and "pixel" in d["options"]["icons"] and "lacquer" in d["options"]["cards"]
    assert d["look"]["motion"] == "lively" and d["look"]["cards"] == "solid"


def test_hand_built_colour_is_saved_as_custom(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "custom", "theme_accent": "#12AB34", "theme_surface": "winter",
        "cards": "pixel", "icons": "ornament", "backdrop": "halftone", "font": "lunar"})
    assert r.status_code == 200, r.text
    cur = client.get("/api/admin/theme", headers=admin).json()["current"]
    assert cur["preset"] == "custom" and cur["surface"] == "winter"
    page = client.get("/app").text
    tag = re.search(r"<html[^>]*>", page).group(0)
    for attr in ('data-cards="pixel"', 'data-icons="ornament"', 'data-backdrop="halftone"'):
        assert attr in tag, tag
    assert "family=Yeseva+One" in page and "#060D1E" in page    # any font with any background


def test_live_preview_gives_the_same_css_as_the_page(client, admin):
    body = {"theme_preset": "custom", "theme_accent": "#FF7A45", "theme_surface": "graphite",
            "font": "comfortaa", "radius": "round", "backdrop": "aurora", "icons": "badge",
            "motion": "lively", "effect": "none"}
    pv = client.post("/api/admin/theme/preview", headers=admin, json=body).json()
    assert "#FF7A45" in pv["css"] and "--r:24px" in pv["css"] and "Comfortaa" in pv["css"]
    assert "family=Comfortaa" in pv["font_href"]
    assert pv["attrs"] == {"backdrop": "aurora", "icons": "badge", "motion": "lively", "effect": "none",
                           "cards": "solid", "splash": "auto"}
    client.post("/api/admin/settings", headers=admin, json=body)
    page = client.get("/app").text
    assert pv["css"].split("}")[0] in page          # the same variables as the real page
    bad = client.post("/api/admin/theme/preview", headers=admin, json={**body, "theme_accent": "red"})
    assert bad.status_code == 400
    # the old colour-only call still works
    old = client.post("/api/admin/theme/preview", headers=admin, json={"accent": "#FF7A45", "surface": "black"})
    assert old.status_code == 200 and old.json()["fill"]


def test_bot_texts_go_to_telegram(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={
        "bot_about_ru": "Быстрый VPN в Telegram", "bot_desc_ru": "Подписка на VPN за минуту.",
        "bot_about_en": "Fast VPN in Telegram"})
    assert r.status_code == 200 and r.json()["bot"] == {"ru": True, "en": True}
    short = Fake.sent("setMyShortDescription")
    assert {"short_description": "Быстрый VPN в Telegram", "language_code": "ru"} in short
    assert {"short_description": "Fast VPN in Telegram", "language_code": ""} in short
    assert {"description": "Подписка на VPN за минуту.", "language_code": "ru"} in Fake.sent("setMyDescription")
    # untouched: nothing is sent again
    before = len(Fake.calls)
    client.post("/api/admin/settings", headers=admin, json={"motion": "calm"})
    assert not [m for m, _ in Fake.calls[before:] if m.startswith("setMy")]


def test_glass_cards_reach_the_app_page(client, admin):
    page = client.get("/app").text
    assert 'data-cards="solid"' in re.search(r"<html[^>]*>", page).group(0)   # the default stays solid
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "sky", "font": "geologica", "radius": "round",
        "icons": "3d", "backdrop": "orbs", "cards": "glass"})
    assert r.status_code == 200, r.text
    tag = re.search(r"<html[^>]*>", client.get("/app").text).group(0)
    assert 'data-cards="glass"' in tag and 'data-backdrop="orbs"' in tag and 'data-icons="3d"' in tag, tag
    assert client.get("/api/admin/theme", headers=admin).json()["look"]["cards"] == "glass"
    pv = client.post("/api/admin/theme/preview", headers=admin, json={
        "theme_preset": "ocean", "font": "manrope", "cards": "glass", "backdrop": "orbs"}).json()
    assert pv["attrs"]["cards"] == "glass" and pv["attrs"]["backdrop"] == "orbs"


def test_neon_parts_and_their_icons(client, admin):
    from app import theme
    assert theme.PRESETS["electric"]["surface"] == "indigo"
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "electric", "font": "unbounded-manrope", "radius": "round",
        "icons": "neon", "backdrop": "night", "cards": "neon"})
    assert r.status_code == 200, r.text
    page = client.get("/app").text
    tag = re.search(r"<html[^>]*>", page).group(0)
    for attr in ('data-icons="neon"', 'data-backdrop="night"', 'data-cards="neon"'):
        assert attr in tag, tag
    assert "#07051A" in page                                   # the indigo ground
    # every glowing picture the page refers to is really served
    names = set(re.findall(r'href="/icons/neon/([a-z]+)\.webp"', page))
    assert len(names) >= 25
    for n in names:
        res = client.get(f"/icons/neon/{n}.webp")
        assert res.status_code == 200 and res.content[:4] == b"RIFF" and res.headers["content-type"] == "image/webp", n
    for bad in ("../theme.py", "x.png", "nope.webp", "HOME.webp"):
        assert client.get(f"/icons/neon/{bad}").status_code == 404, bad
    # a custom theme may use the indigo surface
    assert client.post("/api/admin/settings", headers=admin, json={"theme_surface": "indigo"}).status_code == 200


def test_pixel_parts_and_their_icons(client, admin):
    from app import theme
    assert theme.PRESETS["arcade"]["surface"] == "retro"
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "arcade", "font": "pixel", "radius": "square",
        "icons": "pixel", "backdrop": "retro", "cards": "pixel"})
    assert r.status_code == 200, r.text
    page = client.get("/app").text
    tag = re.search(r"<html[^>]*>", page).group(0)
    for attr in ('data-icons="pixel"', 'data-backdrop="retro"', 'data-cards="pixel"'):
        assert attr in tag, tag
    assert "#100C2E" in page and "--r:0px" in page and "family=Tiny5" in page
    admin_page = client.get("/admin").text                     # the panel keeps its own type
    assert "Tiny5" not in admin_page.split("</head>")[0] and "--r:6px" in admin_page
    # the yellow stays yellow on white: pixel buttons carry their own dark edge
    css = client.post("/api/admin/theme/preview", headers=admin, json={"theme_preset": "arcade", "font": "pixel"}).json()["css"]
    assert '--sig:#FFC21A' in css
    # every icon the app can swap in has a pixel drawing, and every drawing is real
    swap = re.search(r"const HAS = new Set\(\[([^\]]+)\]", page).group(1)
    names = set(re.findall(r'"([a-z]+)"', swap))
    drawn = set(re.findall(r'<symbol id="p-([a-z]+)" viewBox="0 0 16 16">', page))
    assert names <= drawn and len(drawn) == 26, names - drawn
    for n in drawn:
        body = re.search(rf'<symbol id="p-{n}"[^>]*>(.*?)</symbol>', page).group(1)
        assert 'stroke="none"' in body and body.count("<path") >= 3, n
    # the small one-colour signs (arrows, cross, the brand shield...) are redrawn too,
    # in the colour of the place they stand in
    mono = set(re.findall(r'"([a-z]+)"', re.search(r"const MONO = new Set\(\[([^\]]+)\]", page).group(1)))
    glyphs = set(re.findall(r'<symbol id="q-([a-z]+)" viewBox="0 0 16 16"><path stroke="none" fill="currentColor"', page))
    assert mono and mono == glyphs, mono ^ glyphs
    # the light scheme is cream paper, and Telegram's bars take the page's own ground
    assert "--ground:#F4EEDC" in page and 'getPropertyValue("--ground")' in page
    # a custom theme may use the retro surface; the pieces work on their own too
    assert client.post("/api/admin/settings", headers=admin, json={"theme_surface": "retro"}).status_code == 200
    assert client.post("/api/admin/settings", headers=admin, json={"cards": "pixel", "icons": "thin"}).status_code == 200


def test_comic_parts_and_their_stickers(client, admin):
    from app import theme
    # white lettering on the comic red is readable
    th = theme.current.__globals__["_hand_picked"](theme.resolve("#E01E2C", "noir"), "comic")
    assert th["on_fill"] == "#FFFFFF" and theme.contrast("#FFFFFF", th["fill"]) >= 4.5
    r = client.post("/api/admin/settings", headers=admin, json={
        "theme_preset": "comic", "font": "comic", "radius": "sharp",
        "icons": "comic", "backdrop": "halftone", "cards": "comic"})
    assert r.status_code == 200, r.text
    page = client.get("/app").text
    tag = re.search(r"<html[^>]*>", page).group(0)
    for attr in ('data-icons="comic"', 'data-backdrop="halftone"', 'data-cards="comic"'):
        assert attr in tag, tag
    assert "#15121F" in page and "family=Rubik:ital" in page and "Balsamiq" in page
    assert "--ground:#FFF4D8" in page                        # paper in the light scheme
    assert 'content:"БАЦ!"' in page and 'content:"BAM!"' in page
    # every icon the app can swap in has a sticker, and the shade they share is defined
    swap = re.search(r"const HAS = new Set\(\[([^\]]+)\]", page).group(1)
    names = set(re.findall(r'"([a-z]+)"', swap))
    drawn = set(re.findall(r'<symbol id="x-([a-z]+)" viewBox="0 0 24 24">', page))
    assert names == drawn, names ^ drawn
    assert 'id="cxDots"' in page and 'id="cxShade"' in page
    # the panel keeps its own type
    head = client.get("/admin").text.split("</head>")[0]
    assert "Balsamiq" not in head and "Rubik:ital" not in head
    assert client.post("/api/admin/settings", headers=admin, json={"theme_surface": "noir"}).status_code == 200


def test_new_year_parts(client, admin):
    from app import theme
    looks = {
        "newyear": {"theme_preset": "newyear", "font": "festive", "radius": "round",
                    "icons": "ornament", "backdrop": "winter", "cards": "frost"},
        "lunar": {"theme_preset": "lunar", "font": "lunar", "radius": "normal",
                  "icons": "fortune", "backdrop": "lunar", "cards": "lacquer"},
    }
    for key, body in looks.items():
        r = client.post("/api/admin/settings", headers=admin, json=body)
        assert r.status_code == 200, r.text
        page = client.get("/app").text
        tag = re.search(r"<html[^>]*>", page).group(0)
        for k in ("icons", "backdrop", "cards"):
            assert f'data-{k}="{body[k]}"' in tag, tag
        head = client.get("/admin").text.split("</head>")[0]
        assert "family=Nunito:wght@400;600;700;800;900" not in head and "family=Yeseva" not in head   # the panel keeps its own type
        if key == "newyear":
            assert "family=Nunito:wght@400;600;700;800;900" in page
    assert "family=Yeseva+One" in page and "#1A0A0B" in page
    swap = set(re.findall(r'"([a-z]+)"', re.search(r"const HAS = new Set\(\[([^\]]+)\]", page).group(1)))
    ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', page))
    # New Year: every icon is a drawn festive thing (no line icon inside), and every
    # gradient it paints with is defined on the page
    drawn = dict(re.findall(r'<symbol id="o-([a-z]+)"[^>]*>(.*?)</symbol>', page, re.S))
    assert set(drawn) == swap, set(drawn) ^ swap
    for n, body in drawn.items():
        assert "<use" not in body and 'viewBox' not in body, n
        assert set(re.findall(r'url\(#([A-Za-z0-9_-]+)\)', body)) <= ids, n
    # Lunar: a red diamond holding the icon of the same name
    for n in swap:
        assert re.search(rf'<symbol id="f-{n}"[^>]*>.*?<use href="#i-{n}"/>', page), n
    # the switch of icon sets must not rewrite the line icon held inside a bauble
    # (it would point the bauble at itself, and draw nothing)
    assert 'if (u.closest("symbol")) continue;' in page
    # the pictures are inlined, with nothing left to fill in
    assert 'url("{' not in page and "data:image/svg+xml," in page
    # gold on white cannot be read: on paper the Lunar accent is red, and readable
    th = theme.current.__globals__["_hand_picked"](theme.resolve("#F2C14E", "lacquer"), "lunar")
    assert th["fill_light"] == "#C8102E" and theme.contrast(th["text_light"], "#FFFFFF") >= 5.5
    for surface in ("winter", "lacquer"):
        assert client.post("/api/admin/settings", headers=admin, json={"theme_surface": surface}).status_code == 200


def test_page_is_kept_and_revalidated(client, admin):
    """The built page is kept while the settings stay the same; the browser
    asks "still the same?" and gets 304; any change builds it anew."""
    r = client.get("/app")
    tag = r.headers["etag"]
    assert r.status_code == 200 and tag and r.headers["cache-control"] == "no-cache"
    again = client.get("/app", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.headers["etag"] == tag and not again.content
    assert client.post("/api/admin/settings", headers=admin, json={"brand_name": "Fresh Name"}).status_code == 200
    new = client.get("/app", headers={"If-None-Match": tag})
    assert new.status_code == 200 and new.headers["etag"] != tag and "<title>Fresh Name</title>" in new.text


def test_settings_written_elsewhere_show_up(client, db, monkeypatch):
    """Settings are kept in memory, but a write by another process (a backup
    from the server menu) still shows up once the short keep time is over."""
    from app import core
    from app.models import Setting
    assert core.get(db, "brand_name") == core.DEFAULTS["brand_name"]
    other = type(db)(bind=db.get_bind())          # another connection, past core.put
    other.add(Setting(key="brand_name", value="From Elsewhere")); other.commit(); other.close()
    monkeypatch.setattr(core, "_TTL", 0.0)
    assert core.get(db, "brand_name") == "From Elsewhere"
    core.put(db, "brand_name", "Own Write")       # this process: seen at once, whatever the keep time
    monkeypatch.setattr(core, "_TTL", 3600.0)
    assert core.get(db, "brand_name") == "Own Write"


# ---------------------------------------------------------------- fonts and icons
def test_fonts_are_grouped_and_all_have_cyrillic_families(client, admin):
    from app import theme
    fonts = client.get("/api/admin/theme/fonts", headers=admin).json()
    assert len(fonts) >= 30 and {f["kind"] for f in fonts} <= set(theme.FONT_KINDS)
    ids = {f["id"] for f in fonts}
    for plain in ("ptsans", "plex", "fira", "ubuntu", "jost", "commissioner",
                  "ptserif", "lora", "literata", "robotoslab", "oswald", "russo", "tektur", "plexmono"):
        assert plain in ids, plain
    assert all(f["app_only"] for f in fonts if f["kind"] == "fun")
    # each one loads from Google Fonts and names its families in the request
    for f in fonts:
        if f["load"]:
            assert f["d"].replace(" ", "+") in f["load"] and f["b"].replace(" ", "+") in f["load"], f["id"]
    r = client.post("/api/admin/settings", headers=admin, json={"font": "ptserif"})
    assert r.status_code == 200
    page = client.get("/app").text
    assert "PT+Serif" in page and '--font-d:"PT Serif"' in page and '--font-b:"PT Sans"' in page


def test_any_google_font_for_the_headings(client, admin):
    r = client.post("/api/admin/settings", headers=admin, json={"font": "custom", "font_custom": "  Lobster   Two "})
    assert r.status_code == 200, r.text
    page = client.get("/app").text
    assert "family=Lobster+Two&family=Golos+Text" in page
    assert '--font-d:"Lobster Two","Golos Text"' in page and '--font-b:"Golos Text"' in page
    assert "Lobster+Two" in client.get("/admin").text               # the panel takes it too
    # the live preview
    pv = client.post("/api/admin/theme/preview", headers=admin, json={
        "theme_preset": "ocean", "font": "custom", "font_custom": "Pacifico"}).json()
    assert "Pacifico" in pv["font_href"] and '"Pacifico"' in pv["css"]
    # names that are not a font family are refused; a bad stored one falls back
    for bad in ('Lobster"}body{', "a/b", "x" * 41, "<script>"):
        assert client.post("/api/admin/settings", headers=admin, json={"font_custom": bad}).status_code == 400, bad
    from app import theme
    assert theme.font_vars("custom", "normal", "app", "")[1].startswith(':root{--font-d:"Unbounded"')


def test_colour_tiles_and_ring_icons(client, admin):
    for kind in ("tiles", "ring"):
        assert client.post("/api/admin/settings", headers=admin, json={"icons": kind}).status_code == 200
        page = client.get("/app").text
        assert f'data-icons="{kind}"' in re.search(r"<html[^>]*>", page).group(0)
        assert f'[data-icons="{kind}"]' in page
    panel = client.get("/admin").text
    for part in ('["tiles", "icTiles"]', '["ring", "icRing"]', 'icTiles:"Цветные плашки"', "function useCustomFont(",
                 'fkSerif:"С засечками"', "function customFontTyped("):
        assert part in panel, part
