<div align="center">

# VPN Seller

**Your own VPN subscription shop in Telegram: a Mini App and an admin panel for 3x-ui, Marzban and Remnawave**

[![Tests](https://github.com/WWS909/vpn-seller/actions/workflows/tests.yml/badge.svg)](https://github.com/WWS909/vpn-seller/actions/workflows/tests.yml)
[![Demo](https://img.shields.io/badge/demo-@Cristal__vpn__app-2AABEE?logo=telegram&logoColor=white)](https://t.me/Cristal_vpn_app)
[![MIT license](https://img.shields.io/badge/license-MIT-3DA639)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)

[Русский](README.md) · English

**[Try the demo](https://t.me/Cristal_vpn_app)** · [Install](#quick-start) · [Features](#features) · [Screenshots](#screenshots) · [What's new](CHANGELOG.en.md)

</div>

Most open-source projects for selling VPN keys are chat bots with buttons. VPN Seller is a full app inside Telegram: the user opens it from the bot, picks a plan, pays and connects a device in a couple of taps. Keys are issued on your own [3x-ui](https://github.com/MHSanaei/3x-ui), [Marzban](https://github.com/Gozargah/Marzban) or [Remnawave](https://github.com/remnawave/panel) servers, and you run everything from a web admin panel or right from the bot.

> **See it live.** Open [@Cristal_vpn_app](https://t.me/Cristal_vpn_app) and tap "Open" — it's a shop running on VPN Seller. That's what your customers see: home, plans, payment, connecting, support.

```bash
wget -qO install.sh https://raw.githubusercontent.com/WWS909/vpn-seller/main/install.sh && sudo bash install.sh --en
```

![The app](docs/screenshots/app-dark.png)

### How it differs from a bot with buttons

| | A usual bot | VPN Seller |
|---|---|---|
| What the customer sees | messages and buttons | an app: screens, days and traffic left, devices, locations |
| Connecting | a key pasted into the chat | one subscription link for every server, one tap into Happ, v2RayTun and others, a QR code |
| Payment | usually one gateway | Telegram Stars, cards, SBP and crypto — 15 payment providers to pick from, balance and referrals |
| Running it | bot commands | a web admin panel with charts, and the same panel inside Telegram |
| Look | — | 3 home screens and 6 styles, your colour, logo, 30 fonts, 13 icon styles, opening screen |
| Install and updates | by hand | one command, a guided installer, a new version from GitHub in one click |

### Installing — five questions

The installer asks for the bot token, your Telegram ID (it can find it itself), the address, a password and the currency. Everything else — Docker, HTTPS, the bot webhook, the "Open" button — is set up for you.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/install-1.png" alt="Installer: questions"></td>
<td width="50%"><img src="docs/screenshots/install-2.png" alt="Installer: done"></td>
</tr>
</table>

After that the server has a `vpn-seller` command — a menu with the shop's state and every action: update, logs, backup, payment methods, a new address, the night auto-update. Every item has a way back. (The screenshots show the Russian installer; `--en` runs it in English.)

<p align="center"><img src="docs/screenshots/server-menu.png" alt="The server menu" width="420"></p>

### Your own look in a couple of minutes

The look is built from parts, and the panel shows the real app next to them. The two main parts are the **home screen** (what the app opens with) and the **style**. Below is the same shop in the Clean style with its three home screens: Answer, Term and Devices.

<p align="center"><img src="docs/screenshots/homes-en.webp" alt="Three home screens"></p>

<p align="center"><img src="docs/screenshots/homes-dark.webp" alt="The same in Telegram's dark theme"></p>

And this is the same home screen in five styles:

<p align="center"><img src="docs/screenshots/styles-en.webp" alt="Five styles"></p>

![Look: home screen, style and a live preview](docs/screenshots/admin-layouts.webp)

On top of that: the font (30 of them or any from Google Fonts), colour, icons and logo:

<p align="center"><img src="docs/screenshots/looks-en.webp" alt="Fonts and icons"></p>

## Features

**For users**
- Home screen with days left, traffic and devices; locations list; device management.
- Plans with monthly price, discount and badge; a one-time free trial; promo codes.
- One subscription link for all servers at once; one-tap import into Happ, v2RayTun, Incy, OneXray, Karing or Hiddify (the owner picks which to show and which first), QR code, link reset.
- Extra devices and traffic packs, gift subscriptions shared as `t.me/<bot>?start=gift…` links.
- **Support chat** right in the app and in the bot; answers come to the same chat.
- Reviews, FAQ, an "About" page.
- Russian and English (follows the Telegram language, switchable), dark and light theme.

**For the owner**
- **A clean start**: after the install the shop is empty — no plans, no discounts, no documents, the referral program is off. Nothing is sold or given away until you decide; plans, documents and FAQ have templates you edit before saving.
- **Launch tab**: required steps (bot, server, plans, payments, documents, trying it as a customer) and recommended ones (name, logo, trial, seller, bot description, your own FAQ, referrals, warm-up, domain, backups). Ticks come only from facts.
- Admin panel: overview with revenue and problems, **analytics** (funnel "came → opened → trial → connected → paid → renewed", revenue by payment method and plan, churn, LTV), orders, plans, promo codes, gifts, users, broadcasts, reviews, servers, look, settings, log. Works on a phone too.
- **Run it from Telegram**: commands and buttons in the bot itself, and the whole panel opens inside Telegram without a password — Telegram's signature identifies the admin.
- **Your own look**: built from parts — logo, colour, background, font, icons, cards, animations, opening screen; no finished themes. Text colours are adjusted automatically to stay readable.
- **Free trial**: days, devices and traffic — one card under Plans, or `/trial 7` in the bot.
- **Customer warm-up**: a sale on every plan, system notices (subscription ends soon, has ended, trial ends, no purchase after the first visit, traffic runs low) and your own campaigns by segment. Everything with a discount is off until you turn it on.
- **Referrals, your choice**: subscription days or money — a share of friends' payments on a balance. The balance is withdrawn by request (card, SBP, crypto — you set the methods and close requests in the panel or with a button in the bot) or spent on one's own subscription. A captcha against farms, a top of inviters.
- **Mandatory channel subscription** — one or several channels (up to five), for the whole service or only for the free trial.
- **Support in the bot**: customer questions reach the admins with the customer's card; answer by replying in the bot or from the panel — the customer gets it from the bot.
- **Sources**: a bot link of your own for every video, blogger or ad — see how many people came, how many paid, how much money they brought and whether the ad paid off.
- **A check of the VPN itself**: every 2 minutes the shop checks the connection port from the keys; a server with a dead VPN is left out of subscriptions for a while and the admins are told.
- **Renew in one tap**: the reminder about the end of a subscription comes with a Stars invoice for the same plan.
- **Server load** in Locations: not busy / medium / busy — by the people online on the panel.
- **Speed up a VPN server** with one command (`tune.sh`): BBR, big network buffers, limits.
- **3x-ui (2.x and 3.x), Marzban and Remnawave panels** — any number of servers, mixed: a customer gets one subscription from all of them. Connection test and inbound picker right in the form.
- **Daily results in Telegram** every evening (revenue, purchases, new people, what needs a look), **orders and customers to Excel**, a reminder about an **abandoned payment**.
- **Updates from GitHub**: the shop finds a new version itself, sends "what's new" to Telegram and updates with one button (in the bot or the panel). A database backup comes first; if the new version does not start, the previous one comes back by itself. An automatic night update can be turned on.
- **One-command install**: the script installs Docker, asks for the domain, token and password, gets the certificate and starts everything; it also updates, backs up and restores.
- **Access after the end**: the owner sets how many days the VPN keeps working after a subscription ends (0–30, default 0). The app shows “N days until it stops” and the date, the VPN app a line “VPN works until …”. Not for the free trial; a renewal counts from the paid end or from today — these days are not sold.
- **Emergency mode**: one button (or `/emergency on text` in the bot) — a message to every customer at the top of the app and after /start, sales and trials stopped (paid orders are still granted, discount reminders wait) and, optionally, “cut nobody off” while it lasts.
- **Telegram only after the end**: once the VPN has stopped, Telegram keeps working N more hours (the owner chooses) so people can reach the bot and renew. The server gets a separate inbound (a squad in Remnawave) whose routing lets only Telegram through; the shop puts a separate client there while the real one stays off.
- **Emergency mode by itself**: when the panels or the VPN of every server do not answer for longer than set, the mode turns on by itself and off once a server is back. A mode the owner turned on is never turned off by itself.
- **Reachable from Russia**: your own free checker (`scripts/ru-probe.py`, one command on any Russian VPS, several for cities and operators) connects to the servers like a VPN app every 5 minutes and alerts when an operator cuts them off. Plus paid checks from dozens of regions through DPI Checker (`DPICHECKER_API_KEY`), with the price shown before starting.
- **Blacklists**: follow other shops' lists of Telegram IDs (trial farmers, chargebacks) — no trial, or no sales at all; share your own at `/blacklist.txt`.
- **Team**: helpers by Telegram ID with chosen sections (overview, sales, customers, support, system). The server checks the rights on every request; support questions in the bot reach them too.
- **Diagnostics**: the Emergency section of the panel and `/diag` in the bot really check the database, the bot and its webhook, the site's certificate, every panel, the VPN on the servers, payments and stuck orders, background jobs, backups and disk space — and say what to do.
- **Panel sign-in alerts**: when someone signs in to the panel with the password from a new address, the admins get a Telegram message with the address and the device (Chrome · Windows and so on). Password guessing is reported too — once, at the moment the address is shut.
- **Automatic backups**: every day a database archive goes to the admins in Telegram; plus a button in the panel and `/backup`.
- Whitelist-bypass servers for mobile internet: their traffic is sold by the gigabyte (in plans and as packs); without gigabytes the server is not given, when they run out only it goes off.
- Payment gateways and their keys are set up right in the admin panel (Payments), no terminal needed; keys in .env work too.
- Device limit by HWID (Happ, v2RayTun, Incy and other clients sending `X-HWID`), traffic limit with blocking; traffic top-ups are optional (Settings → Traffic top-ups, off at first).
- A removed device gets no subscription until the customer brings it back; unused bought traffic carries over on renewal.
- A new link is given only if at least one server took it; on Remnawave, a reset link or a switched-off user drops open connections at once.
- If a server is down, the purchase still goes through on the others and the missed server catches up in the background. A paid order is never lost: if every server fails, it waits and is delivered automatically.
- Telegram notifications for every payment and server outage.
- A public service page with prices, contacts and your documents — terms and privacy policy (payment providers need it for review). Written under Documents, starting from a template if you like.

**Payments**

| Method | What you need |
|---|---|
| Telegram Stars | Nothing — works out of the box |
| CryptoBot | App token from @CryptoBot → Crypto Pay |
| YooKassa | Shop ID and secret key |
| FreeKassa | Shop ID and two secrets (+ the API key, so the shop checks unconfirmed payments itself) |
| Platega | Merchant ID and secret |
| PayPalych (pally.info) | API token and shop ID |
| Lava | Shop ID, secret and additional keys |
| WATA | API token |
| Heleket (crypto) | Merchant ID and API key |
| Tribute | API key |
| CloudPayments | Public ID and API secret |
| MulenPay | Shop ID, API key and secret key |
| RollyPay | API key and signing secret (SBP, cards, crypto; balance in USDT) |
| cisPay | Shop ID and secret key (cards, SBP) |
| Lave Pay (Laveshka, crypto and SBP) | API key, webhook secret, USD rate |

Cards go through one gateway of your choice (`CARD_GATEWAY`). Users only see the methods that are configured.

## Screenshots

![Admin panel](docs/screenshots/admin-overview.png)

![App screens](docs/screenshots/app-more.png)

<details>
<summary>All screens: the app and the panel</summary>

![Fonts in groups](docs/screenshots/admin-fonts.webp)
![Light theme, English](docs/screenshots/app-light.png)
![Payments: gateways and keys in the panel](docs/screenshots/admin-payments.png)
![Updates: a new version from GitHub in one click](docs/screenshots/admin-updates.png)
![User](docs/screenshots/admin-user.png)
![Server](docs/screenshots/admin-server.png)
![Orders](docs/screenshots/admin-orders.png)
![Look: the constructor and a live preview](docs/screenshots/admin-look.png)
![Opening screen](docs/screenshots/opening.gif)
![Custom theme and logo, the panel in Telegram](docs/screenshots/custom-and-phone.png)
![Sale, invites, channel](docs/screenshots/marketing.png)
![Warm-up](docs/screenshots/admin-warm.png)
![Launch: required and recommended steps](docs/screenshots/admin-launch.png)
![Sources: where customers come from and whether ads paid off](docs/screenshots/admin-sources.png)
![Referrals](docs/screenshots/admin-refs.png)
![Referral balance and withdrawal](docs/screenshots/app-wallet.png)
![Analytics](docs/screenshots/admin-analytics.png)
![Support: app and panel](docs/screenshots/support.png)
![Remnawave server](docs/screenshots/admin-remnawave.png)

</details>

## How it works

```mermaid
flowchart LR
    U["Customer<br/>in Telegram"] -->|opens| APP["Mini App<br/>/app"]
    U -->|writes| BOT["Bot<br/>webhook"]
    O["Owner"] --> ADM["Admin panel<br/>/admin and in the bot"]
    APP --> VS(("VPN Seller<br/>FastAPI · SQLite/PostgreSQL"))
    BOT --> VS
    ADM --> VS
    PAY["Gateways<br/>Stars · cards · SBP · crypto"] -->|payment notice| VS
    VS -->|creates and renews clients| P["Panels<br/>3x-ui · Marzban · Remnawave"]
    VPN["VPN app<br/>Happ, v2RayTun…"] -->|/sub/…| VS
```

There is no separate bot process: Telegram delivers updates to the service's webhook. On start the service registers the webhook, the menu button and the `/start` command itself.

Every customer becomes client `tg<telegram id>` on every enabled server. The service hands out its own subscription link: it collects keys from all panels, labels them with the server's flag and name, and adds remaining traffic and expiry to the headers. When the subscription is over or the device limit is reached, the VPN app shows a clear message instead of keys.

## Quick start

You need: a VPS with Ubuntu or Debian (1 GB of RAM or more), a bot from [@BotFather](https://t.me/BotFather) (`/newbot`) and a 3x-ui, Marzban or Remnawave panel — it can be added after the install. A domain is recommended but optional: without one the installer gives you a temporary Cloudflare address to try things out.

**One command** (as root):

```bash
wget -qO install.sh https://raw.githubusercontent.com/WWS909/vpn-seller/main/install.sh && sudo bash install.sh --en
```

or `curl -fsSL https://raw.githubusercontent.com/WWS909/vpn-seller/main/install.sh | sudo bash -s -- --en`.

A menu opens, then five questions right in the terminal:

1. **Bot token** — checked with Telegram at once.
2. **Your Telegram ID** — the installer can detect it: you send the bot any message.
3. **Address** — your domain or a temporary Cloudflare address.
4. **Admin password** — press Enter and a strong one is generated.
5. **Currency** of prices.

The rest is automatic: Docker, the code, `.env` (mode 600), HTTPS, start, linking the bot (webhook and the Open button). At the end: the admin address, the password and what to do next.

**If ports 80/443 are taken** by another project, the installer sorts it out:

| Who holds the ports | What the installer does |
|---|---|
| nobody | installs its own Caddy with an automatic certificate |
| another project's Caddy in Docker | joins its network, appends a site block to its Caddyfile (backup first, `caddy validate`, restored on any error), reloads Caddy without downtime |
| nginx, Traefik etc. in Docker | joins its network and shows what to add to its config |
| nginx or Caddy on the host | the shop listens on `127.0.0.1:8080` only (not reachable from outside); a ready nginx block is shown |

The shop is never exposed directly, bypassing the proxy: otherwise the admin login rate limit could be dodged with a forged IP.

**The temporary Cloudflare address** (no domain) changes when the server restarts. The shop notices the new address and re-links the bot itself, but subscription links already given to customers stop updating — connect a domain for real sales: `vpn-seller` → Change address.

**After the install** the `vpn-seller` command opens the same menu (on servers set up earlier, the old name `vpn-shop` works too). On top is the shop's card: running or not, the version (and whether a new one is out), the address, today's purchases and revenue, customers, the last backup, free disk space. Then: update (backup first, rollback if it does not start), status and checks, logs, restart; database backup, restore from a backup (pick from the latest), the last 24 hours to Telegram; payment methods (cards, crypto, Stars), change address (domain ↔ temporary), change token/IDs/password, night auto-update; what's new, free up disk space, language, uninstall.

Without the menu: `vpn-seller update | status | logs | backup | restore <zip> | payments | address | settings | uninstall`.
Unattended: `bash install.sh install --token 123:ABC --admin-id 123456789 --domain vpn.example.com --yes` (or `--tunnel` instead of `--domain`).

Then in the admin panel (in the bot — `/admin`) the **Launch** tab walks you through. Required: the bot is linked → a VPN server → plans (from a template if you like) → payments → documents (templates included) → try it yourself as a customer. Recommended: name and logo, free trial, seller and contact, bot description, your own FAQ, referral program, reminders and offers, your own domain, backups. Ticks appear only for facts — the panel really answered, a plan exists, the documents are written, you really got a key.

**By hand**, if Docker is already there:

**1. Bot.** Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.

**2. Configuration.**

```bash
git clone https://github.com/<you>/vpn-seller.git
cd vpn-seller
cp .env.example .env
nano .env
```

Minimum: `DOMAIN`, `BOT_TOKEN`, `SECRET_KEY` (`openssl rand -hex 32`), `ADMIN_PASSWORD`, `ADMIN_TELEGRAM_IDS`. Everything else is described in the file.

**3. Run.**

```bash
docker compose up -d
docker compose logs -f app
```

Caddy obtains the certificate by itself. Open your bot and send `/start` — the app button appears. Admin panel: `https://DOMAIN/admin` in a browser, or `/admin` in the bot.

**4. Servers and plans.** Admin → Servers → Add server: panel type (3x-ui, Marzban or Remnawave), address, token or login/password, Test, tick the inbounds, save. There are no plans after the install: Plans → From a template offers 1, 3, 6 and 12 months with suggested prices (199 / 499 / 899 / 1490 ₽; other currencies get their own or none) — tick, set your prices, add. The free trial is turned on there too.

**5. Direct Mini App link (optional).** In @BotFather → `/newapp`, set the URL to `https://DOMAIN/app` to get a `t.me/<bot>/<app>` link.

## 3x-ui servers

| | 3x-ui 3.x | 3x-ui 2.x |
|---|---|---|
| Auth | API token (Settings → Security → API Token) or login/password | Login/password |
| Subscription links | From the panel API | Via the panel's subscription server — set "Panel subscription address", e.g. `https://host:2096/sub/` |
| Removing clients | Full delete | The last client of an inbound is disabled instead (a 2.x limitation) |

- **Panel address** — as in the browser, with the secret path: `https://host:2053/abc123/`.
- **Inbounds** — comma-separated IDs; Test lists them. VLESS (incl. Reality with `xtls-rprx-vision`), VMess, Trojan, Shadowsocks and Hysteria2 are supported.
- **The address inside keys** is filled in by 3x-ui itself: from the panel's subscription settings or the inbound's External Proxy. If the panel is reached by an internal address, set the public domain/IP in the inbound.
- Clients created by the service are named `tg<id>` (prefix: `XUI_EMAIL_PREFIX`). Don't edit them by hand — the service restores its values on sync.
- A new server gets all active customers via "Push clients" or the nightly sync.

## Sources (where customers come from)

Admin → Sources → New link: a name (e.g. "Video #5") and, optionally, what it cost. You get `https://t.me/<bot>?start=src_<code>`, copied at once. Whoever first comes through it (to the bot or straight into the app) is counted under it; a person who already knew the bot is not counted again.

The report for 7, 30, 90 days or all time: came → opened the app → took the trial → paid, the money they brought (all their payments), what one paying customer cost, and the result "brought minus spent". People without a label and those from referral links are separate rows. The label also shows in the customer card.

## VPN check and auto-hiding

A panel can answer while the VPN itself is dead, and the other way round. So the shop learns from the keys which address and port customers connect to on each server and opens a connection there every 2 minutes. Three failures in a row (about 6 minutes — a short hiccup does not count) and the server is left out of subscriptions for a while, VPN apps stop offering a dead server, and the admins get a message. Once it answers, it comes back by itself. If no server answers, none is left out (the trouble is more likely on the shop's side). UDP protocols (Hysteria, TUIC, WireGuard, mKCP) cannot be checked this way and are not counted. Can be turned off in Settings.

## Renew in one tap

The "subscription ends soon" reminder (and "has ended", if on) comes with a Telegram Stars invoice for the same plan, with the same devices and any discount due. "Pay ⭐" — and the subscription is renewed, no app to open. Next to it, "Other ways to pay". Can be turned off in Settings.

## Server load

Every 2 minutes the shop asks the panels how many people are online (3x-ui, Marzban and Remnawave report it) and shows a badge in Locations: not busy / medium / busy. The threshold is the server's "Meant for, people online" field (200 by default). If the panel has not answered lately, there is no badge — no guessing. Can be turned off in Settings.

## Speeding up a server

```bash
curl -fsSL https://your-shop/scripts/tune.sh -o tune.sh && sudo bash tune.sh
```

Turns on BBR and fq, raises network buffers to 16 MB, connection queues, MTU probing, TCP Fast Open and the open files limit to a million. Shows the values before and after. Changes nothing in the panel or Xray. Run it on VPN servers; `bash tune.sh --undo` removes it.

## Marzban servers

Marzban 0.8.x is supported (tested on 0.8.4). Pick "Marzban" in the server form:

- **Address** — where the dashboard opens, e.g. `https://host:8000/` (a trailing `/dashboard/` is cut off).
- **Login and password** of a sudo Marzban admin (`marzban cli admin create --sudo`). No API token or subscription address needed: Marzban returns the links itself.
- **Inbounds** — tags, comma-separated (`VLESS TCP REALITY, Shadowsocks TCP`); Test lists them.
- A customer in Marzban is the user `tg<id>` with the protocols of the chosen inbounds, expiry, traffic limit and status. Blocking and an ended subscription make it `disabled`; "New link" changes the keys.

## Remnawave servers

Remnawave 2.x and 3.x are supported (the API checked against the 3.4 contract). Pick "Remnawave" in the server form:

- **Address** — where the panel opens, e.g. `https://panel.example.com/`.
- **API token** — Remnawave → API tokens → Create. Without a token, the admin login and password work too.
- **Internal squads** — the squads that give the customer access; Test lists them by name.
- A customer in Remnawave is the user `tg<id>` with the Telegram ID, expiry, traffic limit and the chosen squads. Blocking and an ended subscription make it `DISABLED`; "New link" reissues the keys (old links stop working, the traffic counter stays).
- Remnawave only accepts requests that came through a reverse proxy with HTTPS. When the shop talks to the panel directly (same server or Docker network, `http://remnawave:3000/`), it adds `X-Forwarded-Proto: https` and `X-Forwarded-For` itself — nothing to configure.

3x-ui, Marzban and Remnawave servers can be mixed: the customer's subscription holds keys from all of them.

One panel is one server in the shop: a panel keeps one user per customer, and two entries with the same address would overwrite each other (the shop refuses to save that). If a Remnawave or Marzban panel has nodes in several countries, add it once and tick the squads/inbounds; name the hosts in the panel itself with a flag first ("🇩🇪 Frankfurt") and those names stay in the subscription.

When no panel answers (a restart, an outage), the subscription answers 503 instead of an empty list: customers' VPN apps keep their working keys until the panel is back.

## Support in the bot

Anything a customer writes to the bot (except commands) and questions from the app's Support section go to every admin in `ADMIN_TELEGRAM_IDS` — with the name, ID, subscription and a "Customer card" button. Photos, files and voice messages are passed on as they are. To answer, just **reply to that message** in the bot — the customer gets the answer from the bot and sees it in the app. The whole conversation is in the panel → Support, where you can answer too; waiting threads are marked and shown in the bot's `/admin` menu.

Turned off in Settings → "Support chat in the bot" (then the Support button leads to the contact from settings). Flood protection: 20 messages an hour.

## Analytics

Panel → Analytics: a funnel of the customers who came in 7/30/90/365 days (came, opened the app, took the trial, connected — a VPN client fetched the subscription, paid, renewed), conversion to paid and from the trial, revenue by payment method, plan and kind of purchase, paying and new paying customers, revenue per payer, LTV and churn.

## Payment gateways

The easiest way is the menu on the server: `vpn-seller` → Payment methods — it asks for the keys, writes them to `.env`, restarts the shop and shows the notification URL for the gateway's dashboard. By hand — the variables below in `.env`, then `vpn-seller update`.

Set the notification URL in the gateway's dashboard (they are also listed in Admin → Settings):

| Gateway | Notification URL | Variables |
|---|---|---|
| Telegram Stars | — | `STARS_ENABLED=true`; Star rate in settings (`stars_rate`) |
| CryptoBot | `https://DOMAIN/api/pay/cryptobot` (@CryptoBot → Crypto Pay → My Apps → app → Webhooks) | `CRYPTOBOT_TOKEN` (`CRYPTOBOT_TESTNET=true` for testnet) |
| YooKassa | `https://DOMAIN/api/pay/yookassa` (event `payment.succeeded`) | `CARD_GATEWAY=yookassa`, `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` |
| FreeKassa | `https://DOMAIN/api/pay/freekassa` | `CARD_GATEWAY=freekassa`, `FREEKASSA_MERCHANT_ID`, `FREEKASSA_SECRET1`, `FREEKASSA_SECRET2`, optional `FREEKASSA_API_KEY` |
| Platega | `https://DOMAIN/api/pay/platega` | `CARD_GATEWAY=platega`, `PLATEGA_MERCHANT_ID`, `PLATEGA_SECRET` |
| PayPalych | `https://DOMAIN/api/pay/paypalych` (Result URL) | `CARD_GATEWAY=paypalych`, `PAYPALYCH_TOKEN`, `PAYPALYCH_SHOP_ID` |
| Lava | `https://DOMAIN/api/pay/lava` (Hook URL) | `CARD_GATEWAY=lava`, `LAVA_SHOP_ID`, `LAVA_SECRET_KEY`, `LAVA_ADDITIONAL_KEY` |
| WATA | `https://DOMAIN/api/pay/wata` | `CARD_GATEWAY=wata`, `WATA_TOKEN` |
| Heleket | `https://DOMAIN/api/pay/heleket` | `CRYPTO_GATEWAY=heleket`, `HELEKET_MERCHANT_ID`, `HELEKET_API_KEY` |
| Tribute | `https://DOMAIN/api/pay/tribute` | `CARD_GATEWAY=tribute`, `TRIBUTE_API_KEY` |
| CloudPayments | `https://DOMAIN/api/pay/cloudpayments` (Pay) | `CARD_GATEWAY=cloudpayments`, `CLOUDPAYMENTS_PUBLIC_ID`, `CLOUDPAYMENTS_API_SECRET` |
| MulenPay | `https://DOMAIN/api/pay/mulenpay` | `CARD_GATEWAY=mulenpay`, `MULENPAY_SHOP_ID`, `MULENPAY_API_KEY`, `MULENPAY_SECRET_KEY` |
| RollyPay | `https://DOMAIN/api/pay/rollypay` | `CARD_GATEWAY=rollypay`, `ROLLYPAY_API_KEY`, `ROLLYPAY_SIGNING_SECRET` |
| cisPay | `https://DOMAIN/api/pay/cispay` | `CARD_GATEWAY=cispay`, `CISPAY_SHOP_ID`, `CISPAY_API_KEY` |
| Lave Pay | `https://DOMAIN/api/pay/lavepay` | `CRYPTO_GATEWAY=lavepay`, `LAVEPAY_API_KEY`, `LAVEPAY_WEBHOOK_SECRET`, `LAVEPAY_RUB_PER_USD` |

Every notification is verified — by signature, by the gateway's IP list, or by re-fetching the payment status from its API. A forged webhook request cannot mark an order as paid.

If a notification never arrives (a wrong URL, an outage at the gateway), the service itself asks YooKassa, CryptoBot, Platega, PayPalych, Lava and Heleket about unpaid orders from the last three hours — the customer gets access in that case too.

### «Мой налог» receipts (Russian self-employed)

With the INN and password of lknpd.nalog.ru set (`vpn-seller` → Payment methods → «Мой налог», or `NALOG_INN` and `NALOG_PASSWORD` in `.env`), the shop registers a receipt after every payment through FreeKassa, Platega, PayPalych, Lava or WATA. YooKassa for the self-employed sends its own receipts, so it is not on the list; change it with `NALOG_GATEWAYS`.

- At most one receipt per order: the receipt row is written before it is sent.
- If «Мой налог» is down, the receipt waits and is sent later.
- If the answer is lost (the receipt may exist), the shop does **not** resend it; it tells the admins. In the admin panel → Orders → Receipts, mark whether it is in the «Мой налог» app.
- Refunds and cancelling receipts are done by hand in the «Мой налог» app.

Gateways usually ask for a website describing the service with prices and contacts — that's the home page `https://DOMAIN/`. Fill in "Provider" and "Contact e-mail" in settings. Domain verification files (`xxxx.txt`) go into `data/verify/` and are served at `https://DOMAIN/xxxx.txt`.

## Running it from Telegram

Only people listed in `ADMIN_TELEGRAM_IDS` have these; for everybody else the commands do not exist.

| Command | What it does |
|---|---|
| `/admin` | Menu with buttons and "Open the panel" — the whole admin panel inside Telegram, no password |
| `/stats` | Revenue, customers, subscriptions and what needs attention |
| `/user 123456789` or `/user @name` | Customer card: +3/+7/+30 days, block, reset traffic, sync |
| `/give 123456789 30` | Add days (`-3` to take away) |
| `/servers` | Server state, check, on/off |
| `/orders` | Paid but not granted orders with a "Grant" button, latest payments |
| `/trial 7`, `/trial off` | Free trial |
| `/sale 25 3`, `/sale off` | A 25% sale for three days |
| `/backup` | A database archive right now |
| `/promo SUMMER 20% 100` | 20% off, 100 uses; `/promo CODE 100` — fixed amount, `/promo CODE 7d` — +7 days, `/promo del CODE` — delete |
| Reply to a customer's question | The answer goes to the customer from the bot |
| `/broadcast Text` | Broadcast; the audience is picked with a button (everyone, subscribed, expired, never bought) |

Everything done from the bot goes to the panel's log with your Telegram ID.

## Look

Admin → Look. On the right is **the real app in a phone frame**: every change shows at once, before saving. Buttons under it switch dark/light, a subscribed/new customer, and replay the opening screen and the purchase moment.

- **A constructor, no finished themes** — the look is built from parts, each chosen on its own: colour and dark background, font, corners, icons, cards, background, animations, opening screen. Any part goes with any other — pixel icons on the winter background, say. Below are the special parts; turn them on one by one or together.
- **Glass** (Glass cards, 3D icons, the Orbs background): light-blue orbs behind frosted see-through cards, light running round the main card's edge, a sheen across the glass, a flowing main button, cards that light up and lean a little under the finger. The icons are an own set of colour 3D drawings that pop in, float, and have small habits (the star sparkles, the gift wiggles). Glass cards (the Cards setting) and 3D icons work with any other colour too; the admin panel stays as it was, only in the style's colour.
- **Neon** (Neon icons and cards, the Night city background) — glowing glass icons, a deep indigo background with city lights, a neon grid floor running toward you and sparks. Cards have a neon edge in the colour of their icon; the main card's edge flows through all colours. Titles and icons switch on with a flicker like a neon tube, a pool of light breathes under list icons. In the light scheme the icons sit in dark sockets like app icons. The icons are pictures in `web/icons/neon` — replace them with your own (WebP with a transparent background).
- **Pixel** (Pixel icons and cards, the Retro sunset background, the Tiny5 font) — an old game console screen. At the top of every screen, a pixel sun in sunset bands with slits running down and a cloud drifting past; the scene scrolls away with the page, with square stars and scanlines behind. Windows have cut pixel corners, a bevel and a hard shadow; buttons are arcade buttons that sink into their shadow when pressed. Icons are an own set of 26 pixel drawings (16×16), sitting in inventory slots in lists; the small signs (arrows, cross, plus and minus, the header shield, the purchase check) are pixel too. Fonts: Tiny5 (pixel, with a proper Cyrillic) for headings, numbers and buttons, JetBrains Mono for text. The traffic meter is a health bar of blocks, the main card and the sheets have a running striped marquee. Everything moves in frames: a screen is drawn in from the top, icons pop in one by one, the coin spins, the star blinks, the menu cursor jumps to the tab. The light scheme is cream paper and ink. Font and icon sizes keep every pixel sharp on 2× and 3× screens. Pixel icons, Pixel cards and the Retro sunset background work on their own too, with any colour. One click and the whole app is in one colour. Change any part below and the style becomes Custom.
- **Comic** (Sticker icons, Comic cards, the Halftone background, Rubik + Balsamiq) — a pop-art comic page. Panels with a bold ink outline and a shadow printed in halftone dots, tiles tilted a little like frames on a page. Section heads are yellow narration boxes; the status, support messages, fields and toasts are speech balloons with a tail. Titles and numbers are lettered in Rubik Black Italic with an ink outline and a red offset, text is Balsamiq Sans, like balloon lettering. Icons are an own set of colour stickers with a halftone shade and a white edge. The background is halftone dots from the corners and action lines behind the title. Bold-outlined buttons hit the page when pressed, the traffic meter is a red-and-yellow striped tube. A starburst lands on the opening screen, and a purchase gets a burst with a check and a “BAM!” (“БАЦ!” in Russian). Cream paper by day, a dark “noir issue” by night. Stickers, Comic panels and the Halftone background work on their own too.
- **New Year** (Festive icons, Snowy cards, the Winter night background, Nunito Black) — for December: a winter night with the northern lights, twinkling stars, snow falling in two layers, fir trees along the bottom, a garland on top whose bulbs light in turns. Cards are frosted glass with the sky showing through; snow lies on the main card, the tiles and the menu. Icons are drawn festive things: a gingerbread house, a snow globe, a snowman, a stocking, a bullfinch, a gift, a sparkler; some come alive (the gift wiggles, the star sparkles). Titles and numbers are round heavy letters, white as snow with a candy-red edge. A red bauble swings over the aurora on the opening screen, the loading bar is a candy cane, a purchase gets a bauble with a check and fireworks. By day, a snowy morning.
- **Lunar New Year** (Red diamond icons, Lacquer & gold cards, the Lanterns background, Yeseva One) — red lacquer and gold: a cloud pattern, two lanterns swaying at the top, cards in gold frames with fret corners. The main card is a red envelope (hongbao) with a flap and a gold coin; icons are the red-and-gold diamonds hung on doors, turning in as they appear and glinting. Titles in Yeseva One, numbers in gold, a lantern on the opening screen, a red diamond with a gold check and fireworks on a purchase. By day, rice paper: the accent turns red (gold on white cannot be read), the envelope stays red.
- **Home screen** — what the app opens with. Answer: “Works until Dec 26” on top, one main button under it and a short list — devices, countries, renewal. Term: a calendar to the end of the subscription and renewal right there — the person picks a term and sees the new end date. Devices: the connected devices (phone, laptop, TV — from what the VPN app reports), free places and “Connect one more”. Classic: a card with the days and two tiles. When the subscription is ending, has ended or the traffic is used up, every screen changes its words and its main button.
- **Style** — Clean: a light grey ground and white groups like Telegram’s lists; one colour, only on buttons; no caps labels, no dots after titles, no glow. Soft: after Material, like Android — surfaces tinted with your colour, pill buttons, a pill behind the chosen tab. Brand: your colour across the top like a bank’s app (Telegram paints its header the same). Large: larger text, high contrast, big buttons and clear outlines — easy to read. Cosmos: like VPN sites — a night ground lit by your colour, glass cards, large gradient prices, a “Best choice” ribbon on the best-value plan. All five are light and dark following Telegram; choosing one sets the heading font to Golos (you can change it). Dark classic: dark cards with its own backgrounds, cards and special sets (the Background and Cards settings work there).
- **Name and logo** — PNG, JPEG, WebP or SVG up to 2 MB. The logo shows in the app header, on the opening screen, in the panel, on the site and in the browser tab. The bot's own avatar is changed only in @BotFather (`/setuserpic`) — the logo can be downloaded right there.
- **Colour** — any accent (picker or #code) with a dark background (black, graphite, midnight, indigo, retro, noir, winter, red lacquer). A colour that reads poorly is darkened or lightened for text, in both schemes. The colour dots under the picker are only shortcuts: they fill in the accent and the background.
- **Font** — 30 pairs with Cyrillic in four groups: plain (Manrope, Montserrat, Rubik, Nunito, Onest, Geologica, PT Sans, IBM Plex Sans, Fira Sans, Ubuntu, Jost, Commissioner, system), serif (Playfair, PT Serif, Lora, Literata, Roboto Slab), with character (Unbounded, JetBrains Mono, Exo 2, Comfortaa, Oswald, Russo One, Tektur, IBM Plex Mono) and for special looks (pixel, comic, New Year). You can also type in **any family from Google Fonts** — it takes the headings and numbers, the text stays in Golos; the panel checks at once that the font exists and has Cyrillic. **Corner roundness** (every button, field and card follows it) and **icons** — thin, regular, bold, on tiles in the theme colour, **colour tiles** (a white drawing on a square of its own colour, like a phone's settings: money is green, gifts pink, help cyan), **in rings**, duotone and the special sets below.
- **Background** — glow, aurora, grid, stars or none; always in the colour's tone.
- **Animations** — lively (opening screen, a staged entrance, tab transitions, a menu highlight that travels, a day counter, a purchase moment), calm (soft fades only) or none. People with the system's "reduce motion" never get animation.
- **Opening screen** — logo, name and your own line while the app loads. Seven kinds to choose from: Matches the look (Pixel, Comic, Snowy and Lacquer & gold cards have their own), Waves (circles run out of the mark), Orbit (points run round the mark), Ring (the mark in a running loader ring), Burst (the mark flies in with a flash and sparks, the letters come together), Typing (the name is typed letter by letter after a cursor) and Minimal (only the name and a thin line) — or none. Each kind takes your colours and type. In the panel every kind has a live thumbnail, and the chosen one plays in the phone at once. The screen **fits the logo**: on upload it is looked at — where the drawing is (transparent margins are cut off), its proportions, whether it has transparency, how light it is and its main colour. A wide wordmark gets a wide sign, with the waves and the ring following its shape, and Auto hides the name next to it — the logo already says it. A photo or an icon without transparency gets rounded corners; a dark logo on the dark scheme gets a light plate. A long name gets smaller to stay on one line. Choices: the sign (logo, the first letter of the name or the shield), the plate (auto, tile, circle, none), size, colour (the accent, **the logo's** or your own), background (plain, glow or **filled with the colour** — a brand screen), whether to show the name, and how long the screen stays. Put `{name}` in the line for the person's first name from Telegram ("Hi, {name}!"). **Seasonal effect** — light snow behind the cards.
- **The bot in Telegram** — short description and "What can this bot do?" in Russian and English, sent to Telegram on save.
- **Default scheme**, **home line** and **own buttons** in the customer's profile (up to six).

All of this is put into the page on the server, so the default theme never flashes on open.

## Customer warm-up

Admin → Warm-up.

**Sale** — a discount on every plan for everyone (up to 90%), for N days or until turned off, with its own title. The app shows a banner and crossed-out prices. In the bot: `/sale 25 3`.

**System notices** fire by themselves, each once per event:

| Notice | When | After the install | Discount when turned on |
|---|---|---|---|
| Subscription ends soon | 2 days before the end | on | — |
| Trial ends | 1 day before | off | 20% for 3 days |
| Subscription ended | 1 day after | off | 15% for 3 days |
| Win back | 14 days after | off | 25% for 5 days |
| Didn't connect after the first visit | after 24 hours | off | 10% for 3 days |
| Abandoned payment — started paying and didn't finish | after 60 minutes | on | — (one reminder, a Finish the payment button) |
| Traffic runs low | at 80% | on | — |

Notices with a discount are off after the install — nobody gets a discount until you turn it on.

Each has editable timing, discount, discount lifetime and text (RU/EN, with `{name}`, `{percent}`, `{until}` and other placeholders), and can be sent to yourself as a test.

**Campaigns** are your own messages with a discount: a segment (everyone, never connected, took the trial and never paid, never paid, active, ends within a week, ended), text, percent, discount lifetime and start time. The list shows how many were sent, how many bought and for how much.

A discount from a notice or a campaign is **personal**: it applies by itself and expires after a purchase or on its date. It does not stack with the sale — the bigger one applies. A promo code does not stack with them either: the customer pays the lower price. Discounted prices are rounded down to whole units (to cents in USD, EUR, GBP).

## Referral program

Admin → Referrals. The program is off after the install. Turn it on and choose what the inviter gets. Customers then see "Invite friends" with a `t.me/<bot>?start=r…` link, a Share button, their friends and stats.

**Subscription days** — the inviter gets +N days when the friend pays (for the first payment or for every one).

**Money** — a share of the friend's payments (0–90%) on the inviter's balance:
- **Withdrawal by request.** You set the payout methods: bank card (the number is checked by its checksum), SBP by phone number, crypto (a wallet address), or any of your own. The customer picks a method, enters the details and the amount; the amount leaves the balance right away, so the same money cannot be withdrawn twice. Admins get the request in Telegram with Paid / Decline buttons, and it is listed in the panel. You send the money yourself; a declined or cancelled request returns the money to the balance, and the customer is told.
- **A minimum withdrawal** — optional.
- **Paying for a subscription from the balance** — can be turned on: the balance shows up among the payment methods. Orders paid from a balance earn no share.
- If the program is turned off, what customers earned stays theirs: it can still be withdrawn or spent.

In both modes: the friend gets +N days on their first purchase; a **captcha** for new referrals (the bot asks to tap the right number; until then the referral does not count); a **top of inviters** for all time, a month or a week — by rewards, paying friends or invited, 3 to 50 places. Customers see only the first 3 characters of a name, the panel shows it in full.

## Documents

Admin → Documents. There are no documents after the install — payment providers and banks check them, so this is a required step under Launch.

- **Terms of service** and **privacy policy**, in Russian and English. "Insert the template" puts in a ready text — edit it and save. A template is not legal advice.
- Simple marks: `# Heading`, `- item`, `**bold**`, a blank line for a new paragraph. `{brand}`, `{owner}`, `{contact}` are filled in.
- **Seller and contacts**: name and status, e-mail, support contact.
- **FAQ** for the Help section — an editor of questions and answers with a ready set. Until you add your own, the standard ones show (connecting, devices, TV).
- **About** — a couple of lines in Help and on the public page.

The public page (`/` and `/legal`) shows only written documents; the app links to them once they exist.

## Mandatory channel subscription

There can be several channels (up to five): Add a channel — and a person must join each; the mini app shows a Subscribe button per channel. Admin → Settings → Mandatory channel subscription: the channel's `@username` (or a `-100…` ID and an invite link for a private one) and the mode — the whole service or only the free trial. The bot must be an administrator of the channel, or Telegram does not tell who is subscribed; "Save and check" verifies it. A non-subscriber sees "Subscribe to the channel" with "Subscribe" and "I've subscribed". If the check itself breaks (the bot was removed from the channel), customers are let through rather than locked out.

## Admin settings

Changeable without a restart: free trial (under Plans), service name, welcome text, extra device price and device maximum, traffic packs, Star rate, mandatory HWID, pushing the device limit to panels as `limitIp`, subscription update interval, who sees the admin entry, reviews and gifts. Support, documents, FAQ and seller details are under Documents.

## Daily results and Excel export

**Daily results.** Every day at the chosen hour (18:00 server time by default) the owners get a short Telegram report: revenue and purchases of the last 24 hours against the day before, first-time payers, new people and trials, active subscriptions, how many end within a day, abandoned payments and what needs a look (servers not answering, unanswered support questions, paid orders not given). Admin → Settings → Daily results in Telegram: on/off, the hour, Send now.

**Excel.** Orders has an **Excel** button: orders for a period (this month, last month, 30 days, this year, all time or your own dates) — date, customer, what was bought, plan, amount, payment method, promo code, a total at the bottom. Handy for bookkeeping and taxes. Users has an **Excel** button for the customers: when they came, from where, plan, subscription end, how much they bought and paid. These are real .xlsx tables: numbers add up, dates sort, the header has a filter. When the panel is opened inside Telegram, the file comes to the chat with the bot.

## Backups

Every day at the chosen hour (4:00 server time by default) the service makes an archive and sends it to the admins from `ADMIN_TELEGRAM_IDS` in Telegram. The last 7 are kept in `data/backups`. Admin → Settings → Backups: on/off, the hour, "Make and send now", download any of the kept ones. In the bot: `/backup`.

The archive holds the whole database in a portable form (`db.json`, restores into SQLite or PostgreSQL), a copy of the SQLite file and the logo. It contains everything, panel passwords included — that is why it goes only to the admins.

To restore on a new server: install with the script, then

```bash
sudo vpn-seller restore /path/backup-….zip
```

## Updating

The shop watches for new versions itself. Every 6 hours it looks at the `VERSION` file in the GitHub repository (the one in `install.sh`, or `UPDATE_REPO` in `.env`). When a newer version is out, the owners get a Telegram message — what's new and an **Update now** button. The same is in admin → **Updates** (and a "Version … is out" line on the overview).

How an update goes (the button and `sudo vpn-seller update` do the same):

1. a database backup into `data/backups`;
2. the new code: `git pull`, or the repository archive from GitHub when the shop was not installed from git (`.env`, `data/` and the proxy settings are left alone);
3. rebuild and restart;
4. a check that the shop answers. **If it does not, the previous code comes back by itself**, the shop works again and the owners get a message with the reason.

Afterwards comes "✅ The shop is updated to X" with the list of changes; the panel shows it once too. You can turn on an **automatic update at night** at the hour you choose — one try per version.

The Update button is served by the systemd unit `vpn-seller-update.path`: the shop leaves a job file in `data/`, the unit runs the update on the server. The installer (and every `vpn-seller update`) sets it up. Without systemd the button does not work — update with the command.

For a private repository put a GitHub token that can read it into `.env`: `GITHUB_TOKEN=…`. To release a new version: raise the number in `VERSION`, add a section to `CHANGELOG.md` (and `CHANGELOG.en.md`) and push the code — `bash scripts/publish-github.sh`.

By hand without the menu: `git pull && docker compose up -d --build`. New database columns are added automatically on start.

The default database is SQLite, enough for thousands of customers. For PostgreSQL, uncomment the `db` service in `docker-compose.yml` and set `DATABASE_URL` in `.env`.

## Without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a && . ./.env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
```

Put an HTTPS reverse proxy (nginx, Caddy) in front: Telegram opens Mini Apps and sends webhooks over HTTPS only.

## Development

```bash
pip install -r requirements-dev.txt

# real 3x-ui 3.x and 2.x and Marzban panels locally (Linux x86_64)
scripts/dev-3xui.sh
scripts/dev-marzban.sh
source .dev/env.sh

python -m pytest              # unit and integration tests; panel tests are skipped without panels
                              # Remnawave runs against tests/fake_remnawave.py; with a real panel set
                              # REMNAWAVE_TEST_URL, REMNAWAVE_TEST_TOKEN, REMNAWAVE_TEST_SQUAD
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/test python -m pytest   # the same on PostgreSQL
python tests/e2e/e2e.py       # browser run of the app and the admin panel (Playwright)
python tests/installer/run.py # the whole installer: clean server, curl | bash, another Caddy,
                              # nginx, tunnel, menu, payments, uninstall + a purchase on a real 3x-ui
```

**Checks on GitHub** (`.github/workflows/tests.yml`): on every push GitHub starts real 3x-ui 3.x and 2.x and Marzban panels, runs all the tests and every installer scenario (including a purchase on a real 3x-ui). The result is the badge at the top of this file: green — fine to update the servers, red — look first.

`PAYMENTS_TEST_MODE=true` makes every order free — handy for testing key delivery. Never enable it in production; the admin panel shows a warning.

Layout:

```
app/
  main.py          pages, /sub subscription, startup
  config.py        environment variables
  models.py        tables
  core.py          admin settings, bot texts
  access.py        access on servers, subscription assembly, devices, traffic
  billing.py       prices, orders, promo codes, gifts
  providers/       3x-ui 2.x/3.x, Marzban and Remnawave API clients
  support.py       support chat in the bot
  marketing.py     sale, personal discounts, warm-up, campaigns
  referral.py      referrals: days or money, payouts, the top
  legal.py         the documents and their templates
  starter.py       plan templates (nothing is created by itself)
  vpncheck.py      the check of the VPN itself, auto-hiding
  sources.py       source links and their report
  channel.py       mandatory channel subscription
  backup.py        database archives and restore
  botadmin.py      running the shop from Telegram
  payments/        Stars, CryptoBot, Heleket, Lave Pay, YooKassa, FreeKassa, Platega, PayPalych, Lava, WATA, Tribute, CloudPayments, MulenPay, RollyPay, cisPay
  routers/         app, admin, bot and gateway APIs
  jobs.py          background jobs
web/               index.html (Mini App), admin.html, legal.html
```

## Contributing

Bugs and ideas go to [issues](https://github.com/WWS909/vpn-seller/issues/new/choose), setup questions to [discussions](https://github.com/WWS909/vpn-seller/discussions). How to send a change: [CONTRIBUTING.md](CONTRIBUTING.md). If the project helped you, a star makes it easier for others to find.

## License

[MIT](LICENSE). You are responsible for complying with the laws of your country and your payment providers' rules.
