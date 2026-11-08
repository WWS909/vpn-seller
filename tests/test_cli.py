"""The small commands the server menu (install.sh) runs inside the container."""
from __future__ import annotations

import time

from conftest import Fake, tg


def _lines(capsys) -> dict:
    return dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines() if "=" in line)


def test_summary_for_the_menu_header(client, db, capsys):
    from app import cli
    from app.models import Transaction, User
    client.get("/api/app/me", headers=tg(91001))
    u = db.query(User).filter(User.tg_id == 91001).one()
    u.expires_at = int(time.time()) + 86400
    db.add(Transaction(user_id=u.id, kind="purchase", amount=290, currency="RUB", method="card", status="success",
                       created_at=int(time.time()), paid_at=int(time.time())))
    db.commit()
    assert cli.main(["summary"]) == 0
    d = _lines(capsys)
    assert d["orders"] == "1" and "290" in d["revenue"] and d["users"] == "1" and d["active"] == "1"
    assert d["version"] and d["latest"] == "" and d["auto"] == "0"


def test_auto_update_and_report_from_the_menu(client, admin, db, capsys):
    from app import cli
    assert cli.main(["auto-update", "on", "4"]) == 0
    capsys.readouterr()
    v = client.get("/api/admin/updates", headers=admin).json()
    assert v["auto"] is True and v["hour"] == 4
    assert cli.main(["auto-update", "off"]) == 0
    assert client.get("/api/admin/updates", headers=admin).json()["auto"] is False
    assert cli.main(["auto-update", "maybe"]) == 2
    Fake.reset()
    assert cli.main(["report"]) == 0
    assert "sent=1" in capsys.readouterr().out
    assert any("Итоги за сутки" in b.get("text", "") for b in Fake.sent("sendMessage"))
