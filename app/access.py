"""
Access on the servers.

The database holds what every customer SHOULD have (expiry, limits,
blocked or not). This module pushes that state to every enabled 3x-ui
panel, builds the one subscription link people import, keeps track of
their devices and collects traffic.

Panels are contacted in parallel threads; nothing in a thread touches
the database session — threads get plain snapshots and return results.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import quote, unquote

from sqlalchemy.orm import Session

from . import core
from .config import settings as env
from .models import GB, Device, Server, User, now
from .providers import ClientSpec, PanelError, normalize_url, panel_for


class AccessError(Exception):
    """Access could not be granted on any server."""


class Unavailable(Exception):
    """No panel answered: better no answer than an empty subscription."""


# ---------------------------------------------------------------------------
def servers(db: Session, include_disabled: bool = False) -> list[Server]:
    q = db.query(Server)
    if not include_disabled:
        q = q.filter(Server.enabled.is_(True))
    return q.order_by(Server.sort, Server.id).all()


def bypass_servers(db: Session) -> list[Server]:
    """The enabled servers whose traffic is paid by the gigabyte."""
    return [s for s in servers(db) if s.bypass]


def bypass_left(user: User) -> int:
    """Bytes the customer still has on the whitelist-bypass servers."""
    return max(0, int(user.bypass_limit or 0) - int(user.bypass_used or 0))


def panel_key(server) -> tuple:
    """The panel a server row talks to. Two rows may point at one panel
    (added twice by mistake): it must then be asked once, not twice."""
    kind = server.kind or "3xui"
    try:
        url = normalize_url(server.url or "", kind)
    except PanelError:
        url = (server.url or "").strip().lower()
    return kind, url.rstrip("/").lower()


def release(db: Session) -> None:
    """Give the database connection back before a slow call to a panel.
    The pool has a few connections; a panel answering in 10 s must not keep
    one busy for every request that waits on it (payments would queue up
    behind subscription refreshes). Rows stay usable (expire_on_commit=False)."""
    db.commit()


def _snap(server: Server) -> SimpleNamespace:
    return SimpleNamespace(id=server.id, name=server.name, country=server.country,
                           url=server.url, username=server.username,
                           password=server.password, api_token=server.api_token,
                           inbound_ids=server.inbound_ids, sub_url=server.sub_url,
                           verify_tls=server.verify_tls, api_version=server.api_version,
                           kind=server.kind or "3xui", bypass=bool(server.bypass))


def _for_server(spec: ClientSpec, snap, user: User) -> ClientSpec:
    """The customer as one server should have them. A whitelist-bypass server
    lets them in only while they have gigabytes there, and its panel stops
    them itself at the bought amount (the counters of that panel)."""
    out = ClientSpec(**spec.__dict__)
    if getattr(snap, "bypass", False):
        left = bypass_left(user)
        out.enable = bool(spec.enable and left > 0)
        out.total_bytes = int(user.bypass_limit or 0) if left > 0 else 0
    return out


def email_for(user: User) -> str:
    return f"{env.xui_email_prefix}{user.tg_id}"


# ---------------------------------------------------------------------------
#  When the VPN really stops for a person
# ---------------------------------------------------------------------------
GRACE_MAX = 30
SYNC_BACK = 7 * 86400           # the sync also re-sends those who ended this long ago


def grace_days(db: Session) -> int:
    """Days the VPN keeps working after the paid time (set by the owner, 0 by default)."""
    return max(0, min(GRACE_MAX, core.get_int(db, "grace_days", 0)))


def access_until(db: Session, user: User) -> int:
    """
    The moment the VPN stops: the end of the paid time, plus the days after
    it the owner allows (not for a free trial — that one ends when it ends),
    held further while an emergency with "keep access" lasts for everyone who
    still had the VPN when it began. 0 — never had a subscription.
    Renewing starts from the paid end or from now, never from this moment:
    the days after the end are a courtesy, not sold time.
    """
    from . import emergency
    end = int(user.expires_at or 0)
    if not end:
        return 0
    if not (user.plan and user.plan.is_trial):
        end += grace_days(db) * 86400
    hold = emergency.hold_since(db)
    if hold and end > hold:
        end = max(end, now() + emergency.KEEP_AHEAD)
    return end


def has_access(db: Session, user: User) -> bool:
    return (not user.is_blocked and not user.traffic_blocked
            and access_until(db, user) > now())


# ---------------------------------------------------------------------------
#  After that: a few hours with Telegram only, to reach the bot and pay
# ---------------------------------------------------------------------------
TG_MAX_HOURS = 168


def tg_hours(db: Session) -> int:
    return max(0, min(TG_MAX_HOURS, core.get_int(db, "tg_only_hours", 0)))


def tg_servers(db: Session) -> list[Server]:
    """Servers whose owner made inbounds (squads) that let only Telegram through."""
    return [s for s in servers(db) if (s.tg_inbounds or "").strip()]


def tg_until(db: Session, user: User) -> int:
    """
    When the VPN has stopped (the end, plus the days after it), the owner may
    keep Telegram working for a few hours more — in Russia Telegram often
    does not open without a VPN, and the person must reach the bot to renew.
    This is done with a second, separate client on special inbounds that let
    only Telegram through (set up by the owner in the panel), so the full VPN
    is never left open. Returns the end of that time, or 0 when not now.
    """
    hours = tg_hours(db)
    if not hours or user.is_blocked:
        return 0
    start = access_until(db, user)
    if not start:
        return 0
    end = start + hours * 3600
    return end if start <= now() < end else 0


def tg_spec(user: User, end: int) -> ClientSpec:
    """The Telegram-only client: its own name, key and subscription id, so it
    never touches the customer's real client on the panel."""
    import uuid as uuidlib
    return ClientSpec(email=f"{email_for(user)}_tg",
                      uuid=str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"vpnshop-tg:{user.xui_uuid}")),
                      sub_id=(user.xui_sub_id or "")[:12] + "tg", tg_id=0, expiry_ms=int(end) * 1000,
                      total_bytes=0, limit_ip=0, enable=True, comment="telegram only")


def _tg_snap(server: Server) -> SimpleNamespace:
    snap = _snap(server)
    snap.inbound_ids = server.tg_inbounds or ""
    return snap


def push_tg(db: Session, user: User) -> int:
    """Give the Telegram-only client to the panels when its time is now.
    Returns on how many servers it is in place."""
    end = tg_until(db, user)
    rows = tg_servers(db)
    if not end or not rows:
        return 0
    spec = tg_spec(user, end)
    snaps = [_tg_snap(s) for s in rows]
    release(db)
    results = _parallel(lambda snap: panel_for(snap).ensure(ClientSpec(**spec.__dict__)), snaps, wait=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    for s, r in zip(rows, results):
        if isinstance(r, Exception):
            core.log.warning("telegram-only client on %s failed: %s", s.name, r)
    if ok:
        user.tg_only_until = end
        db.commit()
    _links_cache_drop(user.id)
    return ok


def grace_until(db: Session, user: User) -> int:
    """When the paid time is over but the VPN still works: until when. Else 0."""
    t = now()
    end = access_until(db, user)
    return end if user.expires_at and int(user.expires_at) <= t < end else 0


def sync_floor(db: Session) -> int:
    """The oldest end of a subscription the sync still sends to the panels:
    a week back, plus the days after the end, plus a running hold."""
    from . import emergency
    floor = now() - SYNC_BACK - grace_days(db) * 86400
    hold = emergency.hold_since(db)
    if hold:
        floor = min(floor, hold - grace_days(db) * 86400 - 86400)
    return floor


def spec_for(db: Session, user: User) -> ClientSpec:
    ip_limit = user.device_limit if core.get(db, "panel_ip_limit") == "1" else 0
    return ClientSpec(
        email=email_for(user),
        uuid=user.xui_uuid,
        sub_id=user.xui_sub_id,
        tg_id=int(user.tg_id),
        expiry_ms=access_until(db, user) * 1000,
        total_bytes=int(user.traffic_limit or 0),
        limit_ip=int(ip_limit or 0),
        enable=has_access(db, user),
        comment=(f"@{user.username}" if user.username else "")[:60],
    )


def _parallel(fn, items: list, timeout: float = 40.0, wait: bool = False) -> list:
    """Run fn(item) for every item in threads; exceptions are returned, not raised.

    wait=True (writes to panels): the answer still comes after `timeout`,
    but the call returns only once every thread is done — a late write must
    not land after a newer one made under the customer's lock."""
    if not items:
        return []

    def safe(item):
        try:
            return fn(item)
        except Exception as e:  # noqa: BLE001 — reported per server
            return e

    # Not a `with` block: leaving one waits for every thread, so a panel
    # that hangs would hold the caller far longer than `timeout`. One
    # deadline for all; a late thread finishes on its own and is ignored.
    pool = ThreadPoolExecutor(max_workers=min(8, len(items)))
    try:
        futures = [pool.submit(safe, it) for it in items]
        deadline = time.monotonic() + timeout
        out = []
        for f in futures:
            try:
                out.append(f.result(timeout=max(0.0, deadline - time.monotonic())))
            except TimeoutError:
                out.append(PanelError(f"no answer in {timeout:g} s"))
            except Exception as e:  # noqa: BLE001
                out.append(e)
        return out
    finally:
        pool.shutdown(wait=wait, cancel_futures=not wait)


def _record(db: Session, rows: list[Server], snaps: list, results: list) -> None:
    """Write per-server health from thread results back to the rows."""
    for row, snap, res in zip(rows, snaps, results):
        if snap.api_version and row.api_version != snap.api_version:
            row.api_version = snap.api_version
        if isinstance(res, Exception):
            row.last_error = str(res)[:250]
        else:
            row.last_ok_at = now()
            row.last_error = ""
    db.commit()


# ---------------------------------------------------------------------------
#  Push the desired state to the panels
# ---------------------------------------------------------------------------
_user_locks: dict[int, threading.RLock] = {}
_user_locks_guard = threading.Lock()


def user_lock(user_id: int) -> threading.RLock:
    """One push per customer at a time. A payment and a background sync of
    the same person must not overtake each other: the later one would send
    an older state to the panels."""
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = _user_locks[user_id] = threading.RLock()
        return lock


def push(db: Session, user: User, *, required: bool = False,
         reset_traffic: bool = False, only: list[int] | None = None) -> dict:
    with user_lock(user.id):
        return _push(db, user, required=required, reset_traffic=reset_traffic, only=only)


def _push(db: Session, user: User, *, required: bool, reset_traffic: bool,
          only: list[int] | None) -> dict:
    """
    Bring this customer to the state stored in the database on every
    enabled server.

    required=True is used when a payment is being closed: if not a single
    server accepted the customer, AccessError is raised, the caller rolls
    back, and the gateway retries later — money is never marked as done
    without access behind it. Servers that failed while others succeeded
    are retried by the sync job.
    """
    rows = [s for s in servers(db) if not only or s.id in only]
    if not rows:
        if required:
            raise AccessError("no servers configured")
        return {"ok": 0, "failed": 0, "errors": []}

    spec = spec_for(db, user)
    snaps = [_snap(s) for s in rows]
    if not required:
        # Whatever the caller changed is committed now rather than after the
        # panels answered (it would be either way), and the connection is free
        # meanwhile. Then the customer is read again: a job holding an older
        # copy (the traffic job blocking somebody who has just bought more)
        # must push what is true now. A payment keeps its transaction: its
        # changes are stored only together with access really granted.
        release(db)
        db.refresh(user)
        spec = spec_for(db, user)

    def work(snap):
        panel = panel_for(snap)
        partial = panel.ensure(_for_server(spec, snap, user)) or []
        if reset_traffic:
            try:
                panel.reset_traffic(_for_server(spec, snap, user))
            except PanelError:
                pass        # a fresh client has nothing to reset
        return partial

    results = _parallel(work, snaps, wait=True)
    errors = [f"{r.name}: {res}" for r, res in zip(rows, results) if isinstance(res, Exception)]
    ok = len(rows) - len(errors)
    _links_cache_drop(user.id)
    if required and ok == 0:
        # Nothing was granted: the new expiry and the closed order must not
        # be stored. They are undone before the servers' health is written.
        db.rollback()
        _record(db, rows, snaps, results)
        raise AccessError("; ".join(errors)[:250] or "no server accepted the client")
    _record(db, rows, snaps, results)

    # Some inbounds of a server failed while others worked: the server counts
    # as done, but the sync job comes back for the rest.
    errors += [f"{r.name}: {'; '.join(res)}" for r, res in zip(rows, results)
               if isinstance(res, list) and res]
    user.sync_error = ("; ".join(errors))[:250]
    if ok:
        user.synced_at = now()
    db.commit()
    if not required and tg_hours(db):
        push_tg(db, user)
    return {"ok": ok, "failed": len(errors), "errors": errors}


def reissue(db: Session, user: User) -> dict:
    """
    New VPN credentials and a new subscription link. Old ones stop working.

    Done only if at least one server took the new key: otherwise the old
    key and link are put back (the panels still have the old key, and a
    "new" link would hand out the old, leaked keys). Servers that did not
    answer get the new key from the sync job later.
    """
    with user_lock(user.id):
        return _reissue(db, user)


def _reissue(db: Session, user: User) -> dict:
    import secrets
    import uuid as uuidlib
    old = (user.xui_uuid, user.sub_token)
    user.xui_uuid = str(uuidlib.uuid4())
    user.sub_token = secrets.token_urlsafe(18)
    db.commit()
    res = push(db, user)
    if not res.get("ok"):
        db.refresh(user)
        user.xui_uuid, user.sub_token = old
        db.commit()
        res["reverted"] = True
        return res
    # A fresh start for the devices too, removed ones included: the new link
    # goes only where the person puts it.
    db.query(Device).filter(Device.user_id == user.id).delete()
    db.commit()
    return res


def remove(db: Session, user: User) -> None:
    """Delete the customer from every panel (admin action)."""
    spec = spec_for(db, user)
    rows = servers(db, include_disabled=True)
    snaps = [_snap(s) for s in rows]
    _parallel(lambda snap: panel_for(snap).delete(ClientSpec(**spec.__dict__)), snaps)


# ---------------------------------------------------------------------------
#  The subscription link: /sub/<token>
# ---------------------------------------------------------------------------
# (server, customer) -> (when, links, the customer's uuid then)
_links_cache: dict[tuple[int, int, str], tuple[float, list[str], str]] = {}
_links_lock = threading.Lock()
LINKS_TTL = 90


def _links_cache_drop(user_id: int) -> None:
    with _links_lock:
        for key in [k for k in _links_cache if k[1] == user_id]:
            _links_cache.pop(key, None)


def flag(country: str) -> str:
    c = (country or "").upper()
    if len(c) != 2 or not c.isalpha():
        return ""
    return chr(0x1F1E6 + ord(c[0]) - 65) + chr(0x1F1E6 + ord(c[1]) - 65)


def rename(link: str, remark: str) -> str:
    """Put our own server name into a share link instead of the panel's."""
    if link.startswith("vmess://"):
        try:
            raw = link[8:]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode())
            data["ps"] = remark
            return "vmess://" + base64.b64encode(
                json.dumps(data, ensure_ascii=False).encode()).decode()
        except (ValueError, UnicodeDecodeError):
            return link
    head = link.split("#", 1)[0]
    return f"{head}#{quote(remark)}"


def remark_of(link: str) -> str:
    """The name a share link carries (after #, or "ps" of vmess)."""
    if link.startswith("vmess://"):
        try:
            raw = link[8:]
            return str(json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode()).get("ps") or "")
        except (ValueError, UnicodeDecodeError):
            return ""
    return unquote(link.split("#", 1)[1]).strip() if "#" in link else ""


def _starts_with_flag(text: str) -> bool:
    return len(text) >= 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in text[:2])


def notice(text: str) -> str:
    """
    A placeholder "server" whose name is a message. VPN clients show server
    names prominently, so this is the most reliable way to tell a person
    why the subscription is empty. It points nowhere and carries no traffic.
    """
    return ("vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1"
            f"?type=tcp&security=none#{quote('⚠ ' + text)}")


def collect_links(db: Session, user: User, info: dict | None = None, tg: int = 0) -> list[str]:
    """The customer's keys from every server, named after our servers.
    `info`, when given, gets how many servers there are and answered.
    tg — the end of the Telegram-only time: the keys of that client instead."""
    rows = tg_servers(db) if tg else servers(db)
    if not tg and not bypass_left(user):
        rows = [s for s in rows if not s.bypass]      # no gigabytes there: no keys either
    spec = tg_spec(user, tg) if tg else spec_for(db, user)
    mark = "tg" if tg else ""
    stamp = time.time()
    todo, out, stale = [], {}, {}
    with _links_lock:
        for s in rows:
            hit = _links_cache.get((s.id, user.id, mark))
            # Keys made for an older uuid (the link was reissued while this
            # was being fetched) are never served again.
            if hit and hit[2] != spec.uuid:
                hit = None
            if hit and stamp - hit[0] < LINKS_TTL:
                out[s.id] = hit[1]
            else:
                todo.append(s)
                if hit:
                    stale[s.id] = hit[1]
    snaps = [(_tg_snap(s) if tg else _snap(s)) for s in todo]
    if todo:
        release(db)
    results = _parallel(lambda snap: panel_for(snap, timeout=8).links(ClientSpec(**spec.__dict__)),
                        snaps, timeout=12)
    from . import vpncheck
    learned = False
    answered = len(rows) - len(todo)
    with _links_lock:
        for s, res in zip(todo, results):
            if isinstance(res, list):
                answered += 1
                _links_cache[(s.id, user.id, mark)] = (stamp, res, spec.uuid)
                out[s.id] = res
                if not tg:          # the Telegram-only inbounds are not what customers connect to
                    learned = vpncheck.learn(s, res) or learned
            else:
                core.log.warning("links from %s failed: %s", s.name, res)
                # A panel that does not answer right now must not wipe its
                # servers from people's apps: serve the last known keys.
                if s.id in stale:
                    out[s.id] = stale[s.id]
    if info is not None:
        info.update(servers=len(rows), answered=answered)

    if learned:
        db.commit()                         # where each server's VPN is reached, for the checks

    # A server whose VPN stopped answering is left out — unless every server
    # is "down", which more likely means trouble on the shop's own side.
    if core.get(db, "vpn_autohide") == "1":
        up = [s for s in rows if out.get(s.id) and not vpncheck.is_down(s)]
        if up:
            rows = [s for s in rows if not vpncheck.is_down(s)]
    links = []
    for s in rows:
        items = out.get(s.id) or []
        base = f"{flag(s.country)} {s.name}".strip()
        # Remnawave and Marzban give one key per node. When the owner named
        # the nodes in the panel by place — the name starts with a flag,
        # "🇩🇪 Frankfurt" — that name is kept: one panel may have nodes in
        # several countries. Otherwise the key carries our server's name.
        own = len(items) > 1 and (s.kind or "3xui") in ("remnawave", "marzban")
        for i, link in enumerate(items):
            if own and _starts_with_flag(remark_of(link)):
                links.append(link)
            else:
                links.append(rename(link, base if len(items) == 1 else f"{base} {i + 1}"))
    return links


def _short_date(ts: int, lang: str) -> str:
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts)
    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря")
    return f"{d.day} {months[d.month - 1]}" if lang == "ru" else d.strftime("%b %-d")


def _short_time(ts: int, lang: str) -> str:
    import datetime as dt
    return f"{_short_date(ts, lang)} {dt.datetime.fromtimestamp(ts).strftime('%H:%M')}"


def grace_line(db: Session, user: User, lang: str) -> str:
    """A line for messages about the end: until when the VPN still works."""
    g = grace_until(db, user)
    return core.t(lang, "grace_note", date=_short_date(g, lang)) if g else ""


def subscription(db: Session, user: User, headers: dict) -> tuple[str, dict]:
    """
    Body and headers for a VPN client refreshing its subscription.
    Headers follow the de-facto standard understood by Happ, v2RayTun,
    Hiddify, Streisand and others.
    """
    lang = user.lang or env.default_lang
    brand = core.get(db, "brand_name")

    problem = None
    until = access_until(db, user)
    grace = grace_until(db, user)
    if user.is_blocked:
        problem = "sub_blocked"
    elif until <= now():
        problem = "sub_expired"
    elif user.traffic_blocked:
        problem = "sub_traffic" if core.get(db, "traffic_topup") == "1" else "sub_traffic_renew"
    else:
        problem = register_device(db, user, headers)

    tg = tg_until(db, user) if problem == "sub_expired" else 0
    if tg:
        # The VPN has stopped; Telegram still works for a few hours, to renew.
        links = collect_links(db, user, tg=tg)
        links = [notice(core.t(lang, "sub_tg_only", date=_short_time(tg, lang)))] + links
    elif problem:
        links = [notice(core.t(lang, problem))]
    else:
        info: dict = {}
        links = collect_links(db, user, info)
        if not links and info.get("servers") and not info.get("answered") and user.connected_at:
            # Every panel is unreachable right now (a restart, an outage) while
            # the VPN itself may well work. An error makes VPN apps keep the
            # keys they have; a 200 with a notice would replace them.
            raise Unavailable()
        links = links or [notice(core.t(lang, "sub_empty"))]
        if grace and links:
            # The paid time is over, the VPN still works: the app says so at the top.
            links = [notice(core.t(lang, "sub_grace", date=_short_date(grace, lang)))] + links
        if not user.connected_at:
            user.connected_at = now()           # the funnel: a client really connected
            db.commit()

    body = base64.b64encode("\n".join(links).encode()).decode()
    out_headers = {
        "profile-title": "base64:" + base64.b64encode(brand.encode()).decode(),
        "profile-update-interval": str(core.get_int(db, "sub_update_hours", 12) or 12),
        "subscription-userinfo": (f"upload=0; download={int(user.traffic_used or 0)}; "
                                  f"total={int(user.traffic_limit or 0)}; "
                                  f"expire={int(until or 0)}"),
        "content-disposition": f'attachment; filename="{quote(brand)}"',
        "cache-control": "no-store",
    }
    support = core.get(db, "support_url")
    if support.startswith("http"):
        out_headers["support-url"] = support
    out_headers["profile-web-page-url"] = env.public_url + "/app"
    return body, out_headers


# ---------------------------------------------------------------------------
#  Load: how busy each server is right now
# ---------------------------------------------------------------------------
DEFAULT_CAPACITY = 200          # people online a small VPS carries comfortably
LOAD_FRESH = 600                # older numbers are not shown at all


def load_of(server: Server) -> dict | None:
    """{"level": low | mid | high, "share": 0..1} from the last count of
    people online, or None when there is no fresh count (the panel did not
    answer, or cannot tell) — then nothing is shown rather than a guess."""
    online = server.online if server.online is not None else -1
    if online < 0 or now() - int(server.online_at or 0) > LOAD_FRESH:
        return None
    cap = int(server.capacity or 0) or DEFAULT_CAPACITY
    share = online / cap
    level = "low" if share < 0.5 else "mid" if share < 0.85 else "high"
    return {"level": level, "share": round(min(1.0, share), 2)}


def refresh_load(db: Session, rows: list[Server] | None = None) -> int:
    """Ask every enabled panel how many people are online. Returns how many answered."""
    rows = servers(db) if rows is None else rows
    snaps = [_snap(s) for s in rows]
    release(db)

    def work(snap):
        panel = panel_for(snap, timeout=8)
        fn = getattr(panel, "online", None)
        return fn() if fn else None

    results = _parallel(work, snaps, timeout=12)
    got = 0
    for row, res in zip(rows, results):
        if isinstance(res, int) and not isinstance(res, bool) and res >= 0:
            row.online, row.online_at = res, now()
            got += 1
    db.commit()
    return got


# ---------------------------------------------------------------------------
#  Devices
# ---------------------------------------------------------------------------
def register_device(db: Session, user: User, headers: dict) -> str | None:
    """
    Note the device that is refreshing the subscription. Returns a text key
    when it must be refused (limit reached, or no device id while one is
    required), None when it may connect.
    """
    h = {k.lower(): v for k, v in headers.items()}
    hwid = (h.get("x-hwid") or "").strip()[:128]
    if not hwid:
        return "sub_hwid" if core.get(db, "require_hwid") == "1" else None

    dev = db.query(Device).filter(Device.user_id == user.id, Device.hwid == hwid).first()
    if dev is not None and dev.blocked:
        return "sub_device_removed"
    if dev is None:
        limit = int(user.device_limit or 0)
        if limit and active_devices(db, user).count() >= limit:
            return "sub_devices"
        dev = Device(user_id=user.id, hwid=hwid)
        db.add(dev)
    dev.model = unquote(h.get("x-device-model", ""))[:128] or dev.model or ""
    dev.os = unquote(h.get("x-device-os", ""))[:64] or dev.os or ""
    dev.os_version = unquote(h.get("x-ver-os", ""))[:64] or dev.os_version or ""
    dev.user_agent = h.get("user-agent", "")[:255]
    dev.last_seen = now()
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — the same device raced itself; it is registered
        db.rollback()
    return None


def active_devices(db: Session, user: User):
    return db.query(Device).filter(Device.user_id == user.id, Device.blocked.isnot(True))


def _dev_out(d: Device) -> dict:
    return {"id": d.id, "model": d.model or _app_name(d.user_agent), "os": d.os,
            "os_version": d.os_version, "app": _app_name(d.user_agent),
            "last_seen": d.last_seen, "first_seen": d.first_seen}


def devices(db: Session, user: User) -> list[dict]:
    return [_dev_out(d) for d in active_devices(db, user).order_by(Device.last_seen.desc()).all()]


def removed_devices(db: Session, user: User) -> list[dict]:
    rows = (db.query(Device).filter(Device.user_id == user.id, Device.blocked.is_(True))
            .order_by(Device.last_seen.desc()).limit(20).all())
    return [_dev_out(d) for d in rows]


def _app_name(ua: str) -> str:
    return (ua or "").split("/")[0].split(" ")[0][:32]


def forget_device(db: Session, user: User, device_id: int) -> bool:
    """The customer removes a device: its slot is free, and it is refused
    from now on — otherwise it would simply come back on the next refresh.
    (It still has the keys it already got; a new link takes them away.)"""
    n = (db.query(Device).filter(Device.user_id == user.id, Device.id == device_id,
                                 Device.blocked.isnot(True))
         .update({"blocked": True}, synchronize_session=False))
    db.commit()
    return bool(n)


def restore_device(db: Session, user: User, device_id: int) -> str | None:
    """Let a removed device in again. Returns an error key or None."""
    dev = db.query(Device).filter(Device.user_id == user.id, Device.id == device_id,
                                  Device.blocked.is_(True)).first()
    if dev is None:
        return "device_not_found"
    limit = int(user.device_limit or 0)
    if limit and active_devices(db, user).count() >= limit:
        return "devices_full"
    dev.blocked = False
    db.commit()
    return None


# ---------------------------------------------------------------------------
#  Traffic
# ---------------------------------------------------------------------------
def collect_traffic(db: Session) -> dict:
    """
    Sum traffic of every customer over all servers (one request per server)
    and close access for those who went over their limit.
    Returns {tg_id: bytes} for the people whose traffic changed.
    """
    started = now()
    rows, seen = [], set()
    for s in servers(db):
        # One panel behind two server rows holds one counter per person:
        # asking it twice would count every gigabyte twice.
        if panel_key(s) not in seen:
            seen.add(panel_key(s))
            rows.append(s)
    snaps = [_snap(s) for s in rows]
    release(db)
    results = _parallel(lambda snap: panel_for(snap).traffic_by_email(), snaps)
    _record(db, rows, snaps, results)

    # With a server missing from this round the sums are too small; then
    # counters are only allowed to grow, never to drop.
    partial = any(isinstance(res, Exception) for res in results)
    totals: dict[int, int] = {}
    bypass: dict[int, int] = {}          # the whitelist-bypass servers: their own gigabytes
    for snap, res in zip(snaps, results):
        if isinstance(res, Exception):
            continue
        # Marzban and Remnawave change the name a little (lower case, "_"
        # for "-"): the prefix is compared in the panel's own spelling.
        prefix = user_prefix(snap.kind)
        into = bypass if snap.bypass else totals
        for email, used in res.items():
            m = re.fullmatch(r"(\d+)(?:[-_].*)?", email[len(prefix):]) if email.startswith(prefix) else None
            if m:
                tg = int(m.group(1))
                into[tg] = into.get(tg, 0) + used

    changed = {}
    over = []
    over_bypass = []
    seen_ids = list(set(totals) | set(bypass))
    for user in (db.query(User).filter(User.tg_id.in_(seen_ids)).populate_existing().all()
                 if seen_ids else []):
        if (user.synced_at or 0) > started:
            continue    # pushed while we were reading (a purchase may have reset the counters)
        if user.tg_id in bypass:
            b = bypass[user.tg_id]
            if partial and b < (user.bypass_used or 0):
                b = user.bypass_used or 0
            had = bypass_left(user) > 0
            user.bypass_used = b
            if had and not bypass_left(user) and user.bypass_limit:
                over_bypass.append(user)
        if user.tg_id not in totals:
            continue
        used = totals.get(user.tg_id, 0)
        if partial and used < (user.traffic_used or 0):
            used = user.traffic_used or 0
        if used != user.traffic_used:
            user.traffic_used = used
            changed[user.tg_id] = used
        if (user.traffic_limit and used >= user.traffic_limit
                and not user.traffic_blocked and user.has_subscription):
            user.traffic_blocked = True
            over.append(user)
    db.commit()
    for user in over:
        push(db, user)
        core.audit(db, "Traffic limit reached", f"tg:{user.tg_id}",
                   f"{used_gb(user.traffic_used)} / {used_gb(user.traffic_limit)} GB")
    ids = [s.id for s in rows if s.bypass]
    for user in over_bypass:
        if user in over:
            continue                      # already pushed everywhere
        push(db, user, only=ids)
        _links_cache_drop(user.id)
        core.audit(db, "Bypass gigabytes used up", f"tg:{user.tg_id}",
                   f"{used_gb(user.bypass_used)} / {used_gb(user.bypass_limit)} GB")
        from . import notify
        lang = user.lang or env.default_lang
        notify.send_safe(db, user, core.t(lang, "bypass_out"))
    return {"changed": changed, "over": [u.tg_id for u in over],
            "bypass_over": [u.tg_id for u in over_bypass]}


def user_prefix(kind: str) -> str:
    """XUI_EMAIL_PREFIX as the panel of this kind writes it in a username."""
    prefix = env.xui_email_prefix or ""
    if kind == "marzban":
        return re.sub(r"[^a-z0-9_]", "_", prefix.lower())
    if kind == "remnawave":
        return re.sub(r"[^A-Za-z0-9_-]", "_", prefix)
    return prefix


def used_gb(value: int) -> str:
    return f"{(value or 0) / GB:.1f}"


# ---------------------------------------------------------------------------
#  Background reconciliation
# ---------------------------------------------------------------------------
_sync_cursor = 0


def sync_users(db: Session, only_failed: bool = True, server_ids: list[int] | None = None,
               limit: int = 200) -> dict:
    """
    Re-push customers to the panels. With only_failed=True — those whose
    last push had errors; otherwise everyone who has or recently had a
    subscription (after adding a server, or as a daily safety net).
    """
    global _sync_cursor
    q = db.query(User.id).filter(User.expires_at > sync_floor(db))
    if only_failed:
        q = q.filter(User.sync_error != "")
        # In turns: a dead server must not keep the same first 200 people
        # failing every round while somebody further down waits for a day.
        ids = [i for (i,) in q.filter(User.id > _sync_cursor).order_by(User.id).limit(limit).all()]
        if len(ids) < limit:
            ids += [i for (i,) in q.filter(User.id <= _sync_cursor).order_by(User.id)
                    .limit(limit - len(ids)).all()]
        _sync_cursor = ids[-1] if ids and len(ids) >= limit else 0
    else:
        ids = [i for (i,) in q.order_by(User.id).limit(limit).all()]
    release(db)
    done = failed = 0
    for user_id in ids:
        # This loop is slow (panels answer in seconds): each customer is read
        # again right before the push, so a purchase made meanwhile is what
        # gets sent, not the state from the start of the loop.
        with user_lock(user_id):
            user = db.get(User, user_id)
            if user is None:
                continue
            db.refresh(user)
            res = push(db, user, only=server_ids)
        done += 1
        failed += 1 if res["failed"] else 0
    return {"users": done, "with_errors": failed}
