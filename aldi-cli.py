#!/usr/bin/env python3
"""
aldi-cli — Fetch and extract product data from ALDI SÜD prospectus pages
           (hosted on Publitas) into an atomic, DB-friendly SQLite store
           designed for TIMELINE ANALYSIS across weekly publications.

DATA MODEL
----------
Two-layer design (Slowly Changing Dimension Type 2):

  Identity layer (deduplicated, updated in place):
    publications           one row per weekly prospectus
    products               canonical product identity, keyed by stable hash
                           of normalized (title, product_type)

  Observation layer (one row per product x publication):
    product_offerings      the fact table — price, page, hotspot position,
                           fetched_at. UNIQUE(product_id, publication_id).
    product_photos         1:N photo URLs per offering
    product_labels         1:N custom labels per offering

  Text-versioning (SCD Type 2 — preserves every version ALDI ever used):
    product_titles         (product_id, title, first_seen_pub_id, last_seen_pub_id)
    product_descriptions   (product_id, description, first_seen_pub_id, last_seen_pub_id)

  Spatial / structural:
    spreads, pages, hotspots (per-publication, kept for completeness)

VIEWS
-----
  v_price_history         one row per (product, publication) with price +
                           publication date — ready for time-series analysis
  v_current_products      latest title/description/price per canonical product

This lets you answer:
  - "How did Avocado's price evolve over the last 6 months?"
      -> SELECT * FROM v_price_history WHERE product_key = '...'
  - "Which products had their description changed between KW27 and KW28?"
      -> query product_descriptions for rows where first_seen != last_seen
  - "Which products appeared or disappeared?"
      -> set-difference on product_offerings between two publication_ids

USAGE
-----
    aldi-cli.py fetch <url> [--db PATH] [--with-images]
    aldi-cli.py list [--db PATH]
    aldi-cli.py show   <pub-id-or-slug> [--db PATH]
    aldi-cli.py products <pub-id-or-slug> [--db PATH] [--category TXT]
                          [--max-price N] [--min-price N] [--search TXT]
                          [--json] [--csv]
    aldi-cli.py price-history <product-key-or-title> [--db PATH] [--json]
    aldi-cli.py export <pub-id-or-slug> [--db PATH] [--format json|csv] [-o FILE]
    aldi-cli.py download-images <pub-id-or-slug> [--db PATH] [--out DIR]
                                [--quality at2400|at600|...]

AUTOMATION
----------
    aldi-cli.py auto-fetch [--url URL] [--jitter 12-18] [--log-file FILE]
                            [--dry-run] [--with-images]
        One-shot: discover the current week's prospectus URL (via redirect
        from https://prospekt.aldi-sued.de/), check if its publication_id
        is already in the DB, fetch if missing, exit.

    aldi-cli.py daemon [--url URL] [--hours 12-18] [--log-file FILE]
                       [--dry-run] [--with-images]
        Long-running: each day, pick a random time within the hours window,
        sleep until then, run the auto-fetch logic, repeat forever.

    aldi-cli.py install-cron   [--url URL] [--hours 12-18]
    aldi-cli.py install-systemd [--url URL] [--hours 12-18] [-o FILE]
        Generate a crontab line / systemd unit that runs auto-fetch daily.

URL pattern supported:
    https://prospekt.aldi-sued.de/<slug>/page/<n>
    https://prospekt.aldi-sued.de/<slug>/

The DB is a single SQLite file with normalized tables, foreign keys,
single-transaction writes (atomic), and idempotent re-fetches.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_DB = os.environ.get("ALDI_DB", "/home/z/my-project/download/aldi.db")
DEFAULT_IMG_DIR = "/home/z/my-project/download/aldi-images"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


# --------------------------------------------------------------------------- #
# URL parsing
# --------------------------------------------------------------------------- #

URL_RE = re.compile(
    r"^https?://prospekt\.aldi-sued\.de/(?P<slug>[a-z0-9-]+)(?:/page/(?P<page>\d+))?/?$",
    re.IGNORECASE,
)


@dataclass
class ParsedURL:
    slug: str
    start_page: int = 1
    base: str = "https://prospekt.aldi-sued.de"


def parse_url(url: str) -> ParsedURL:
    m = URL_RE.match(url.strip())
    if not m:
        raise SystemExit(
            f"ERROR: URL does not match expected pattern.\n"
            f"  Got: {url}\n"
            f"  Expected: https://prospekt.aldi-sued.de/<slug>[/page/<n>]"
        )
    return ParsedURL(
        slug=m.group("slug"),
        start_page=int(m.group("page") or 1),
    )


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

session: requests.Session | None = None


def http() -> requests.Session:
    global session
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
    return session


def get_html(url: str) -> str:
    r = http().get(url, timeout=30)
    r.raise_for_status()
    return r.text


def get_json(url: str) -> Any:
    r = http().get(url, timeout=30)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Page / config scraping
# --------------------------------------------------------------------------- #

CONFIG_RE = re.compile(r"var\s+data\s*=\s*(\{.*?\});\s*\n", re.DOTALL)


def extract_publication_config(html: str) -> dict:
    """Pull the `var data = {...}` JSON blob out of the page HTML."""
    m = CONFIG_RE.search(html)
    if not m:
        raise SystemExit("ERROR: could not find publication config in HTML")
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# Product identity (stable natural key)
# --------------------------------------------------------------------------- #

def normalize_text(s: str) -> str:
    """Normalize a product title/type for stable hashing:
       lowercase, fold unicode, strip diacritics, collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def product_key(title: str, product_type: str = "") -> str:
    """Stable 16-char hex key for canonical product identity.
    Same product in different weeks -> same key."""
    raw = normalize_text(title) + "|" + normalize_text(product_type)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# DB schema (timeline-analysis oriented)
# --------------------------------------------------------------------------- #

SCHEMA = r"""
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- === IDENTITY LAYER ====================================================== --

CREATE TABLE IF NOT EXISTS publications (
    id                INTEGER PRIMARY KEY,           -- remote publication id
    slug              TEXT    NOT NULL UNIQUE,
    group_id          INTEGER NOT NULL,
    title             TEXT,
    original_title    TEXT,
    language          TEXT,
    canonical_url     TEXT,
    pdf_url           TEXT,
    num_pages         INTEGER,
    description       TEXT,
    valid_for         TEXT,                          -- raw description (kept for compatibility)
    valid_dates       TEXT,                          -- parsed: "29.06.2026 – 04.07.2026"
    valid_date_start  TEXT,                          -- ISO: "2026-06-29"
    valid_date_end    TEXT,                          -- ISO: "2026-07-04"
    customer_name     TEXT,
    cache_token       TEXT,
    fetched_at        TEXT    NOT NULL,              -- ISO-8601 UTC
    raw_config        TEXT                            -- full JSON config blob
);

CREATE TABLE IF NOT EXISTS products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key       TEXT    NOT NULL UNIQUE,        -- stable hash(title|type)
    canonical_title   TEXT,                           -- most recently seen title
    canonical_type    TEXT,                           -- most recently seen type
    first_seen_pub_id INTEGER REFERENCES publications(id) ON DELETE SET NULL,
    last_seen_pub_id  INTEGER REFERENCES publications(id) ON DELETE SET NULL,
    first_seen_at     TEXT,                           -- ISO-8601 UTC
    last_seen_at      TEXT                            -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_products_canonical_title ON products(canonical_title);

-- === OBSERVATION LAYER =================================================== --

CREATE TABLE IF NOT EXISTS spreads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id    INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    spread_idx        INTEGER NOT NULL,
    page_range        TEXT    NOT NULL,
    num_hotspots      INTEGER DEFAULT 0,
    UNIQUE(publication_id, spread_idx)
);

CREATE TABLE IF NOT EXISTS pages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id    INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    page_number       INTEGER NOT NULL,
    spread_id         INTEGER REFERENCES spreads(id) ON DELETE SET NULL,
    page_id_remote    INTEGER,
    image_at2400      TEXT,
    image_at2000      TEXT,
    image_at1600      TEXT,
    image_at1200      TEXT,
    image_at1000      TEXT,
    image_at800       TEXT,
    image_at600       TEXT,
    image_at200       TEXT,
    UNIQUE(publication_id, page_number)
);

CREATE TABLE IF NOT EXISTS hotspots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id    INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    spread_id         INTEGER NOT NULL REFERENCES spreads(id) ON DELETE CASCADE,
    hotspot_id_remote INTEGER NOT NULL,
    type              TEXT,
    title             TEXT,
    page_range        TEXT,
    clickable         INTEGER,
    position_left     REAL,
    position_top      REAL,
    position_width    REAL,
    position_height   REAL,
    position_icon_left REAL,
    position_icon_top REAL,
    raw               TEXT,
    UNIQUE(publication_id, hotspot_id_remote)
);

CREATE TABLE IF NOT EXISTS product_offerings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    publication_id    INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    hotspot_id        INTEGER REFERENCES hotspots(id) ON DELETE SET NULL,
    product_id_remote INTEGER NOT NULL,              -- Publitas' per-week id (NOT stable)
    title             TEXT,                          -- title AS SEEN in this publication
    description       TEXT,                          -- description AS SEEN in this publication
    brand             TEXT,                          -- brand AS SEEN (e.g. "RIO D'ORO", "MILSANI")
    price             TEXT,                          -- raw regular price string
    price_numeric     REAL,                          -- parsed regular price (NULL if unparseable)
    discounted_price  TEXT,                          -- raw sale price string (if on sale)
    discounted_price_numeric REAL,                   -- parsed sale price
    currency          TEXT DEFAULT 'EUR',
    product_type      TEXT,
    webshop_identifier TEXT,                         -- per-week, e.g. "999222_kw27"
    webshop_url       TEXT,                          -- per-week URL (recipes, webshop links)
    spread            TEXT,
    page_range        TEXT,
    raw               TEXT,                          -- full JSON of this product entry
    -- Same canonical product can appear multiple times in one publication
    -- (e.g. listed on two pages). Each catalog entry gets its own row,
    -- all linked to the same canonical product. Price-history aggregates.
    UNIQUE(product_id, publication_id, product_id_remote)
);

CREATE INDEX IF NOT EXISTS idx_offerings_pub     ON product_offerings(publication_id);
CREATE INDEX IF NOT EXISTS idx_offerings_product ON product_offerings(product_id);
CREATE INDEX IF NOT EXISTS idx_offerings_price   ON product_offerings(price_numeric);
CREATE INDEX IF NOT EXISTS idx_offerings_type    ON product_offerings(product_type);
CREATE INDEX IF NOT EXISTS idx_offerings_brand   ON product_offerings(brand);

CREATE TABLE IF NOT EXISTS product_photos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id       INTEGER NOT NULL REFERENCES product_offerings(id) ON DELETE CASCADE,
    url               TEXT    NOT NULL,
    kind              TEXT,
    width             INTEGER,
    UNIQUE(offering_id, url)
);

CREATE TABLE IF NOT EXISTS product_labels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id       INTEGER NOT NULL REFERENCES product_offerings(id) ON DELETE CASCADE,
    key               TEXT    NOT NULL,
    value             TEXT,
    UNIQUE(offering_id, key)
);

-- === SCD TYPE 2: TEXT VERSIONING ========================================= --

CREATE TABLE IF NOT EXISTS product_titles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    title             TEXT    NOT NULL,
    first_seen_pub_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    last_seen_pub_id  INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    UNIQUE(product_id, title)
);

CREATE INDEX IF NOT EXISTS idx_titles_product ON product_titles(product_id);

CREATE TABLE IF NOT EXISTS product_descriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    description       TEXT    NOT NULL,
    first_seen_pub_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    last_seen_pub_id  INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    UNIQUE(product_id, description)
);

CREATE INDEX IF NOT EXISTS idx_descs_product ON product_descriptions(product_id);

CREATE TABLE IF NOT EXISTS product_brands (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    brand             TEXT    NOT NULL,
    first_seen_pub_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    last_seen_pub_id  INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    UNIQUE(product_id, brand)
);

CREATE INDEX IF NOT EXISTS idx_brands_product ON product_brands(product_id);

-- === VIEWS =============================================================== --

-- One row per (product, publication) — ready for time-series analysis.
-- Aggregates when a product appears multiple times in one publication
-- (e.g. listed on two pages): min/avg/max price across that week's entries.
DROP VIEW IF EXISTS v_price_history;
CREATE VIEW v_price_history AS
SELECT
    p.id              AS product_id,
    p.product_key,
    p.canonical_title,
    p.canonical_type,
    pub.id            AS publication_id,
    pub.slug          AS publication_slug,
    pub.original_title AS publication_original_title,
    pub.fetched_at    AS publication_fetched_at,
    pub.valid_for,
    MIN(o.title)      AS title_this_week,
    MIN(o.price)      AS price_min_raw,
    MIN(o.price_numeric) AS price_min,
    MAX(o.price_numeric) AS price_max,
    AVG(o.price_numeric) AS price_avg,
    o.currency,
    MIN(o.product_type) AS product_type_this_week,
    MIN(o.page_range) AS page_range,
    COUNT(*)          AS n_offerings,
    GROUP_CONCAT(DISTINCT o.webshop_identifier) AS webshop_identifiers
FROM products p
JOIN product_offerings o  ON o.product_id = p.id
JOIN publications pub     ON pub.id = o.publication_id
GROUP BY p.id, pub.id
ORDER BY p.product_key, pub.fetched_at;

-- Latest observation per canonical product.
DROP VIEW IF EXISTS v_current_products;
CREATE VIEW v_current_products AS
SELECT
    p.*,
    o.price_numeric   AS current_price,
    o.currency,
    o.title           AS current_title,
    o.product_type    AS current_type,
    o.webshop_identifier AS current_webshop_id,
    pub.slug          AS current_publication_slug,
    pub.fetched_at    AS current_fetched_at
FROM products p
JOIN product_offerings o  ON o.id = (
    SELECT o2.id FROM product_offerings o2
    WHERE o2.product_id = p.id
    ORDER BY o2.publication_id DESC LIMIT 1
)
JOIN publications pub ON pub.id = o.publication_id;
"""


# Migration: upgrade older DBs (pre-timeline schema) to the new layout.
# Strategy: if the legacy `products` table has a `publication_id` column
# (i.e. it's the old per-publication layout), back up the data and rebuild.
MIGRATION_CHECK_SQL = """
SELECT COUNT(*) FROM pragma_table_info('products')
WHERE name = 'publication_id';
"""


def open_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    migrate_legacy_schema(conn)
    conn.executescript(SCHEMA)
    add_missing_columns(conn)
    return conn


def add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from older DBs (in-place ALTER TABLE).
    This lets us evolve the schema without re-fetching everything."""
    # Map (table, column) -> DDL for the column (without the column name prefix)
    # Only includes columns added AFTER the initial schema.
    new_columns = [
        ("publications", "valid_dates",      "TEXT"),
        ("publications", "valid_date_start", "TEXT"),
        ("publications", "valid_date_end",   "TEXT"),
        ("product_offerings", "brand",                    "TEXT"),
        ("product_offerings", "discounted_price",         "TEXT"),
        ("product_offerings", "discounted_price_numeric", "REAL"),
        ("product_offerings", "webshop_url",              "TEXT"),
    ]
    for table, column, ddl in new_columns:
        # Check if the column already exists
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not cols:
            continue  # table doesn't exist yet (CREATE TABLE will handle it)
        existing = {c[1] for c in cols}
        if column not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass  # column may have been added by a concurrent call
    conn.commit()


def migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """If the DB has the old per-publication `products` table, back it up
    and drop the legacy child tables so the new schema can take over."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='products'"
    )
    if not cur.fetchone():
        return  # nothing to migrate

    has_old_layout = conn.execute(MIGRATION_CHECK_SQL).fetchone()[0] > 0
    if not has_old_layout:
        return  # already new schema

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"[migrate] Legacy schema detected. Backing up old tables with suffix _legacy_{ts}",
          file=sys.stderr)
    for t in ("products", "product_photos", "product_labels"):
        conn.execute(f"ALTER TABLE IF EXISTS {t} RENAME TO {t}_legacy_{ts};")
    # Old tables backed up; new schema will be created by SCHEMA script.


# --------------------------------------------------------------------------- #
# Fetch pipeline
# --------------------------------------------------------------------------- #

@dataclass
class FetchResult:
    publication_id: int
    slug: str
    num_spreads: int
    num_pages: int
    num_hotspots: int
    num_offerings: int
    num_new_products: int
    db_path: str


def parse_price(p: Any) -> float | None:
    if p is None or p == "":
        return None
    try:
        return float(str(p).replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_valid_dates(description: str) -> tuple[str, str, str]:
    """Extract the validity date range from ALDI's description string.

    The description looks like:
        "Entdecke den Prospekt... Aktuelle Angebote für: Montag 29.06.2026 I
         Donnerstag 02.07.2026 I Freitag 03.07.2026 I Samstag 04.07.2026"

    Returns (valid_dates_pretty, iso_start, iso_end) e.g.:
        ("29.06. – 04.07.2026", "2026-06-29", "2026-07-04")

    If parsing fails, returns ("", "", "").
    """
    if not description:
        return "", "", ""
    # Find all DD.MM.YYYY dates in the description
    dates = re.findall(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", description)
    if not dates:
        return "", "", ""
    # Convert to (day, month, year) ints + ISO strings
    iso_dates = []
    for d, m, y in dates:
        iso_dates.append((f"{y}-{int(m):02d}-{int(d):02d}", int(d), int(m), int(y)))
    if not iso_dates:
        return "", "", ""
    iso_dates.sort(key=lambda x: x[0])
    first_iso, first_d, first_m, first_y = iso_dates[0]
    last_iso, last_d, last_m, last_y = iso_dates[-1]
    # Pretty range: "29.06. – 04.07.2026" (year only at the end)
    pretty = f"{first_d:02d}.{first_m:02d}. – {last_d:02d}.{last_m:02d}.{last_y}"
    return pretty, first_iso, last_iso


def spreads_list(num_pages: int) -> list[str]:
    """Reproduce the Publitas spread layout: page 1 alone, then 2-3, 4-5, ..."""
    if num_pages < 1:
        return []
    spreads = ["1"]
    i = 2
    while i <= num_pages:
        spreads.append(f"{i}-{i+1}" if i + 1 <= num_pages else str(i))
        i += 2
    return spreads


def fetch_publication(url: str, db_path: str, with_images: bool = False,
                      img_dir: str = DEFAULT_IMG_DIR,
                      progress=lambda *a, **k: None) -> FetchResult:
    parsed = parse_url(url)
    page_url = f"{parsed.base}/{parsed.slug}/page/{parsed.start_page}"
    progress(f"Fetching page: {page_url}")
    html = get_html(page_url)
    cfg = extract_publication_config(html)

    pub_id = cfg["id"]
    group_id = cfg["groupId"]
    config = cfg.get("config", {})
    cache_token = cfg.get("cacheToken", "")
    num_pages = cfg.get("numPages", 0)
    title = config.get("publicationTitle") or cfg.get("groupTitle")
    original_title = config.get("publicationOriginalTitle")
    language = config.get("language", "de")
    canonical = config.get("canonicalUrl", f"{parsed.base}/{parsed.slug}/")
    pdf_url = config.get("downloadPdfUrl")
    description = config.get("description", "")
    valid_for = description
    customer_name = config.get("customerName")

    spreads_url = f"{parsed.base}/{parsed.slug}/spreads.json?version={cache_token}&page=1"
    progress(f"Fetching spreads: {spreads_url}")
    spreads_data = get_json(spreads_url)

    if isinstance(spreads_data, dict) and "pages" in spreads_data:
        spreads_objs = spreads_data["pages"] if isinstance(spreads_data["pages"], list) \
            else spreads_data.get("spreads", [])
    elif isinstance(spreads_data, list):
        spreads_objs = spreads_data
    else:
        spreads_objs = list(spreads_data.values()) if isinstance(spreads_data, dict) else []

    spread_ranges = spreads_list(num_pages)
    progress(f"Publication has {num_pages} pages -> {len(spread_ranges)} spreads")

    all_hotspots: dict[str, list[dict]] = {}
    for i, spread in enumerate(spread_ranges, 1):
        h_url = (f"{parsed.base}/{parsed.slug}/page/{spread}/"
                 f"hotspots_data.json?version={cache_token}&page=1")
        progress(f"  [{i}/{len(spread_ranges)}] spread {spread}")
        try:
            all_hotspots[spread] = get_json(h_url)
        except requests.HTTPError as e:
            progress(f"      HTTP {e.response.status_code} — skipping")
            all_hotspots[spread] = []

    progress(f"Writing to DB: {db_path}")
    conn = open_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    n_hotspots = 0
    n_offerings = 0
    n_new_products = 0
    valid_pretty, valid_start, valid_end = parse_valid_dates(description)
    try:
        with conn:  # atomic transaction
            # Upsert publication
            conn.execute(
                """
                INSERT INTO publications
                  (id, slug, group_id, title, original_title, language,
                   canonical_url, pdf_url, num_pages, description, valid_for,
                   valid_dates, valid_date_start, valid_date_end,
                   customer_name, cache_token, fetched_at, raw_config)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  slug=excluded.slug, group_id=excluded.group_id,
                  title=excluded.title, original_title=excluded.original_title,
                  language=excluded.language, canonical_url=excluded.canonical_url,
                  pdf_url=excluded.pdf_url, num_pages=excluded.num_pages,
                  description=excluded.description, valid_for=excluded.valid_for,
                  valid_dates=excluded.valid_dates,
                  valid_date_start=excluded.valid_date_start,
                  valid_date_end=excluded.valid_date_end,
                  customer_name=excluded.customer_name, cache_token=excluded.cache_token,
                  fetched_at=excluded.fetched_at, raw_config=excluded.raw_config
                """,
                (pub_id, parsed.slug, group_id, title, original_title, language,
                 canonical, pdf_url, num_pages, description, valid_for,
                 valid_pretty, valid_start, valid_end,
                 customer_name, cache_token, now_iso,
                 json.dumps(cfg, ensure_ascii=False)),
            )

            # Wipe this publication's observations (idempotent refetch).
            # Canonical `products` rows are NEVER deleted — they are the timeline.
            conn.execute("""
                DELETE FROM product_labels
                WHERE offering_id IN (
                    SELECT id FROM product_offerings WHERE publication_id=?
                )
            """, (pub_id,))
            conn.execute("""
                DELETE FROM product_photos
                WHERE offering_id IN (
                    SELECT id FROM product_offerings WHERE publication_id=?
                )
            """, (pub_id,))
            conn.execute("DELETE FROM product_offerings WHERE publication_id=?", (pub_id,))
            conn.execute("DELETE FROM hotspots        WHERE publication_id=?", (pub_id,))
            conn.execute("DELETE FROM pages           WHERE publication_id=?", (pub_id,))
            conn.execute("DELETE FROM spreads         WHERE publication_id=?", (pub_id,))

            # Spreads
            spread_db_ids: dict[str, int] = {}
            for idx, spread in enumerate(spread_ranges):
                cur = conn.execute(
                    "INSERT INTO spreads (publication_id, spread_idx, page_range, num_hotspots) "
                    "VALUES (?,?,?,?)",
                    (pub_id, idx, spread, len(all_hotspots.get(spread, []))),
                )
                spread_db_ids[spread] = cur.lastrowid

            # Pages
            for idx, spread in enumerate(spread_ranges):
                spread_obj = spreads_objs[idx] if idx < len(spreads_objs) and \
                    isinstance(spreads_objs[idx], dict) else None
                pages_in_spread = []
                if spread_obj and "pages" in spread_obj and \
                        isinstance(spread_obj["pages"], list):
                    pages_in_spread = spread_obj["pages"]
                if spread == "1":
                    page_nums = [1]
                elif "-" in spread:
                    a, b = spread.split("-")
                    page_nums = list(range(int(a), int(b) + 1))
                else:
                    page_nums = [int(spread)]
                for j, pn in enumerate(page_nums):
                    page_obj = pages_in_spread[j] if j < len(pages_in_spread) else {}
                    imgs = page_obj.get("images", {}) if isinstance(page_obj, dict) else {}
                    conn.execute(
                        """INSERT INTO pages
                          (publication_id, page_number, spread_id, page_id_remote,
                           image_at2400, image_at2000, image_at1600, image_at1200,
                           image_at1000, image_at800, image_at600, image_at200)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pub_id, pn, spread_db_ids[spread],
                         page_obj.get("id") if isinstance(page_obj, dict) else None,
                         imgs.get("at2400"), imgs.get("at2000"), imgs.get("at1600"),
                         imgs.get("at1200"), imgs.get("at1000"), imgs.get("at800"),
                         imgs.get("at600"), imgs.get("at200")),
                    )

            # Hotspots + product offerings + canonical products + SCD2 text
            for spread, hotspots in all_hotspots.items():
                sid = spread_db_ids[spread]
                for h in hotspots:
                    pos = h.get("position") or {}
                    h_title = h.get("title")
                    if h_title == "{first_product_title}":
                        h_title = h.get("firstProductTitle") or h_title
                    cur = conn.execute(
                        """INSERT INTO hotspots
                          (publication_id, spread_id, hotspot_id_remote, type, title,
                           page_range, clickable,
                           position_left, position_top, position_width, position_height,
                           position_icon_left, position_icon_top, raw)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pub_id, sid, h.get("id"), h.get("type"), h_title,
                         spread, 1 if h.get("clickable") else 0,
                         pos.get("left"), pos.get("top"), pos.get("width"), pos.get("height"),
                         pos.get("iconLeft"), pos.get("iconTop"),
                         json.dumps(h, ensure_ascii=False)),
                    )
                    hid = cur.lastrowid
                    n_hotspots += 1

                    for prod in h.get("products", []) or []:
                        p_title = (prod.get("title") or "").strip()
                        p_type = prod.get("productType", "") or ""
                        p_desc = (prod.get("description") or "").strip()
                        p_price_raw = prod.get("price", "")
                        p_price_num = parse_price(p_price_raw)
                        p_key = product_key(p_title, p_type)

                        # Upsert canonical product (identity layer)
                        existing = conn.execute(
                            "SELECT id, first_seen_pub_id, first_seen_at FROM products WHERE product_key=?",
                            (p_key,),
                        ).fetchone()
                        if existing:
                            pid = existing["id"]
                            conn.execute(
                                """UPDATE products SET
                                    canonical_title=?, canonical_type=?,
                                    last_seen_pub_id=?, last_seen_at=?
                                   WHERE id=?""",
                                (p_title, p_type, pub_id, now_iso, pid),
                            )
                        else:
                            cur2 = conn.execute(
                                """INSERT INTO products
                                  (product_key, canonical_title, canonical_type,
                                   first_seen_pub_id, last_seen_pub_id,
                                   first_seen_at, last_seen_at)
                                  VALUES (?,?,?,?,?,?,?)""",
                                (p_key, p_title, p_type, pub_id, pub_id, now_iso, now_iso),
                            )
                            pid = cur2.lastrowid
                            n_new_products += 1

                        # Insert offering (observation layer)
                        p_brand = prod.get("brand", "") or ""
                        p_disc_raw = prod.get("discountedPrice", "")
                        p_disc_num = parse_price(p_disc_raw) if p_disc_raw else None
                        p_webshop_url = prod.get("webshopUrl", "") or ""
                        cur3 = conn.execute(
                            """INSERT INTO product_offerings
                              (product_id, publication_id, hotspot_id, product_id_remote,
                               title, description, brand,
                               price, price_numeric,
                               discounted_price, discounted_price_numeric,
                               currency, product_type,
                               webshop_identifier, webshop_url,
                               spread, page_range, raw)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (pid, pub_id, hid, prod.get("id"),
                             p_title, p_desc, p_brand,
                             p_price_raw, p_price_num,
                             p_disc_raw if p_disc_raw else "", p_disc_num,
                             "EUR", p_type,
                             prod.get("webshopIdentifier", ""), p_webshop_url,
                             spread, spread,
                             json.dumps(prod, ensure_ascii=False)),
                        )
                        oid = cur3.lastrowid
                        n_offerings += 1

                        # SCD Type 2: titles
                        _scd2_upsert(conn, "product_titles", pid, pub_id, now_iso,
                                     "title", p_title)
                        # SCD Type 2: descriptions (only when non-empty)
                        if p_desc:
                            _scd2_upsert(conn, "product_descriptions", pid, pub_id,
                                         now_iso, "description", p_desc)
                        # SCD Type 2: brands (only when non-empty)
                        if p_brand:
                            _scd2_upsert(conn, "product_brands", pid, pub_id,
                                         now_iso, "brand", p_brand)

                        # Photos
                        for ph in prod.get("photoUrls", []) or []:
                            for kind, url in ph.items():
                                if url:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO product_photos (offering_id, url, kind) VALUES (?,?,?)",
                                        (oid, url, kind),
                                    )
                        if prod.get("photoSharingUrl"):
                            conn.execute(
                                "INSERT OR IGNORE INTO product_photos (offering_id, url, kind) VALUES (?,?,?)",
                                (oid, prod["photoSharingUrl"], "sharing"),
                            )

                        # Custom labels
                        for k, v in prod.items():
                            if k.startswith("customLabel") and v not in (None, ""):
                                conn.execute(
                                    "INSERT OR REPLACE INTO product_labels (offering_id, key, value) VALUES (?,?,?)",
                                    (oid, k, str(v)),
                                )
        progress("Commit complete.")
    finally:
        conn.close()

    if with_images:
        download_images(db_path, parsed.slug, img_dir, quality="at600",
                        include_pages=True, include_products=True,
                        progress=progress)

    return FetchResult(
        publication_id=pub_id,
        slug=parsed.slug,
        num_spreads=len(spread_ranges),
        num_pages=num_pages,
        num_hotspots=n_hotspots,
        num_offerings=n_offerings,
        num_new_products=n_new_products,
        db_path=db_path,
    )


def _scd2_upsert(conn: sqlite3.Connection, table: str, product_id: int,
                 pub_id: int, now_iso: str, value_col: str, value: str) -> None:
    """SCD Type 2 upsert:
       - If (product_id, value) already exists, extend its last_seen to this pub.
       - Otherwise start a new version row.
    """
    row = conn.execute(
        f"SELECT id FROM {table} WHERE product_id=? AND {value_col}=?",
        (product_id, value),
    ).fetchone()
    if row:
        conn.execute(
            f"UPDATE {table} SET last_seen_pub_id=?, last_seen_at=? WHERE id=?",
            (pub_id, now_iso, row["id"]),
        )
    else:
        conn.execute(
            f"""INSERT INTO {table}
              (product_id, {value_col}, first_seen_pub_id, last_seen_pub_id,
               first_seen_at, last_seen_at)
              VALUES (?,?,?,?,?,?)""",
            (product_id, value, pub_id, pub_id, now_iso, now_iso),
        )


# --------------------------------------------------------------------------- #
# Image download
# --------------------------------------------------------------------------- #

def download_images(db_path: str, slug: str, img_dir: str,
                    quality: str = "at600",
                    include_pages: bool = True,
                    include_products: bool = True,
                    progress=lambda *a, **k: None) -> None:
    base = "https://prospekt.aldi-sued.de"
    conn = open_db(db_path)
    try:
        if include_pages:
            pages_dir = os.path.join(img_dir, slug, "pages")
            os.makedirs(pages_dir, exist_ok=True)
            rows = conn.execute(
                f"SELECT page_number, image_{quality} AS url FROM pages WHERE image_{quality} IS NOT NULL"
            ).fetchall()
            progress(f"Downloading {len(rows)} page images ({quality})...")
            for r in rows:
                url = urljoin(base + "/", r["url"])
                out = os.path.join(pages_dir, f"page_{r['page_number']:02d}.jpg")
                _download_file(url, out)

        if include_products:
            prod_dir = os.path.join(img_dir, slug, "products")
            os.makedirs(prod_dir, exist_ok=True)
            rows = conn.execute(
                """SELECT pp.url, o.product_id_remote
                   FROM product_photos pp
                   JOIN product_offerings o ON o.id = pp.offering_id
                   WHERE pp.kind = 'full'"""
            ).fetchall()
            progress(f"Downloading {len(rows)} product photos...")
            for r in rows:
                url = r["url"]
                if not url.startswith("http"):
                    url = urljoin(base + "/", url)
                out = os.path.join(prod_dir, f"{r['product_id_remote']}.jpg")
                _download_file(url, out)
    finally:
        conn.close()


def _download_file(url: str, out_path: str, retries: int = 3) -> None:
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return
    for attempt in range(retries):
        try:
            r = http().get(url, timeout=60, stream=True)
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            return
        except Exception:
            if attempt == retries - 1:
                return
            time.sleep(1 + attempt)


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #

def _resolve_pub(conn: sqlite3.Connection, ident: str) -> sqlite3.Row:
    try:
        pid = int(ident)
        row = conn.execute("SELECT * FROM publications WHERE id=?", (pid,)).fetchone()
        if row:
            return row
    except ValueError:
        pass
    row = conn.execute("SELECT * FROM publications WHERE slug=?", (ident,)).fetchone()
    if not row:
        raise SystemExit(f"ERROR: no publication matches '{ident}'")
    return row


def cmd_fetch(args):
    def progress(msg):
        if not args.quiet:
            print(msg, file=sys.stderr)
    result = fetch_publication(
        url=args.url, db_path=args.db,
        with_images=args.with_images, img_dir=args.out, progress=progress,
    )
    print(json.dumps({
        "publication_id": result.publication_id,
        "slug": result.slug,
        "num_spreads": result.num_spreads,
        "num_pages": result.num_pages,
        "num_hotspots": result.num_hotspots,
        "num_offerings": result.num_offerings,
        "num_new_products": result.num_new_products,
        "db_path": result.db_path,
    }, indent=2))


def cmd_list(args):
    conn = open_db(args.db)
    try:
        rows = conn.execute("""
            SELECT p.*,
                   (SELECT COUNT(*) FROM product_offerings WHERE publication_id=p.id) AS n_offerings,
                   (SELECT COUNT(*) FROM hotspots WHERE publication_id=p.id) AS n_hotspots
            FROM publications p
            ORDER BY p.fetched_at DESC
        """).fetchall()
        if not rows:
            print("(no publications yet. Run: aldi-cli.py fetch <url>)")
            return
        print(f"{'ID':>10}  {'SLUG':<25} {'PAGES':>5} {'OFFER':>5} {'HOT':>5}  FETCHED_AT            TITLE")
        for r in rows:
            print(f"{r['id']:>10}  {r['slug']:<25} {r['num_pages'] or 0:>5} "
                  f"{r['n_offerings']:>5} {r['n_hotspots']:>5}  "
                  f"{r['fetched_at'][:19]:<20}  {r['title'] or ''}")
    finally:
        conn.close()


def cmd_show(args):
    conn = open_db(args.db)
    try:
        pub = _resolve_pub(conn, args.ident)
        print(f"Publication: {pub['title']}")
        print(f"  ID:              {pub['id']}")
        print(f"  Slug:            {pub['slug']}")
        print(f"  Original title:  {pub['original_title']}")
        print(f"  Language:        {pub['language']}")
        print(f"  Pages:           {pub['num_pages']}")
        print(f"  PDF:             {pub['pdf_url']}")
        print(f"  Canonical:       {pub['canonical_url']}")
        print(f"  Description:     {pub['description']}")
        print(f"  Fetched at:      {pub['fetched_at']}")

        n_o = conn.execute("SELECT COUNT(*) FROM product_offerings WHERE publication_id=?",
                           (pub['id'],)).fetchone()[0]
        n_h = conn.execute("SELECT COUNT(*) FROM hotspots WHERE publication_id=?",
                           (pub['id'],)).fetchone()[0]
        n_s = conn.execute("SELECT COUNT(*) FROM spreads WHERE publication_id=?",
                           (pub['id'],)).fetchone()[0]
        # How many products in this publication were seen for the first time?
        n_new = conn.execute(
            "SELECT COUNT(*) FROM products WHERE first_seen_pub_id=?", (pub['id'],)
        ).fetchone()[0]
        print(f"\n  Spreads:         {n_s}")
        print(f"  Hotspots:        {n_h}")
        print(f"  Offerings:       {n_o}")
        print(f"  First-time seen: {n_new}")

        cats = conn.execute("""
            SELECT product_type, COUNT(*) c
            FROM product_offerings
            WHERE publication_id=? AND product_type != ''
            GROUP BY product_type
            ORDER BY c DESC
            LIMIT 10
        """, (pub['id'],)).fetchall()
        if cats:
            print(f"\n  Top categories:")
            for c in cats:
                print(f"    {c['c']:>3}  {c['product_type']}")

        ps = conn.execute("""
            SELECT COUNT(*) n, MIN(price_numeric) lo, MAX(price_numeric) hi, AVG(price_numeric) avg
            FROM product_offerings WHERE publication_id=? AND price_numeric IS NOT NULL
        """, (pub['id'],)).fetchone()
        if ps and ps['n']:
            print(f"\n  Price stats (n={ps['n']}):")
            print(f"    min: {ps['lo']:.2f} €   max: {ps['hi']:.2f} €   avg: {ps['avg']:.2f} €")
    finally:
        conn.close()


def cmd_products(args):
    conn = open_db(args.db)
    try:
        pub = _resolve_pub(conn, args.ident)
        where = ["o.publication_id = ?"]
        params: list[Any] = [pub['id']]
        if args.category:
            where.append("o.product_type LIKE ?")
            params.append(f"%{args.category}%")
        if args.search:
            where.append("(o.title LIKE ? OR o.description LIKE ?)")
            params += [f"%{args.search}%", f"%{args.search}%"]
        if args.min_price is not None:
            where.append("o.price_numeric >= ?")
            params.append(args.min_price)
        if args.max_price is not None:
            where.append("o.price_numeric <= ?")
            params.append(args.max_price)
        sql = (
            "SELECT o.*, p.product_key FROM product_offerings o "
            "JOIN products p ON p.id = o.product_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY o.page_range, o.title"
        )
        rows = conn.execute(sql, params).fetchall()

        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        elif args.csv:
            w = csv.DictWriter(sys.stdout, fieldnames=[
                'id', 'product_key', 'product_id_remote', 'title', 'price',
                'price_numeric', 'currency', 'product_type', 'description',
                'webshop_identifier', 'spread', 'page_range',
            ])
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in w.fieldnames})
        else:
            print(f"{'#':>3}  {'TITLE':<45} {'PRICE':>8}  {'PAGE':>6}  "
                  f"{'KEY':<18}  CATEGORY")
            for i, r in enumerate(rows, 1):
                price = f"{r['price_numeric']:.2f} €" if r['price_numeric'] is not None else "-"
                print(f"{i:>3}  {r['title'][:45]:<45} {price:>8}  "
                      f"{r['page_range']:>6}  {r['product_key']:<18}  {r['product_type']}")
            print(f"\n{len(rows)} offering(s)")
    finally:
        conn.close()


def cmd_price_history(args):
    """Show price evolution for a product across all publications."""
    conn = open_db(args.db)
    try:
        # Resolve product by key or by title (substring)
        if re.fullmatch(r"[a-f0-9]{16}", args.ident):
            rows = conn.execute(
                "SELECT * FROM products WHERE product_key=?", (args.ident,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM products WHERE canonical_title LIKE ?",
                (f"%{args.ident}%",),
            ).fetchall()
        if not rows:
            raise SystemExit(f"ERROR: no product matches '{args.ident}'")
        if len(rows) > 1 and not args.all:
            print(f"Multiple matches (use --all or be more specific):")
            for r in rows:
                print(f"  {r['product_key']}  {r['canonical_title']}  "
                      f"(first seen: {r['first_seen_at'][:10]})")
            return

        targets = rows if args.all else rows[:1]
        out: list[dict] = []
        for p in targets:
            history = conn.execute(
                """SELECT * FROM v_price_history WHERE product_id=?
                   ORDER BY publication_fetched_at""",
                (p['id'],),
            ).fetchall()
            out.append({
                "product_key": p['product_key'],
                "canonical_title": p['canonical_title'],
                "canonical_type": p['canonical_type'],
                "first_seen_at": p['first_seen_at'],
                "last_seen_at": p['last_seen_at'],
                "history": [dict(h) for h in history],
            })

        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return

        for entry in out:
            print(f"\n=== {entry['canonical_title']} ===")
            print(f"  product_key: {entry['product_key']}")
            print(f"  type:        {entry['canonical_type'] or '(none)'}")
            print(f"  first seen:  {entry['first_seen_at'][:10]}")
            print(f"  last seen:   {entry['last_seen_at'][:10]}")
            print()
            print(f"  {'PUBLICATION':<20} {'FETCHED':<12} {'PRICE':>8}  "
                  f"{'OFFR':>4}  {'TITLE THIS WEEK':<40}")
            print(f"  {'-'*20} {'-'*12} {'-'*8}  {'-'*4}  {'-'*40}")
            for h in entry['history']:
                price = f"{h['price_min']:.2f} €" if h['price_min'] is not None else "-"
                if h['n_offerings'] > 1 and h['price_max'] != h['price_min']:
                    price += f"-{h['price_max']:.2f}"
                pub_label = h['publication_original_title'] or h['publication_slug']
                print(f"  {pub_label[:20]:<20} {h['publication_fetched_at'][:10]:<12} "
                      f"{price:>8}  {h['n_offerings']:>4}  {h['title_this_week'][:40]:<40}")
            prices = [h['price_min'] for h in entry['history']
                      if h['price_min'] is not None]
            if prices:
                print(f"\n  price min: {min(prices):.2f} €   "
                      f"max: {max(prices):.2f} €   "
                      f"observations: {len(prices)}")

        # Show SCD2 text versions if any
        if targets and not args.json:
            p = targets[0]
            titles = conn.execute(
                "SELECT title, first_seen_at, last_seen_at FROM product_titles "
                "WHERE product_id=? ORDER BY first_seen_at",
                (p['id'],),
            ).fetchall()
            if len(titles) > 1:
                print(f"\n  Title history (SCD2):")
                for t in titles:
                    print(f"    {t['first_seen_at'][:10]} -> {t['last_seen_at'][:10]}:  {t['title']}")
            descs = conn.execute(
                "SELECT description, first_seen_at, last_seen_at FROM product_descriptions "
                "WHERE product_id=? ORDER BY first_seen_at",
                (p['id'],),
            ).fetchall()
            if len(descs) > 1:
                print(f"\n  Description history (SCD2):")
                for d in descs:
                    snippet = (d['description'][:80] + '…') if len(d['description']) > 80 \
                        else d['description']
                    print(f"    {d['first_seen_at'][:10]} -> {d['last_seen_at'][:10]}:  {snippet}")
    finally:
        conn.close()


def cmd_export(args):
    conn = open_db(args.db)
    try:
        pub = dict(_resolve_pub(conn, args.ident))
        pub_id = pub['id']

        spreads = [dict(r) for r in conn.execute(
            "SELECT * FROM spreads WHERE publication_id=?", (pub_id,))]
        pages = [dict(r) for r in conn.execute(
            "SELECT * FROM pages WHERE publication_id=?", (pub_id,))]
        hotspots = [dict(r) for r in conn.execute(
            "SELECT * FROM hotspots WHERE publication_id=?", (pub_id,))]
        offerings = [dict(r) for r in conn.execute(
            "SELECT * FROM product_offerings WHERE publication_id=?", (pub_id,))]
        photos = [dict(r) for r in conn.execute("""
            SELECT pp.*, o.product_id_remote
            FROM product_photos pp
            JOIN product_offerings o ON o.id = pp.offering_id
            WHERE o.publication_id=?
        """, (pub_id,))]
        labels = [dict(r) for r in conn.execute("""
            SELECT pl.*, o.product_id_remote
            FROM product_labels pl
            JOIN product_offerings o ON o.id = pl.offering_id
            WHERE o.publication_id=?
        """, (pub_id,))]
        products = [dict(r) for r in conn.execute(
            "SELECT * FROM products WHERE id IN (SELECT product_id FROM product_offerings WHERE publication_id=?)",
            (pub_id,))]

        bundle = {
            "publication": pub,
            "products": products,
            "spreads": spreads,
            "pages": pages,
            "hotspots": hotspots,
            "product_offerings": offerings,
            "product_photos": photos,
            "product_labels": labels,
        }

        out_path = args.output or f"/home/z/my-project/download/{pub['slug']}_export.{args.format}"
        if args.format == "json":
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(bundle, f, indent=2, ensure_ascii=False)
        elif args.format == "csv":
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["product_key", "product_id_remote", "title", "price",
                            "price_numeric", "currency", "product_type", "description",
                            "webshop_identifier", "spread", "page_range", "labels", "photos"])
                for o in offerings:
                    plabels = {l['key']: l['value'] for l in labels
                               if l['product_id_remote'] == o['product_id_remote']}
                    pphotos = [ph['url'] for ph in photos
                               if ph['product_id_remote'] == o['product_id_remote']]
                    pkey = next((p['product_key'] for p in products
                                 if p['id'] == o['product_id']), '')
                    w.writerow([
                        pkey, o['product_id_remote'], o['title'], o['price'],
                        o['price_numeric'], o['currency'], o['product_type'],
                        o['description'], o['webshop_identifier'],
                        o['spread'], o['page_range'],
                        json.dumps(plabels, ensure_ascii=False),
                        json.dumps(pphotos, ensure_ascii=False),
                    ])
        print(f"Exported {len(offerings)} offerings to: {out_path}")
    finally:
        conn.close()


def cmd_download_images(args):
    conn = open_db(args.db)
    try:
        pub = _resolve_pub(conn, args.ident)
    finally:
        conn.close()
    download_images(
        db_path=args.db, slug=pub['slug'], img_dir=args.out, quality=args.quality,
        include_pages=args.pages, include_products=args.products,
        progress=lambda m: print(m, file=sys.stderr),
    )
    print(f"Images saved under: {os.path.join(args.out, pub['slug'])}")


# --------------------------------------------------------------------------- #
# Automation: auto-fetch, daemon, install-cron
# --------------------------------------------------------------------------- #

LANDING_URL = "https://prospekt.aldi-sued.de/"


def _log(msg: str, log_file: str | None = None) -> None:
    """Timestamped log to stderr (and optionally a file)."""
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr, flush=True)
    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def discover_current_url(explicit_url: str | None = None) -> str:
    """Resolve the current week's prospectus URL.
       - If explicit_url is given, use it (after following redirects).
       - Otherwise, follow the redirect from the landing page.
    """
    src = explicit_url or LANDING_URL
    r = http().get(src, timeout=30, allow_redirects=True)
    r.raise_for_status()
    final = r.url
    # Sanity check: must match the prospectus URL pattern
    if not URL_RE.match(final):
        # Maybe the landing page returned HTML directly without redirect
        # (happens if the URL is already a /page/ URL). Try the explicit URL.
        if explicit_url and URL_RE.match(explicit_url):
            return explicit_url
        raise SystemExit(f"Could not resolve a prospectus URL from {src!r} "
                         f"(final: {final!r})")
    return final


def peek_publication_id(url: str) -> tuple[int, str] | None:
    """Lightweight check: fetch only the HTML page (no spreads/hotspots),
       extract the publication_id and slug from the config blob.
       Returns (pub_id, slug) or None if the page has no config."""
    try:
        html = get_html(url)
        cfg = extract_publication_config(html)
        return (cfg.get("id"), cfg.get("slug"))
    except Exception:
        return None


def db_has_publication(db_path: str, pub_id: int) -> bool:
    """Check whether a publication_id already exists in the DB."""
    if not os.path.exists(db_path):
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM publications WHERE id=?", (pub_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def parse_hour_range(s: str) -> tuple[int, int]:
    """Parse '12-18' -> (12, 18)."""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s.strip())
    if not m:
        raise SystemExit(f"Invalid hour range: {s!r} (expected e.g. '12-18')")
    a, b = int(m.group(1)), int(m.group(2))
    if not (0 <= a <= 23 and 0 <= b <= 23):
        raise SystemExit(f"Hours out of range: {s!r}")
    if a >= b:
        raise SystemExit(f"Start hour must be before end hour: {s!r}")
    return a, b


def next_random_time(hour_start: int, hour_end: int,
                     now: datetime | None = None) -> datetime:
    """Pick a random time between hour_start:00 and hour_end:00 today,
       local time. If that time has already passed today, pick tomorrow's."""
    now = now or datetime.now().astimezone()
    today_target = now.replace(
        hour=hour_start, minute=0, second=0, microsecond=0
    )
    window_seconds = (hour_end - hour_start) * 3600
    offset = random.randint(0, window_seconds)
    target = today_target + timedelta(seconds=offset)
    if target <= now:
        target += timedelta(days=1)
    return target


def auto_fetch_once(url: str | None, db_path: str, log_file: str | None,
                    with_images: bool, img_dir: str,
                    dry_run: bool = False, force: bool = False) -> str:
    """One iteration of the auto-fetch logic. Returns a status string:
       'fetched', 'skipped-existing', 'dry-run-skip', 'dry-run-fetch', or 'error'.

    If `force` is True, re-fetch even if the publication_id is already in the DB
    (useful when ALDI publishes a partial week early and fills it in later)."""
    _log("Resolving current prospectus URL...", log_file)
    try:
        current_url = discover_current_url(url)
    except Exception as e:
        _log(f"ERROR resolving URL: {e}", log_file)
        return "error"

    _log(f"Current URL: {current_url}", log_file)
    _log("Peeking publication id (lightweight HTML fetch)...", log_file)
    peek = peek_publication_id(current_url)
    if not peek:
        _log("ERROR: could not extract publication id from page config", log_file)
        return "error"

    pub_id, slug = peek
    _log(f"Discovered: pub_id={pub_id}  slug={slug}", log_file)

    if db_has_publication(db_path, pub_id) and not force:
        _log(f"SKIP — publication {pub_id} ({slug}) already in DB", log_file)
        return "skipped-existing"

    if db_has_publication(db_path, pub_id) and force:
        _log(f"FORCE — re-fetching publication {pub_id} ({slug}) even though it exists", log_file)

    if dry_run:
        _log(f"DRY RUN — would fetch publication {pub_id} ({slug})", log_file)
        return "dry-run-fetch"

    _log(f"NEW week detected — fetching {current_url}", log_file)
    try:
        result = fetch_publication(
            url=current_url, db_path=db_path,
            with_images=with_images, img_dir=img_dir,
            progress=lambda m: _log(f"  {m}", log_file),
        )
        _log(f"OK — fetched {result.num_offerings} offerings, "
             f"{result.num_new_products} new products", log_file)
        return "fetched"
    except Exception as e:
        _log(f"ERROR during fetch: {e}", log_file)
        return "error"


def cmd_auto_fetch(args):
    """One-shot: discover current week, check DB, fetch if missing, exit."""
    if args.jitter:
        h_start, h_end = parse_hour_range(args.jitter)
        target = next_random_time(h_start, h_end)
        now = datetime.now().astimezone()
        sleep_s = (target - now).total_seconds()
        _log(f"Jitter: sleeping {sleep_s/3600:.2f}h until "
             f"{target.strftime('%Y-%m-%d %H:%M:%S')} (window {args.jitter})",
             args.log_file)
        time.sleep(max(0, sleep_s))

    status = auto_fetch_once(
        url=args.url, db_path=args.db, log_file=args.log_file,
        with_images=args.with_images, img_dir=args.out, dry_run=args.dry_run,
        force=args.force,
    )
    print(status)


def cmd_daemon(args):
    """Long-running: each day, sleep until a random time in the window,
       then run auto-fetch-once. Loops forever."""
    h_start, h_end = parse_hour_range(args.hours)
    _log(f"Daemon starting. Daily window: {h_start:02d}:00-{h_end:02d}:00 "
         f"local time. DB: {args.db}", args.log_file)
    _log(f"URL source: {args.url or LANDING_URL + ' (auto-discover via redirect)'}",
         args.log_file)

    while True:
        target = next_random_time(h_start, h_end)
        now = datetime.now().astimezone()
        sleep_s = (target - now).total_seconds()
        _log(f"Next check at {target.strftime('%Y-%m-%d %H:%M:%S')} "
             f"(sleeping {sleep_s/3600:.2f}h)", args.log_file)
        try:
            time.sleep(max(0, sleep_s))
        except KeyboardInterrupt:
            _log("Interrupted by user, exiting.", args.log_file)
            return

        _log("Wake — starting daily check.", args.log_file)
        try:
            auto_fetch_once(
                url=args.url, db_path=args.db, log_file=args.log_file,
                with_images=args.with_images, img_dir=args.out,
                dry_run=args.dry_run, force=args.force,
            )
        except Exception as e:
            _log(f"ERROR in daily run: {e}", args.log_file)

        # Avoid immediately re-entering the loop and recomputing today's slot
        time.sleep(60)


def cmd_install_cron(args):
    """Generate a crontab line that runs auto-fetch daily with jitter."""
    h_start, h_end = parse_hour_range(args.hours)
    script = os.path.abspath(__file__)
    py = sys.executable
    db = os.path.abspath(args.db)
    log = os.path.abspath(args.log_file) if args.log_file else os.path.join(
        os.path.dirname(db), "aldi-daemon.log"
    )
    img = os.path.abspath(args.out)

    # Cron runs at h_start:00 daily; the CLI's --jitter then sleeps a random
    # 0-to-(h_end-h_start) hours before doing the actual fetch.
    cmd = (f"{py} {script} auto-fetch --db {db} --log-file {log} "
           f"--jitter {h_start}-{h_end} --out {img}")
    if args.url:
        cmd += f" --url {args.url}"
    if args.with_images:
        cmd += " --with-images"

    cron_line = f"# aldi-cli: daily prospectus fetch at random time {h_start}-{h_end}"
    cron_line2 = f"{0} {h_start} * * * {cmd}"

    print("# Add these lines to your crontab (run `crontab -e` to edit):")
    print(cron_line)
    print(cron_line2)
    print()
    print(f"# Log file: {log}")
    print(f"# Database: {db}")
    print()
    print("# Or install via:")
    print(f"   (crontab -l 2>/dev/null; echo '{cron_line2}') | crontab -")


def cmd_install_systemd(args):
    """Generate a systemd user service file for the daemon."""
    script = os.path.abspath(__file__)
    py = sys.executable
    db = os.path.abspath(args.db)
    log = os.path.abspath(args.log_file) if args.log_file else os.path.join(
        os.path.dirname(db), "aldi-daemon.log"
    )
    img = os.path.abspath(args.out)
    h_start, h_end = parse_hour_range(args.hours)

    url_arg = f"--url {args.url}" if args.url else ""
    img_arg = "--with-images" if args.with_images else ""

    unit = f"""[Unit]
Description=ALDI SÜD prospectus daemon (daily fetch at random {h_start}-{h_end})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={py} {script} daemon --db {db} --log-file {log} --hours {h_start}-{h_end} --out {img} {url_arg} {img_arg}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""
    out_path = args.output or "aldi-daemon.service"
    with open(out_path, "w") as f:
        f.write(unit)
    print(f"Wrote systemd unit: {out_path}")
    print()
    print("To install as a user service:")
    print(f"  mkdir -p ~/.config/systemd/user")
    print(f"  cp {out_path} ~/.config/systemd/user/aldi-daemon.service")
    print(f"  systemctl --user daemon-reload")
    print(f"  systemctl --user enable --now aldi-daemon.service")
    print(f"  journalctl --user -u aldi-daemon.service -f   # tail logs")


# --------------------------------------------------------------------------- #
# Argument parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aldi-cli",
        description=("Fetch ALDI SÜD prospectus product data into a "
                     "timeline-analysis-ready SQLite store."),
    )
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"SQLite DB path (default: {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="Fetch a prospectus URL into the DB")
    pf.add_argument("url", help="https://prospekt.aldi-sued.de/<slug>[/page/<n>]")
    pf.add_argument("--with-images", action="store_true",
                    help="Also download page + product images")
    pf.add_argument("--out", default=DEFAULT_IMG_DIR,
                    help=f"Image dir (default: {DEFAULT_IMG_DIR})")
    pf.add_argument("--quiet", action="store_true")
    pf.set_defaults(func=cmd_fetch)

    pl = sub.add_parser("list", help="List publications in the DB")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="Show details for a publication")
    ps.add_argument("ident", help="publication id or slug")
    ps.set_defaults(func=cmd_show)

    pp = sub.add_parser("products", help="List product offerings for a publication")
    pp.add_argument("ident", help="publication id or slug")
    pp.add_argument("--category", help="filter by product_type (substring)")
    pp.add_argument("--search", help="filter by title/description (substring)")
    pp.add_argument("--min-price", type=float)
    pp.add_argument("--max-price", type=float)
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--csv", action="store_true")
    pp.set_defaults(func=cmd_products)

    ph = sub.add_parser("price-history",
                        help="Show price evolution for a product across all publications")
    ph.add_argument("ident", help="product_key (16-hex) or title substring")
    ph.add_argument("--all", action="store_true",
                    help="show history for all matching products (default: only first)")
    ph.add_argument("--json", action="store_true")
    ph.set_defaults(func=cmd_price_history)

    pe = sub.add_parser("export", help="Export full publication to JSON or CSV")
    pe.add_argument("ident", help="publication id or slug")
    pe.add_argument("--format", choices=["json", "csv"], default="json")
    pe.add_argument("-o", "--output", help="output file path (default: auto)")
    pe.set_defaults(func=cmd_export)

    pd = sub.add_parser("download-images", help="Download page + product images")
    pd.add_argument("ident", help="publication id or slug")
    pd.add_argument("--out", default=DEFAULT_IMG_DIR)
    pd.add_argument("--quality", default="at600",
                    choices=["at200", "at600", "at800", "at1000", "at1200",
                             "at1600", "at2000", "at2400"])
    pd.add_argument("--pages", action="store_true", default=True)
    pd.add_argument("--products", action="store_true", default=True)
    pd.set_defaults(func=cmd_download_images)

    # --- Automation -------------------------------------------------------- #
    pa = sub.add_parser("auto-fetch",
                        help="One-shot: discover current week, fetch if missing, exit")
    pa.add_argument("--url", default=None,
                    help="Prospectus URL or landing page (default: auto-discover via redirect)")
    pa.add_argument("--jitter", default=None,
                    help="Random delay window, e.g. '12-18' (sleeps random 0-6h before fetching)")
    pa.add_argument("--with-images", action="store_true",
                    help="Also download page + product images when fetching")
    pa.add_argument("--out", default=DEFAULT_IMG_DIR, help=f"Image dir (default: {DEFAULT_IMG_DIR})")
    pa.add_argument("--log-file", default=None, help="Append timestamped logs to this file")
    pa.add_argument("--dry-run", action="store_true",
                    help="Log what would happen without fetching")
    pa.add_argument("--force", action="store_true",
                    help="Re-fetch even if the publication_id is already in the DB")
    pa.set_defaults(func=cmd_auto_fetch)

    pdm = sub.add_parser("daemon",
                         help="Long-running: each day, sleep until random time in window, then auto-fetch")
    pdm.add_argument("--url", default=None,
                     help="Prospectus URL or landing page (default: auto-discover)")
    pdm.add_argument("--hours", default="12-18",
                     help="Daily random-time window, e.g. '12-18' (default: 12-18)")
    pdm.add_argument("--with-images", action="store_true",
                     help="Also download page + product images when fetching")
    pdm.add_argument("--out", default=DEFAULT_IMG_DIR, help=f"Image dir (default: {DEFAULT_IMG_DIR})")
    pdm.add_argument("--log-file", default=None, help="Append timestamped logs to this file")
    pdm.add_argument("--dry-run", action="store_true",
                     help="Log what would happen without fetching")
    pdm.add_argument("--force", action="store_true",
                     help="Re-fetch even if the publication_id is already in the DB")
    pdm.set_defaults(func=cmd_daemon)

    pic = sub.add_parser("install-cron",
                         help="Print a crontab line that runs auto-fetch daily with jitter")
    pic.add_argument("--url", default=None, help="Prospectus URL (default: auto-discover)")
    pic.add_argument("--hours", default="12-18", help="Random-time window (default: 12-18)")
    pic.add_argument("--with-images", action="store_true")
    pic.add_argument("--out", default=DEFAULT_IMG_DIR)
    pic.add_argument("--log-file", default=None)
    pic.set_defaults(func=cmd_install_cron)

    pis = sub.add_parser("install-systemd",
                         help="Generate a systemd user service file for the daemon")
    pis.add_argument("--url", default=None, help="Prospectus URL (default: auto-discover)")
    pis.add_argument("--hours", default="12-18", help="Random-time window (default: 12-18)")
    pis.add_argument("--with-images", action="store_true")
    pis.add_argument("--out", default=DEFAULT_IMG_DIR)
    pis.add_argument("--log-file", default=None)
    pis.add_argument("-o", "--output", default=None, help="Output .service file path")
    pis.set_defaults(func=cmd_install_systemd)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
