"""
A thank-you for a review: bonus days, once per customer.

Given when the admin PUBLISHES the review (not when it is written: a
two-word review written for the days gets nothing until a person read it),
only to someone who has paid at least once (a fresh account with a trial
cannot farm days), and taken with one conditional UPDATE so that two
clicks on "publish" cannot give it twice.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from . import core
from .config import settings as env
from .models import Review, Transaction, User, now


def reward_days(db: Session) -> int:
    return max(0, min(365, core.get_int(db, "review_reward_days", 0)))


def paid_before(db: Session, user: User) -> bool:
    return db.query(Transaction.id).filter(
        Transaction.user_id == user.id, Transaction.status == "success",
        Transaction.amount > 0, Transaction.method != "admin",
        Transaction.gateway != "test").first() is not None


def bonus_for(db: Session, user: User) -> int:
    """What this person would get for a review now (0 = nothing)."""
    days = reward_days(db)
    if not days or user.review_rewarded_at or not paid_before(db, user):
        return 0
    return days


def reward(db: Session, review: Review) -> int:
    """Called when a review is published. Returns the days given."""
    from . import access, notify
    user = db.get(User, review.user_id)
    if user is None:
        return 0
    days = bonus_for(db, user)
    if not days:
        return 0
    took = db.execute(update(User).where(User.id == user.id, User.review_rewarded_at == 0)
                      .values(review_rewarded_at=now())).rowcount
    db.commit()
    if not took:
        return 0
    with access.user_lock(user.id):
        db.refresh(user)
        user.expires_at = max(now(), int(user.expires_at or 0)) + days * 86400
        db.commit()
        try:
            access.push(db, user)
        except Exception:  # noqa: BLE001 — the days are saved; the sync job pushes them
            pass
    core.audit(db, "Review reward", f"tg:{user.tg_id}", f"+{days} d for review #{review.id}")
    notify.send_safe(db, user, core.t(user.lang or env.default_lang, "review_reward", days=days))
    return days
