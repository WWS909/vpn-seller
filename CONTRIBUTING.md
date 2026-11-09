# Как помочь проекту

Русский · [English](#english)

Спасибо, что заглянули. Помочь можно и без программирования.

## Нашли ошибку

Откройте [заявку об ошибке](https://github.com/WWS909/vpn-seller/issues/new?template=bug.yml). Чтобы её можно было повторить, напишите:

- что делали и что получилось вместо ожидаемого;
- версию — она видна в меню на сервере (`vpn-seller`) и в админке → «Обновления»;
- какая панель: 3x-ui, Marzban или Remnawave;
- журнал: `vpn-seller` → «Журнал», последние строки с ошибкой.

Уберите из журнала токены, пароли и адреса серверов.

Уязвимость не публикуйте в заявке — см. [SECURITY.md](SECURITY.md).

## Есть идея

Откройте [заявку с идеей](https://github.com/WWS909/vpn-seller/issues/new?template=idea.yml) или напишите в [обсуждениях](https://github.com/WWS909/vpn-seller/discussions). Лучше всего описать задачу («клиенты не находят, где продлить»), а не готовое решение.

## Хотите прислать код

1. Поднимите проект локально — раздел [«Разработка»](README.md#разработка) в README.
2. Небольшие правки присылайте сразу; для большой сначала откройте заявку и договоритесь о подходе.
3. Перед отправкой запустите `python -m pytest`. Если меняли экраны — `python tests/e2e/e2e.py`.
4. Новое поведение — новый тест рядом с похожими в `tests/`.
5. Тексты в приложении и админке есть на двух языках (таблицы `I18N` в `web/index.html` и `web/admin.html`) — добавляйте оба.
6. В `CHANGELOG.md` и `CHANGELOG.en.md` одной строкой опишите, что изменилось для владельца магазина, простыми словами.

GitHub сам прогонит все тесты, включая настоящие панели и установщик. Зелёная отметка — можно смотреть.

## Переводы

Нужны люди, которые проверят английские тексты и добавят новые языки. Строки лежат в `I18N` в `web/index.html`, `web/admin.html` и в `app/core.py` (тексты бота).

---

## English

Thanks for stopping by. You can help without writing code.

- **Found a bug?** Open a [bug report](https://github.com/WWS909/vpn-seller/issues/new?template=bug.yml) with what you did, what happened, the version (server menu `vpn-seller` or admin → Updates), your panel (3x-ui, Marzban or Remnawave) and the last log lines (`vpn-seller` → Logs). Remove tokens, passwords and server addresses. Security issues go through [SECURITY.md](SECURITY.md), not public issues.
- **Have an idea?** Open an [idea](https://github.com/WWS909/vpn-seller/issues/new?template=idea.yml) or start a [discussion](https://github.com/WWS909/vpn-seller/discussions). Describe the problem, not only the solution.
- **Sending code?** See [Development](README.en.md) for the local setup. Run `python -m pytest` (and `python tests/e2e/e2e.py` for screen changes), add a test next to similar ones, add texts in both languages (`I18N` tables), and a line in both changelogs written for a shop owner. CI runs everything, real panels and the installer included.
- **Translations** are welcome: `I18N` in `web/index.html`, `web/admin.html` and the bot texts in `app/core.py`.
