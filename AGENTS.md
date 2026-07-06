# AGENTS.md — Discount Project Working Guide

> **Read this file BEFORE doing anything in this repo.** It contains everything an AI agent needs to know to work effectively on this project without access to previous chat sessions.

## Project Overview

The Discount Project is a 4-repo system for fetching, storing, browsing, and administering German supermarket discount data (ALDI SÜD + REWE).

| Repo | Purpose | Tech | Deployed |
|---|---|---|---|
| `discount-fetcher-cli` | Daily ALDI prospectus fetcher (Python CLI + GitHub Actions) | Python 3.12, SQLite | GitHub Actions (daily cron) |
| `discount-map` | Interactive map of stores with discount fetching | Next.js 16, Leaflet, Supabase | Vercel |
| `discount-database` | Discount product browser with analytics | Next.js 16, Supabase, Recharts | Vercel |
| `discount-admin` | Admin panel for managing everything | Next.js 16, Supabase | Vercel (auth-protected) |

## Critical Working Rules

### 1. ALWAYS work on the latest main branch
```bash
git clone --branch main https://github.com/BirdNest055/<repo>.git
cd <repo>
# Make changes directly on main for hotfixes
# OR: git checkout -b dev for feature work, then merge dev → main
```
**NEVER** start from an old clone. **ALWAYS** `git pull origin main` before making changes. If dev branch exists, merge main into dev first to avoid conflicts.

### 2. NEVER delete existing functionality
- Read the existing code BEFORE making changes
- If you're adding a new feature, add it as a NEW file/tab/component — don't modify existing ones unless necessary
- If you must modify existing code, understand what it does first
- After merge conflicts, ALWAYS verify no code was accidentally deleted (check for missing functions, handlers, etc.)

### 3. Verify before claiming success
- **NO COMPLETION CLAIMS WITHOUT TESTING THE DEPLOYED APP**
- After deploying, use `curl` to test the actual production URL
- For UI changes, use VLM (agent-browser + VLM skill) to visually verify
- Show evidence: paste the curl output or VLM result

### 4. Test-driven for new logic
- Write the test first for new functions (storage, API routes, analysis)
- Run `bun run test` (vitest) before pushing
- All tests must pass before merging to main

### 5. Systematic debugging
- When a bug is reported: reproduce it → find root cause → fix root cause → verify
- NEVER just "re-add" something that went missing without understanding WHY it was lost
- Check merge conflict markers in ALL files after a merge (not just the one that failed)

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ discount-fetcher │     │   discount-map    │     │ discount-database│
│      -cli        │     │  (map + fetch)    │     │ (browse + charts)│
│                  │     │                   │     │                  │
│ Python CLI       │     │ Next.js + Leaflet │     │ Next.js + Recharts│
│ GitHub Actions   │     │ Supabase stores   │     │ Supabase discounts│
│ SQLite timeline  │     │ CloakBrowser REWE │     │ 6 tabs: Dashboard│
│ Email via Resend │     │ Product search    │     │ Products, Hot Deals│
│                  │     │ Auto-fetch sched  │     │ Leaderboard, Compare│
│                  │     │                   │     │ Analysis          │
└────────┬─────────┘     └────────┬──────────┘     └────────┬─────────┘
         │                        │                         │
         │ commits discounts.db   │ writes discounts        │ reads discounts
         ▼                        ▼                         ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │                    Supabase (PostgreSQL)                         │
    │                                                                 │
    │  Tables: stores (2,289), discounts (2,000+),                    │
    │  fetch_log, auto_fetch_settings                                 │
    │  Project: tihmjdpkjfrzgpoxnqdy                                  │
    └─────────────────────────────────────────────────────────────────┘
         │
         ▼
    ┌──────────────────┐
    │  discount-admin  │
    │  (control panel) │
    │                  │
    │  Stores CRUD     │
    │  Discounts CRUD  │
    │  Fetch control   │
    │  Data ops        │
    │  System monitor  │
    └──────────────────┘
```

### Data flow
1. **ALDI (national):** `discount-fetcher-cli` fetches daily → SQLite → email. The map app also fetches ALDI on-demand → Supabase under `aldi-sued-national`.
2. **REWE (regional):** `discount-map` triggers GitHub Actions per store → CloakBrowser bypasses Cloudflare → fetches offers → saves to Supabase.
3. **Browse:** `discount-database` reads from Supabase, shows products with filtering, sorting, charts, and cross-data analysis.
4. **Admin:** `discount-admin` manages stores, discounts, fetch schedules, and system health.

### Key design decisions
- **ALDI is national** — all ALDI stores share one prospectus. Stored under `aldi-sued-national`. In product search, ALDI shows as a SINGLE marker (not 164 markers).
- **REWE is regional** — each store has different offers. Stored per-store.
- **ALDI SÜD only** (not ALDI Nord) — filtered by latitude ≤ 51.5°.
- **No "aldi" in tool names** — the tools are about discounts in general. ALDI SÜD is a supported chain, but the tool name is `discount-fetcher-cli`, not `aldi-cli`.
- **stores.json is DEPRECATED** — stores now live in the Supabase `stores` table. The `SupabaseStoreProvider` reads from Supabase with a 5-minute cache.
- **All auto-fetch settings were cleared** — no stores auto-fetch. Fetching is manual only.
- **REWE fetcher has `id: fetch`** — critical for the "Save to Supabase" step to work. If this is missing, fetches succeed but products are NEVER saved.

## Supabase Schema

```sql
-- stores (2,289 rows) — managed via admin panel
CREATE TABLE stores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  brand TEXT NOT NULL,         -- "aldi-sued" | "rewe"
  lat DOUBLE PRECISION NOT NULL,
  lng DOUBLE PRECISION NOT NULL,
  address TEXT,
  offers_url TEXT,             -- REWE offers URL (null for ALDI = national)
  source TEXT DEFAULT 'manual',
  osm_id BIGINT,
  opening_hours TEXT,
  is_active BOOLEAN DEFAULT TRUE,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- discounts (2,000+ rows) — fetched products
CREATE TABLE discounts (
  id BIGSERIAL PRIMARY KEY,
  store_id TEXT NOT NULL,
  product_title TEXT,
  brand TEXT,
  price NUMERIC,
  regular_price NUMERIC,
  currency TEXT DEFAULT 'EUR',
  category TEXT,
  valid_from TEXT,
  valid_until TEXT,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  is_on_sale BOOLEAN GENERATED ALWAYS AS (
    price IS NOT NULL AND regular_price IS NOT NULL AND price < regular_price
  ) STORED
);

-- fetch_log — audit trail
CREATE TABLE fetch_log (
  id BIGSERIAL PRIMARY KEY,
  store_id TEXT NOT NULL,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  success BOOLEAN NOT NULL,
  error TEXT,
  client_ip TEXT,
  duration_ms INTEGER,
  count INTEGER
);

-- auto_fetch_settings — per-store schedule (all cleared currently)
CREATE TABLE auto_fetch_settings (
  store_id TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  interval_hours INTEGER NOT NULL DEFAULT 24 CHECK (interval_hours IN (0, 24, 72, 168)),
  last_auto_fetched_at TIMESTAMPTZ,
  last_auto_fetch_status TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

## Store ID conventions
- ALDI national: `aldi-sued-national`
- ALDI from OSM: `aldi-sued-osm-<osm_id>`
- REWE from OSM: `rewe-osm-<osm_id>`
- REWE from discovery: `rewe-bayern-<wwIdent>` or `rewe-admin-<wwIdent>`
- REWE manual: `rewe-<city>-<n>` (e.g. `rewe-erlangen-1`)

## Pagination — CRITICAL
Supabase REST API caps at **1,000 rows per request**. ALL query functions MUST paginate:
```typescript
const PAGE_SIZE = 500;
let allData = [];
let offset = 0;
while (true) {
  const { data } = await db.from("table").select("*").order("id").range(offset, offset + PAGE_SIZE - 1);
  if (!data || data.length === 0) break;
  allData = allData.concat(data);
  if (data.length < PAGE_SIZE) break;
  offset += PAGE_SIZE;
}
```
**If you forget this, the dashboard will show 1,000 instead of 2,000+ discounts.**

## Credentials
All credentials are stored as:
- **GitHub secrets** (for GHA workflows): `RESEND_API_KEY`, `NOTIFY_EMAIL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `GITHUB_TOKEN`
- **Vercel env vars** (for web apps): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `GITHUB_TOKEN`, `CRON_SECRET`, `DISCOUNT_DB_URL`, `DISCOUNT_MAP_URL`, `ADMIN_PASSWORD`, `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET`
- **Never commit credentials to git.** If you need them, ask the user or check the `AI_AGENT_INSTRUCTIONS.md` file (stored locally, not in git).

## Vercel project names (cannot be renamed)
- `aldi-map` (project ID: `prj_vca5pux2f6ugJ4QdDMSsAPGHl56e`) → serves discount-map
- `aldi-web` (project ID: `prj_zvHfYok7i01DDPJEHdJANOwH7F7a`) → serves discount-database
- `discount-admin` (project ID: `prj_HkGvSvOc6jdwKVwONN8WEgCC573A`) → serves admin panel

The auto-generated URLs (`aldi-map-birdnest055s-projects.vercel.app`) still work — they're Vercel's canonical URLs based on the project name. Don't try to rename the Vercel projects (it breaks git connections + env vars).

## Admin panel SSO protection
Vercel auto-enables SSO protection on the admin project after deploys. After every deploy:
```bash
curl -X PATCH -H "Authorization: Bearer $VERCEL_TOKEN" -H "Content-Type: application/json" \
  "https://api.vercel.com/v9/projects/prj_HkGvSvOc6jdwKVwONN8WEgCC573A" -d '{"ssoProtection": null}'
```

## REWE fetch workflow — CRITICAL
The `.github/workflows/rewe-fetch.yml` fetch step MUST have:
```yaml
- name: Run REWE fetcher
  id: fetch                    # ← THIS IS CRITICAL
  continue-on-error: true      # ← THIS IS CRITICAL
```
Without `id: fetch`, the "Save to Supabase" step's condition (`steps.fetch.outputs.FETCH_SUCCESS == 'true'`) NEVER matches, and products are NEVER saved.

## Known issues / gotchas
1. **Vercel SSO protection** re-enables on admin panel after every deploy — must disable manually
2. **Supabase 1,000-row cap** — all queries must paginate
3. **Product search** uses a local proxy (`/api/product-search`) because cross-origin requests to the database app hang
4. **City search** is Enter-only (not real-time) to avoid lag
5. **ALDI in product search** shows as a single national marker, not 164 individual markers
6. **CloakBrowser** needs font packages installed (see `rewe-fetch.yml`)
7. **Store dedup** — stores are deduplicated by lat/lng (4 decimal places) both in Supabase and client-side
8. **`stores.json` is deprecated** — don't edit it. Use the admin panel or Supabase directly.

## How to deploy
1. Make changes on main (or dev → merge to main)
2. Push to `origin main`
3. Vercel auto-deploys (check status via Vercel API)
4. Wait 75-90 seconds for build
5. Verify: `curl https://<url>/api/health`
6. For admin panel: also disable SSO protection
7. For UI changes: use VLM to visually verify

## How to test
- **Unit tests:** `bun run test` (vitest) — 79 tests in discount-map
- **API tests:** `curl https://<url>/api/<endpoint>` — verify response
- **UI tests:** Use `agent-browser` skill to navigate + screenshot, then `VLM` skill to analyze
- **End-to-end:** Trigger a REWE fetch via `POST /api/fetch` → wait 90s → check Supabase for discounts
