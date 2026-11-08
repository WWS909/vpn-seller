# What's new

Each version is a `## X.Y.Z — date` heading with one change per "-" line.
The shop reads this file to tell the owner what is new.

## 1.5.0 — 2026-11-09
- Fonts: 30 pairs with Cyrillic, grouped in the panel — plain, serif, with character, for special looks. New: PT Sans, PT Serif, IBM Plex Sans and Mono, Fira Sans, Ubuntu, Jost, Commissioner, Lora, Literata, Roboto Slab, Oswald, Russo One, Tektur.
- Your own font: type in any Google Fonts family for headings and numbers; the panel checks at once that it exists and has Cyrillic.
- Icons: two new styles — "Colour tiles" (a white drawing on a square of its own colour, like a phone's settings) and "In rings".
- Fixed: the Montserrat, JetBrains Mono, Comfortaa, Playfair and Yeseva One pairs never loaded their text font (Golos) — the text fell back to the system font.
- The GitHub page: the @Cristal_vpn_app demo shop, installer and server-menu screenshots, a comparison with a bot, a diagram of how it works.

## 1.4.0 — 2026-11-09
- Mandatory subscription: several channels can be added (up to five) — a person joins each, the mini app has a button per channel.
- Traffic top-ups became a feature of their own: Settings → Traffic top-ups, off at first. Off — when the traffic runs out, a renewal is offered.
- Admin overview: until the first sales, the revenue and plan charts show a pale example with a note instead of being empty.
- Server menu: the previous logo; every item has a way back (a Yes / Back choice, 0 while entering keys cancels with nothing changed).

## 1.3.0 — 2026-11-09
- A new server menu (vpn-seller): the VPN SELLER name, a coloured logo, the shop's card on top — running or not, the version and whether a new one is out, the address, today's purchases and revenue, customers, the last backup, free space.
- Menu items are grouped. New ones: restart, restore from a backup (pick from a list), send the last 24 hours, night auto-update, what's new, free up disk space.

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
