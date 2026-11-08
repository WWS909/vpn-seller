"""
The shop's documents: the terms of service and the privacy policy.

A fresh install has none. The owner writes them in the panel or starts
from the templates below and edits them — the templates are a starting
point, not legal advice.

The text is plain, with three marks:
    # Heading
    - list item
    **bold**
A blank line starts a new paragraph. {brand}, {owner} and {contact} are
filled in when the page is shown, so a renamed shop stays right.
"""

from __future__ import annotations

import html
import re

from sqlalchemy.orm import Session

from . import core

DOCS = ("terms", "privacy")

TEMPLATES = {
    "terms": {
        "ru": """# 1. Общие положения
1.1. Соглашение регулирует использование сервиса {brand} (далее — «Сервис»). Оплачивая тариф, пользователь принимает условия Соглашения.

# 2. Услуга
2.1. Сервис предоставляет доступ к VPN-серверам на срок оплаченного тарифа. Доступ выдаётся автоматически после подтверждения оплаты.

2.2. Число одновременно подключённых устройств и объём трафика ограничены условиями тарифа. Лишнее устройство можно отвязать в приложении.

2.3. Сервис не гарантирует работу в каждой сети в любой момент: доступность зависит в том числе от оператора связи пользователя.

# 3. Оплата и возврат
3.1. Стоимость указана в приложении и на этой странице до оплаты. Автоматических списаний нет: подписка продлевается только вручную.

3.2. Если оплата прошла, а доступ не был выдан по технической причине на стороне Сервиса, пользователь вправе обратиться в поддержку в течение 14 дней: доступ будет выдан или деньги возвращены.

3.3. До отмены платежа через банк пользователь обязуется обратиться в поддержку Сервиса.

# 4. Ограничения
4.1. Запрещено использовать Сервис для действий, нарушающих законодательство, для рассылки спама, атак на сети и распространения вредоносных программ.

4.2. Запрещено передавать доступ третьим лицам сверх оплаченного числа устройств. При нарушении Сервис вправе ограничить доступ.

# 5. Ответственность
5.1. Сервис предоставляется «как есть». Сервис не отвечает за косвенные убытки от использования или недоступности Сервиса.

# 6. Изменения
6.1. Актуальная редакция Соглашения всегда доступна на этой странице.""",
        "en": """# 1. General
1.1. These terms govern the use of {brand} (the “Service”). By paying for a plan the user accepts them.

# 2. The service
2.1. The Service provides access to VPN servers for the paid term. Access is granted automatically after payment is confirmed.

2.2. The number of simultaneously connected devices and the traffic are limited by the plan. A device can be removed in the app.

2.3. Availability in every network at any moment cannot be guaranteed; it also depends on the user’s internet provider.

# 3. Payment and refunds
3.1. Prices are shown in the app and on this page before payment. There is no auto-renewal: a subscription is only renewed manually.

3.2. If payment went through but access was not granted for a technical reason on the Service’s side, the user may contact support within 14 days: access will be granted or the money returned.

3.3. Before disputing a payment with the bank, the user agrees to contact support.

# 4. Restrictions
4.1. Using the Service for illegal activity, spam, network attacks or malware is prohibited.

4.2. Sharing access beyond the paid number of devices is prohibited; the Service may restrict access if this happens.

# 5. Liability
5.1. The Service is provided “as is” and is not liable for indirect losses from its use or unavailability.

# 6. Changes
6.1. The current version of these terms is always on this page.""",
    },
    "privacy": {
        "ru": """# 1. Какие данные обрабатываются
- идентификатор, имя и username в Telegram — для входа и связи;
- история заказов: тариф, сумма, дата, способ оплаты;
- подключённые устройства: модель, система, время последнего обновления подписки — их сообщает VPN-приложение, чтобы работал лимит устройств;
- объём трафика — чтобы работали тарифы с лимитом.

**Данные банковских карт Сервис не получает и не хранит** — платёж целиком обрабатывает платёжный провайдер.

Сервис не ведёт журналов посещаемых пользователем сайтов.

# 2. Зачем
Только для работы Сервиса: выдачи оплаченного доступа, учёта устройств и трафика, уведомлений об окончании подписки, ответов поддержки и предотвращения злоупотреблений.

# 3. Передача
Данные передаются платёжному провайдеру в объёме, нужном для оплаты, и по требованию закона. Данные не продаются и не используются для рекламы.

# 4. Хранение и удаление
Данные хранятся, пока пользователь пользуется Сервисом, и удаляются по обращению в поддержку, кроме сведений, которые закон обязывает сохранить.""",
        "en": """# 1. What is processed
- Telegram id, name and username — to sign in and to contact the user;
- order history: plan, amount, date, payment method;
- connected devices: model, system, last subscription refresh — reported by the VPN app so the device limit works;
- traffic volume — for plans with a traffic limit.

**The Service never receives or stores card details** — payments are processed entirely by the payment provider.

The Service does not log the websites users visit.

# 2. Why
Only to run the Service: granting paid access, counting devices and traffic, reminders about the end of the term, support and abuse prevention.

# 3. Sharing
Data is shared with the payment provider as far as needed for payment, and when required by law. It is never sold or used for advertising.

# 4. Storage and deletion
Data is kept while the Service is used and deleted on request to support, except what the law requires to keep.""",
    },
}


def text(db: Session, doc: str, lang: str) -> str:
    """The owner's text of a document in a language, or in the other one
    when only one was written. "" = there is no such document."""
    own = core.get(db, f"{doc}_{lang}").strip()
    if own:
        return own
    other = "en" if lang == "ru" else "ru"
    return core.get(db, f"{doc}_{other}").strip()


def exists(db: Session) -> bool:
    """At least one document is written."""
    return any(text(db, d, "ru") for d in DOCS)


def complete(db: Session) -> bool:
    """Both documents are written (in at least one language)."""
    return all(text(db, d, "ru") for d in DOCS)


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def to_html(raw: str, values: dict) -> str:
    """The owner's plain text as safe HTML: everything is escaped first,
    then the three marks become tags."""
    body = html.escape(raw.replace("\r\n", "\n"))
    for key, value in values.items():
        body = body.replace("{" + key + "}", html.escape(str(value or "")))
    out, para, items = [], [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for line in body.split("\n"):
        s = _BOLD.sub(r"<b>\1</b>", line.strip())
        if not s:
            flush()
        elif s.startswith("# "):
            flush()
            out.append(f"<h3>{s[2:].strip()}</h3>")
        elif s.startswith("- "):
            if para:
                flush()
            items.append(s[2:].strip())
        else:
            if items:
                flush()
            para.append(s)
    flush()
    return "".join(out)


def public(db: Session) -> dict:
    """The documents for the public page: {doc: {ru: html, en: html}},
    only those that exist."""
    values = {"brand": core.get(db, "brand_name"), "owner": core.get(db, "legal_owner"),
              "contact": core.get(db, "legal_contact")}
    out = {}
    for doc in DOCS:
        pair = {lang: to_html(text(db, doc, lang), values) for lang in ("ru", "en")}
        if pair["ru"]:
            out[doc] = pair
    return out
