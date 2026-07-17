# Amazon Deals → Telegram Bot

Scrapes Amazon's deals page on a schedule, posts new ones to your Telegram
channel with your Associates affiliate link attached, and remembers what
it's already posted so nothing repeats.

Runs for free on GitHub Actions — no server to manage.

```
GitHub Actions (every 4h)
        │
        ▼
  scraper.py  ──►  filter new + above min discount  ──►  telegram_poster.py
        │                                                        │
        ▼                                                        ▼
 finds ASINs via                                         posts photo + caption
 /dp/<ASIN> links,                                        + your affiliate link
 climbs DOM to card                                              │
                                                                  ▼
                                                    data/posted_deals.json
                                                    (committed back to repo)
```

## Before you start — please read

- **This breaks Amazon's Terms of Service.** Scraping their pages isn't
  something they permit. In practice that mostly means: no criminal
  exposure for a small personal bot, but Amazon can and does block scraper
  traffic, and could in theory send a cease-and-desist to persistent
  scrapers. You're accepting that risk by choosing this route.
- **GitHub Actions IPs are well known to anti-bot systems.** Because so
  many scrapers run on Actions, Amazon is more likely to challenge/block
  requests from it than from a normal home IP. If runs start consistently
  returning 0 deals, this is the most likely reason — see Troubleshooting.
- **The scraper's field extraction is a best-effort heuristic**, not
  verified against Amazon's live markup (I built this without being able
  to reach amazon.com to check). It locks onto product links
  (`/dp/ASIN`) since those are stable, then reads title/price/discount
  out of the surrounding card — but you should expect to spend a little
  time calibrating it against what Amazon actually serves you. See
  Troubleshooting below.

## Setup

### 1. Create your Telegram bot
Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
follow the prompts → copy the token it gives you (looks like
`123456789:AAExample...`).

### 2. Add the bot to your channel
Add your bot as an **admin** of your channel (needs "Post Messages"
permission). Then get your channel ID:
- If your channel is public: use `@your_channel_username` directly.
- If it's private: forward any message from the channel to
  [@userinfobot](https://t.me/userinfobot), or temporarily add
  [@RawDataBot](https://t.me/RawDataBot) to the channel to see the numeric
  chat id (looks like `-1001234567890`).

### 3. Get your Amazon Associates tracking ID
This is the `tag` value from your existing affiliate links, e.g. in
`.../dp/B000000000?tag=yourname-20` it's `yourname-20`. Found in your
Associates Central account under "Manage Your Tracking IDs" if you need
to look it up.

### 4. Configure the repo
Push this project to a GitHub repo, then go to **Settings → Secrets and
variables → Actions** and add these **Secrets**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from step 1 |
| `TELEGRAM_CHANNEL_ID` | from step 2 |
| `AMAZON_ASSOCIATE_TAG` | from step 3 |

Optionally add these as repo **Variables** (same screen, "Variables" tab)
to override defaults without touching code:

| Name | Default | Meaning |
|---|---|---|
| `AMAZON_DOMAIN` | `amazon.com` | which marketplace to scrape, e.g. `amazon.in`, `amazon.co.uk`, `amazon.de` |
| `MAX_DEALS_PER_RUN` | `5` | cap on posts per run |
| `MIN_DISCOUNT_PERCENT` | `20` | skip deals below this % off |

### 5. Test locally first (strongly recommended)
```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in your real values, keep DRY_RUN=true
python src/main.py
```
This prints what *would* be posted without touching Telegram or writing
any state. Confirm the titles/prices look right before going live.

### 6. Go live
In your repo's **Actions** tab, select "Post Amazon Deals to Telegram" →
**Run workflow** to trigger it manually once and confirm a real post
lands in your channel. After that it runs automatically every 4 hours
(edit the cron line in `.github/workflows/post-deals.yml` to change the
schedule).

## Troubleshooting

**Scrape returns 0 deals every time**
Amazon's markup (or the `/deals` URL itself) doesn't match what this
scraper expects, or requests are being blocked/challenged. Run locally
with `DRY_RUN=true` and `headless=False` (edit the `scrape_deals()` call
in `main.py` temporarily) to watch the browser and see what's actually
loading. If you land on a CAPTCHA or "unusual traffic" page, that's
Amazon's bot detection — slowing down your run frequency is the main
lever you have here without crossing into actively defeating their
anti-bot measures, which this project intentionally doesn't attempt.

**Titles, prices, or images look wrong**
Open the deals page in Chrome, right-click a deal tile → Inspect, and
compare against the assumptions in `src/scraper.py`:
- `PRICE_RE` expects `$X,XXX.XX` — adjust if your marketplace formats
  prices differently (e.g. `AMAZON_DOMAIN=amazon.co.uk` uses `£`).
- `_guess_title()` prefers an `aria-label` or `title` attribute on the
  product link, falling back to the first non-price line of text.
- The "climb to card" logic stops as soon as it sees a price — if that's
  grabbing the wrong container, look at `_CLIMB_TO_CARD_JS` in
  `scraper.py`.

**Telegram posting fails**
Double check the bot is an *admin* of the channel with post permission,
and that `TELEGRAM_CHANNEL_ID` is exactly right (public channels need the
`@` prefix). If only images fail, `telegram_poster.py` automatically
falls back to a text-only post, so check your Action logs for the actual
error.

**Duplicate posts**
Almost always means `data/posted_deals.json` isn't actually getting
committed back after each run, so every run starts from the same "nothing
posted yet" state. Two things to check, in order:
1. Open the **Actions** tab → a recent run → the "Commit updated
   posted-deals log" step. If it's failing (not just the git commands
   printing "nothing to commit"), go to **Settings → Actions → General →
   Workflow permissions** and make sure **"Read and write permissions"**
   is selected. This repo's workflow file requests write access, but that
   request is capped by this repo-level setting — if it's set to
   read-only, the `git push` fails with a permissions error even though
   the YAML looks correct.
2. If you triggered several manual runs close together while testing,
   they can race and both post before either commits. The workflow
   includes a `concurrency` group specifically to queue overlapping runs
   instead — confirm you're running the latest version of
   `.github/workflows/post-deals.yml`.

If posts look like literal duplicates of the *same* title/price on every
run even with the above both fine, you may be looking at the currency
bug described below rather than a persistence issue.

**Wrong currency, missing prices, or "duplicate-looking" posts on a
non-US marketplace (e.g. `amazon.in`)**
The original version of this scraper only recognized `$`-formatted
prices, so on marketplaces like India that use `₹` (and often no decimal
places, e.g. `₹1,299` rather than `₹1,299.00`), it found zero prices at
all. That had a second effect: the DOM-climbing logic uses "found a
price" as its signal to stop climbing toward the product's card
container — with no match possible, it always climbed the maximum
distance, which on some layouts pulls in a shared ancestor across
multiple different products, making genuinely different ASINs come out
with the same extracted title text. That looks exactly like duplicate
posts even though the underlying products (and dedup tracking) were
actually distinct. This is fixed as of the current `scraper.py` — prices
are matched per-marketplace via the `CURRENCY_SYMBOL` map at the top of
the file. If you're scraping a marketplace not already listed there
(anything beyond `.com`, `.in`, `.co.uk`, `.ca`, `.com.au`, `.de`, `.fr`,
`.it`, `.es`), add its symbol to that map.

## Project structure
```
src/
  scraper.py          # Playwright scraper — finds deals via /dp/ASIN links
  affiliate.py         # builds your affiliate links from product URLs
  telegram_poster.py   # posts formatted messages to your channel
  storage.py            # tracks posted ASINs to avoid repeats
  main.py               # ties it all together
tests/
  test_affiliate.py     # run with: python tests/test_affiliate.py
.github/workflows/
  post-deals.yml         # the schedule that runs everything
```
