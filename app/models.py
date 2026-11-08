"""Database tables."""

from __future__ import annotations

import secrets
import time
import uuid as uuidlib

from sqlalchemy import (BigInteger, Boolean, Column, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from .db import Base

GB = 1024 ** 3


def now() -> int:
    return int(time.time())


def _token() -> str:
    return secrets.token_urlsafe(18)


def _sub_id() -> str:
    # 3x-ui subscription id: short, url-safe, lowercase like the panel's own.
    return secrets.token_hex(8)


class User(Base):
    """A Telegram user = a customer."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(64), default="")
    first_name = Column(String(128), default="")
    lang = Column(String(8), default="")

    # Our own subscription link: /sub/<sub_token>. It never changes when
    # servers are added or replaced, so people never re-import it.
    sub_token = Column(String(64), unique=True, index=True, default=_token)
    # Credentials of this person on the 3x-ui panels.
    xui_uuid = Column(String(64), default=lambda: str(uuidlib.uuid4()))
    xui_sub_id = Column(String(32), default=_sub_id)

    plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    expires_at = Column(Integer, default=0)
    device_limit = Column(Integer, default=0)            # 0 = no limit
    traffic_limit = Column(BigInteger, default=0)        # bytes, 0 = unlimited
    traffic_used = Column(BigInteger, default=0)         # bytes, all servers
    # Bytes of traffic packs bought on top of the plan in this period. What
    # is left of them moves into the next period when the plan is renewed:
    # paid gigabytes do not burn.
    traffic_extra = Column(BigInteger, default=0)
    traffic_blocked = Column(Boolean, default=False)     # limit reached, disabled on panels
    # Gigabytes on the whitelist-bypass servers (Server.bypass), bought apart:
    # used and limit are both in the panels' counters (bytes).
    bypass_limit = Column(BigInteger, default=0)
    bypass_used = Column(BigInteger, default=0)

    trial_used = Column(Boolean, default=False)
    is_blocked = Column(Boolean, default=False)          # blocked by admin
    bot_blocked = Column(Boolean, default=False)         # blocked the bot — skip messages
    created_at = Column(Integer, default=now)
    reminded_for = Column(Integer, default=0)            # expiry date we already reminded about
    synced_at = Column(Integer, default=0)               # last successful push to panels
    sync_error = Column(String(255), default="")

    # Referral program: who brought this person. Set once, when the person
    # first comes; with the captcha on — only after it is solved.
    referrer_id = Column(Integer, index=True, nullable=True)
    ref_pending = Column(Integer, default=0)             # referrer waiting for the captcha
    captcha = Column(String(8), default="")              # the expected answer
    ref_days = Column(Integer, default=0)                # bonus days earned by inviting
    review_rewarded_at = Column(Integer, default=0)      # when the review bonus was given (once)
    # Money mode of the referral program: what can be withdrawn now, and
    # everything ever earned. Changed only with conditional UPDATEs.
    ref_balance = Column(Float, default=0)
    ref_earned = Column(Float, default=0)
    # Mandatory channel: when membership was last confirmed.
    channel_ok_at = Column(Integer, default=0)
    # Funnel: first time the app was opened, first time a VPN client fetched
    # the subscription.
    app_opened_at = Column(Integer, default=0)
    connected_at = Column(Integer, default=0)
    # Support chat: the person's last message, unanswered while > support_answered_at.
    support_at = Column(Integer, default=0)
    support_answered_at = Column(Integer, default=0)
    support_human_at = Column(Integer, default=0)        # the AI helper handed the thread to people
    # Where the person first came from: the code of a source link
    # (t.me/<bot>?start=src_<code>), set once, on the first contact.
    source = Column(String(40), default="", index=True)
    # The Telegram-only time after the end that the panels already hold (app/access.py)
    tg_only_until = Column(Integer, default=0)

    plan = relationship("Plan", lazy="joined")

    @property
    def is_active(self) -> bool:
        return (not self.is_blocked and not self.traffic_blocked
                and (self.expires_at or 0) > now())

    @property
    def has_subscription(self) -> bool:
        return (self.expires_at or 0) > now()


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    name_en = Column(String(64), default="")
    days = Column(Integer, default=30)
    price = Column(Float, default=0)
    old_price = Column(Float, default=0)             # crossed-out price, 0 = none
    badge = Column(String(24), default="")
    devices = Column(Integer, default=3)             # included devices, 0 = unlimited
    traffic_gb = Column(Integer, default=0)          # 0 = unlimited
    bypass_gb = Column(Integer, default=0)           # GB on the whitelist-bypass servers included
    is_trial = Column(Boolean, default=False)        # free, once per person
    enabled = Column(Boolean, default=True)
    sort = Column(Integer, default=0)


class Server(Base):
    """A 3x-ui panel. Customers get a client on every enabled server."""
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)        # shown to customers
    country = Column(String(2), default="")          # ISO code: DE, NL, FI
    url = Column(String(255), nullable=False)        # panel address incl. base path
    username = Column(String(128), default="")
    password = Column(String(255), default="")
    api_token = Column(String(1024), default="")     # 3x-ui v3 / Remnawave API token
    inbound_ids = Column(String(1024), default="")   # "1,3", tags or squad uuids
    sub_url = Column(String(255), default="")        # panel subscription base, needed for 3x-ui v2
    verify_tls = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)
    sort = Column(Integer, default=0)
    kind = Column(String(12), default="3xui")        # 3xui | marzban | remnawave

    api_version = Column(String(16), default="")     # "3", "2", "m0.8.4" (Marzban), "r2.3.0" (Remnawave)
    last_ok_at = Column(Integer, default=0)
    last_error = Column(String(255), default="")
    down_alerted_at = Column(Integer, default=0)     # admins were told the panel is down
    clients_count = Column(Integer, default=0)

    # Load: people online right now, as the panel reports it, and how many
    # the server is meant to carry (0 = the default).
    online = Column(Integer, default=-1)             # -1 = unknown
    online_at = Column(Integer, default=0)
    capacity = Column(Integer, default=0)

    # The VPN itself, not the panel: the addresses customers connect to
    # (learned from the keys) and whether they answer. A server whose VPN
    # stops answering is left out of subscriptions until it is back.
    endpoints = Column(String(512), default="")      # "host:port,host:port" (TCP ones)
    vpn_fails = Column(Integer, default=0)           # failed checks in a row
    vpn_down_at = Column(Integer, default=0)         # 0 = answering
    vpn_ok_at = Column(Integer, default=0)
    vpn_sni = Column(String(512), default="")         # "host:port=sni,…" — the names keys present (for the Russia probes)
    tg_inbounds = Column(String(1024), default="")   # inbounds / squads that let through only Telegram
    # A server that gets through the mobile operators' whitelists: its traffic
    # is paid by the gigabyte (User.bypass_*), apart from the subscription.
    bypass = Column(Boolean, default=False)

    @property
    def inbounds(self) -> list:
        """Inbound ids (3x-ui) or tags (Marzban) this server gives out."""
        parts = [x.strip() for x in (self.inbound_ids or "").split(",") if x.strip()]
        return parts if self.kind in ("marzban", "remnawave") else [int(x) for x in parts if x.isdigit()]


class Device(Base):
    """
    A device that fetched the subscription. VPN clients such as Happ,
    v2RayTun and Hiddify send X-HWID with every refresh; that is how the
    device limit works, independent of the panel version.
    """
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("user_id", "hwid"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    hwid = Column(String(128), nullable=False)
    model = Column(String(128), default="")
    os = Column(String(64), default="")
    os_version = Column(String(64), default="")
    user_agent = Column(String(255), default="")
    first_seen = Column(Integer, default=now)
    last_seen = Column(Integer, default=now)
    # Removed by the customer: the subscription is refused to this device
    # (it would otherwise come back on its next refresh) until they return it.
    blocked = Column(Boolean, default=False)


class Transaction(Base):
    """
    An order. Created as pending when the person picks what to buy, and
    closed as success only after the gateway confirms payment AND access
    has been granted on the panels.
    """
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind = Column(String(16), default="purchase")    # purchase | devices | traffic | gift
    plan_id = Column(Integer, nullable=True)
    devices = Column(Integer, default=0)             # devices in the bought subscription
    qty = Column(Integer, default=0)                 # extra devices bought
    gb = Column(Integer, default=0)                  # extra traffic bought
    bonus_days = Column(Integer, default=0)          # from a promo code
    promo_id = Column(Integer, nullable=True)
    discount = Column(Integer, default=0)            # automatic discount applied, % (sale or offer)
    offer_id = Column(Integer, nullable=True)        # the personal offer that gave it
    # What the plan gave when the order was made: an order paid later gets
    # what the person saw, even if the plan was edited meanwhile.
    days = Column(Integer, nullable=True)
    traffic_gb = Column(Integer, nullable=True)
    bypass_gb = Column(Integer, nullable=True)       # bypass GB: the plan's (purchase) or the pack's (bypass)

    amount = Column(Float, default=0)
    currency = Column(String(8), default="RUB")
    method = Column(String(16), default="")          # stars | crypto | card | balance | free | admin
    gateway = Column(String(16), default="")         # yookassa | freekassa | platega | paypalych | lava | wata | tribute | cloudpayments | mulenpay | cryptobot | heleket | lavepay | stars
    stars = Column(Integer, default=0)               # expected amount in XTR
    ext_amount = Column(Float, default=0)            # the invoice in the gateway's own currency (Lave Pay: USD)
    external_id = Column(String(128), default="", index=True)
    status = Column(String(16), default="pending")   # pending | processing | success | failed
    error = Column(String(255), default="")
    created_at = Column(Integer, default=now)
    paid_at = Column(Integer, default=0)
    # When the money was confirmed (the first verified notification). Unlike
    # paid_at it does not move when access is granted later: it is the date
    # of the income, e.g. on a «Мой налог» receipt.
    money_at = Column(Integer, default=0)
    # A paid order that could not be granted yet: how often it was tried and
    # when the next try is due (the retry job slows down for stubborn ones).
    tries = Column(Integer, default=0)
    next_try_at = Column(Integer, default=0)

    user = relationship("User", lazy="joined")


class Promo(Base):
    __tablename__ = "promos"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, index=True)
    kind = Column(String(16), default="percent")     # percent | amount | days
    value = Column(Float, default=0)
    limit = Column(Integer, default=0)               # total uses, 0 = unlimited
    used = Column(Integer, default=0)
    new_only = Column(Boolean, default=False)        # only for people who never paid
    expires_at = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)


class PromoUse(Base):
    """One code — once per person."""
    __tablename__ = "promo_uses"
    __table_args__ = (UniqueConstraint("promo_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    promo_id = Column(Integer, ForeignKey("promos.id", ondelete="CASCADE"))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    created_at = Column(Integer, default=now)


class Gift(Base):
    """A gift code: bought by one person, redeemed by anyone."""
    __tablename__ = "gifts"

    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, index=True)
    tx_id = Column(Integer, index=True, nullable=True)       # the order that paid for it
    plan_id = Column(Integer, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    used_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Float, default=0)
    created_at = Column(Integer, default=now)
    used_at = Column(Integer, default=0)

    plan = relationship("Plan", foreign_keys=[plan_id], lazy="joined")


class SupportMessage(Base):
    """One message of the support chat, either way."""
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_admin = Column(Boolean, default=False)
    text = Column(Text, default="")
    media = Column(String(16), default="")           # photo | document | voice ... ("" = text)
    source = Column(String(8), default="bot")        # bot | app | panel
    admin_tg = Column(BigInteger, nullable=True)     # who answered
    refs = Column(String(1024), default="")          # ",chat:msg,chat:msg," — the copies admins got
    at = Column(Integer, default=now, index=True)


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    stars = Column(Integer, default=5)
    text = Column(Text, default="")
    status = Column(String(16), default="pending")   # pending | published | rejected
    created_at = Column(Integer, default=now)

    user = relationship("User", lazy="joined")


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id = Column(Integer, primary_key=True)
    text = Column(Text)
    audience = Column(String(16), default="all")     # all | active | expiring | inactive | never
    button_text = Column(String(64), default="")
    sent = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    total = Column(Integer, default=0)
    # Progress through the recipients. A restart in the middle of a
    # broadcast continues from here instead of sending it twice.
    cursor = Column(Integer, default=0)
    status = Column(String(16), default="sending")   # sending | done | cancelled
    created_at = Column(Integer, default=now)


class Setting(Base):
    """Key/value settings edited in the admin panel."""
    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")


class Log(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    level = Column(String(8), default="info")        # info | warn | error
    action = Column(String(96))
    actor = Column(String(64), default="system")
    details = Column(Text, default="")
    created_at = Column(Integer, default=now, index=True)


class Offer(Base):
    """A personal discount: from a warm-up notice or a campaign. Applied
    automatically at checkout while it is valid; spent by one purchase."""
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    percent = Column(Integer, default=0)
    expires_at = Column(Integer, default=0)
    source = Column(String(32), default="")          # trigger:<key> | campaign:<id>
    used_at = Column(Integer, default=0)
    tx_id = Column(Integer, nullable=True)
    created_at = Column(Integer, default=now)


class Campaign(Base):
    """A warm-up campaign: a segment, a message and an optional discount."""
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), default="")
    segment = Column(String(24), default="all")
    text_ru = Column(Text, default="")
    text_en = Column(Text, default="")
    percent = Column(Integer, default=0)             # 0 = a message without a discount
    valid_days = Column(Integer, default=3)          # how long the discount lasts
    start_at = Column(Integer, default=0)            # 0 = right away
    status = Column(String(16), default="scheduled")  # scheduled | sending | done | cancelled
    cursor = Column(Integer, default=0)
    total = Column(Integer, default=0)
    sent = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    created_at = Column(Integer, default=now)


class WarmSent(Base):
    """A system notice already sent — the same event never repeats."""
    __tablename__ = "warm_sent"
    __table_args__ = (UniqueConstraint("user_id", "trigger", "stamp"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger = Column(String(24))
    stamp = Column(BigInteger, default=0)            # what the event is about (e.g. an expiry date)
    created_at = Column(Integer, default=now)


class RefReward(Base):
    """What a referrer got for a payment of the person they invited:
    bonus days or money (a share of the payment)."""
    __tablename__ = "ref_rewards"

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    referee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tx_id = Column(Integer, unique=True)             # one reward per payment, never twice
    days = Column(Integer, default=0)
    money = Column(Float, default=0)                 # the referrer's share, in the shop's currency
    amount = Column(Float, default=0)                # what the invited person paid
    created_at = Column(Integer, default=now)


class Payout(Base):
    """A referrer's request to withdraw earned money. The amount leaves the
    balance when the request is made and comes back if it is rejected or
    cancelled; the owner pays by hand and marks it paid."""
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount = Column(Float, default=0)
    currency = Column(String(8), default="RUB")
    method = Column(String(32), default="")          # id of the owner's payout method
    method_title = Column(String(64), default="")    # its name at the time of the request
    details = Column(String(255), default="")        # card, phone and bank, wallet...
    status = Column(String(16), default="pending", index=True)  # pending | paid | rejected | cancelled
    note = Column(String(255), default="")           # the owner's reason for a rejection
    created_at = Column(Integer, default=now)
    done_at = Column(Integer, default=0)
    done_by = Column(String(64), default="")

    user = relationship("User", lazy="joined")


class Receipt(Base):
    """A receipt in «Мой налог» for one paid order. The row is created
    before the receipt is sent, so a receipt can never be sent twice; see
    app/nalog.py for what each status means."""
    __tablename__ = "receipts"

    id = Column(Integer, primary_key=True)
    tx_id = Column(Integer, unique=True, index=True)     # one receipt per order, never two
    amount = Column(Float, default=0)
    name = Column(String(128), default="")              # the line on the receipt
    paid_at = Column(Integer, default=0)                # when the money came: the date on the receipt
    status = Column(String(16), default="pending", index=True)
    uuid = Column(String(64), default="")               # the receipt number in «Мой налог»
    error = Column(String(255), default="")
    tries = Column(Integer, default=0)
    next_try_at = Column(Integer, default=0)
    created_at = Column(Integer, default=now)
    updated_at = Column(Integer, default=now)


class Source(Base):
    """A link with a label, to see which video, blogger or ad brings
    customers and money: t.me/<bot>?start=src_<code>."""
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    code = Column(String(40), unique=True, index=True)
    name = Column(String(80), default="")
    cost = Column(Float, default=0)                  # what was spent on it, to see if it paid off
    created_at = Column(Integer, default=now)


class Blacklisted(Base):
    """A Telegram account on one of the shared blacklists the owner follows
    (see app/blacklist.py). Rows of a list are replaced as a whole on every
    refresh; the shop's own blocked customers are not stored here."""
    __tablename__ = "blacklist"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, index=True, nullable=False)
    source = Column(String(255), default="", index=True)   # the URL of the list
    reason = Column(String(200), default="")
    added_at = Column(Integer, default=now)


class ProbeResult(Base):
    """What one of the owner's checkers inside Russia (scripts/ru-probe.py)
    last saw for one address of one server. One row per (checker, address),
    updated on every report."""
    __tablename__ = "probe_results"
    __table_args__ = (UniqueConstraint("probe", "server_id", "endpoint", name="uq_probe_target"),)

    id = Column(Integer, primary_key=True)
    probe = Column(String(60), nullable=False, index=True)    # "Москва · МТС"
    server_id = Column(Integer, index=True, nullable=False)
    endpoint = Column(String(80), default="")
    tcp_ok = Column(Boolean, default=False)
    tls_ok = Column(Boolean, default=False)                   # the handshake got an answer
    error = Column(String(160), default="")
    ms = Column(Integer, default=0)
    fails = Column(Integer, default=0)                        # failed reports in a row
    alerted_at = Column(Integer, default=0)                   # admins were told it is blocked
    at = Column(Integer, default=now)


class TeamMember(Base):
    """A helper of the owner: signs in to the panel through Telegram and sees
    only the sections the owner gave (app/team.py). The owner — ADMIN_LOGIN
    and ADMIN_TELEGRAM_IDS — always sees everything."""
    __tablename__ = "team"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, index=True, nullable=False)
    name = Column(String(64), default="")
    sections = Column(String(128), default="support")     # "stats,sales,people,support,system"
    enabled = Column(Boolean, default=True)
    created_at = Column(Integer, default=now)
    seen_at = Column(Integer, default=0)
