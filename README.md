# AutoSearch — marketplace monitor Telegram bot

Telegram bot that watches search pages on 8 marketplaces (Avito, OLX,
Vinted, Kufar, Mercari, Юла, Grailed, Fruitsfamily) and forwards new
listings to subscribers in real time, translated into their language
and converted into their currency.

Public bot: [@avito_ntc_bot](https://t.me/avito_ntc_bot)

## What it does

1. User opens a marketplace, applies filters (brand, size, price, region),
   and pastes the URL into the bot.
2. Bot polls the URL every 60 seconds, deduplicates against per-source
   state, and pushes new items as cards with photo, price (in user's
   currency), location, and translated description.
3. Per-subscription stop-words (blacklist) drop irrelevant items before
   they hit the wire.

Tariffs are gated through YooKassa with НПД (samozanyaty) fiscal
receipts — there's no Telegram Payments dependency.

## Stack

| Layer | Choice |
|---|---|
| Bot framework | [aiogram 3.15](https://github.com/aiogram/aiogram) |
| Database | PostgreSQL via [asyncpg](https://github.com/MagicStack/asyncpg) |
| HTTP | [httpx](https://github.com/encode/httpx) + [curl_cffi](https://github.com/yifeikong/curl_cffi) for Cloudflare-fronted sites |
| Translation | [deep-translator](https://github.com/nidhaloff/deep-translator) (Google) |
| Payments | YooKassa REST API (СБП + cards), receipts via «Мой налог» |
| Hosting | Railway (autodeploy from `main`) |
| Errors | Sentry (env-gated) |

## Repo layout

```
.
├── bot.py                 — entrypoint, dispatcher wiring, Sentry init
├── handlers.py            — all Telegram handlers (commands, FSM, callbacks)
├── scheduler.py           — per-subscription polling loop
├── database.py            — asyncpg pool + schema + all queries
├── config.py              — env-driven configuration
├── webhook.py             — YooKassa webhook (aiohttp side-app)
├── parser.py              — source-detection facade
├── parsers/               — one module per marketplace
│   ├── avito.py
│   ├── olx.py
│   ├── vinted.py
│   ├── kufar.py
│   ├── mercari.py
│   ├── youla.py
│   ├── grailed.py
│   ├── fruitsfamily.py
│   └── common.py          — shared throttling, allowlists, body-size cap
├── services/yookassa.py   — REST client + receipt helpers
├── docs/PRIVACY.md        — 152-ФЗ privacy policy
├── docs/OFFER.md          — public offer (договор)
├── middleware.py          — per-user throttle
├── bot_i18n.py            — 8-language localisation
└── test_unit.py           — unit suite (no DB, no network)
```

## Local development

Requires Python 3.13 + a Postgres 16+ instance.

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env         # then fill in the secrets
python bot.py
```

Run the unit suite:

```bash
pip install pytest
pytest test_unit.py -v
```

Tests are pure-Python — no DB, no Telegram, no network. CI runs them
on every push (see `.github/workflows/test.yml`).

## Configuration

All configuration is via environment variables; see `config.py` for the
canonical list. Required:

| Variable | What it is |
|---|---|
| `BOT_TOKEN` | BotFather token for @avito_ntc_bot |
| `DATABASE_URL` | Postgres DSN |
| `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` | YooKassa credentials |
| `WEBHOOK_DOMAIN` | public HTTPS host for the YooKassa webhook |

Optional but recommended:

| Variable | What it is |
|---|---|
| `SENTRY_DSN` | enables crash reporting |
| `PROXY_LIST` | comma-separated proxy URLs for RU sources |
| `ADMIN_TELEGRAM_IDS` | comma-separated Telegram IDs allowed to use `/admin` |

## Deployment

Railway autodeploys from `main` via the existing `Dockerfile`. The image
runs as a non-root user (UID 10001) and writes nothing outside `/app`.

## Privacy & compliance

- 152-ФЗ: privacy policy at [docs/PRIVACY.md](docs/PRIVACY.md), data-subject rights via `/export_my_data` and `/delete_my_account`.
- 422-ФЗ НПД: every paid subscription generates an automatic «Мой налог» receipt through YooKassa.
- Telegram payments are NOT used (no Telegram Stars, no native invoices).

## License

MIT — see [LICENSE](LICENSE).
