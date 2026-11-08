"""
The referral program. Off until the owner turns it on.

A person shares their link (t.me/<bot>?start=r<id>). Whoever comes through
it for the first time becomes their referral — right away, or after a
captcha in the bot when the owner turned it on against bot farms.

The owner picks what an inviter gets when a friend pays:
* "days"  — `ref_days` bonus days (the first payment, or every one with
  `ref_every`);
* "money" — `ref_percent` % of the payment on a balance. The balance can
  be withdrawn by a request (card, phone number, crypto — the methods the
  owner set up; the owner pays by hand and marks it paid) and, if the
  owner allows, can pay for a subscription.
In both modes the friend may get `ref_friend_days` on their first purchase.

Money moves only with conditional UPDATEs: a balance is never taken twice
by two requests at once, a request is decided once.
"""

from __future__ import annotations

import json
import random
import re

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import core
from .config import settings as env
from .models import Payout, RefReward, Transaction, User, now

MAX_PERCENT = 90
PAYOUT_KINDS = ("card", "sbp", "crypto", "other")
_EPS = 0.0001


def enabled(db: Session) -> bool:
    return core.get(db, "ref_enabled") == "1"


def mode(db: Session) -> str:
    return "money" if core.get(db, "ref_mode") == "money" else "days"


def percent(db: Session) -> int:
    return max(0, min(MAX_PERCENT, core.get_int(db, "ref_percent", 0)))


def code_of(user: User) -> str:
    return f"r{user.id}"


def link(db: Session, user: User) -> str:
    from . import notify
    name = notify.username()
    return f"https://t.me/{name}?start={code_of(user)}" if name else ""


def _referrer_by_code(db: Session, code: str) -> User | None:
    code = (code or "").strip()
    if not code.startswith("r") or not code[1:].isdigit():
        return None
    return db.get(User, int(code[1:]))


def _is_fresh(db: Session, user: User) -> bool:
    """Only a newcomer can become somebody's referral."""
    from .billing import paid_before
    return (user.referrer_id is None and not user.ref_pending and not user.expires_at
            and not paid_before(db, user))


def attach(db: Session, user: User, code: str) -> str:
    """Link a newcomer to the inviter. Returns "linked", "captcha" or ""."""
    if not enabled(db):
        return ""
    ref = _referrer_by_code(db, code)
    if not ref or ref.id == user.id or not _is_fresh(db, user) or ref.referrer_id == user.id:
        return ""
    if core.get(db, "ref_captcha") == "1":
        user.ref_pending = ref.id
        user.captcha = ""
        db.commit()
        return "captcha"
    user.referrer_id = ref.id
    db.commit()
    core.audit(db, "Referral", f"tg:{user.tg_id}", f"invited by tg:{ref.tg_id}")
    return "linked"


# ---------------------------------------------------------------------------
#  Captcha: pick the named number among four buttons
# ---------------------------------------------------------------------------
def captcha(db: Session, user: User) -> tuple[str, list[str]]:
    options = random.sample(range(10, 100), 4)
    answer = str(random.choice(options))
    user.captcha = answer
    db.commit()
    lang = user.lang or env.default_lang
    return core.t(lang, "captcha", n=answer), [str(o) for o in options]


def solve(db: Session, user: User, answer: str) -> bool | None:
    """True — solved, the referral counts; False — wrong, ask again;
    None — nothing was pending."""
    if not user.ref_pending:
        return None
    if user.captcha and answer == user.captcha:
        ref = db.get(User, user.ref_pending)
        user.ref_pending, user.captcha = 0, ""
        if ref and _is_fresh_after_captcha(db, user):
            user.referrer_id = ref.id
            db.commit()
            core.audit(db, "Referral", f"tg:{user.tg_id}", f"invited by tg:{ref.tg_id} (captcha)")
            return True
        db.commit()
        return True
    return False


def _is_fresh_after_captcha(db: Session, user: User) -> bool:
    from .billing import paid_before
    return user.referrer_id is None and not paid_before(db, user)


# ---------------------------------------------------------------------------
#  Rewards
# ---------------------------------------------------------------------------
def _first_paid(db: Session, tx: Transaction, user: User, kinds: tuple = ("purchase",)) -> bool:
    return db.query(Transaction).filter(Transaction.user_id == user.id, Transaction.id != tx.id,
                                        Transaction.status == "success", Transaction.amount > 0,
                                        Transaction.kind.in_(kinds)).first() is None


def friend_bonus(db: Session, tx: Transaction, user: User) -> int:
    """Extra days for the invited person's first paid purchase. Not for an
    order paid from the referral balance: that is the shop's own money."""
    if not (enabled(db) and user.referrer_id and tx.kind == "purchase" and tx.amount > 0
            and tx.method != "balance"):
        return 0
    return max(0, core.get_int(db, "ref_friend_days", 0)) if _first_paid(db, tx, user) else 0


def reward(db: Session, tx: Transaction, user: User) -> int | float:
    """What the inviter gets after a successful payment of their referral:
    days or money, by the owner's choice. Returns the amount given."""
    if not (enabled(db) and user.referrer_id and tx.amount > 0):
        return 0
    ref = db.get(User, user.referrer_id)
    if not ref:
        return 0
    if mode(db) == "money":
        return _reward_money(db, tx, user, ref)
    return _reward_days(db, tx, user, ref)


def _reward_days(db: Session, tx: Transaction, user: User, ref: User) -> int:
    from . import access, billing, notify
    if tx.kind != "purchase" or tx.method == "balance":
        return 0
    days = max(0, core.get_int(db, "ref_days", 0))
    if not days:
        return 0
    if core.get(db, "ref_every") != "1" and \
            db.query(RefReward).filter(RefReward.referee_id == user.id).first():
        return 0
    db.add(RefReward(referrer_id=ref.id, referee_id=user.id, tx_id=tx.id, days=days,
                     amount=tx.amount))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return 0                        # this payment was already rewarded
    # Under the inviter's lock and from a fresh read: the inviter may be
    # paying for their own order at this very moment, and one of the two
    # "+days" would otherwise be lost.
    with access.user_lock(ref.id):
        db.refresh(ref)
        ref.expires_at = max(now(), int(ref.expires_at or 0)) + days * 86400
        if not ref.plan_id and not ref.device_limit:
            # Never bought anything: as many devices as the trial gives.
            ref.device_limit = billing.trial_info(db)["devices"] or 1
        ref.ref_days = int(ref.ref_days or 0) + days
        db.commit()
        try:
            access.push(db, ref)
        except Exception:  # noqa: BLE001 — the sync job retries; the days are saved
            pass
    lang = ref.lang or env.default_lang
    notify.send_safe(db, ref, core.t(lang, "ref_reward", days=days))
    core.audit(db, "Referral reward", f"tg:{ref.tg_id}", f"+{days} d for #{tx.id} of tg:{user.tg_id}")
    return days


def _reward_money(db: Session, tx: Transaction, user: User, ref: User) -> float:
    from . import notify
    # An order paid from a balance is money the shop already counted once:
    # a share of it would pay the same money out twice.
    if tx.method == "balance" or tx.kind not in ("purchase", "devices", "traffic", "bypass", "gift"):
        return 0
    share = round(float(tx.amount) * percent(db) / 100, 2)
    if share <= 0:
        return 0
    if core.get(db, "ref_every") != "1" and \
            db.query(RefReward).filter(RefReward.referee_id == user.id).first():
        return 0
    # The reward row and the balance go in one commit: the unique tx_id
    # makes a second reward for the same payment fail as a whole.
    db.add(RefReward(referrer_id=ref.id, referee_id=user.id, tx_id=tx.id, money=share,
                     amount=tx.amount))
    db.execute(update(User).where(User.id == ref.id)
               .values(ref_balance=User.ref_balance + share, ref_earned=User.ref_earned + share))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return 0
    db.refresh(ref)
    lang = ref.lang or env.default_lang
    notify.send_safe(db, ref, core.t(lang, "ref_reward_money", amount=core.money(share, lang),
                                     balance=core.money(ref.ref_balance, lang)))
    core.audit(db, "Referral reward", f"tg:{ref.tg_id}",
               f"+{share:g} {env.currency} for #{tx.id} of tg:{user.tg_id}")
    return share


# ---------------------------------------------------------------------------
#  Payout methods — set by the owner
# ---------------------------------------------------------------------------
def clean_methods(raw) -> list[dict]:
    """The owner's list of payout methods, checked and trimmed."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    out, seen = [], set()
    for i, m in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(m, dict) or m.get("kind") not in PAYOUT_KINDS:
            continue
        ru = str(m.get("ru") or "").strip()[:40]
        en = str(m.get("en") or "").strip()[:40] or ru
        if not ru:
            continue
        mid = re.sub(r"[^a-z0-9_-]", "", str(m.get("id") or "").lower())[:24] or f"{m['kind']}{i + 1}"
        while mid in seen:
            mid += "x"
        seen.add(mid)
        out.append({"id": mid, "kind": m["kind"], "ru": ru, "en": en,
                    "hint_ru": str(m.get("hint_ru") or "").strip()[:80],
                    "hint_en": str(m.get("hint_en") or "").strip()[:80],
                    "on": bool(m.get("on", True))})
        if len(out) >= 8:
            break
    return out


def payout_methods(db: Session, only_on: bool = True) -> list[dict]:
    methods = clean_methods(core.get(db, "ref_payout_methods"))
    return [m for m in methods if m["on"]] if only_on else methods


def payouts_open(db: Session) -> bool:
    """A referrer can ask for a withdrawal now. Also after the program was
    switched off: money already earned stays the person's (every request
    is still approved by the admin by hand)."""
    return mode(db) == "money" and bool(payout_methods(db))


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _details(kind: str, account: str, bank: str) -> str:
    """Where to send the money, checked for the method's kind."""
    from .billing import OrderError
    account = (account or "").strip()
    bank = (bank or "").strip()
    if kind == "card":
        digits = re.sub(r"[\s-]", "", account)
        if not (digits.isdigit() and 13 <= len(digits) <= 19 and _luhn(digits)):
            raise OrderError("payout_card")
        return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4))
    if kind == "sbp":
        digits = re.sub(r"\D", "", account)
        if not (10 <= len(digits) <= 15) or not (2 <= len(bank) <= 40):
            raise OrderError("payout_phone")
        return f"+{digits} · {bank}"
    if kind == "crypto":
        if not re.fullmatch(r"[A-Za-z0-9:_-]{20,128}", account):
            raise OrderError("payout_wallet")
        return account
    if not (3 <= len(account) <= 200):
        raise OrderError("payout_details")
    return account[:200]


# ---------------------------------------------------------------------------
#  Withdrawals
# ---------------------------------------------------------------------------
def request_payout(db: Session, user: User, method_id: str, account: str, bank: str = "",
                   amount: float | None = None) -> Payout:
    """A referrer asks to withdraw. The amount leaves the balance right away
    (one conditional UPDATE), so two requests at once cannot both take it."""
    from .billing import OrderError
    if not payouts_open(db):
        raise OrderError("payout_off")
    method = next((m for m in payout_methods(db) if m["id"] == method_id), None)
    if not method:
        raise OrderError("payout_method")
    details = _details(method["kind"], account, bank)
    if db.query(Payout).filter(Payout.user_id == user.id, Payout.status == "pending").first():
        raise OrderError("payout_pending")
    db.refresh(user)
    balance = round(float(user.ref_balance or 0), 2)
    value = balance if amount is None else round(float(amount), 2)
    minimum = max(0.0, core.get_float(db, "ref_min_payout", 0))
    if value <= 0 or value < minimum - _EPS:
        if minimum:
            raise OrderError("payout_min", {"min": core.money(minimum, user.lang)})
        raise OrderError("balance_low")
    taken = db.execute(update(User).where(User.id == user.id, User.ref_balance + _EPS >= value)
                       .values(ref_balance=User.ref_balance - value)).rowcount
    if not taken:
        db.rollback()
        raise OrderError("balance_low")
    lang = user.lang or env.default_lang
    p = Payout(user_id=user.id, amount=value, currency=env.currency, method=method["id"],
               method_title=method["en" if lang == "en" else "ru"], details=details)
    db.add(p)
    db.commit()                         # the balance and the request together
    db.refresh(p)
    core.audit(db, "Payout requested", f"tg:{user.tg_id}", f"#{p.id} {value:g} {env.currency} · {p.method_title}")
    try:
        from . import botadmin
        botadmin.payout_notice(db, p)
    except Exception as e:  # noqa: BLE001 — the request is saved; the panel shows it
        core.log.warning("payout notice failed: %s", e)
    return p


def _refund(db: Session, p: Payout) -> None:
    db.execute(update(User).where(User.id == p.user_id)
               .values(ref_balance=User.ref_balance + p.amount))


def cancel_payout(db: Session, user: User, payout_id: int) -> bool:
    """The referrer takes their own request back while it is still waiting."""
    got = db.execute(update(Payout).where(Payout.id == payout_id, Payout.user_id == user.id,
                                          Payout.status == "pending")
                     .values(status="cancelled", done_at=now(), done_by=f"tg:{user.tg_id}")).rowcount
    if not got:
        db.rollback()
        return False
    p = db.get(Payout, payout_id)
    _refund(db, p)
    db.commit()
    core.audit(db, "Payout cancelled", f"tg:{user.tg_id}", f"#{payout_id}")
    return True


def decide(db: Session, payout_id: int, paid: bool, actor: str, note: str = "") -> Payout | None:
    """The owner marks a request paid (after sending the money) or rejects
    it (the money returns to the balance). Only a waiting request changes,
    and only once — a double click does nothing the second time."""
    from . import notify
    status = "paid" if paid else "rejected"
    got = db.execute(update(Payout).where(Payout.id == payout_id, Payout.status == "pending")
                     .values(status=status, done_at=now(), done_by=str(actor)[:64],
                             note=(note or "").strip()[:255])).rowcount
    if not got:
        db.rollback()
        return None
    p = db.get(Payout, payout_id)
    if not paid:
        _refund(db, p)
    db.commit()
    db.refresh(p)
    user = db.get(User, p.user_id)
    core.audit(db, "Payout " + status, actor, f"#{p.id} {p.amount:g} {p.currency} tg:{user.tg_id if user else '?'}")
    if user:
        lang = user.lang or env.default_lang
        if paid:
            text = core.t(lang, "payout_paid", amount=core.money(p.amount, lang), method=p.method_title)
        else:
            reason = f": {p.note}" if p.note else ""
            text = core.t(lang, "payout_rejected", amount=core.money(p.amount, lang), reason=reason)
        notify.send_safe(db, user, text)
    return p


def payout_out(p: Payout, full: bool = False) -> dict:
    out = {"id": p.id, "amount": round(p.amount, 2), "currency": p.currency, "method": p.method_title,
           "status": p.status, "note": p.note, "created_at": p.created_at, "done_at": p.done_at}
    if full:
        u = p.user
        out.update({"details": p.details, "method_id": p.method, "done_by": p.done_by,
                    "tg_id": u.tg_id if u else 0, "username": u.username if u else "",
                    "first_name": u.first_name if u else "",
                    "balance": round(float(u.ref_balance or 0), 2) if u else 0})
    else:
        out["details"] = _mask_details(p.details)
    return out


def _mask_details(d: str) -> str:
    """The customer sees their own details half hidden in the history."""
    d = d or ""
    if len(d) <= 8:
        return d
    return d[:4] + "…" + d[-4:]


# ---------------------------------------------------------------------------
#  Paying for an order from the balance
# ---------------------------------------------------------------------------
def pay_with_balance(db: Session, tx: Transaction, user: User) -> dict:
    """
    The order is paid from the referral balance: the money leaves the
    balance and the order becomes "paid" in one commit, then it is granted
    like any paid order. If no server takes the customer right now, the
    order stays paid and the retry job grants it — nothing is lost.
    """
    from . import billing
    from .billing import OrderError
    if core.get(db, "ref_balance_pay") != "1":
        raise OrderError("balance_off")
    amount = round(float(tx.amount), 2)
    taken = db.execute(update(User).where(User.id == user.id, User.ref_balance + _EPS >= amount)
                       .values(ref_balance=User.ref_balance - amount)).rowcount
    if not taken:
        db.rollback()
        db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.status == "pending")
                   .values(status="cancelled", error="not enough balance"))
        db.commit()
        raise OrderError("balance_low")
    moved = db.execute(update(Transaction).where(Transaction.id == tx.id, Transaction.status == "pending")
                       .values(status="paid", gateway="balance", external_id=f"balance:{tx.id}")).rowcount
    if not moved:
        db.rollback()                   # the balance stays as it was
        raise OrderError("order_not_found")
    db.commit()
    core.audit(db, "Paid from the balance", f"tg:{user.tg_id}", f"#{tx.id} {amount:g} {env.currency}")
    db.refresh(tx)
    try:
        billing.finish(db, tx)
    except Exception:  # noqa: BLE001 — stays "paid"; the retry job grants it
        return {"type": "paid"}
    return {"type": "done"}


# ---------------------------------------------------------------------------
#  Numbers
# ---------------------------------------------------------------------------
def stats(db: Session, user: User) -> dict:
    def total(q) -> float:
        return round(float(q.scalar() or 0), 2)
    return {
        "invited": db.query(User).filter(User.referrer_id == user.id).count(),
        "paid": db.query(func.count(func.distinct(RefReward.referee_id)))
                  .filter(RefReward.referrer_id == user.id).scalar() or 0,
        "days": int(user.ref_days or 0),
        "balance": max(0.0, round(float(user.ref_balance or 0), 2)),
        "earned": round(float(user.ref_earned or 0), 2),
        "withdrawn": total(db.query(func.sum(Payout.amount))
                           .filter(Payout.user_id == user.id, Payout.status == "paid")),
        "pending": total(db.query(func.sum(Payout.amount))
                         .filter(Payout.user_id == user.id, Payout.status == "pending")),
        "spent": total(db.query(func.sum(Transaction.amount))
                       .filter(Transaction.user_id == user.id, Transaction.method == "balance",
                               Transaction.status.in_(("success", "paid", "processing")))),
    }


def friends(db: Session, user: User, limit: int = 30) -> list[dict]:
    """The person's own referrals, newest first, names half hidden."""
    rows = (db.query(User).filter(User.referrer_id == user.id)
            .order_by(User.id.desc()).limit(limit).all())
    if not rows:
        return []
    ids = [u.id for u in rows]
    got = {rid: (int(d or 0), round(float(m or 0), 2)) for rid, d, m in
           db.query(RefReward.referee_id, func.sum(RefReward.days), func.sum(RefReward.money))
           .filter(RefReward.referrer_id == user.id, RefReward.referee_id.in_(ids))
           .group_by(RefReward.referee_id).all()}
    return [{"name": mask(u), "at": u.created_at, "paid": u.id in got,
             "days": got.get(u.id, (0, 0))[0], "money": got.get(u.id, (0, 0))[1]} for u in rows]


def mask(user: User) -> str:
    name = (user.username or user.first_name or "").strip()
    return (name[:3] + "***") if name else "***"


def top(db: Session, full: bool = False) -> list[dict]:
    """The top of inviters with the owner's settings (period, type, size).
    "days" means the reward: days in days mode, money in money mode."""
    period = core.get(db, "top_period")
    kind = core.get(db, "top_type")
    size = max(3, min(50, core.get_int(db, "top_size", 10)))
    since = {"week": now() - 7 * 86400, "month": now() - 30 * 86400}.get(period, 0)
    if kind == "invited":
        q = (db.query(User.referrer_id, func.count(User.id))
             .filter(User.referrer_id.isnot(None), User.created_at >= since)
             .group_by(User.referrer_id))
    else:
        if kind == "days":
            value = func.sum(RefReward.money) if mode(db) == "money" else func.sum(RefReward.days)
        else:
            value = func.count(func.distinct(RefReward.referee_id))
        q = (db.query(RefReward.referrer_id, value)
             .filter(RefReward.created_at >= since).group_by(RefReward.referrer_id))
    rows = sorted(q.all(), key=lambda r: -(r[1] or 0))[:size]
    users = {u.id: u for u in db.query(User).filter(User.id.in_([r[0] for r in rows])).all()} if rows else {}
    out = []
    for i, (uid, value) in enumerate(rows, 1):
        u = users.get(uid)
        if not u:
            continue
        v = round(float(value or 0), 2)
        item = {"place": i, "name": mask(u), "value": int(v) if v == int(v) else v}
        if full:
            item.update({"tg_id": u.tg_id, "username": u.username, "first_name": u.first_name,
                         **stats(db, u)})
        out.append(item)
    return out
