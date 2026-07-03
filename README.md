# aldi-cli

A single-file Python CLI that fetches [ALDI SÜD prospectus](https://prospekt.aldi-sued.de/) pages (hosted on Publitas), extracts every product with all its text metadata, and stores it in an **atomic, normalized, timeline-analysis-ready** SQLite database.

## Why this schema

The requirement: store every weekly prospectus in a way that lets you answer **"how did this product's price / description / availability evolve over time?"** That means:

- Same product across weeks → **deduplicated identity**, but **one observation row per week** (duplicates allowed for price analysis).
- Different dates → different `publication_id` (and therefore different `valid_for` / `fetched_at`).
- Text changes (title, description) → **preserved as versioned history** (SCD Type 2), not overwritten.
- Raw JSON kept on every row → nothing ever lost.

## Architecture: two-layer design (SCD Type 2)

```
IDENTITY LAYER (deduplicated, updated in place)
  publications              one row per weekly prospectus (KW27, KW28, ...)
  products                  canonical product identity, keyed by stable hash
                            of normalized (title, product_type). Same product
                            in different weeks -> same row here.

OBSERVATION LAYER (one row per product x publication)
  product_offerings         the FACT table. UNIQUE(product_id, publication_id,
                            product_id_remote). Holds price, page, position,
                            fetched_at. Same product in KW27 + KW28 = 2 rows.
  product_photos            1:N photo URLs per offering (URLs only, no blobs)
  product_labels            1:N custom labels per offering

TEXT VERSIONING (SCD Type 2 — preserves every version ALDI ever used)
  product_titles            (product_id, title, first_seen_pub, last_seen_pub)
  product_descriptions      (product_id, description, first_seen_pub, last_seen_pub)
                            When ALDI changes the text, a NEW row starts and the
                            old row's last_seen is frozen. Both stay queryable.

STRUCTURAL (per-publication, kept for completeness)
  spreads, pages, hotspots

VIEWS (ready for downstream integration)
  v_price_history           one row per (product, publication) with price
                            min/avg/max — ready for time-series. Aggregates
                            when a product appears multiple times in one week.
  v_current_products        latest title/description/price per canonical product
```

## Stable product key

ALDI's `webshop_identifier` (e.g. `999222_kw27`) is **per-week**, not stable. So the canonical key is a normalized hash:

```python
key = sha1(normalize_title(title) + "|" + normalize_title(product_type))[:16]
```

Normalization: lowercase, strip diacritics, collapse whitespace. So "Avocado" in KW27 and "Avocado" in KW28 map to the same `products.id`.

**Known tradeoff:** if ALDI renames a product (e.g. "Bier" → "Bier Premium"), it becomes a new canonical product. This is by design — you can detect renames by querying for products whose `last_seen_pub_id` is the week before another product's `first_seen_pub_id`, with otherwise matching attributes.

## Install

No build step. Requires Python 3.10+ with `requests`.

```bash
chmod +x aldi-cli/aldi-cli.py
ln -s "$(pwd)/aldi-cli/aldi-cli.py" /usr/local/bin/aldi-cli  # optional
```

## Quick start

```bash
# Fetch weekly prospectuses (run once per week)
aldi-cli fetch "https://prospekt.aldi-sued.de/kw27-26-op-mp/page/1"
aldi-cli fetch "https://prospekt.aldi-sued.de/kw28-...-.../page/1"

# List all stored publications
aldi-cli list

# Show one
aldi-cli show kw27-26-op-mp

# Query offerings for a single week
aldi-cli products kw27-26-op-mp --max-price 1 --category "Eis"
aldi-cli products kw27-26-op-mp --search "kuchen" --json

# Timeline analysis: price evolution across all weeks
aldi-cli price-history "Avocado"
aldi-cli price-history abe1fa5c1964a47c    # by product_key
aldi-cli price-history "Bier" --all --json

# Export for external tools
aldi-cli export kw27-26-op-mp --format csv -o kw27.csv
aldi-cli export kw27-26-op-mp --format json -o kw27.json
```

## Automation (daily auto-fetch)

The CLI has built-in automation that **visits the prospectus landing page once a day at a random time, detects the current week's publication, and fetches it only if it isn't already in the DB**.

```bash
# One-shot: discover current week, fetch if missing, exit.
# Ideal for cron — the --jitter flag sleeps a random 0-6h first.
aldi-cli auto-fetch --jitter 12-18 --log-file /var/log/aldi.log

# Long-running daemon: each day, sleep until a random time in the window,
# then run the auto-fetch logic. Loops forever.
aldi-cli daemon --hours 12-18 --log-file /var/log/aldi.log

# Generate a crontab line that runs auto-fetch daily
aldi-cli install-cron --hours 12-18

# Generate a systemd user service file for the daemon
aldi-cli install-systemd --hours 12-18 -o aldi-daemon.service
```

### How auto-discovery works

1. Fetch `https://prospekt.aldi-sued.de/` (the landing page 302-redirects to the current week's URL).
2. Fetch only the HTML of that URL (lightweight — no spreads/hotspots yet).
3. Extract the `publication_id` from the embedded config blob.
4. Check if that `publication_id` is already in the `publications` table.
5. If yes → skip (already have this week). If no → run the full fetch pipeline.

### The `--force` flag

If ALDI publishes a partial week early (a few placeholder products) and fills it in later, the daemon would normally skip it forever (the publication_id doesn't change). Use `--force` to re-fetch even when the publication_id is already in the DB. The fetch is idempotent — canonical `products` rows are preserved (they're the timeline), only that week's `product_offerings` are replaced.

```bash
aldi-cli auto-fetch --force   # re-fetch current week even if already in DB
```

### Install as a systemd user service (recommended for always-on machines)

```bash
aldi-cli install-systemd --hours 12-18 -o ~/.config/systemd/user/aldi-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now aldi-daemon.service
journalctl --user -u aldi-daemon.service -f   # tail logs
```

### Install via cron (recommended for occasional machines)

```bash
aldi-cli install-cron --hours 12-18
# Copy the printed line into your crontab:
crontab -e
# 0 12 * * * /usr/bin/python3 /path/to/aldi-cli.py auto-fetch --db ... --jitter 12-18 ...
```

### Deploy on GitHub Actions (recommended — free forever, zero maintenance)

This repo ships with a ready-to-use GitHub Actions workflow at
`.github/workflows/daily-fetch.yml`. It runs `auto-fetch` once a day at 12:00 UTC
with a random 0-6h jitter, then commits the updated `aldi.db` back to the repo
(so the DB persists in git history — free backups included).

**Setup (2 minutes, no server, no credit card):**

1. Fork or clone this repo to your GitHub account.
2. Go to the repo's **Settings → Actions → General** and ensure
   "Allow all actions" is enabled.
3. Under **Workflow permissions**, enable **"Read and write permissions"**
   (so the workflow can push the DB back).
4. Go to the **Actions** tab and click "Enable workflows".
5. Click **"Daily ALDI fetch"** → **"Run workflow"** to test it manually.

The workflow will then run automatically every day at 12:00 UTC. The actual
fetch happens at a random time between 12:00 and 18:00 UTC (the CLI's
`--jitter 0-6` flag sleeps a random 0-6 hours first).

**Why this is the best option:**
- ✅ Free forever for public repos (2000 min/month free for private — you'll use ~60)
- ✅ No server, no credit card, no maintenance
- ✅ DB persists in git — free automatic backups + full history
- ✅ Manual trigger anytime via the Actions UI
- ✅ Logs visible in the Actions tab for every run

## Commands

| Command | Purpose |
|---|---|
| `fetch <url>` | Fetch → SQLite (atomic, idempotent) |
| `list` | All publications in DB |
| `show <id\|slug>` | Publication summary + category breakdown + price stats |
| `products <id\|slug>` | Offerings for one publication (with `--category`, `--search`, `--min-price`, `--max-price`, `--json`, `--csv`) |
| `price-history <key\|title>` | Price evolution + SCD2 text history across all publications (`--all`, `--json`) |
| `export <id\|slug>` | Full export to JSON or CSV |
| `download-images <id\|slug>` | Page JPEGs + product photos (`--quality`, `--pages`, `--products`) |
| `auto-fetch` | One-shot: discover current week, fetch if missing, exit (`--jitter`, `--force`, `--log-file`, `--dry-run`) |
| `daemon` | Long-running: daily auto-fetch at random time in window (`--hours`, `--force`, `--log-file`, `--dry-run`) |
| `install-cron` | Print crontab line for daily auto-fetch (`--hours`) |
| `install-systemd` | Generate systemd user service file for the daemon (`--hours`, `-o`) |

## Timeline analysis queries

Once you have 2+ publications, these patterns work directly against the SQLite DB:

```sql
-- Price evolution: products seen in both KW27 and KW28
SELECT p.canonical_title,
       MAX(CASE WHEN ph.publication_id = <KW27_ID> THEN ph.price_min END) AS kw27,
       MAX(CASE WHEN ph.publication_id = <KW28_ID> THEN ph.price_min END) AS kw28
FROM v_price_history ph
JOIN products p ON p.id = ph.product_id
GROUP BY p.id
HAVING kw27 IS NOT NULL AND kw28 IS NOT NULL
ORDER BY ABS(kw28 - kw27) DESC;

-- New products in KW28 (first seen there)
SELECT canonical_title, first_seen_at
FROM products WHERE first_seen_pub_id = <KW28_ID>;

-- Products that disappeared (last seen in KW27, not in KW28)
SELECT canonical_title FROM products
WHERE last_seen_pub_id = <KW27_ID>;

-- SCD2: products whose description changed
SELECT p.canonical_title, COUNT(*) AS n_versions
FROM product_descriptions pd
JOIN products p ON p.id = pd.product_id
GROUP BY p.id HAVING n_versions > 1;
```

## Atomicity & DB-friendliness

- **Single-transaction writes**: every `fetch` is wrapped in `with conn:` — a failure rolls back the entire publication update. The DB never holds a partial state.
- **Foreign keys ON**: `PRAGMA foreign_keys = ON` enforced on every connection; deleting a publication cascades to all child offerings/photos/labels.
- **Idempotent**: re-fetching the same URL replaces that publication's offerings cleanly (canonical `products` rows are preserved — they're the timeline).
- **Normalized**: photos and labels are 1:N child tables, not JSON blobs. Text versions are SCD2 rows, not overwrites.
- **Indexed**: lookups by product, publication, price, and category are all index-backed.
- **Portable**: SQLite is a single file — easy to ship, copy, or import into Postgres/MySQL via `.dump`.
- **Schema migration**: opening an old-schema DB auto-migrates (legacy tables are renamed with a timestamp suffix; new schema takes over).

## What's stored vs. not stored

| Stored | Not stored |
|---|---|
| All product text (title, description, category) | Binary images (only URLs kept) |
| All prices (raw + parsed numeric) | Full-resolution page renders by default (use `download-images`) |
| All custom labels (validity dates, "new" flags, units) | User session data, cookies |
| All hotspot positions (where on the page) | Tracking/telemetry payloads |
| All photo URLs (thumb + full + sharing) | |
| Raw JSON of every product + every hotspot (full audit trail) | |
| Every text version ever seen (SCD2) | |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `ALDI_DB` | `/home/z/my-project/download/aldi.db` | SQLite path |

## How extraction works

1. **Parse URL** → extract `slug`.
2. **Fetch HTML** → regex-extract the `var data = {...}` config blob (publication id, num_pages, cache_token, pdf_url).
3. **Fetch spreads.json** → page→image mapping.
4. **Fetch hotspots_data.json** for each spread (page 1, then 2-3, 4-5, …, 30-31).
5. **For each product** in each hotspot:
   - Compute stable `product_key` → upsert into `products` (identity layer).
   - Insert one row into `product_offerings` (observation layer).
   - SCD2-upsert title and description into `product_titles` / `product_descriptions`.
   - Insert photos and labels as 1:N children of the offering.
6. **Commit once** (atomic).
