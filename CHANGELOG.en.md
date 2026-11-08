# What's new

Each version is a `## X.Y.Z — date` heading with one change per "-" line.
The shop reads this file to tell the owner what is new.

## 1.2.1 — 2026-11-08
- Security: the client address from X-Forwarded-For is taken only from our own proxy (Caddy, cloudflared, nginx), so the admin login limit cannot be dodged with a made-up header.
- Security: without SECRET_KEY (the shop started by hand, without the installer) the shop makes its own random key in data/ and uses it instead of an empty one.
- The server form warns when the panel certificate check is off.
- Gift code activation is rate limited, like the Code field.
- Fixed: WATA payments needed the cryptography package.

## 1.2.0 — 2026-11-08
- Daily results in Telegram: every evening the owner gets a report — revenue and purchases of the last 24 hours against the day before, new people, what ends tomorrow and what needs a look. In the bot — /report.
- Excel export: orders for any period (with a total — for bookkeeping and taxes) and the customer list. Inside Telegram the file comes to the chat.
- Abandoned payment reminder: if someone started paying and didn't finish, after an hour the bot reminds them once with a Finish the payment button.

## 1.1.0 — 2026-11-08
- Updates from GitHub: the shop finds a new version itself, sends "what's new" to Telegram and updates with one button — in the bot or in admin → Updates.
- A database backup is made before an update; if the new version does not start, the shop goes back to the previous one by itself.
- Optional automatic update at night, at the hour you choose.
- No finished themes: the look is built from parts — colour, background, font, corners, icons, cards, animations, opening screen.
- Colour is any accent with a dark background; the colour dots stay as quick picks.
- Whitelist bypass: paid gigabytes in plans and extra packs.
- Payments are set up right in admin → Payments; RollyPay and cisPay added.
- The extra device price is charged for every month of the subscription.
- A new admin overview: revenue by day, plans, recent sales, server load.
- One-tap adding of the subscription to the app: Happ, v2RayTun, INCY, OneXray, Karing, Hiddify.
- Promo codes of any shape: letters, digits, signs.

## 1.0.0
- The first version.
