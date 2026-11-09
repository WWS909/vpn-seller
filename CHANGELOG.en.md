# What's new

Each version is a `## X.Y.Z — date` heading with one change per "-" line.
The shop reads this file to tell the owner what is new.

## 1.7.1 — 2026-11-09
- Three new styles that change more than colours. Newspaper: ink on newsprint, rules instead of cards, plans as a dotted price list, a hatched calendar, solid-ink buttons. Brutal: thick outlines, hard shadows, sticker labels, buttons that sink into their shadow, a tilted tag on the best-value plan. Terminal: a console — monospace in your colour, dashed frames, a cursor after the headline, [ buttons ]. All light and dark.
- Choosing a style sets a heading font that suits it: Old Standard for Newspaper, Dela Gothic for Brutal, DotGothic16 for Terminal. A font you picked yourself stays.
- 18 new fonts with Cyrillic, 50 in all. Serif: Old Standard, Cormorant, Zen Antique. With character: Podkova, Jura, Kelly Slab, Dela Gothic. A new Unusual group (headings and prices in the app only): Rubik Mono, Ruslan Display, Handjet, DotGothic16, Rubik Glitch, Rubik Doodle Shadow, Seymour One, Train One, Underdog, Caveat, Shantell Sans.
- Fixed: faces with a single weight (Russo One, Yeseva One and the new ones) are no longer made bold by the browser, so the strokes stay clean.
- Header: the colour of the bar with the logo — as the style, your colour, a gradient, a colour of its own or a picture. The text colour is picked for you; Telegram’s top bar takes the same colour.
- Picture on home: City, Bauhaus, Contour map, Waves, Star chart — drawings in your colour that follow light and dark by themselves. Or your own picture (PNG, JPEG, WebP up to 4 MB), which can also go into the header.
- Buttons: six shapes over any style — Key, Stamp, With an arrow, Ticket, Pixels, Double outline.
- Feedback on press: a light tap of the phone on buttons and choices — it can be turned off.
- Entrance: the blocks come in from below, with a spring, from the side, out of a blur or unfolding; the same on a tab switch. The panel plays the chosen one at once.
- Confetti in your colour after a payment, if you like.
- Seasonal effect: falling leaves and petals besides the snow.

## 1.7.0 — 2026-11-09
- The look constructor is rebuilt around two main parts: Home screen and Style. The Pass, Poster and Tiles layouts of 1.6.0 are gone (shops that chose them get Classic back).
- Three new home screens: Answer (“Works until Dec 26”, one button and a short list), Term (a calendar to the end of the subscription with renewal right there) and Devices (connected devices, free places, “Connect one more”). The words and the main button change when the subscription is ending, has ended or the traffic is used up.
- A new Clean style: light like Telegram’s lists, dark when Telegram is dark; the shop’s colour only on buttons. Choosing it sets the heading font to Golos.
- A new font: Golos (one face for headings and text).
- Devices get their own drawing: a phone, a laptop or a TV — from what the VPN app reports.
- Lines like “iOS 18 · Happ · today” now use commas; “Total for 1 year” instead of “Total · 1 year”.
- The “Connect a device” sheet in the Clean style: “Phone and computer / TV” is a segmented switch like on an iPhone; “Reset the link” is a quiet button, not a second big one.
- On the new home screens someone without a subscription is offered the free trial, when the shop has one.
- The panel’s preview shows two more states: Ending and Ended.
- Fixed: the older single “channel” field works again when it is set on its own after the channel list was saved.
- Three more styles: Soft (after Material, like Android: surfaces tinted with your colour, pill buttons, a pill behind the chosen tab), Brand (your colour across the top like a bank’s app; Telegram paints its header the same) and Large (larger text, high contrast, big buttons and clear outlines — easy to read). All work with any home screen, light and dark.
- A Cosmos style, like VPN sites: a night ground lit by your colour, glass cards with a thin coloured edge, large gradient prices, the main button in a gradient to pink, pill captions with a lit dot. The best-value plan wears a “Best choice” ribbon. There is a light version.

## 1.6.0 — 2026-11-09
- Screen layout — the new main setting of the look. Besides Classic there are four layouts that change how the screens are built, not the colour: Like Telegram (lists and the system font), Pass (the subscription is a card with a date, a tear-off line and a barcode), Poster (a colour block and very large type) and Tiles (colour squares like a phone's home screen).
- The new layouts have no caps labels over headings, no dots after titles and no glow; they bring their own background and cards.
- In the panel each layout has a sketch of the screen, and the chosen one shows at once in the phone on the right.

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
