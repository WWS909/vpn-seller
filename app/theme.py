"""
The look of the service: accent colour, surfaces, fonts, icons, background,
motion, the opening screen and the logo.

There are no finished bundles: the owner builds the look from parts
(colour, background, font, corners, icons, cards, motion, opening screen),
each chosen on its own. The colour dots in the panel are only shortcuts
that fill in an accent and a dark background.

Everything is set in the admin panel and injected into the pages on the
server as CSS variables, so the very first paint already has the right
colours (no flash of the default theme).

Colours that carry text are not taken as they are: they are checked for
contrast and darkened or lightened until they are readable — an owner can
pick any accent without making the interface illegible.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from . import core
from .config import settings as env

# ---------------------------------------------------------------------------
#  Ready-made themes
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict] = {
    # The original amber keeps its hand-picked text colour for light mode.
    "amber":   {"accent": "#F2B441", "surface": "black",    "name": "Amber",
                "text_light": "#7A5406"},
    "emerald": {"accent": "#3DD68C", "surface": "black",    "name": "Emerald"},
    "ocean":   {"accent": "#4DA3FF", "surface": "midnight", "name": "Ocean"},
    "violet":  {"accent": "#A78BFA", "surface": "graphite", "name": "Violet"},
    "crimson": {"accent": "#FF5A5F", "surface": "black",    "name": "Crimson"},
    "mono":    {"accent": "#E6E8EC", "surface": "graphite", "name": "Mono"},
    "neon":    {"accent": "#3DFF9A", "surface": "black",    "name": "Neon"},
    "sunset":  {"accent": "#FF7A45", "surface": "black",    "name": "Sunset"},
    "gold":    {"accent": "#D9B26A", "surface": "black",    "name": "Gold"},
    "sky":     {"accent": "#8AD4FF", "surface": "midnight", "name": "Sky"},
    "electric": {"accent": "#5CE1FF", "surface": "indigo",  "name": "Electric"},
    # Arcade buttons keep their yellow on white too: they carry a dark pixel
    # edge, so the fill does not need to be dark to be seen.
    "arcade":  {"accent": "#FFD23F", "surface": "retro",    "name": "Arcade",
                "fill_light": "#FFC21A"},
    # Comic red carries white lettering with a black outline, like a comic title.
    "comic":   {"accent": "#E01E2C", "surface": "noir",     "name": "Comic", "on_fill": "#FFFFFF"},
    "newyear": {"accent": "#D7263D", "surface": "winter",   "name": "New Year", "on_fill": "#FFFFFF"},
    # Gold on red lacquer by night; on light paper the accent turns red (gold
    # on white cannot be read), the gold stays in the ornaments.
    "lunar":   {"accent": "#F2C14E", "surface": "lacquer",  "name": "Lunar", "fill_light": "#C8102E",
                "text_light": "#A50D25"},
}

# Dark surfaces: page, card, raised card, hairline, soft hairline, and two
# extra steps the admin panel uses for inputs and hovers.
SURFACES: dict[str, dict] = {
    "black":    {"ground": "#000000", "card": "#0C0D0F", "card2": "#131519",
                 "card3": "#191C21", "line": "#22252B", "line2": "#2A2E37", "soft": "#191C21"},
    "graphite": {"ground": "#111214", "card": "#1A1C20", "card2": "#212429",
                 "card3": "#272A30", "line": "#2F333A", "line2": "#383D46", "soft": "#24272C"},
    "midnight": {"ground": "#060913", "card": "#0E1422", "card2": "#141B2C",
                 "card3": "#1A2233", "line": "#253047", "line2": "#2E3A55", "soft": "#1A2233"},
    "indigo":   {"ground": "#07051A", "card": "#100B24", "card2": "#17102F",
                 "card3": "#1E1639", "line": "#2C2252", "line2": "#382C66", "soft": "#1E1639"},
    # The deep violet-blue of an old game console's screen.
    "retro":    {"ground": "#100C2E", "card": "#1A1542", "card2": "#221C52",
                 "card3": "#2A2361", "line": "#3A3180", "line2": "#463C96", "soft": "#2A2361"},
    # A night issue of a comic: ink-dark violet pages.
    "noir":     {"ground": "#15121F", "card": "#221C31", "card2": "#2A2340",
                 "card3": "#332B4C", "line": "#3D3557", "line2": "#4A4168", "soft": "#2A2340"},
    # A winter night sky.
    "winter":   {"ground": "#060D1E", "card": "#0F1A33", "card2": "#152442",
                 "card3": "#1B2D50", "line": "#25395F", "line2": "#2F4876", "soft": "#1B2D50"},
    # Red-black lacquer.
    "lacquer":  {"ground": "#1A0A0B", "card": "#2A0F12", "card2": "#351418",
                 "card3": "#401A1E", "line": "#5A2328", "line2": "#6B2C31", "soft": "#401A1E"},
}
LIGHT_GROUND = "#FFFFFF"

# Font pairs: headings and numbers / text. All of them have Cyrillic.
FONTS: dict[str, dict] = {
    "unbounded":  {"kind": "char", "name": "Unbounded + Golos", "d": "Unbounded", "b": "Golos Text",
                   "load": "family=Golos+Text:wght@400;500;600;700&family=Unbounded:wght@300;500;700"},
    "manrope":    {"kind": "sans", "name": "Manrope", "d": "Manrope", "b": "Manrope",
                   "load": "family=Manrope:wght@300;400;500;600;700"},
    "montserrat": {"kind": "sans", "name": "Montserrat + Golos", "d": "Montserrat", "b": "Golos Text",
                   "load": "family=Montserrat:wght@300;500;700&family=Golos+Text:wght@400;500;600;700"},
    "rubik":      {"kind": "sans", "name": "Rubik", "d": "Rubik", "b": "Rubik",
                   "load": "family=Rubik:wght@300;400;500;600;700"},
    "nunito":     {"kind": "sans", "name": "Nunito", "d": "Nunito", "b": "Nunito",
                   "load": "family=Nunito:wght@300;400;500;600;700"},
    "mono":       {"kind": "char", "name": "JetBrains Mono + Golos", "d": "JetBrains Mono", "b": "Golos Text",
                   "load": "family=JetBrains+Mono:wght@300;500;700&family=Golos+Text:wght@400;500;600;700"},
    "onest":      {"kind": "sans", "name": "Onest", "d": "Onest", "b": "Onest",
                   "load": "family=Onest:wght@300;400;500;600;700"},
    "exo":        {"kind": "char", "name": "Exo 2", "d": "Exo 2", "b": "Exo 2",
                   "load": "family=Exo+2:wght@300;400;500;600;700"},
    "comfortaa":  {"kind": "char", "name": "Comfortaa + Golos", "d": "Comfortaa", "b": "Golos Text",
                   "load": "family=Comfortaa:wght@300;500;700&family=Golos+Text:wght@400;500;600;700"},
    "playfair":   {"kind": "serif", "name": "Playfair + Golos", "d": "Playfair Display", "b": "Golos Text",
                   "load": "family=Playfair+Display:wght@400;500;600;700&family=Golos+Text:wght@400;500;600;700"},
    "geologica":  {"kind": "sans", "name": "Geologica + Manrope", "d": "Geologica", "b": "Manrope",
                   "load": "family=Geologica:wght@200;300;500;600&family=Manrope:wght@400;500;600;700"},
    "unbounded-manrope": {"kind": "char", "name": "Unbounded + Manrope", "d": "Unbounded", "b": "Manrope",
                          "load": "family=Unbounded:wght@300;500;700&family=Manrope:wght@400;500;600;700"},
    # Plain, familiar type: the faces of Russian and English print and of
    # good apps. All with a full Cyrillic.
    "ptsans":     {"kind": "sans", "name": "PT Sans", "d": "PT Sans", "b": "PT Sans",
                   "load": "family=PT+Sans:wght@400;700"},
    "plex":       {"kind": "sans", "name": "IBM Plex Sans", "d": "IBM Plex Sans", "b": "IBM Plex Sans",
                   "load": "family=IBM+Plex+Sans:wght@300;400;500;600;700"},
    "fira":       {"kind": "sans", "name": "Fira Sans", "d": "Fira Sans", "b": "Fira Sans",
                   "load": "family=Fira+Sans:wght@300;400;500;600;700"},
    "ubuntu":     {"kind": "sans", "name": "Ubuntu", "d": "Ubuntu", "b": "Ubuntu",
                   "load": "family=Ubuntu:wght@300;400;500;700"},
    "jost":       {"kind": "sans", "name": "Jost", "d": "Jost", "b": "Jost",
                   "load": "family=Jost:wght@300;400;500;600;700"},
    "commissioner": {"kind": "sans", "name": "Commissioner", "d": "Commissioner", "b": "Commissioner",
                     "load": "family=Commissioner:wght@300;400;500;600;700"},
    # Serifs for the headings, a calm sans for the text.
    "ptserif":    {"kind": "serif", "name": "PT Serif + PT Sans", "d": "PT Serif", "b": "PT Sans",
                   "load": "family=PT+Serif:wght@400;700&family=PT+Sans:wght@400;700"},
    "lora":       {"kind": "serif", "name": "Lora + Golos", "d": "Lora", "b": "Golos Text",
                   "load": "family=Lora:wght@400;500;600;700&family=Golos+Text:wght@400;500;600;700"},
    "literata":   {"kind": "serif", "name": "Literata + Golos", "d": "Literata", "b": "Golos Text",
                   "load": "family=Literata:wght@300;400;500;600;700&family=Golos+Text:wght@400;500;600;700"},
    "robotoslab": {"kind": "serif", "name": "Roboto Slab + Golos", "d": "Roboto Slab", "b": "Golos Text",
                   "load": "family=Roboto+Slab:wght@300;400;500;700&family=Golos+Text:wght@400;500;600;700"},
    # Headings with a character of their own.
    "oswald":     {"kind": "char", "name": "Oswald + Golos", "d": "Oswald", "b": "Golos Text",
                   "load": "family=Oswald:wght@300;400;500;700&family=Golos+Text:wght@400;500;600;700"},
    "russo":      {"kind": "char", "name": "Russo One + Golos", "d": "Russo One", "b": "Golos Text",
                   "load": "family=Russo+One&family=Golos+Text:wght@400;500;600;700"},
    "tektur":     {"kind": "char", "name": "Tektur + Golos", "d": "Tektur", "b": "Golos Text",
                   "load": "family=Tektur:wght@400;500;700&family=Golos+Text:wght@400;500;600;700"},
    "plexmono":   {"kind": "char", "name": "IBM Plex Mono + Sans", "d": "IBM Plex Mono", "b": "IBM Plex Sans",
                   "load": "family=IBM+Plex+Mono:wght@300;400;500;700&family=IBM+Plex+Sans:wght@400;500;600;700"},
    # A pixel font with a clean Cyrillic (Pixelify's д and П are not usable),
    # with a terminal font for the text: a smooth sans next to pixels looks pasted on.
    # Only for the mini app: it is sharp at sizes the app sets for it, and
    # the panel keeps its own type (it takes only the colour of a style).
    "pixel":      {"kind": "fun", "name": "Tiny5 + JetBrains Mono", "d": "Tiny5", "d2": "JetBrains Mono", "b": "JetBrains Mono",
                   "load": "family=Tiny5&family=JetBrains+Mono:wght@400;500;600;700", "app_only": True},
    # Comic lettering: the heaviest italic for titles, a hand-lettered sans for the balloons.
    "comic":      {"kind": "fun", "name": "Rubik Black Italic + Balsamiq", "d": "Rubik", "b": "Balsamiq Sans",
                   "load": "family=Rubik:ital,wght@1,700;1,900&family=Balsamiq+Sans:wght@400;700", "app_only": True},
    # New Year: round heavy letters (white with a candy-red edge in the app), a gold display serif for the Lunar one.
    "festive":    {"kind": "fun", "name": "Nunito Black", "d": "Nunito", "b": "Nunito",
                   "load": "family=Nunito:wght@400;600;700;800;900", "app_only": True},
    "lunar":      {"kind": "fun", "name": "Yeseva One + Golos", "d": "Yeseva One", "b": "Golos Text",
                   "load": "family=Yeseva+One&family=Golos+Text:wght@400;500;600;700", "app_only": True},
    "system":     {"kind": "sans", "name": "System", "d": "system-ui", "b": "system-ui", "load": ""},
}
# The groups of the picker in the panel, in this order. "custom" is any family
# from Google Fonts typed in by the owner (font_custom), for the headings.
FONT_KINDS = ("sans", "serif", "char", "fun", "custom")
CUSTOM_FONT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,39}$")
# Corner roundness: (app cards, admin cards)
RADII = {"square": (0, 6), "sharp": (8, 8), "normal": (16, 13), "round": (24, 18)}
MODES = ("auto", "dark", "light")

# The background behind the cards, the icon drawing, how much moves, and
# a seasonal effect. Each is an attribute on <html>; the pages style them.
BACKDROPS = ("glow", "aurora", "orbs", "night", "retro", "halftone", "winter", "lunar", "grid", "stars", "plain")
ICONS = ("thin", "regular", "bold", "badge", "tiles", "ring", "duo", "3d", "neon", "pixel", "comic", "ornament", "fortune")
MOTIONS = ("lively", "calm", "off")
EFFECTS = ("none", "snow")
# The kind of opening screen: "auto" follows the cards (pixel, comic, snowy and
# lacquer cards have their own); the others are the same for every look.
SPLASHES = ("auto", "pulse", "orbit", "ring", "burst", "type", "minimal")
# How the opening screen fits the brand. The colour is "style" (the accent), "logo" or a #hex.
SPLASH_OPTS = {"splash_mark": ("logo", "letter", "shield"), "splash_plate": ("auto", "tile", "circle", "none"),
               "splash_size": ("s", "m", "l"), "splash_bg": ("app", "glow", "flood"),
               "splash_name": ("auto", "show", "hide"), "splash_speed": ("fast", "normal", "slow")}
# Cards: solid, frosted glass that lets the background show through, neon
# edges, pixel blocks with a hard shadow like an old game, or comic panels.
CARDS = ("solid", "glass", "neon", "pixel", "comic", "frost", "lacquer")

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------------------------------------------------------------------------
#  Colour arithmetic (WCAG 2.x)
# ---------------------------------------------------------------------------
def rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(c: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02X}" for v in c)


def luminance(hex_: str) -> float:
    def ch(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = rgb(hex_)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = rgb(a), rgb(b)
    return to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def readable_on(color: str, ground: str, target: float = 4.5) -> str:
    """The colour moved toward black or white (away from the ground) just
    enough to reach the contrast target."""
    if contrast(color, ground) >= target:
        return color.upper()
    toward = "#000000" if luminance(ground) > 0.5 else "#FFFFFF"
    for i in range(1, 101):
        c = mix(color, toward, i / 100)
        if contrast(c, ground) >= target:
            return c
    return toward


def text_on(fill: str) -> str:
    """Dark or light text for a filled button, whichever reads better."""
    dark, light = mix(fill, "#000000", 0.88), "#FFFFFF"
    return dark if contrast(dark, fill) >= contrast(light, fill) else light


# ---------------------------------------------------------------------------
#  The theme currently configured
# ---------------------------------------------------------------------------
def current(db: Session) -> dict:
    preset = core.get(db, "theme_preset") or "amber"
    if preset in PRESETS:
        accent, surface = PRESETS[preset]["accent"], PRESETS[preset]["surface"]
    else:
        preset = "custom"
        accent = core.get(db, "theme_accent")
        surface = core.get(db, "theme_surface")
    if not HEX.match(accent or ""):
        accent = PRESETS["amber"]["accent"]
    if surface not in SURFACES:
        surface = "black"
    mode = core.get(db, "theme_mode")
    th = resolve(accent, surface, mode if mode in MODES else "auto", preset)
    return _hand_picked(th, preset)


def _hand_picked(th: dict, preset: str) -> dict:
    """Colours a ready-made theme sets by hand instead of computing them."""
    p = PRESETS.get(preset) or {}
    if p.get("text_light"):
        th["text_light"] = p["text_light"]
    if p.get("fill_light"):
        th["fill_light"] = p["fill_light"]
        th["on_fill_light"] = text_on(p["fill_light"])
    if p.get("on_fill"):
        th["on_fill"] = th["on_fill_light"] = p["on_fill"]
    return th


def resolve(accent: str, surface: str, mode: str = "auto", preset: str = "custom") -> dict:
    s = SURFACES[surface]
    accent = accent.upper()
    # The accent as a fill must stand out from the cards; the accent as text
    # must be readable on the page, in both schemes.
    fill = accent if contrast(accent, s["card"]) >= 3 else readable_on(accent, s["card"], 3)
    # On white a bright fill still works as a button (its label carries it),
    # but a near-white accent would vanish — that one goes dark.
    fill_light = accent if contrast(accent, LIGHT_GROUND) >= 1.6 \
        else readable_on(accent, LIGHT_GROUND, 4.5)
    return {
        "preset": preset, "accent": accent, "surface": surface, "mode": mode,
        "fill": fill, "on_fill": text_on(fill),
        "fill_light": fill_light, "on_fill_light": text_on(fill_light),
        "text_dark": readable_on(accent, s["card"]),
        # Accent-coloured text on white gets a margin above the minimum:
        # thin coloured type reads worse than black of the same contrast.
        "text_light": readable_on(accent, LIGHT_GROUND, 5.5),
        # Faint grey text stays readable on the lighter surfaces too.
        "muted": readable_on("#787F8E", s["card2"]),
        "rgb": ",".join(str(v) for v in rgb(fill)),
        **{f"s_{k}": v for k, v in s.items()},
    }


def font_css(db: Session, page: str) -> tuple[str, str]:
    """(<link> to load the font, CSS variables) for the chosen font pair."""
    href, vars_ = font_vars(core.get(db, "font"), core.get(db, "radius"), page, core.get(db, "font_custom"))
    # Not render-blocking: the opening screen shows at once in the system
    # font and the chosen one takes over as soon as it arrives.
    link = (f'<link rel="preload" as="style" href="{href}" onload="this.onload=null;this.rel=\'stylesheet\'">'
            f'<noscript><link rel="stylesheet" href="{href}"></noscript>') if href else ""
    return link, vars_


def custom_font(name: str) -> dict | None:
    """A family from Google Fonts typed in by the owner: it takes the headings
    and numbers, the text stays in Golos (a decorative face is hard to read
    in small text, and the signs it lacks, like ₽, come from Golos too)."""
    name = " ".join(str(name or "").split())
    if not CUSTOM_FONT.match(name):
        return None
    return {"kind": "custom", "name": name, "d": name, "d2": "Golos Text", "b": "Golos Text",
            "load": f"family={name.replace(' ', '+')}&family=Golos+Text:wght@400;500;600;700"}


def font_vars(font: str, radius_id: str, page: str, custom: str = "") -> tuple[str, str]:
    """(stylesheet URL or "", CSS variables) for a font pair and a corner size."""
    f = (custom_font(custom) if font == "custom" else FONTS.get(font)) or FONTS["unbounded"]
    if page == "admin" and f.get("app_only"):
        f = FONTS["unbounded"]
    stack = "system-ui,-apple-system,sans-serif"
    d = f'"{f["d"]}",{stack}' if f["d"] != "system-ui" else stack
    if f.get("d2"):                     # signs the display font lacks (₽, €) come from its pair
        d = f'"{f["d"]}","{f["d2"]}",{stack}'
    b = f'"{f["b"]}",{stack}' if f["b"] != "system-ui" else stack
    r_app, r_admin = RADII.get(radius_id) or RADII["normal"]
    radius = r_admin if page == "admin" else r_app
    href = f"https://fonts.googleapis.com/css2?{f['load']}&display=swap" if f["load"] else ""
    return href, f":root{{--font-d:{d};--font-b:{b};--r:{radius}px}}"


def _pick(value: str, allowed: tuple, default: str) -> str:
    return value if value in allowed else default


def look(db: Session) -> dict:
    """Background, icons, motion, effect and the opening screen."""
    return {
        "backdrop": _pick(core.get(db, "backdrop"), BACKDROPS, "glow"),
        "icons": _pick(core.get(db, "icons"), ICONS, "regular"),
        "motion": _pick(core.get(db, "motion"), MOTIONS, "lively"),
        "effect": _pick(core.get(db, "effect"), EFFECTS, "none"),
        "cards": _pick(core.get(db, "cards"), CARDS, "solid"),
        "splash": core.get(db, "splash") != "0",
        "splash_kind": _pick(core.get(db, "splash_kind"), SPLASHES, "auto"),
        "splash_cfg": splash_cfg(db),
        "logo_info": logo_info(db),
        "splash_text": {"ru": core.get(db, "splash_text_ru")[:60],
                        "en": core.get(db, "splash_text_en")[:60]},
    }


def html_attrs(lk: dict) -> str:
    return (f' data-backdrop="{lk["backdrop"]}" data-icons="{lk["icons"]}"'
            f' data-motion="{lk["motion"]}" data-effect="{lk["effect"]}"'
            f' data-cards="{lk.get("cards", "solid")}" data-splash="{lk.get("splash_kind", "auto")}"')


def preview(params: dict) -> dict:
    """What the mini app would look like with these (unsaved) settings:
    the same CSS the page would get, for the live preview in the panel."""
    preset = str(params.get("theme_preset") or "amber")
    if preset in PRESETS:
        accent, surface = PRESETS[preset]["accent"], PRESETS[preset]["surface"]
    else:
        accent = str(params.get("theme_accent") or "")
        surface = str(params.get("theme_surface") or "black")
        if not HEX.match(accent):
            raise ValueError("theme_accent: a colour like #F2B441")
        if surface not in SURFACES:
            raise ValueError("theme_surface: " + " | ".join(SURFACES))
        preset = "custom"
    th = _hand_picked(resolve(accent, surface, "auto", preset), preset)
    href, fvars = font_vars(str(params.get("font") or ""), str(params.get("radius") or ""), "app",
                            str(params.get("font_custom") or ""))
    return {
        "css": css(th, "app") + fvars, "font_href": href,
        "attrs": {"backdrop": _pick(str(params.get("backdrop")), BACKDROPS, "glow"),
                  "icons": _pick(str(params.get("icons")), ICONS, "regular"),
                  "motion": _pick(str(params.get("motion")), MOTIONS, "lively"),
                  "effect": _pick(str(params.get("effect")), EFFECTS, "none"),
                  "cards": _pick(str(params.get("cards")), CARDS, "solid"),
                  "splash": _pick(str(params.get("splash_kind")), SPLASHES, "auto")},
        "brand": {"ground": th["s_ground"], "fill": th["fill"], "on_fill": th["on_fill"],
                  "fill_light": th["fill_light"], "on_fill_light": th["on_fill_light"]},
    }


def links(db: Session) -> list[dict]:
    """The owner's own buttons (channel, chat, site) shown in the profile."""
    rows = core.get_json(db, "links_json", [])
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url", "")).strip()
        title_ru = str(r.get("ru", "")).strip()[:40]
        if not title_ru or not re.match(r"^(https://|tg://|mailto:)", url):
            continue
        out.append({"ru": title_ru, "en": str(r.get("en", "")).strip()[:40] or title_ru,
                    "url": url[:300]})
    return out[:6]


def css(th: dict, page: str) -> str:
    """CSS variables for one of the pages: app, admin or legal."""
    if page == "admin":
        return (":root{"
                f"--bg:{th['s_ground']};--surface:{th['s_card']};--surface2:{th['s_card2']};"
                f"--surface3:{th['s_card3']};--surface4:{th['s_line']};"
                f"--brd:{th['s_line']};--brd2:{th['s_line2']};--muted2:{th['muted']};"
                f"--accent:{th['fill']};--accent-txt:{th['text_dark']};--accent-rgb:{th['rgb']};"
                f"--accent-weak:rgba({th['rgb']},.13);--accent-brd:rgba({th['rgb']},.45);"
                f"--on-accent:{th['on_fill']};--warn:{th['fill']};--warn-weak:rgba({th['rgb']},.14)}}")
    dark = (f"--ground:{th['s_ground']};--card:{th['s_card']};--card-2:{th['s_card2']};"
            f"--line:{th['s_line']};--line-soft:{th['s_soft']};--txt-3:{th['muted']};")
    accent = (f"--sig:{th['fill']};--sig-rgb:{th['rgb']};--on-sig:{th['on_fill']};")
    out = (f":root{{{dark}{accent}--sig-txt:{th['text_dark']}}}"
           f":root[data-theme=\"light\"]{{--sig:{th['fill_light']};--on-sig:{th['on_fill_light']};"
           f"--sig-txt:{th['text_light']}}}")
    if page == "legal":
        # The public page follows the system scheme with a media query. The
        # theme's dark surfaces apply only in the dark scheme: in the light one
        # the page keeps its own light background (a dark background with the
        # light scheme's dark text made headings and prices unreadable).
        out = (f":root{{{accent}--sig-txt:{th['text_dark']}}}"
               f"@media not all and (prefers-color-scheme: light){{:root{{{dark}}}}}"
               "@media (prefers-color-scheme: light){:root{"
               f"--sig:{th['fill_light']};--on-sig:{th['on_fill_light']};"
               f"--sig-txt:{th['text_light']}}}}}")
    return out


# ---------------------------------------------------------------------------
#  Logo
# ---------------------------------------------------------------------------
LOGO_MAX = 2 * 1024 * 1024
LOGO_SIDE = 256          # stored size: sharp on phones, small to load
LOGO_SOURCE_MAX = 2048
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
              "svg": "image/svg+xml"}


def brand_dir() -> Path:
    d = env.data_dir / "brand"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sniff(data: bytes) -> str | None:
    """The real type of an uploaded image, by its first bytes — never by
    the name or the type the browser claims."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:1024].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return "svg"
    return None


def _normalize(data: bytes) -> bytes:
    """A raster logo becomes a square PNG of LOGO_SIDE pixels: fitted, not
    cropped, on a transparent background. Animation is refused."""
    import io

    from PIL import Image, UnidentifiedImageError
    too_big = ValueError(f"at most {LOGO_SOURCE_MAX}×{LOGO_SOURCE_MAX} pixels")
    try:
        im = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        raise too_big
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ValueError("the image cannot be read")
    # The size is in the header: checked before the pixels are decoded, so
    # a small file claiming a giant picture never fills the memory.
    if max(im.size) > LOGO_SOURCE_MAX:
        raise too_big
    if getattr(im, "n_frames", 1) > 1:
        raise ValueError("animated images are not supported")
    try:
        im.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValueError("the image cannot be read")
    im = im.convert("RGBA")
    im.thumbnail((LOGO_SIDE, LOGO_SIDE), Image.LANCZOS)
    square = Image.new("RGBA", (LOGO_SIDE, LOGO_SIDE), (0, 0, 0, 0))
    square.paste(im, ((LOGO_SIDE - im.width) // 2, (LOGO_SIDE - im.height) // 2), im)
    out = io.BytesIO()
    square.save(out, "PNG", optimize=True)
    return out.getvalue()


def save_logo(db: Session, data: bytes) -> str:
    if not data:
        raise ValueError("empty file")
    if len(data) > LOGO_MAX:
        raise ValueError("the logo must be at most 2 MB")
    kind = sniff(data)
    if not kind:
        raise ValueError("only PNG, JPEG, WebP or SVG")
    if kind != "svg":
        data, kind = _normalize(data), "png"
    name = f"logo-{hashlib.sha256(data).hexdigest()[:12]}.{kind}"
    (brand_dir() / name).write_bytes(data)
    old = core.get(db, "logo")
    core.put(db, "logo", name)
    if old and old != name:
        (brand_dir() / old).unlink(missing_ok=True)
    logo_info(db)
    return name


def splash_cfg(db: Session) -> dict:
    """The owner's choices for the opening screen, checked."""
    out = {k[7:]: _pick(core.get(db, k), allowed, core.DEFAULTS[k]) for k, allowed in SPLASH_OPTS.items()}
    colour = core.get(db, "splash_color")
    out["color"] = colour if colour in ("style", "logo") or HEX.match(colour or "") else "style"
    return out


def _main_colour(pixels: list[tuple[int, int, int]]) -> str | None:
    """The colour a logo is known by: the most used vivid one (not white,
    black or grey). None for a black-and-white logo."""
    import colorsys
    buckets: dict[tuple, list] = {}
    for r, g, b in pixels:
        key = (r >> 4, g >> 4, b >> 4)
        e = buckets.setdefault(key, [0, 0, 0, 0])
        e[0] += 1; e[1] += r; e[2] += g; e[3] += b
    best, score = None, 0.0
    for n, r, g, b in buckets.values():
        r, g, b = r / n, g / n, b / n
        h, sat, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if sat < .28 or v < .22:
            continue
        sc = n * (.4 + sat) * (.5 + v)
        if sc > score:
            best, score = (r, g, b), sc
    if not best or score < len(pixels) * .02:
        return None
    return "#%02X%02X%02X" % tuple(round(c) for c in best)


def analyze_logo(path: Path) -> dict:
    """What the logo is like, for the opening screen to fit it:
    box   — where the drawing is, as parts of the picture (the rest is transparent),
    ratio — width to height of the drawing (3 = a wide wordmark),
    solid — a full rectangle, like a photo or an app icon (no transparency),
    lum   — how light it is, 0 black … 1 white,
    color — its main vivid colour, or None."""
    if path.suffix == ".svg":
        text = path.read_text(errors="ignore")[:200_000]
        ratio = 1.0
        m = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', text)
        if m and float(m.group(2)) > 0:
            ratio = float(m.group(1)) / float(m.group(2))
        cols = [c.upper() for c in re.findall(r'#([0-9a-fA-F]{6})\b', text)]
        cols += ["%02X%02X%02X" % (int(c[0] * 17), int(c[1] * 17), int(c[2] * 17))
                 for c in (x.upper() for x in re.findall(r'#([0-9a-fA-F]{3})\b', text))
                 if all(ch in "0123456789ABCDEF" for ch in c)]
        px = [(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)) for c in cols]
        lum = (sum(.2126 * r + .7152 * g + .0722 * b for r, g, b in px) / len(px) / 255) if px else .5
        return {"box": [0, 0, 1, 1], "ratio": round(max(.2, min(ratio, 6)), 3), "solid": False,
                "lum": round(lum, 3), "color": _main_colour(px * 50) if px else None}
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(path.read_bytes())).convert("RGBA")
    w, h = im.size
    box = im.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox() or (0, 0, w, h)
    x0, y0, x1, y1 = box
    raw = im.crop(box).tobytes()
    opaque = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 4) if raw[i + 3] > 200]
    area = max(1, (x1 - x0) * (y1 - y0))
    lum = sum(.2126 * r + .7152 * g + .0722 * b for r, g, b in opaque) / max(1, len(opaque)) / 255
    return {"box": [round(x0 / w, 4), round(y0 / h, 4), round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)],
            "ratio": round((x1 - x0) / max(1, y1 - y0), 3), "solid": len(opaque) / area > .96,
            "lum": round(lum, 3), "color": _main_colour(opaque)}


def logo_info(db: Session) -> dict | None:
    """The analysis of the current logo; worked out once and kept (a logo
    uploaded before this existed gets it on first use)."""
    name = core.get(db, "logo")
    path = logo_path(name)
    if not path:
        return None
    try:
        info = json.loads(core.get(db, "logo_info") or "{}")
    except ValueError:
        info = {}
    if info.get("file") != name:
        try:
            info = {"file": name, **analyze_logo(path)}
        except Exception:                     # an odd file: the screen just shows it plainly
            info = {"file": name, "box": [0, 0, 1, 1], "ratio": 1, "solid": False, "lum": .5, "color": None}
        core.put(db, "logo_info", json.dumps(info))
    return info


def reset_brand(db: Session) -> None:
    """Back to the standard name and the shield."""
    core.put(db, "brand_name", core.DEFAULTS["brand_name"])
    remove_logo(db)


def remove_logo(db: Session) -> None:
    old = core.get(db, "logo")
    core.put(db, "logo", "")
    core.put(db, "logo_info", "")
    if old:
        (brand_dir() / old).unlink(missing_ok=True)


def logo_path(name: str) -> Path | None:
    if not re.fullmatch(r"logo-[0-9a-f]{12}\.(png|jpg|webp|svg)", name or ""):
        return None
    p = brand_dir() / name
    return p if p.is_file() else None


def logo_url(db: Session) -> str:
    name = core.get(db, "logo")
    return f"/brand/{name}" if name and logo_path(name) else ""


# ---------------------------------------------------------------------------
#  Into the page
# ---------------------------------------------------------------------------
def inject(db: Session, page_html: str, page: str) -> str:
    """Put the theme and the brand into a page before it is sent."""
    th = current(db)
    lk = look(db)
    brand = core.get(db, "brand_name")
    logo = logo_url(db)
    boot = {"brand": brand, "logo": logo, "mode": th["mode"], "ground": th["s_ground"],
            "fill": th["fill"], "on_fill": th["on_fill"],
            "fill_light": th["fill_light"], "on_fill_light": th["on_fill_light"],
            "motion": lk["motion"], "splash": lk["splash"], "splash_text": lk["splash_text"],
            "splash_cfg": lk["splash_cfg"], "logo_info": lk["logo_info"],
            "effect": lk["effect"]}
    boot_js = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    font_link, font_vars = font_css(db, page)
    head = (f'{font_link}<style id="kv-theme">{css(th, page)}{font_vars}</style>'
            f"<script>window.KV_BRAND={boot_js};</script>")
    if logo:
        kind = LOGO_TYPES[logo.rsplit(".", 1)[1]]
        head += f'<link rel="icon" href="{html.escape(logo)}" type="{kind}">'
    page_html = page_html.replace("</head>", head + "</head>", 1)
    # Background, icons and motion are attributes on <html>, there from the
    # first paint (the opening screen reads them before any script).
    if page != "admin":
        page_html = re.sub(r"<html([^>]*)>", lambda m: f"<html{m.group(1)}{html_attrs(lk)}>",
                           page_html, count=1)
    # The title is right from the first byte, before any script runs.
    # A function, not a string: a "\" in the brand name must stay a "\".
    title = f"<title>{html.escape(brand)}</title>"
    return re.sub(r"<title>[^<]*</title>", lambda m: title, page_html, count=1) \
        if page != "admin" else page_html
