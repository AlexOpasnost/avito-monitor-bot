# Avito Monitor Bot — Status Report
**Date:** 2026-04-05

---

## What Works Well

### Core Functionality
- **Avito page loading** — cloudscraper + mobile proxy (Megafone) bypasses Avito's anti-bot
- **IP rotation** — auto-rotates on 403/429/302 blocks, up to 5 retries with progressive backoff
- **Proxy verification** — checks actual proxy IP at startup via ipify.org
- **Item extraction** — parses 74-82 items per page from HTML `data-item-id` blocks
- **SERP isolation** — only parses `catalog-serp` container, ignores recommendations

### Notifications (Telegram)
- Photo of the item
- Title (city suffix removed to avoid duplication)
- Price
- Views count
- City (extracted from URL path, 50+ Russian cities mapped)
- Full clickable URL (tracking params stripped)
- Description (from item detail page, up to 300 chars)
- Seller name
- Exact publication date (HH:MM:SS DD.MM.YYYY from detail page)
- "Open on Avito" button

### Smart Behavior
- **First scan** — marks all existing items as "seen", no spam on subscription start
- **Max 5 items per cycle** — prevents proxy overload
- **2-day age filter** — skips items older than 2 days
- **Pause/Resume** — `/stop` pauses (preserves links), `/start` resumes
- **Delete vs Pause** — `/delete` permanent, `/stop` temporary (DB `deleted` flag)
- **URL cleaning** — strips `context=`, `slocation=` tracking garbage, keeps `f=` filters
- **Deduplication** — groups subscriptions by URL, parses each unique URL once

### Filter Decoding
- **`f=` parameter decoded** — base64 binary with varint pairs
- **Condition filter** detected: `Новое с биркой` (value 5608890)
- **Price range** decoded: `{"from":1000,"to":0}`
- **Brand IDs** extracted: 4 brand value IDs found

---

## What We're Working On (Current Issues)

### 1. Server-Side Filter Bypass (CRITICAL)
**Problem:** Avito's server does NOT fully apply `f=` filters in HTML rendering. Items outside the selected brands appear in search results.

**Impact:** Bot sends items with brands (e.g., Supreme) that user didn't select.

**Solution in progress:**
- Decode ALL filter param/value pairs from `f=` binary
- During enrichment, extract item attributes (Состояние, Бренд, Размер) from detail page JSON
- Compare item attributes against decoded filters
- Skip items that don't match

**Status:** Filter decoding works. Attribute extraction partially works (found attributes in JSON structure on detail pages). Need to:
1. Build complete param_id → attribute name mapping
2. Build value_id → attribute value mapping
3. Implement strict comparison

### 2. Brand Filter Verification
**Problem:** Brand IDs (3378456, 34922032, etc.) are internal Avito IDs. No public mapping exists.

**Approach:**
- On item detail page, extract brand name from JSON attributes
- On first scan, collect all brand names from filtered results → build whitelist
- On subsequent scans, only send items whose brand is in the whitelist

### 3. Attribute Extraction Reliability
**Problem:** Item attributes (Состояние, Бренд) are stored in escaped JSON within `__initialData__` on detail pages. Current regex extraction is unreliable.

**Approach:** Parse `__initialData__` JSON properly, navigate to attributes section.

### 4. Page Title Empty
**Problem:** `title='?'` on loaded pages — Avito SSR doesn't set proper title for filtered pages.

**Impact:** Cosmetic only, doesn't affect functionality.

---

## Architecture

```
User → Telegram → aiogram bot (Railway)
                      ↓
              Scheduler (60s interval)
                      ↓
              cloudscraper + mobile proxy → Avito HTML
                      ↓
              HTML parser (data-item-id in catalog-serp)
                      ↓
              For each NEW item → fetch detail page (enrichment)
                      ↓
              Extract: date, description, views, seller, attributes
                      ↓
              Filter: age < 2 days, price in range, condition match
                      ↓
              Format notification → send to Telegram with photo
```

## Tech Stack
- Python 3.13 + aiogram 3.15 + asyncpg
- cloudscraper (WAF bypass) + requests
- Mobile proxy: Megafone (mproxy.site, IP rotation via API)
- Database: PostgreSQL on Neon (serverless)
- Hosting: Railway
- Telegram API: direct (no CF Worker proxy needed)

## Files
- `bot.py` — startup, polling, proxy check
- `handlers.py` — /start, /stop, /list, /delete, URL handling
- `parser.py` — Avito page fetching, HTML parsing, enrichment, filter decoding
- `scheduler.py` — periodic checking, notification formatting, item filtering
- `database.py` — asyncpg pool, users/subscriptions/sent_items tables
- `config.py` — env vars

## Statistics
- ~30 commits since project start
- Bot handles 3 concurrent subscriptions
- Processes ~80 items per page scan
- Enriches 5 items per cycle (detail page fetch)
- IP rotation: ~1 rotation per cycle when blocked
