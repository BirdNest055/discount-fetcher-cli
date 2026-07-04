# aldi-cli

> Automatically fetches ALDI SÜD weekly prospectus data into a SQLite database — with brands, prices, sale prices, and full timeline history.

[![GitHub Actions](https://img.shields.io/badge/daily%20fetch-GitHub%20Actions-blue)](https://github.com/BirdNest055/aldi-cli/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## What it does

Every day at 12:00 UTC, a GitHub Actions workflow checks if the current ALDI SÜD prospectus has expired. If it has, it fetches the new week's catalog — every product, brand, price, sale price, description, and category — and stores it in a SQLite database that's committed back to the repo.

You get an email when a new week is fetched. No email when there's nothing new (no spam).

## Quick stats

- **175 products** per week (typical)
- **93% have brand names** (RIO D'ORO, CROFTON, MILSANI, etc.)
- **36 products on sale** per week (typical)
- **Price range:** 0.44 € – 49.99 €
- **30-second fetch** — no wasted compute

## How to use

### Option 1: Let it run automatically (already set up)

The daily workflow runs on GitHub Actions. You get an email when a new week is fetched. That's it — nothing to do.

**Trigger manually:** Go to [Actions → Daily ALDI fetch → Run workflow](https://github.com/BirdNest055/aldi-cli/actions/workflows/daily-fetch.yml). You can choose:
- **Mode:** `expiry` (default, only fetch after current week expires), `daily` (always check), `manual`
- **Force:** bypass the expiry check (useful for testing)

### Option 2: Run it locally

```bash
git clone https://github.com/BirdNest055/aldi-cli.git
cd aldi-cli
pip install -r requirements.txt

# Fetch a specific week
python aldi-cli.py fetch "https://prospekt.aldi-sued.de/kw27-26-op-mp/page/1"

# Auto-discover and fetch the current week
python aldi-cli.py auto-fetch --mode expiry

# See what's in the DB
python aldi-cli.py list
python aldi-cli.py show kw27-26-op-mp

# Search products
python aldi-cli.py products kw27-26-op-mp --search "avocado"
python aldi-cli.py products kw27-26-op-mp --max-price 1
python aldi-cli.py products kw27-26-op-mp --category "Eis"

# Price history for a product
python aldi-cli.py price-history "Avocado"

# Compare two weeks
python aldi-cli.py export kw27-26-op-mp --format csv -o kw27.csv
```

## Commands

| Command | What it does |
|---|---|
| `fetch <url>` | Fetch a prospectus URL into the DB |
| `auto-fetch` | Auto-discover current week, fetch if expired (`--mode expiry` by default) |
| `list` | Show all publications in the DB |
| `show <slug>` | Publication details + category breakdown + price stats |
| `products <slug>` | List products with filters (`--search`, `--category`, `--brand`, `--max-price`, `--min-price`, `--onSale`) |
| `price-history <name>` | Price evolution for a product across all weeks |
| `export <slug>` | Export to JSON or CSV |
| `download-images <slug>` | Download page JPEGs + product photos |
| `daemon` | Long-running mode (for self-hosting) |
| `install-cron` | Generate a crontab line |
| `install-systemd` | Generate a systemd service file |

## Database schema

The database uses a **Slowly Changing Dimension (SCD Type 2)** design for timeline analysis:

```
publications          One row per weekly prospectus (KW27, KW28, ...)
                      Includes valid_dates ("29.06. – 04.07.2026")

products              Canonical product identity (deduplicated across weeks)
                      Keyed by hash(normalized title + product_type)

product_offerings     One row per product × week (the fact table)
                      Holds price, brand, sale price, page number, etc.

product_titles        SCD2: every title version a product ever had
product_descriptions  SCD2: every description version
product_brands        SCD2: every brand version (if ALDI rebrands something)
```

**Why this design:** You can query "how did Avocado's price evolve over 6 months?" and get a clean time series. Same product = same `products.id`. Different weeks = different `product_offerings` rows. Text changes are preserved, not overwritten.

## Automation

### Fetching modes

| Mode | Behavior |
|---|---|
| `expiry` (default) | Only fetches when the current publication has expired (valid_date_end < today). No spam — you only get emails when there's actually a new week. |
| `daily` | Always checks for a new publication. Use this if you want to catch early/partial publications. |
| `manual` | Same as daily, for manual triggers. |

### Email notifications

The workflow sends an email (via [Resend](https://resend.com)) when:
- ✅ A new week was fetched (includes DB summary, top categories, week-over-week changes)
- ❌ An error occurred

No email when the current week is still valid (`not-due` status).

**Setup:** Add two repository secrets:
- `RESEND_API_KEY` — free API key from resend.com
- `NOTIFY_EMAIL` — your email address

### Vercel integration

When a new week is fetched and `aldi.db` is committed, the workflow calls a Vercel deploy hook that triggers the [aldi-web](https://github.com/BirdNest055/aldi-web) app to rebuild. The web app downloads the latest `aldi.db` during its build step.

**Secret:** `VERCEL_DEPLOY_HOOK` — the Vercel deploy hook URL

## Dev / Prod branches

| Branch | Purpose |
|---|---|
| `main` | Production. The daily cron reads from here. Protected. |
| `dev` | Development. Push freely, test via `dev-test.yml` workflow (uses a separate `aldi-dev.db`). |

**Workflow:**
1. Make changes on `dev`
2. Push to `dev` → `dev-test.yml` runs automatically
3. When dev-test succeeds → merge `dev` → `main`
4. `main` picks up the change on the next daily run

## Files

```
aldi-cli/
├── aldi-cli.py              ← the CLI (single file, ~1900 lines)
├── error_handler.py         ← typed exceptions, circuit breaker, retry, state
├── requirements.txt         ← just `requests`
├── .github/workflows/
│   ├── daily-fetch.yml      ← production: daily cron + manual dispatch
│   └── dev-test.yml         ← dev: push trigger + manual + smoke tests
├── aldi.db                  ← the database (committed, grows ~50 KB/week)
├── aldi.log                 ← fetch logs
├── aldi.state.json          ← circuit-breaker state (committed for cross-run dedup)
├── LICENSE                  ← MIT
└── README.md                ← this file
```

## Error handling & anti-spam

`error_handler.py` provides a comprehensive error-catching layer:

### Typed exceptions

| Exception          | When                                                  | Exit code | Retryable |
|--------------------|-------------------------------------------------------|-----------|-----------|
| `NetworkError`     | HTTP failure, timeout, DNS, 5xx server error          | 10        | Yes (3× with 5s/15s/45s backoff) |
| `ParseError`       | JSON decode failure, missing config blob, bad HTML    | 20        | No        |
| `StorageError`     | SQLite write failure, disk full, locked DB            | 30        | No        |
| `ConfigurationError` | Bad CLI args, missing env vars                      | 40        | No        |
| `AldiError` (base) | Unknown / unexpected                                   | 50        | No        |

### Circuit breaker (anti-spam)

State is persisted to `aldi.state.json` (next to the DB). After **3 consecutive identical errors** (same category + stage + message), the circuit opens and subsequent identical errors **suppress email notifications**. The circuit closes when:
- A different error type occurs (resets counter)
- A run succeeds (clears state, sends recovery email)

This prevents daily email spam if the ALDI site is down for a week.

### Anti-loop guarantees

- `cmd_daemon`: exits after circuit opens → systemd restarts with `RestartSec=60`
- `retry_network`: max 3 attempts, only for `NetworkError` (5xx/timeout), never for 4xx/parse/storage
- GitHub Actions: 1 cron run/day, no auto-retry on failure
- `fetch_publication`: 0 retries for parse/storage errors (they won't fix themselves)

### Email notification rules

| Run status         | Notify? | Reason                                  |
|--------------------|---------|-----------------------------------------|
| `fetched` (success)| Yes     | New week available                      |
| `skipped-existing` | No      | Week already in DB — no news            |
| `not-due`          | No      | Current week still valid — no news      |
| Error (first)      | Yes     | New problem — needs attention           |
| Error (same, ≤3×)  | Yes     | Still failing — gentle reminder         |
| Error (same, >3×)  | **No**  | Circuit open — suppress duplicate spam  |
| Error (different)  | Yes     | New error type — reset counter          |
| Success after error| Yes     | Recovery — confirm the fix worked       |

### Error report format

When an error occurs, the CLI emits both a human-readable report and a machine-readable line for the GHA workflow:

```
ERROR CATEGORY: network
ERROR STAGE:    discover_current_url
EXIT CODE:      10
RETRYABLE:      True
MESSAGE:        HTTP 503 fetching landing page https://prospekt.aldi-sued.de/

TRACEBACK:
  ...
```

Machine-readable (stderr, parsed by `daily-fetch.yml`):
```
error|network|discover_current_url|f3d2948e9b6754d5|notify|circuit-closed
```

Fields: `error|<category>|<stage>|<signature>|<notify|suppress>|<circuit-open|circuit-closed>`

## Data sources

- **Landing page:** `https://prospekt.aldi-sued.de/` (302-redirects to current week)
- **Publication config:** embedded in the HTML page's `<script>` tag
- **Spreads index:** `/<slug>/spreads.json` (page → image URL mapping)
- **Hotspots:** `/<slug>/page/<spread>/hotspots_data.json` (product data per spread)

The CLI fetches all 16 spreads' hotspot data, extracts products, and writes to SQLite atomically (single transaction).

## License

MIT — see [LICENSE](LICENSE).
