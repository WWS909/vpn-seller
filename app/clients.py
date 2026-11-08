"""
VPN apps the mini app offers for connecting, with the links that add the
subscription in one tap.

Every app here reads the shop's subscription as it is (a list of share
links, base64) and its headers (name, expiry, traffic). "hwid" means the
app sends X-HWID by itself, so the device limit counts it; when the owner
requires a device id, only those are offered.

The link templates: {url} the subscription as it is, {enc} the same
percent-encoded, {name} the shop's name percent-encoded.

Not here on purpose: sing-box and Clash. They want their own config format
(JSON / YAML), not a list of share links, so the shop's link would not
open in them.
"""

APPS = {
    "happ": {
        "name": "Happ", "hwid": True,
        "link": "happ://add/{url}",
        "get": {"ios": "https://apps.apple.com/app/happ-proxy-utility/id6504287215",
                "android": "https://play.google.com/store/apps/details?id=com.happproxy",
                "windows": "https://github.com/Happ-proxy/happ-desktop/releases",
                "macos": "https://github.com/Happ-proxy/happ-desktop/releases"},
    },
    "v2raytun": {
        "name": "v2RayTun", "hwid": True,
        "link": "v2raytun://import/{url}",
        "get": {"ios": "https://apps.apple.com/app/v2raytun/id6476628951",
                "android": "https://play.google.com/store/apps/details?id=com.v2raytun.android",
                "windows": "https://v2raytun.com/",
                "macos": "https://v2raytun.com/"},
    },
    "incy": {
        "name": "Incy", "hwid": True,
        "link": "incy://add/{url}",
        "get": {"ios": "https://apps.apple.com/app/incy/id6756943388",
                "android": "https://play.google.com/store/apps/details?id=llc.itdev.incy",
                "windows": "https://github.com/INCY-DEV/incy-platforms/releases",
                "macos": "https://github.com/INCY-DEV/incy-platforms/releases"},
    },
    "onexray": {
        "name": "OneXray", "hwid": False,
        "link": "onexray://onexray.com/sub/add?url={enc}#{name}",
        "get": {"ios": "https://apps.apple.com/app/onexray/id6745748773",
                "android": "https://play.google.com/store/apps/details?id=net.yuandev.onexray",
                "windows": "https://github.com/OneXray/OneXray/releases/latest",
                "macos": "https://apps.apple.com/app/onexray/id6745748773"},
    },
    "karing": {
        # sends a device id only when the person turns it on in the app
        "name": "Karing", "hwid": False,
        "link": "karing://install-config?url={enc}&name={name}",
        "get": {"ios": "https://apps.apple.com/app/karing/id6472431552",
                "android": "https://github.com/KaringX/karing/releases",
                "windows": "https://github.com/KaringX/karing/releases",
                "macos": "https://apps.apple.com/app/karing/id6472431552"},
    },
    "hiddify": {
        "name": "Hiddify", "hwid": False,
        "link": "hiddify://import/{url}#{name}",
        "get": {"ios": "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532",
                "android": "https://play.google.com/store/apps/details?id=app.hiddify.com",
                "windows": "https://github.com/hiddify/hiddify-app/releases",
                "macos": "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532"},
    },
}
DEFAULT = "happ,v2raytun,incy,onexray,karing,hiddify"


def parse(value: str) -> list[str]:
    """The owner's list: known ids in their order, each once."""
    out = []
    for part in (value or "").split(","):
        k = part.strip().lower()
        if k in APPS and k not in out:
            out.append(k)
    return out


def clean(value: str) -> str:
    """For saving: raises ValueError when nothing known is left."""
    ids = parse(value)
    if not ids:
        raise ValueError("connect_apps: at least one of " + ", ".join(APPS))
    return ",".join(ids)


def offered(value: str, require_hwid: bool) -> list[dict]:
    """What the mini app shows, first is the one it suggests."""
    ids = parse(value) or parse(DEFAULT)
    if require_hwid:
        ids = [k for k in ids if APPS[k]["hwid"]] or [k for k in APPS if APPS[k]["hwid"]]
    return [{"id": k, **APPS[k]} for k in ids]


# The schemes the shop may hand to the phone (the /open page refuses others).
SCHEMES = {a["link"].split("://", 1)[0] for a in APPS.values()}
